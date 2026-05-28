import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from database import get_supabase
from rbac import require_plan, check_container_limit
from services import docker_manager

logger = logging.getLogger(__name__)
router = APIRouter()


class ProvisionContainerRequest(BaseModel):
    name: str


def _get_owned_env(env_id: str, user_id: str) -> dict:
    sb = get_supabase()
    result = sb.table("environments").select("*").eq("id", env_id).maybe_single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Environment not found")
    if result.data["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your environment")
    return result.data


@router.post("/environments/{env_id}/containers/provision", status_code=201)
async def provision_container(
    env_id: str,
    body: ProvisionContainerRequest,
    user: dict = Depends(get_current_user),
    profile: dict = Depends(require_plan),
):
    env = _get_owned_env(env_id, user["id"])
    if env["status"] != "ready":
        raise HTTPException(status_code=400, detail="Environment is not ready")
    check_container_limit(profile, env_id)

    sb = get_supabase()
    api_key = secrets.token_hex(32)
    container_name = f"belleq-user-{uuid.uuid4().hex[:8]}"
    container_id = str(uuid.uuid4())

    master_result = await docker_manager.provision_container(
        env=env,
        container_name=container_name,
        api_key=api_key,
        user_id=user["id"],
    )

    port = master_result.get("port", env["master_port"] + 1)

    container = {
        "id": container_id,
        "environment_id": env_id,
        "user_id": user["id"],
        "name": body.name,
        "container_name": container_name,
        "api_key": api_key,
        "port": port,
        "status": "running",
    }
    result = sb.table("containers").insert(container).execute()

    sb.table("audit_logs").insert({
        "user_id": user["id"],
        "action": "container.create",
        "resource_id": container_id,
        "detail": f"container_name={container_name}",
    }).execute()

    logger.info("Container %s provisioned in environment %s", container_name, env_id)
    return result.data[0]


@router.get("/environments/{env_id}/containers")
async def list_containers(env_id: str, user: dict = Depends(get_current_user)):
    _get_owned_env(env_id, user["id"])
    sb = get_supabase()
    result = sb.table("containers").select("*").eq("environment_id", env_id).order("created_at", desc=True).execute()
    return result.data


@router.delete("/environments/{env_id}/containers/{container_id}", status_code=204)
async def delete_container(
    env_id: str,
    container_id: str,
    user: dict = Depends(get_current_user),
):
    env = _get_owned_env(env_id, user["id"])
    sb = get_supabase()

    result = sb.table("containers").select("*").eq("id", container_id).eq("environment_id", env_id).maybe_single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Container not found")

    container = result.data
    try:
        await docker_manager.delete_container(env, container["container_name"])
    except Exception:
        logger.exception("Failed to delete container %s from master", container["container_name"])

    sb.table("containers").update({"status": "stopped"}).eq("id", container_id).execute()

    sb.table("audit_logs").insert({
        "user_id": user["id"],
        "action": "container.delete",
        "resource_id": container_id,
    }).execute()
    logger.info("Container %s deleted", container["container_name"])
