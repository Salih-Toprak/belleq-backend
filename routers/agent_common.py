"""Shared helpers for the agent-layer routers: ownership checks + enums.

Every agent endpoint must verify the requesting user owns the parent context
(SECURITY rule: a user can only touch agents/tasks/KB in a context they own).
Context ownership is the ``containers.workspace_id == user.id`` check, mirroring
contexts._owned_context_or_404.
"""

from __future__ import annotations

from fastapi import HTTPException

from database import get_supabase
from services import connector_store

KB_SCOPES = {"master", "scoped", "both"}
PROVIDERS = {"belleq", "byok"}
AGENT_STATUSES = {"active", "paused", "archived"}


def owned_context_or_404(context_id: str, workspace_id: str) -> dict:
    """Return the context row iff the workspace owns it, else 404."""
    sb = get_supabase()
    row = (
        sb.table("containers")
        .select("id, container_name, workspace_id, status, qdrant_collection, name")
        .eq("id", context_id)
        .eq("workspace_id", workspace_id)
        .maybe_single()
        .execute()
    ).data
    if not row:
        raise HTTPException(status_code=404, detail="Context not found")
    return row


def validate_connector_ids(workspace_id: str, connector_ids: list[str]) -> None:
    """Agents may only use connectors that belong to their workspace."""
    if not connector_ids:
        return
    owned = {c.get("connector_id") for c in connector_store.list_for_workspace(workspace_id)}
    unknown = [c for c in connector_ids if c not in owned]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown connector(s) for this workspace: {', '.join(unknown)}",
        )


def validate_enum(value: str, allowed: set[str], field: str) -> None:
    if value not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {field}: {value!r}. Allowed: {', '.join(sorted(allowed))}",
        )
