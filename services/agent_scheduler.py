"""Scheduling for agent tasks (APScheduler, matching master/user).

Two kinds of schedule, both built by the dashboard from a friendly picker (users
never see cron):
  • Recurring — runs on a repeating pattern (every day / chosen weekdays / day of
    month) at a chosen time, in the user's own timezone. On each fire we create a
    fresh ``pending`` task cloned from the template and run that, leaving the
    template untouched.
  • One-time — runs a single time at a specific date+time, then never again. The
    task runs itself (no clone) and becomes part of history.

Encodings stored in ``task.trigger`` (see ``_parse_schedule``): ``once:<iso>``,
``cron:<expr>:<tz>``, a legacy bare cron expression, or manual/webhook.

The scheduler is process-local (dies with the backend), so ``reload_jobs`` runs
on startup to re-register every active scheduled task from the DB.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from database import get_supabase
from services import agent_runner, agent_store

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

_UNSCHEDULED = {"manual", "webhook", ""}


def _parse_schedule(trigger: str):
    """Decode a task's trigger. Returns
    ("once", aware_dt) | ("cron", expr, tz) | None (not scheduled)."""
    t = (trigger or "").strip()
    if t.lower() in _UNSCHEDULED:
        return None
    if t.startswith("once:"):
        raw = t[len("once:"):].strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return ("once", dt)
    if t.startswith("cron:"):
        expr, _, tz = t[len("cron:"):].rpartition(":")
        return ("cron", expr.strip(), tz.strip() or "UTC")
    return ("cron", t, "UTC")  # legacy bare cron, UTC


def is_scheduled(trigger: str) -> bool:
    return _parse_schedule(trigger) is not None


def is_cron(trigger: str) -> bool:  # back-compat alias for older call sites
    return is_scheduled(trigger)


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
    """Recurring fire: clone the template task into a fresh run and execute it."""
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
        logger.info("agent_schedule_fired template=%s new_task=%s", template_task_id, new_task["id"])
    except Exception:  # noqa: BLE001
        logger.exception("agent_schedule_fire_failed template=%s", template_task_id)


def _spawn_once_run(task_id: str) -> None:
    """One-time fire: run THIS task once (it is not a template), then unregister."""
    try:
        task = agent_store.get_task(task_id)
        if not task or task.get("status") != "pending":
            unregister_task(task_id)
            return
        agent = agent_store.get_agent(task["agent_id"])
        if not agent or agent.get("status") != "active":
            unregister_task(task_id)
            return
        asyncio.create_task(agent_runner.trigger_run(task_id))
        logger.info("agent_once_fired task=%s", task_id)
    finally:
        unregister_task(task_id)


def register_task(task: dict) -> None:
    """(Re)register a scheduled task. No-op for manual/webhook triggers."""
    if _scheduler is None:
        return
    parsed = _parse_schedule(task.get("trigger", ""))
    if parsed is None:
        return
    task_id = task["id"]

    if parsed[0] == "once":
        _, dt = parsed
        # Past-due one-time task that never ran (e.g. backend was down): run now.
        if dt <= datetime.now(timezone.utc):
            if task.get("status") == "pending":
                _spawn_once_run(task_id)
            return
        _scheduler.add_job(
            _spawn_once_run, trigger=DateTrigger(run_date=dt), args=[task_id],
            id=_job_id(task_id), replace_existing=True, misfire_grace_time=3600,
        )
        logger.info("agent_once_registered task=%s at=%s", task_id, dt.isoformat())
        return

    _, expr, tz = parsed
    try:
        cron = CronTrigger.from_crontab(expr, timezone=tz)
    except (ValueError, Exception):  # noqa: BLE001 — bad expr or unknown tz
        logger.warning("agent_schedule_invalid task=%s expr=%r tz=%r", task_id, expr, tz)
        return
    _scheduler.add_job(
        _spawn_clone_run, trigger=cron, args=[task_id], id=_job_id(task_id),
        replace_existing=True, max_instances=1, coalesce=True,
    )
    logger.info("agent_schedule_registered task=%s expr=%s tz=%s", task_id, expr, tz)


def unregister_task(task_id: str) -> None:
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(_job_id(task_id))
        logger.info("agent_schedule_unregistered task=%s", task_id)
    except Exception:  # noqa: BLE001 — job may not exist
        pass


def reload_jobs() -> None:
    """Re-register every active scheduled task from the DB (startup resume).

    Completed/failed one-time tasks are skipped (they already ran); recurring
    templates and pending one-time tasks are (re)registered."""
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
        parsed = _parse_schedule(t.get("trigger", ""))
        if parsed is None:
            continue
        # A one-time task that already ran shouldn't be resurrected.
        if parsed[0] == "once" and t.get("status") != "pending":
            continue
        agent = agent_store.get_agent(t["agent_id"])
        if not agent or agent.get("status") != "active":
            continue
        register_task(t)
        active += 1
    logger.info("agent_scheduler_reloaded scheduled_tasks=%s", active)
