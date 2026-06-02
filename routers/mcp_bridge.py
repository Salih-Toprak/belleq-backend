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


async def _close(upstream: httpx.Response, client: httpx.AsyncClient) -> None:
    await upstream.aclose()
    await client.aclose()


@router.api_route(
    "/mcp/{env_id}/{container_id}",
    methods=["GET", "POST", "DELETE", "OPTIONS"],
)
@router.api_route(
    "/mcp/{env_id}/{container_id}/{rest:path}",
    methods=["GET", "POST", "DELETE", "OPTIONS"],
)
async def mcp_bridge(
    env_id: str,
    container_id: str,
    request: Request,
    rest: str = "",
):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS)

    env = _resolve_env(env_id)
    if not env or not env.get("public_ip"):
        return Response(
            content="Environment is not available",
            status_code=502,
            headers=_CORS,
        )

    port = env.get("master_port") or 9000
    target = f"http://{env['public_ip']}:{port}/mcp/{container_id}"
    if rest:
        target += f"/{rest}"

    fwd_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP
    }
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
        logger.error("master_unreachable env=%s ip=%s", env_id, env.get("public_ip"))
        return Response(content="Master is unreachable", status_code=502, headers=_CORS)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.exception("mcp_bridge_failed env=%s", env_id)
        return Response(content=f"Bridge error: {exc}", status_code=502, headers=_CORS)

    resp_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP
    }
    resp_headers.update(_CORS)

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=resp_headers,
        background=BackgroundTask(_close, upstream, client),
    )
