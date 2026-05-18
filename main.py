import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from routers.auth import router as auth_router
from routers.containers import router as containers_router
from routers.environments import router as environments_router
from routers.proxy import router as proxy_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI(title="Belleq Platform API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(environments_router, prefix="/environments", tags=["environments"])
app.include_router(containers_router, tags=["containers"])
app.include_router(proxy_router, tags=["proxy"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": "belleq-platform"}
