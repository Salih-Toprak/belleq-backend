"""Agent task endpoints: create/list/detail, manual run, run log, and webhook.

Runs never block the HTTP response — they are spawned with ``asyncio.create_task``
(the backend's existing background-task pattern) and execute in the per-context
container via services.agent_runner.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth import get_current_user
from services import agent_runner, agent_scheduler, agent_store

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agent-tasks"])


class CreateTaskBody(BaseModel):
    instruction: str
    trigger: str = "manual"  # manual | <cron expr> | webhook
    run_now: bool = False


class StepCallbackBody(BaseModel):
    task_id: str
    token: str
    steps: list[dict] = []


def _owned_agent_or_404(agent_id: str, workspace_id: str) -> dict:
    agent = agent_store.get_owned_agent(agent_id, workspace_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.post("/agents/{agent_id}/tasks", status_code=201)
async def create_task(
    agent_id: str,
    body: CreateTaskBody,
    user: dict = Depends(get_current_user),
):
    ws = user["id"]
    agent = _owned_agent_or_404(agent_id, ws)

    task = agent_store.create_task(
        {
            "agent_id": agent_id,
            "context_id": agent["context_id"],
            "workspace_id": ws,
            "instruction": body.instruction,
            "trigger": body.trigger.strip() or "manual",
            "status": "pending",
        }
    )

    # A cron trigger registers a recurring schedule; run_now also kicks one off.
    if agent_scheduler.is_cron(task["trigger"]):
        agent_scheduler.register_task(task)
    if body.run_now:
        asyncio.create_task(agent_runner.trigger_run(task["id"]))
        logger.info("agent_task_created_and_running id=%s agent=%s", task["id"], agent_id)
    return agent_store.public_task(task)


@router.get("/agents/{agent_id}/tasks")
async def list_tasks(agent_id: str, user: dict = Depends(get_current_user)):
    _owned_agent_or_404(agent_id, user["id"])
    return [agent_store.public_task(t) for t in agent_store.list_tasks(agent_id, user["id"])]


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, user: dict = Depends(get_current_user)):
    task = agent_store.get_owned_task(task_id, user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return agent_store.public_task(task)


@router.post("/tasks/{task_id}/run")
async def run_task(task_id: str, user: dict = Depends(get_current_user)):
    task = agent_store.get_owned_task(task_id, user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.get("status") == "running":
        raise HTTPException(status_code=409, detail="Task is already running")

    agent_store.update_task(task_id, {"status": "pending", "cancel_requested": False})
    asyncio.create_task(agent_runner.trigger_run(task_id))
    logger.info("agent_task_run_triggered id=%s ws=%s", task_id, user["id"])
    return {"id": task_id, "status": "pending", "message": "Run enqueued"}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, user: dict = Depends(get_current_user)):
    """Stop a run on demand. Marks the task cancelled immediately (so the UI
    updates now) and flags cancel_requested — the container checks this at each
    step and stops cleanly; any late result won't override the cancelled state."""
    task = agent_store.get_owned_task(task_id, user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.get("status") not in ("pending", "running"):
        return {"id": task_id, "status": task.get("status"), "message": "Not running"}
    agent_store.update_task(task_id, {"status": "cancelled", "cancel_requested": True})
    logger.info("agent_task_cancel_requested id=%s ws=%s", task_id, user["id"])
    return {"id": task_id, "status": "cancelled"}


@router.get("/tasks/{task_id}/runs")
async def get_task_runs(task_id: str, user: dict = Depends(get_current_user)):
    task = agent_store.get_owned_task(task_id, user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return agent_store.list_runs_for_task(task_id)


@router.post("/webhooks/agents/{agent_id}")
async def agent_webhook(agent_id: str, request: Request):
    """Public webhook trigger. Creates a task from the agent's configuration and
    runs it, passing the raw webhook body as additional instruction context.

    Unauthenticated by design (external callers) — the agent id is the routing
    secret. The agent's own stored ``workspace_id`` scopes everything downstream;
    no caller identity is trusted.
    """
    agent = agent_store.get_agent(agent_id)
    if not agent or agent.get("status") != "active":
        raise HTTPException(status_code=404, detail="Agent not found")

    raw = ""
    try:
        raw = (await request.body()).decode("utf-8", errors="replace")[:20000]
    except Exception:  # noqa: BLE001
        raw = ""
    instruction = "Webhook received."
    if raw.strip():
        instruction = f"Webhook received. Payload:\n{raw}"

    task = agent_store.create_task(
        {
            "agent_id": agent_id,
            "context_id": agent["context_id"],
            "workspace_id": agent["workspace_id"],
            "instruction": instruction,
            "trigger": "webhook",
            "status": "pending",
        }
    )
    asyncio.create_task(agent_runner.trigger_run(task["id"]))
    logger.info("agent_webhook_fired agent=%s task=%s", agent_id, task["id"])
    return {"task_id": task["id"], "status": "pending"}


@router.post("/internal/agents/step")
async def agent_step_callback(body: StepCallbackBody):
    """Live-progress sink: the user container streams each run step here as it
    happens so the dashboard can show the run unfold in real time.

    Authenticated by the per-run token (minted in agent_runner.trigger_run and
    stored on the task) — not a user session and not the shared internal token,
    so a container can only append steps to the one task it was handed. The final
    authoritative step set is reconciled by agent_runner.replace_runs at the end.
    """
    task = agent_store.get_task(body.task_id)
    if not task or (task.get("run_token") or "") != body.token or not body.token:
        raise HTTPException(status_code=403, detail="Invalid run token")
    # Only promote pending -> running; never clobber a cancelled task back to running.
    if task.get("status") == "pending":
        agent_store.update_task(body.task_id, {"status": "running"})
    written = agent_store.upsert_run_steps(body.task_id, task["agent_id"], body.steps)
    # Tell the container to stop at this step boundary if the user hit Stop.
    return {"ok": True, "steps": written, "cancel": bool(task.get("cancel_requested"))}
