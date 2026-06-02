import asyncio
import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from database import get_supabase
from routers.auth import router as auth_router
from routers.containers import router as containers_router
from routers.environments import router as environments_router
from routers.mcp_bridge import router as mcp_bridge_router
from routers.proxy import router as proxy_router
from services.poller import poll_until_ready

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
app.include_router(proxy_router, tags=["proxy"])
app.include_router(mcp_bridge_router, tags=["mcp-bridge"])


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


@app.get("/health")
async def health():
    return {"status": "ok", "service": "belleq-platform"}
