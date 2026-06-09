"""Workspace → master proxy (collapsed model).

Replaces the per-environment proxy. The dashboard calls /workspace/proxy/...;
we resolve the workspace's *home host* (the shared/dedicated host where its
contexts live), inject the master admin key and X-Workspace-Id, and forward.

The master uses X-Workspace-Id to scope connector management so two workspaces
sharing a host can't see each other's connectors.
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from auth import get_current_user
from database import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter()

TIMEOUT = 30.0
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def _home_host(workspace_id: str) -> dict | None:
    """The host where this workspace's contexts live (workspace affinity)."""
    sb = get_supabase()
    ctxs = (
        sb.table("containers")
        .select("host_id")
        .eq("workspace_id", workspace_id)
        .neq("status", "stopped")
        .not_.is_("host_id", "null")
        .limit(1)
        .execute()
    ).data or []
    if not ctxs:
        return None
    host = (
        sb.table("hosts").select("*").eq("id", ctxs[0]["host_id"]).maybe_single().execute()
    ).data
    return host


@router.api_route(
    "/workspace/proxy/{master_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def workspace_proxy(
    master_path: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    host = _home_host(user["id"])
    if not host or not host.get("master_endpoint"):
        raise HTTPException(
            status_code=409,
            detail="No active context yet. Create a context first.",
        )

    target = f"{host['master_endpoint']}/{master_path}"
    headers = {
        "X-Admin-Key": host["master_api_key"],
        "X-Workspace-Id": user["id"],
    }
    content_type = request.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            upstream = await client.request(
                request.method,
                target,
                params=dict(request.query_params),
                content=body or None,
                headers=headers,
            )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        logger.error("home_master_unreachable ws=%s host=%s", user["id"], host.get("id"))
        raise HTTPException(status_code=502, detail="Master is unreachable")
    except httpx.HTTPError as exc:
        logger.exception("workspace_proxy_failed ws=%s", user["id"])
        raise HTTPException(status_code=502, detail=f"Proxy request failed: {exc}")

    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )
