"""Durable store for the agent execution layer (Postgres via Supabase).

CRUD + serialization for agents, tasks, run logs, and the shared-KB review
queue. Mirrors connector_store.py: thin helpers over ``get_supabase()`` plus a
``_public`` redaction that strips secret material before anything reaches the
dashboard.

SECURITY: ``api_key_encrypted`` is dropped by every serializer here, so it can
never leak through an API response. The only reader of the raw value is
``get_agent_decrypted_key`` (used by the runner when triggering a BYOK run).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from database import get_supabase

logger = logging.getLogger(__name__)

AGENTS = "agents"
TASKS = "agent_tasks"
RUNS = "agent_runs"
REVIEW = "kb_review_queue"

# Stripped from every agent payload returned to a client.
_AGENT_SECRET_FIELDS = {"api_key_encrypted"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _start_of_day_utc() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def public_agent(row: dict[str, Any]) -> dict[str, Any]:
    """Agent shape for API responses — never includes the encrypted key.

    Also exposes ``has_api_key`` so the dashboard can show whether a BYOK key is
    set without ever seeing it.
    """
    out = {k: v for k, v in row.items() if k not in _AGENT_SECRET_FIELDS}
    out["has_api_key"] = bool((row.get("api_key_encrypted") or "").strip())
    return out


def public_task(row: dict[str, Any]) -> dict[str, Any]:
    """Task shape for API responses — strips the per-run callback token."""
    return {k: v for k, v in row.items() if k != "run_token"}


# ── agents ───────────────────────────────────────────────────────────────────
def create_agent(row: dict[str, Any]) -> dict[str, Any]:
    sb = get_supabase()
    res = sb.table(AGENTS).insert(row).execute()
    return res.data[0]


def get_agent(agent_id: str) -> dict[str, Any] | None:
    sb = get_supabase()
    return (
        sb.table(AGENTS).select("*").eq("id", agent_id).maybe_single().execute()
    ).data


def get_owned_agent(agent_id: str, workspace_id: str) -> dict[str, Any] | None:
    sb = get_supabase()
    return (
        sb.table(AGENTS)
        .select("*")
        .eq("id", agent_id)
        .eq("workspace_id", workspace_id)
        .maybe_single()
        .execute()
    ).data


def list_agents(context_id: str, workspace_id: str) -> list[dict[str, Any]]:
    sb = get_supabase()
    return (
        sb.table(AGENTS)
        .select("*")
        .eq("context_id", context_id)
        .eq("workspace_id", workspace_id)
        .neq("status", "archived")
        .order("created_at", desc=True)
        .execute()
    ).data or []


def update_agent(agent_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    sb = get_supabase()
    patch = {**patch, "updated_at": _now()}
    res = sb.table(AGENTS).update(patch).eq("id", agent_id).execute()
    return res.data[0] if res.data else None


def get_agent_decrypted_key(row: dict[str, Any]) -> str:
    """Decrypt a BYOK key for the runner. Import-local so crypto stays a
    runner-time concern and never gets pulled into a serialization path."""
    from crypto import decrypt_secret

    return decrypt_secret(row.get("api_key_encrypted") or "")


# ── tasks ────────────────────────────────────────────────────────────────────
def create_task(row: dict[str, Any]) -> dict[str, Any]:
    sb = get_supabase()
    return sb.table(TASKS).insert(row).execute().data[0]


def get_task(task_id: str) -> dict[str, Any] | None:
    sb = get_supabase()
    return (
        sb.table(TASKS).select("*").eq("id", task_id).maybe_single().execute()
    ).data


def get_owned_task(task_id: str, workspace_id: str) -> dict[str, Any] | None:
    sb = get_supabase()
    return (
        sb.table(TASKS)
        .select("*")
        .eq("id", task_id)
        .eq("workspace_id", workspace_id)
        .maybe_single()
        .execute()
    ).data


def list_tasks(agent_id: str, workspace_id: str) -> list[dict[str, Any]]:
    sb = get_supabase()
    return (
        sb.table(TASKS)
        .select("*")
        .eq("agent_id", agent_id)
        .eq("workspace_id", workspace_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []


def update_task(task_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    sb = get_supabase()
    res = sb.table(TASKS).update(patch).eq("id", task_id).execute()
    return res.data[0] if res.data else None


def agent_spend_today(agent_id: str) -> float:
    """Sum cost_usd across this agent's tasks created since 00:00 UTC."""
    sb = get_supabase()
    rows = (
        sb.table(TASKS)
        .select("cost_usd")
        .eq("agent_id", agent_id)
        .gte("created_at", _start_of_day_utc())
        .execute()
    ).data or []
    return float(sum((r.get("cost_usd") or 0) for r in rows))


# ── run logs ─────────────────────────────────────────────────────────────────
def insert_runs(task_id: str, agent_id: str, steps: list[dict[str, Any]]) -> int:
    """Bulk-insert the step log the executor returned for one task run."""
    if not steps:
        return 0
    sb = get_supabase()
    rows = [
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "step_number": int(s.get("step_number", i)),
            "type": str(s.get("type", "")),
            "input_summary": str(s.get("input_summary", ""))[:4000],
            "output_summary": str(s.get("output_summary", ""))[:4000],
        }
        for i, s in enumerate(steps)
    ]
    sb.table(RUNS).insert(rows).execute()
    return len(rows)


def _run_row(task_id: str, agent_id: str, step: dict[str, Any], fallback_no: int) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "agent_id": agent_id,
        "step_number": int(step.get("step_number", fallback_no)),
        "type": str(step.get("type", "")),
        "input_summary": str(step.get("input_summary", ""))[:4000],
        "output_summary": str(step.get("output_summary", ""))[:4000],
    }


def upsert_run_steps(task_id: str, agent_id: str, steps: list[dict[str, Any]]) -> int:
    """Idempotently insert streamed steps (live progress): each step replaces any
    existing row with the same (task_id, step_number) so re-sent flushes don't
    duplicate. The final replace_runs() reconciles the authoritative set."""
    if not steps:
        return 0
    sb = get_supabase()
    numbers = sorted({int(s.get("step_number", i)) for i, s in enumerate(steps)})
    if numbers:
        sb.table(RUNS).delete().eq("task_id", task_id).in_("step_number", numbers).execute()
    rows = [_run_row(task_id, agent_id, s, i) for i, s in enumerate(steps)]
    sb.table(RUNS).insert(rows).execute()
    return len(rows)


def replace_runs(task_id: str, agent_id: str, steps: list[dict[str, Any]]) -> int:
    """Delete any existing run rows for the task, then insert the authoritative
    final set. Makes final persistence idempotent regardless of live streaming."""
    sb = get_supabase()
    sb.table(RUNS).delete().eq("task_id", task_id).execute()
    if not steps:
        return 0
    rows = [_run_row(task_id, agent_id, s, i) for i, s in enumerate(steps)]
    sb.table(RUNS).insert(rows).execute()
    return len(rows)


def list_runs_for_task(task_id: str) -> list[dict[str, Any]]:
    sb = get_supabase()
    return (
        sb.table(RUNS)
        .select("*")
        .eq("task_id", task_id)
        .order("step_number", desc=False)
        .execute()
    ).data or []


def list_runs_for_agent(agent_id: str, limit: int = 200) -> list[dict[str, Any]]:
    sb = get_supabase()
    return (
        sb.table(RUNS)
        .select("*")
        .eq("agent_id", agent_id)
        .order("timestamp", desc=True)
        .limit(limit)
        .execute()
    ).data or []


# ── shared-KB review queue ───────────────────────────────────────────────────
def enqueue_review(row: dict[str, Any]) -> dict[str, Any]:
    sb = get_supabase()
    return sb.table(REVIEW).insert(row).execute().data[0]


def list_pending_reviews(context_id: str, workspace_id: str) -> list[dict[str, Any]]:
    sb = get_supabase()
    return (
        sb.table(REVIEW)
        .select("*")
        .eq("context_id", context_id)
        .eq("workspace_id", workspace_id)
        .eq("status", "pending")
        .order("created_at", desc=True)
        .execute()
    ).data or []


def get_owned_review(item_id: str, workspace_id: str) -> dict[str, Any] | None:
    sb = get_supabase()
    return (
        sb.table(REVIEW)
        .select("*")
        .eq("id", item_id)
        .eq("workspace_id", workspace_id)
        .maybe_single()
        .execute()
    ).data


def set_review_status(item_id: str, status: str) -> dict[str, Any] | None:
    sb = get_supabase()
    res = sb.table(REVIEW).update({"status": status}).eq("id", item_id).execute()
    return res.data[0] if res.data else None
