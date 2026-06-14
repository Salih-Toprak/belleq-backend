"""Per-account connector API.

- ``GET /connectors``: dashboard-facing list from the durable store (redacted),
  so connectors are visible even with zero contexts / no live master.
- ``POST /internal/connectors/sync`` and ``DELETE /internal/connectors/{ws}/{id}``:
  internal endpoints masters call to mirror connector changes here. Authenticated
  by a shared ``X-Internal-Token`` (not a user session).
"""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from config import settings
from services import connector_store

logger = logging.getLogger(__name__)
router = APIRouter()


def _require_internal_token(x_internal_token: str = Header(default="", alias="X-Internal-Token")) -> None:
    expected = (settings.BACKEND_INTERNAL_TOKEN or "").strip()
    if not expected:
        # Misconfiguration: refuse rather than accept unauthenticated writes.
        raise HTTPException(status_code=503, detail="Connector sync is not configured")
    if x_internal_token != expected:
        logger.warning("connector_sync_auth_failed")
        raise HTTPException(status_code=403, detail="Invalid internal token")


class ConnectorSyncBody(BaseModel):
    workspace_id: str
    connector_id: str
    display_name: str = ""
    transport: str = "streamable_http"
    url: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    enabled: bool = True
    auth_status: str = "none"
    tool_count: int = 0
    last_status: str = "unknown"
    secrets_encrypted: str = "{}"
    metadata: dict = Field(default_factory=dict)
    added_at: str | None = None
    updated_at: str | None = None


@router.get("/connectors")
async def list_connectors(user: dict = Depends(get_current_user)):
    """List the caller's connectors from the durable store (redacted)."""
    connectors = connector_store.list_redacted(user["id"])
    return {"count": len(connectors), "connectors": connectors}


@router.post("/internal/connectors/sync")
async def sync_connector(
    body: ConnectorSyncBody,
    _: None = Depends(_require_internal_token),
):
    connector_store.upsert(body.model_dump())
    return {"ok": True}


@router.delete("/internal/connectors/{workspace_id}/{connector_id}")
async def delete_connector(
    workspace_id: str,
    connector_id: str,
    _: None = Depends(_require_internal_token),
):
    connector_store.delete(workspace_id, connector_id)
    return {"ok": True}
