import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from database import get_supabase
from rbac import get_current_profile, require_plan, check_environment_limit
from services import aws
from services.poller import poll_until_ready

logger = logging.getLogger(__name__)
router = APIRouter()


class ProvisionRequest(BaseModel):
    name: str
    region: str = "eu-west-1"


def _check_ownership(env: dict | None, user_id: str) -> dict:
    if not env:
        raise HTTPException(status_code=404, detail="Environment not found")
    if env["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your environment")
    return env


def _public(env: dict) -> dict:
    """Strip the master admin key before returning an environment to a client."""
    return {k: v for k, v in env.items() if k != "master_api_key"}


@router.get("")
async def list_environments(user: dict = Depends(get_current_user)):
    sb = get_supabase()
    result = sb.table("environments").select("*, containers(count)").eq("user_id", user["id"]).order("created_at", desc=True).execute()
    return [_public(env) for env in result.data]


@router.post("/provision", status_code=201)
async def provision_environment(
    body: ProvisionRequest,
    user: dict = Depends(get_current_user),
    profile: dict = Depends(require_plan),
):
    check_environment_limit(profile)
    sb = get_supabase()
    env_id = str(uuid.uuid4())
    master_api_key = secrets.token_hex(32)

    env = {
        "id": env_id,
        "user_id": user["id"],
        "name": body.name,
        "status": "provisioning",
        "aws_region": body.region,
        "master_api_key": master_api_key,
        "master_port": 9000,
    }
    result = sb.table("environments").insert(env).execute()

    sb.table("audit_logs").insert({
        "user_id": user["id"],
        "action": "environment.provision",
        "resource_id": env_id,
    }).execute()

    logger.info("Provisioning environment %s for user %s", env_id, user["id"])

    async def _provision_background():
        try:
            ec2_result = await aws.provision_ec2(
                instance_name=f"belleq-{body.name}-{env_id[:8]}",
                master_api_key=master_api_key,
                region=body.region,
            )
            sb.table("environments").update({
                "aws_instance_id": ec2_result["instance_id"],
                "public_ip": ec2_result["public_ip"],
            }).eq("id", env_id).execute()

            asyncio.create_task(poll_until_ready(env_id))
        except Exception as exc:
            logger.exception("Failed to provision EC2 for environment %s", env_id)
            sb.table("environments").update({
                "status": "error",
                "error_message": str(exc),
            }).eq("id", env_id).execute()

    asyncio.create_task(_provision_background())
    return _public(result.data[0])


@router.get("/{env_id}")
async def get_environment(env_id: str, user: dict = Depends(get_current_user)):
    sb = get_supabase()
    result = sb.table("environments").select("*, containers(count)").eq("id", env_id).maybe_single().execute()
    env = _check_ownership(result.data, user["id"])
    return _public(env)


@router.get("/{env_id}/status")
async def get_environment_status(env_id: str, user: dict = Depends(get_current_user)):
    sb = get_supabase()
    result = sb.table("environments").select("user_id, status, public_ip, ready_at, error_message").eq("id", env_id).maybe_single().execute()
    _check_ownership(result.data, user["id"])
    data = result.data
    data.pop("user_id", None)
    return data


@router.delete("/{env_id}", status_code=204)
async def delete_environment(env_id: str, user: dict = Depends(get_current_user)):
    sb = get_supabase()
    result = sb.table("environments").select("*").eq("id", env_id).maybe_single().execute()
    env = _check_ownership(result.data, user["id"])

    if env.get("aws_instance_id"):
        try:
            await aws.terminate_ec2(env["aws_instance_id"], env["aws_region"])
        except Exception:
            logger.exception("Failed to terminate EC2 %s", env["aws_instance_id"])

    sb.table("environments").update({"status": "terminated"}).eq("id", env_id).execute()
    sb.table("containers").update({"status": "stopped"}).eq("environment_id", env_id).execute()

    sb.table("audit_logs").insert({
        "user_id": user["id"],
        "action": "environment.delete",
        "resource_id": env_id,
    }).execute()
    logger.info("Environment %s terminated", env_id)
