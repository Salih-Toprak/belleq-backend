"""Public streaming bridge from a stable URL to a customer's (dynamic) master.

The master containers live on per-customer EC2 instances whose IPs change, so
the MCP endpoint a user pastes into their AI client must point at the *static*
platform backend. This router resolves the environment's current master address
and streams the MCP protocol through to it.

    https://mcp.belleq.app/mcp/{env_id}/{container_id}
        -> http://{master_public_ip}:{master_port}/mcp/{container_id}

Unauthenticated by design: AI clients (Claude, etc.) connect directly with no
Belleq session. The env_id + container_id act as the capability for now;
hardening with a per-container token is a follow-up.
"""

import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from database import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter()

# Hop-by-hop headers that must not be forwarded across a proxy.
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

_CORS = {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
    "access-control-allow-headers": "*",
    "access-control-max-age": "86400",
}


def _resolve_env(env_id: str) -> dict | None:
    sb = get_supabase()
    res = (
        sb.table("environments")
        .select("public_ip, master_port, status")
        .eq("id", env_id)
        .maybe_single()
        .execute()
    )
    return res.data if res else None


def _resolve_context(context_id: str) -> str | None:
    """Resolve a context to its master's `/mcp/{container_name}` target URL.

    The context lives on a (dynamic) host; we look up its host's master endpoint
    and the container name the aggregator serves.
    """
    sb = get_supabase()
    # NB: maybe_single().execute() returns None (not a response) when no row
    # matches — guard with `res.data if res else None` (see _resolve_environment),
    # or `.data` raises AttributeError on None and the bridge 500s.
    res = (
        sb.table("containers")
        .select("container_name, host_id")
        .eq("id", context_id)
        .maybe_single()
        .execute()
    )
    ctx = res.data if res else None
    if not ctx or not ctx.get("host_id"):
        return None
    hres = (
        sb.table("hosts")
        .select("master_endpoint, public_ip")
        .eq("id", ctx["host_id"])
        .maybe_single()
        .execute()
    )
    host = hres.data if hres else None
    if not host:
        return None
    endpoint = host.get("master_endpoint") or (f"http://{host.get('public_ip')}:9000")
    return f"{endpoint}/mcp/{ctx['container_name']}"


async def _close(upstream: httpx.Response, client: httpx.AsyncClient) -> None:
    await upstream.aclose()
    await client.aclose()


async def _stream_proxy(request: Request, target: str, rest: str, log_id: str) -> Response:
    """Stream a request through to an upstream MCP target."""
    if rest:
        target = f"{target}/{rest}"
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    body = await request.body()

    # No read timeout: MCP responses can be long-lived streams.
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
    try:
        upstream_req = client.build_request(
            request.method,
            target,
            headers=fwd_headers,
            content=body or None,
            params=dict(request.query_params),
        )
        upstream = await client.send(upstream_req, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        await client.aclose()
        logger.error("master_unreachable id=%s", log_id)
        return Response(content="Master is unreachable", status_code=502, headers=_CORS)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.exception("mcp_bridge_failed id=%s", log_id)
        return Response(content=f"Bridge error: {exc}", status_code=502, headers=_CORS)

    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
    resp_headers.update(_CORS)
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=resp_headers,
        background=BackgroundTask(_close, upstream, client),
    )


# ── New: per-context endpoint (collapsed model) ──────────────────────────────
#   https://mcp.belleq.app/c/{context_id}  ->  the context's host master
@router.api_route("/c/{context_id}", methods=["GET", "POST", "DELETE", "OPTIONS"])
@router.api_route("/c/{context_id}/{rest:path}", methods=["GET", "POST", "DELETE", "OPTIONS"])
async def mcp_bridge_context(context_id: str, request: Request, rest: str = ""):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS)
    target = _resolve_context(context_id)
    if not target:
        return Response(content="Context is not available", status_code=502, headers=_CORS)
    return await _stream_proxy(request, target, rest, log_id=f"ctx:{context_id}")


def _resolve_workspace(workspace_id: str) -> str | None:
    """Workspace 'connect everything' endpoint -> the workspace's home master."""
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
    hres = (
        sb.table("hosts")
        .select("master_endpoint, public_ip")
        .eq("id", ctxs[0]["host_id"])
        .maybe_single()
        .execute()
    )
    host = hres.data if hres else None
    if not host:
        return None
    endpoint = host.get("master_endpoint") or (f"http://{host.get('public_ip')}:9000")
    return f"{endpoint}/mcp/w_{workspace_id}"


# ── New: workspace 'connect everything' endpoint ─────────────────────────────
#   https://mcp.belleq.app/w/{workspace_id}  ->  workspace-scoped aggregation
@router.api_route("/w/{workspace_id}", methods=["GET", "POST", "DELETE", "OPTIONS"])
@router.api_route("/w/{workspace_id}/{rest:path}", methods=["GET", "POST", "DELETE", "OPTIONS"])
async def mcp_bridge_workspace(workspace_id: str, request: Request, rest: str = ""):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS)
    target = _resolve_workspace(workspace_id)
    if not target:
        return Response(content="Workspace has no contexts yet", status_code=502, headers=_CORS)
    return await _stream_proxy(request, target, rest, log_id=f"ws:{workspace_id}")


# ── Legacy: per-environment endpoint (kept for existing connectors) ──────────
@router.api_route("/mcp/{env_id}/{container_id}", methods=["GET", "POST", "DELETE", "OPTIONS"])
@router.api_route("/mcp/{env_id}/{container_id}/{rest:path}", methods=["GET", "POST", "DELETE", "OPTIONS"])
async def mcp_bridge(env_id: str, container_id: str, request: Request, rest: str = ""):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS)
    env = _resolve_env(env_id)
    if not env or not env.get("public_ip"):
        return Response(content="Environment is not available", status_code=502, headers=_CORS)
    port = env.get("master_port") or 9000
    target = f"http://{env['public_ip']}:{port}/mcp/{container_id}"
    return await _stream_proxy(request, target, rest, log_id=f"env:{env_id}")
