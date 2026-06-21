"""Inbound Telegram → agent (two-way chat).

When two-way chat is enabled on an agent, its bot's webhook points here. A user
messages the bot, Telegram POSTs the update, and we run the agent on that message
and reply in the same chat (the reply is sent after the run finishes — see
services.agent_runner). The agent uses the knowledge base normally; the user just
sees a chat.

Auth: Telegram echoes the per-agent ``secret_token`` (set at setWebhook) in the
``X-Telegram-Bot-Api-Secret-Token`` header — we require it to match. The agent id
is in the path. No user session is involved.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Header, Request

from services import agent_runner, agent_store

logger = logging.getLogger(__name__)
router = APIRouter(tags=["telegram"])


@router.post("/internal/telegram/webhook/{agent_id}")
async def telegram_webhook(
    agent_id: str,
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default="", alias="X-Telegram-Bot-Api-Secret-Token"),
):
    # Always 200 to Telegram (so it doesn't retry-storm); we just no-op on problems.
    agent = agent_store.get_agent(agent_id)
    if not agent or not agent.get("telegram_enabled"):
        return {"ok": True}
    secret = (agent.get("telegram_secret") or "").strip()
    if not secret or x_telegram_bot_api_secret_token != secret:
        logger.warning("telegram_webhook_bad_secret agent=%s", agent_id)
        return {"ok": True}
    if agent.get("status") != "active":
        return {"ok": True}

    try:
        update = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": True}
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or not text:
        return {"ok": True}

    allowed = [str(c) for c in (agent.get("telegram_allowed_chats") or [])]
    if allowed and str(chat_id) not in allowed:
        logger.info("telegram_webhook_chat_not_allowed agent=%s chat=%s", agent_id, chat_id)
        return {"ok": True}

    task = agent_store.create_task(
        {
            "agent_id": agent_id,
            "context_id": agent["context_id"],
            "workspace_id": agent["workspace_id"],
            "instruction": text,
            "trigger": "telegram",
            "reply_chat": str(chat_id),
            "status": "pending",
        }
    )
    asyncio.create_task(agent_runner.trigger_run(task["id"]))
    logger.info("telegram_message_received agent=%s chat=%s task=%s", agent_id, chat_id, task["id"])
    return {"ok": True}
