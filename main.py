import asyncio
import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from database import get_supabase
from routers.auth import router as auth_router
from routers.connectors import router as connectors_router
from routers.containers import router as containers_router
from routers.contexts import router as contexts_router
from routers.environments import router as environments_router
from routers.kb_api import router as kb_api_router
from routers.mcp_bridge import router as mcp_bridge_router
from routers.proxy import router as proxy_router
from routers.workspace_proxy import router as workspace_proxy_router
from services.poller import empty_host_sweep_loop, poll_until_ready

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Belleq Platform API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _request_host(request: Request) -> str:
    """The hostname the client addressed, honouring the TLS-terminating proxy."""
    raw = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return raw.split(",")[0].split(":")[0].strip().lower()


def _is_api_path(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


# Always reachable regardless of host (infra health checks, root).
_HOST_AGNOSTIC_PATHS = {"/", "/health"}


@app.middleware("http")
async def enforce_host_routing(request: Request, call_next):
    """Keep each subdomain to its designated traffic.

    - The REST API (`/v1/*`) is accepted ONLY on ``API_HOST`` (api.belleq.app).
    - Everything else (MCP bridge + dashboard control plane) is accepted only on
      other hosts (mcp.belleq.app) and rejected on the API host.

    Disabled when ``API_HOST`` is blank. CORS preflight (OPTIONS) and a couple of
    infra paths always pass so health checks and browsers aren't broken.
    """
    api_host = (settings.API_HOST or "").strip().lower()
    if (
        not api_host
        or request.method == "OPTIONS"
        or request.url.path in _HOST_AGNOSTIC_PATHS
    ):
        return await call_next(request)

    host = _request_host(request)
    on_api_host = host == api_host
    is_api = _is_api_path(request.url.path)

    # API host: serve only the API. Other hosts: never serve the API.
    if on_api_host != is_api:
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"This endpoint is not served on '{host}'. "
                    f"Use https://{api_host} for the REST API (/v1/*), "
                    f"and {settings.MCP_HOST} for MCP and dashboard requests."
                )
            },
        )
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all so unhandled exceptions still return CORS headers.

    Without this, ServerErrorMiddleware converts the exception to a plain 500
    *before* CORSMiddleware can add Access-Control-Allow-Origin, causing the
    browser to see a CORS error instead of the real 500.
    """
    logger.error(
        "Unhandled exception on %s %s\n%s",
        request.method,
        request.url,
        traceback.format_exc(),
    )
    origin = request.headers.get("origin", "")
    headers = {}
    if origin in settings.cors_origins_list:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers=headers,
    )

app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(environments_router, prefix="/environments", tags=["environments"])
app.include_router(containers_router, tags=["containers"])
app.include_router(contexts_router, tags=["contexts"])
app.include_router(connectors_router, tags=["connectors"])
app.include_router(workspace_proxy_router, tags=["workspace-proxy"])
app.include_router(proxy_router, tags=["proxy"])
app.include_router(mcp_bridge_router, tags=["mcp-bridge"])
app.include_router(kb_api_router, tags=["kb-rest-api"])


@app.on_event("startup")
async def resume_provisioning_polls():
    """Re-start pollers for any environments still stuck in 'provisioning'.

    This covers the case where the backend restarts (container rebuild, deploy)
    while a poll_until_ready task was in-flight — the async task dies with the
    process, leaving the DB status stuck at 'provisioning' forever.
    """
    sb = get_supabase()
    result = (
        sb.table("environments")
        .select("id")
        .eq("status", "provisioning")
        .execute()
    )
    for env in result.data:
        logger.info("Resuming poller for environment %s", env["id"])
        asyncio.create_task(poll_until_ready(env["id"]))


@app.on_event("startup")
async def start_empty_host_sweep():
    """Background loop terminating empty hosts so we never pay for idle EC2."""
    if settings.EMPTY_HOST_SWEEP_ENABLED:
        asyncio.create_task(empty_host_sweep_loop())
        logger.info("empty_host_sweep scheduled")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "belleq-platform"}
