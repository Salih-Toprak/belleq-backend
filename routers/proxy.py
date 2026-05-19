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


def _get_owned_env(env_id: str, user_id: str) -> dict:
    sb = get_supabase()
    result = sb.table("environments").select("*").eq("id", env_id).maybe_single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Environment not found")
    if result.data["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your environment")
    return result.data


@router.api_route(
    "/environments/{env_id}/proxy/{master_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def proxy_to_master(
    env_id: str,
    master_path: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Forward an arbitrary master-container request for an owned environment.

    The browser never sees the master's admin key — it is injected here from
    the environment record, so every dashboard call is scoped to the caller's
    own environment and authorized by their Supabase token.
    """
    env = _get_owned_env(env_id, user["id"])

    if env["status"] != "ready":
        raise HTTPException(status_code=409, detail="Environment is not ready")
    if not env.get("public_ip"):
        raise HTTPException(status_code=409, detail="Environment has no public IP yet")

    target = f"http://{env['public_ip']}:{env['master_port']}/{master_path}"
    body = await request.body()

    headers = {"X-Admin-Key": env["master_api_key"]}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type

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
        logger.error("Environment %s unreachable at %s", env_id, env["public_ip"])
        raise HTTPException(status_code=502, detail="Environment master is unreachable")
    except httpx.HTTPError as exc:
        logger.exception("Proxy request to environment %s failed", env_id)
        raise HTTPException(status_code=502, detail=f"Proxy request failed: {exc}")

    response_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )
