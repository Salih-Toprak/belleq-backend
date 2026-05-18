import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_supabase
from services import docker_manager

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_owned_env(env_id: str, user_id: str) -> dict:
    sb = get_supabase()
    result = sb.table("environments").select("*").eq("id", env_id).maybe_single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Environment not found")
    if result.data["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your environment")
    return result.data


@router.get("/environments/{env_id}/proxy/stats")
async def proxy_stats(env_id: str, user: dict = Depends(get_current_user)):
    env = _get_owned_env(env_id, user["id"])
    return await docker_manager.get_aggregate_stats(env)


@router.get("/environments/{env_id}/proxy/docs")
async def proxy_docs(env_id: str, user: dict = Depends(get_current_user)):
    env = _get_owned_env(env_id, user["id"])
    return await docker_manager.get_aggregate_docs(env)


@router.post("/environments/{env_id}/proxy/sync")
async def proxy_sync(env_id: str, request: Request, user: dict = Depends(get_current_user)):
    env = _get_owned_env(env_id, user["id"])
    body = await request.json() if await request.body() else {}
    return await docker_manager.trigger_sync(env, source_id=body.get("source_id"))


@router.get("/environments/{env_id}/proxy/sources")
async def proxy_get_sources(env_id: str, user: dict = Depends(get_current_user)):
    env = _get_owned_env(env_id, user["id"])
    return await docker_manager.get_sources(env)


@router.post("/environments/{env_id}/proxy/sources")
async def proxy_create_source(env_id: str, request: Request, user: dict = Depends(get_current_user)):
    env = _get_owned_env(env_id, user["id"])
    body = await request.json()
    return await docker_manager.create_source(env, body)
