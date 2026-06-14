"""Durable, per-account store for MCP connectors (Postgres via Supabase).

Connectors live on ephemeral EC2 masters; this is their permanent home on the
(static) backend. Masters mirror every change here, and a freshly provisioned
master is re-hydrated from here — so connectors survive deleting all contexts
and terminating the host.

Secrets are stored ONLY as the ``secrets_encrypted`` ciphertext the master
produced (Fernet, shared key). The backend never decrypts them; the dashboard
list strips them entirely.
"""

from __future__ import annotations

import logging
from typing import Any

from database import get_supabase

logger = logging.getLogger(__name__)

_TABLE = "workspace_connectors"


def upsert(payload: dict[str, Any]) -> None:
    """Insert/update one connector for a workspace (keyed by workspace+connector)."""
    sb = get_supabase()
    row = {
        "workspace_id": payload["workspace_id"],
        "connector_id": payload["connector_id"],
        "display_name": payload.get("display_name", ""),
        "transport": payload.get("transport", "streamable_http"),
        "url": payload.get("url", "") or "",
        "command": payload.get("command", "") or "",
        "args": payload.get("args", []) or [],
        "enabled": bool(payload.get("enabled", True)),
        "auth_status": payload.get("auth_status", "none") or "none",
        "tool_count": int(payload.get("tool_count", 0) or 0),
        "last_status": payload.get("last_status", "unknown") or "unknown",
        "secrets_encrypted": payload.get("secrets_encrypted", "{}") or "{}",
        "metadata": payload.get("metadata", {}) or {},
        "added_at": payload.get("added_at"),
        "updated_at": payload.get("updated_at"),
    }
    sb.table(_TABLE).upsert(row, on_conflict="workspace_id,connector_id").execute()
    logger.info(
        "connector_persisted ws=%s connector_id=%s",
        row["workspace_id"],
        row["connector_id"],
    )


def delete(workspace_id: str, connector_id: str) -> None:
    sb = get_supabase()
    (
        sb.table(_TABLE)
        .delete()
        .eq("workspace_id", workspace_id)
        .eq("connector_id", connector_id)
        .execute()
    )
    logger.info("connector_deleted ws=%s connector_id=%s", workspace_id, connector_id)


def list_for_workspace(workspace_id: str) -> list[dict[str, Any]]:
    """Raw rows incl. ``secrets_encrypted`` — for hydrating a master."""
    sb = get_supabase()
    res = (
        sb.table(_TABLE)
        .select("*")
        .eq("workspace_id", workspace_id)
        .execute()
    )
    return res.data or []


def _redact(row: dict[str, Any]) -> dict[str, Any]:
    """Dashboard-facing shape — never includes secret material."""
    return {
        "connector_id": row.get("connector_id"),
        "display_name": row.get("display_name", ""),
        "transport": row.get("transport", "streamable_http"),
        "url": row.get("url", ""),
        "command": row.get("command", ""),
        "args": row.get("args", []) or [],
        "headers": {},
        "env": {},
        "enabled": bool(row.get("enabled", True)),
        "last_status": row.get("last_status", "unknown"),
        "last_error": "",
        "last_checked_at": None,
        "tool_count": int(row.get("tool_count", 0) or 0),
        "auth_status": row.get("auth_status", "none"),
        "added_at": row.get("added_at"),
        "updated_at": row.get("updated_at"),
        "metadata": row.get("metadata", {}) or {},
    }


def list_redacted(workspace_id: str) -> list[dict[str, Any]]:
    """Connectors for the dashboard — secrets stripped. Works with zero contexts."""
    return [_redact(r) for r in list_for_workspace(workspace_id)]
