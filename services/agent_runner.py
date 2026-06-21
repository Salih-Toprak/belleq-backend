"""Trigger an agent task run and persist its outcome.

The execution loop itself runs in the per-context belleq-user container (where
the KB, LLM SDKs, and connectors are reachable). This module is the backend side
of the hop:

    backend  --X-Admin-Key-->  master /master/agents/{container}/run
    master   --X-Master-Key->  container /internal/agents/run

The container runs the full agentic loop and returns the final result, the cost,
the things it wrote to the KB, and a step-by-step run log. The backend persists
all of that into Supabase (the durable owner of agent state) and queues any
``scope="shared"`` writes for human review.

Budget is enforced here, before the trigger: an agent that has already spent its
daily limit fails fast without calling any LLM. The remaining budget is also
passed to the executor so a single long run can stop itself mid-loop.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

import httpx

from config import settings
from database import get_supabase
from services import agent_store, connector_store

logger = logging.getLogger(__name__)

# Generous: a multi-step agentic loop with several tool/LLM calls can run for a
# while. The HTTP call is made from a background task, so it never blocks a
# user-facing request.
RUN_TIMEOUT = 600.0


class BudgetExceededException(Exception):
    """Raised when an agent has reached its daily spend limit."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _context_and_host(context_id: str) -> tuple[dict, dict]:
    """Resolve the context container row + its host (master endpoint/key)."""
    sb = get_supabase()
    ctx = (
        sb.table("containers")
        .select("id, container_name, host_id, status, qdrant_collection, name")
        .eq("id", context_id)
        .maybe_single()
        .execute()
    ).data
    if not ctx:
        raise RuntimeError("Context not found")
    if not ctx.get("host_id"):
        raise RuntimeError("Context is still provisioning")
    host = (
        sb.table("hosts").select("*").eq("id", ctx["host_id"]).maybe_single().execute()
    ).data
    if not host or not host.get("master_endpoint"):
        raise RuntimeError("Context host is unavailable")
    return ctx, host


def check_budget(agent: dict) -> None:
    """Raise BudgetExceededException if the agent is at/over its daily limit."""
    limit = agent.get("budget_limit_usd")
    if limit is None:
        return
    spent = agent_store.agent_spend_today(agent["id"])
    if spent >= float(limit):
        raise BudgetExceededException(
            f"Daily budget limit reached (${spent:.4f} / ${float(limit):.2f})"
        )


def _budget_remaining(agent: dict) -> float | None:
    limit = agent.get("budget_limit_usd")
    if limit is None:
        return None
    return max(0.0, float(limit) - agent_store.agent_spend_today(agent["id"]))


def _build_step_callback(task_id: str, run_token: str) -> dict | None:
    """Where the container streams each step as it happens, for live progress.

    Disabled (returns None) when no public backend URL is configured — the run
    still works, the UI just shows steps only after completion. The per-run token
    (not the shared internal token) is what the container holds, so a container
    can only write to the one task it was handed."""
    base = (getattr(settings, "BACKEND_PUBLIC_URL", "") or "").strip().rstrip("/")
    if not base or not run_token:
        return None
    return {"url": f"{base}/internal/agents/step", "token": run_token, "task_id": task_id}


def _build_run_payload(task: dict, agent: dict, ctx: dict) -> dict:
    """The executor's input. The BYOK key is decrypted here and travels only over
    the trusted private network (backend -> master -> container); it is never
    logged or returned to a client."""
    api_key = ""
    if (agent.get("provider") or "belleq") in ("byok", "openrouter"):
        api_key = agent_store.get_agent_decrypted_key(agent)

    return {
        "step_callback": _build_step_callback(task["id"], task.get("run_token", "")),
        "task": {
            "id": task["id"],
            "instruction": task.get("instruction", ""),
            "trigger": task.get("trigger", "manual"),
        },
        "agent": {
            "id": agent["id"],
            "name": agent.get("name", ""),
            "role_description": agent.get("role_description", ""),
            "kb_scope": agent.get("kb_scope", "scoped"),
            "kb_section_ids": agent.get("kb_section_ids", []) or [],
            "connector_ids": agent.get("connector_ids", []) or [],
            "provider": agent.get("provider", "belleq"),
            "model": agent.get("model", ""),
            "api_key": api_key,  # plaintext, BYOK only; private network only
            "budget_remaining_usd": _budget_remaining(agent),
        },
        "context": {
            "id": ctx["id"],
            "name": ctx.get("name", ""),
            "qdrant_collection": ctx.get("qdrant_collection", ""),
            "container_name": ctx.get("container_name", ""),
        },
    }


async def trigger_run(task_id: str) -> None:
    """Background entrypoint: run one task end-to-end and persist the outcome.

    Never raises out — any failure marks the task ``failed`` with a message so a
    wedged run can't crash the server or leave the row stuck at ``running``.
    """
    agent = task = None
    try:
        task = agent_store.get_task(task_id)
        if not task:
            logger.warning("agent_run_task_missing task=%s", task_id)
            return
        agent = agent_store.get_agent(task["agent_id"])
        if not agent:
            agent_store.update_task(task_id, {"status": "failed", "result": "Agent not found"})
            return
        if agent.get("status") == "archived":
            agent_store.update_task(task_id, {"status": "failed", "result": "Agent is archived"})
            return

        # Budget gate — fail fast before any LLM spend.
        try:
            check_budget(agent)
        except BudgetExceededException as exc:
            agent_store.update_task(
                task_id,
                {"status": "failed", "result": str(exc), "completed_at": _now()},
            )
            logger.info("agent_run_budget_blocked task=%s agent=%s", task_id, agent["id"])
            return

        ctx, host = _context_and_host(agent["context_id"])
        # Per-run token authorizes the container's live step callbacks for THIS
        # task only. Stored on the task; validated by /internal/agents/step.
        run_token = secrets.token_urlsafe(24)
        agent_store.update_task(task_id, {"status": "running", "run_token": run_token})
        task["run_token"] = run_token

        payload = _build_run_payload(task, agent, ctx)
        target = f"{host['master_endpoint']}/master/agents/{ctx['container_name']}/run"
        headers = {"X-Admin-Key": host["master_api_key"], "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=RUN_TIMEOUT) as client:
            resp = await client.post(target, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"Executor error {resp.status_code}: {resp.text[:300]}")
        out = resp.json()

        _persist_result(task, agent, ctx, out)
        _reply_telegram(agent, task, out)
        await _fire_notification(agent, task, ctx, host, out)
        logger.info(
            "agent_run_complete task=%s agent=%s status=%s cost=%.4f",
            task_id, agent["id"], out.get("status", "completed"), float(out.get("cost_usd", 0) or 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("agent_run_failed task=%s", task_id)
        try:
            agent_store.update_task(
                task_id,
                {"status": "failed", "result": str(exc)[:1000], "completed_at": _now()},
            )
        except Exception:  # noqa: BLE001
            logger.exception("agent_run_failmark_failed task=%s", task_id)


# Connector display-name / id fragments that mark a messaging connector the agent
# can deliver notifications through.
_MESSAGING_HINTS = ("slack", "discord", "telegram", "teams", "mattermost")


def _messaging_connector_ids(agent: dict) -> list[str]:
    """The agent's attached connectors that look like a messaging channel."""
    attached = set(agent.get("connector_ids") or [])
    if not attached:
        return []
    out: list[str] = []
    for c in connector_store.list_for_workspace(agent.get("workspace_id", "")):
        cid = c.get("connector_id")
        if cid not in attached:
            continue
        hay = f"{cid} {c.get('display_name', '')}".lower()
        if any(h in hay for h in _MESSAGING_HINTS):
            out.append(cid)
    return out


def _reply_telegram(agent: dict, task: dict, out: dict) -> None:
    """For a two-way Telegram run, reply to the originating chat with the result.
    Uses the agent's own (backend-decryptable) bot token. Best-effort."""
    if task.get("trigger") != "telegram":
        return
    chat = (task.get("reply_chat") or "").strip()
    if not chat:
        return
    token = agent_store.get_agent_telegram_token(agent)
    if not token:
        return
    text = (out.get("result") or out.get("final_text") or "").strip() or "(no reply)"
    from services import telegram

    telegram.send_message(token, chat, text)


async def _fire_notification(agent: dict, task: dict, ctx: dict, host: dict, out: dict) -> None:
    """Notify the user of a finished run (success OR failure) by posting a summary
    through an attached Slack/Discord/etc. connector. No webhook URL or API key —
    the user just attaches a messaging connector and flips on notifications.

    Delivery is deterministic (the backend triggers it regardless of run outcome)
    and runs in the container, which calls the connector's send-message tool. All
    best-effort: a failed notification never affects the run."""
    if not agent.get("notify_enabled"):
        return
    # The user explicitly picks which communication connector(s) to notify
    # through; fall back to auto-detecting a messaging connector among the
    # agent's tools for older agents that predate the selector.
    messaging_ids = list(agent.get("notify_connector_ids") or []) or _messaging_connector_ids(agent)
    if not messaging_ids:
        logger.info("agent_notify_skipped_no_connector agent=%s", agent.get("id"))
        return

    status = out.get("status") or "completed"
    result = (out.get("result") or out.get("final_text") or "").strip()[:1200]
    cost = float(out.get("cost_usd", 0) or 0)
    verb = "completed" if status == "completed" else f"finished with status: {status}"
    message = (
        f"🔔 Agent “{agent.get('name')}” {verb}.\n\n"
        f"Task: {(task.get('instruction') or '').strip()[:300]}\n\n"
        f"{result or '(no text result)'}\n\n"
        f"(${cost:.4f} spent)"
    )

    api_key = ""
    if (agent.get("provider") or "belleq") in ("byok", "openrouter"):
        api_key = agent_store.get_agent_decrypted_key(agent)
    payload = {
        "agent": {
            "id": agent.get("id"),
            "name": agent.get("name", ""),
            "provider": agent.get("provider", "belleq"),
            "model": agent.get("model", ""),
            "api_key": api_key,
            "connector_ids": messaging_ids,
        },
        "message": message,
    }
    target = f"{host['master_endpoint']}/master/agents/{ctx['container_name']}/notify"
    headers = {"X-Admin-Key": host["master_api_key"], "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            await client.post(target, json=payload, headers=headers)
    except Exception:  # noqa: BLE001 — notifications are best-effort
        logger.warning("agent_notify_failed agent=%s", agent.get("id"), exc_info=True)


def _persist_result(task: dict, agent: dict, ctx: dict, out: dict) -> None:
    """Write the executor's response into Supabase: task result + run steps, then
    queue shared writes for review."""
    kb_writes = out.get("kb_writes", []) or []
    status = out.get("status") or "completed"
    agent_store.update_task(
        task["id"],
        {
            "status": status,
            "result": (out.get("result") or out.get("final_text") or "")[:100000],
            "kb_writes": kb_writes,
            "tokens_used": int(out.get("tokens_used", 0) or 0),
            "cost_usd": float(out.get("cost_usd", 0) or 0),
            "completed_at": _now(),
        },
    )
    # Authoritative reconcile: replaces any steps streamed live during the run.
    agent_store.replace_runs(task["id"], agent["id"], out.get("runs", []) or [])
    propagate_to_master_kb(kb_writes, agent, ctx, task_id=task["id"])


def propagate_to_master_kb(
    kb_writes: list[dict], agent: dict, ctx: dict, task_id: str | None = None
) -> None:
    """Queue ``scope="shared"`` writes for human approval; private writes need no
    propagation (the container already wrote them to its scoped KB)."""
    for w in kb_writes or []:
        if (w.get("scope") or "private") != "shared":
            continue
        agent_store.enqueue_review(
            {
                "context_id": agent["context_id"],
                "workspace_id": agent["workspace_id"],
                "agent_id": agent["id"],
                "task_id": task_id,
                "content": str(w.get("content", ""))[:100000],
                "tags": w.get("tags", []) or [],
                "status": "pending",
            }
        )


async def write_to_context_kb(context_id: str, content: str, tags: list[str]) -> dict:
    """Upsert approved review content into the context KB (the belleq-user
    container), via the same master passthrough the KB REST API uses. Reuses the
    container's KBWriter — the backend never touches Qdrant directly."""
    _ctx, host = _context_and_host(context_id)
    target = f"{host['master_endpoint']}/master/kb/{_ctx['container_name']}/agent_write"
    headers = {"X-Admin-Key": host["master_api_key"], "Content-Type": "application/json"}
    body = {"content": content, "tags": tags, "scope": "shared", "source": "kb-review-approved"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(target, json=body, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"KB write failed {resp.status_code}: {resp.text[:300]}")
    return resp.json()
