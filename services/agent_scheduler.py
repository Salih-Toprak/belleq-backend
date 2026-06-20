"""Cron scheduling for agent tasks (APScheduler, matching master/user).

A task whose ``trigger`` is a 5-field cron expression (e.g. ``0 9 * * 1``) is a
recurring template: on each fire we create a fresh ``pending`` task cloned from
it and run that, leaving the template task untouched. ``manual`` and ``webhook``
triggers are never scheduled here.

The scheduler is process-local (it dies with the backend), so ``reload_jobs`` is
called on startup to re-register every active cron task from the DB — the same
resume-on-restart pattern the provisioning pollers use.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import get_supabase
from services import agent_runner, agent_store

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

_NON_CRON = {"manual", "webhook", ""}


def is_cron(trigger: str) -> bool:
    return (trigger or "").strip().lower() not in _NON_CRON


def _job_id(task_id: str) -> str:
    return f"agent_task:{task_id}"


def start() -> None:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
        _scheduler.start()
        logger.info("agent_scheduler_started")
    reload_jobs()


def _spawn_clone_run(template_task_id: str) -> None:
    """Cron fire: clone the template task into a fresh run and execute it."""
    try:
        template = agent_store.get_task(template_task_id)
        if not template:
            unregister_task(template_task_id)
            return
        agent = agent_store.get_agent(template["agent_id"])
        if not agent or agent.get("status") != "active":
            unregister_task(template_task_id)
            return
        new_task = agent_store.create_task(
            {
                "agent_id": template["agent_id"],
                "context_id": template["context_id"],
                "workspace_id": template["workspace_id"],
                "instruction": template.get("instruction", ""),
                "trigger": template.get("trigger", "manual"),
                "status": "pending",
            }
        )
        asyncio.create_task(agent_runner.trigger_run(new_task["id"]))
        logger.info("agent_cron_fired template=%s new_task=%s", template_task_id, new_task["id"])
    except Exception:  # noqa: BLE001
        logger.exception("agent_cron_fire_failed template=%s", template_task_id)


def register_task(task: dict) -> None:
    """(Re)register a cron task. No-op for manual/webhook triggers."""
    if _scheduler is None:
        return
    trigger = task.get("trigger", "")
    if not is_cron(trigger):
        return
    try:
        cron = CronTrigger.from_crontab(trigger.strip(), timezone=timezone.utc)
    except ValueError:
        logger.warning("agent_cron_invalid task=%s expr=%r", task.get("id"), trigger)
        return
    _scheduler.add_job(
        _spawn_clone_run,
        trigger=cron,
        args=[task["id"]],
        id=_job_id(task["id"]),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("agent_cron_registered task=%s expr=%s", task["id"], trigger.strip())


def unregister_task(task_id: str) -> None:
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(_job_id(task_id))
        logger.info("agent_cron_unregistered task=%s", task_id)
    except Exception:  # noqa: BLE001 — job may not exist
        pass


def reload_jobs() -> None:
    """Re-register every active cron task from the DB (startup resume)."""
    sb = get_supabase()
    rows = (
        sb.table(agent_store.TASKS)
        .select("id, agent_id, trigger, status")
        .neq("trigger", "manual")
        .neq("trigger", "webhook")
        .execute()
    ).data or []
    active = 0
    for t in rows:
        agent = agent_store.get_agent(t["agent_id"])
        if not agent or agent.get("status") != "active":
            continue
        register_task(t)
        active += 1
    logger.info("agent_scheduler_reloaded cron_tasks=%s", active)
