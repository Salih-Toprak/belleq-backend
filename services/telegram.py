"""Thin Telegram Bot API helpers for two-way agent chat.

Used by the backend to register/unregister an agent bot's webhook and to send
replies. The bot token is the agent's own (stored Fernet-encrypted here and
decrypted only in-process). Outbound notifications go through the connector
instead; this is the inbound/reply path for conversational agents.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT = 15.0


def set_webhook(token: str, url: str, secret: str) -> tuple[bool, str]:
    """Point the bot at our webhook. Telegram will echo ``secret`` back in the
    ``X-Telegram-Bot-Api-Secret-Token`` header so we can authenticate updates."""
    try:
        r = httpx.post(
            _API.format(token=token, method="setWebhook"),
            json={
                "url": url,
                "secret_token": secret,
                "allowed_updates": ["message"],
                "drop_pending_updates": True,
            },
            timeout=_TIMEOUT,
        )
        data = r.json() if r.content else {}
        if r.status_code >= 400 or not data.get("ok"):
            return False, str(data.get("description") or r.text)[:300]
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:300]


def delete_webhook(token: str) -> None:
    """Best-effort: stop Telegram from delivering updates for this bot."""
    try:
        httpx.post(
            _API.format(token=token, method="deleteWebhook"),
            json={"drop_pending_updates": False},
            timeout=_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        logger.warning("telegram_delete_webhook_failed", exc_info=True)


def send_message(token: str, chat_id: str, text: str) -> bool:
    """Send a message; returns True on success. Best-effort."""
    try:
        r = httpx.post(
            _API.format(token=token, method="sendMessage"),
            json={"chat_id": chat_id, "text": text[:4096]},
            timeout=_TIMEOUT,
        )
        return r.status_code < 400
    except Exception:  # noqa: BLE001
        logger.warning("telegram_send_failed chat=%s", chat_id, exc_info=True)
        return False
