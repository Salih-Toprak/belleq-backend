"""Context provisioning — the collapsed, automatic flow (no user-facing env).

Creating a context: pick the plan, place the context on a host (shared pool for
Starter, dedicated for Pro/Team) via the scheduler, provision the container on
that host's master with the right caps/labels/collection, and store the row.
Host provisioning can take minutes, so the work runs in a background task and
the row carries a `status` the dashboard polls (provisioning → running | error).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import naming
from auth import get_current_user
from config import settings
from database import get_supabase
from plan_config import ResourceCaps, is_unlimited, plan_for_role
from rbac import require_plan
from services import scheduler

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/contexts", tags=["contexts"])

_SECRET_FIELDS = {"api_key", "master_api_key"}


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _SECRET_FIELDS}


class CreateContextBody(BaseModel):
    name: str
    region: str = "eu-west-1"


async def _master_provision(host: dict, payload: dict) -> dict:
    headers = {"X-Admin-Key": host["master_api_key"], "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{host['master_endpoint']}/master/containers/provision",
            json=payload,
            headers=headers,
        )
        r.raise_for_status()
        return r.json()


async def _master_delete(host: dict, container_name: str) -> None:
    headers = {"X-Admin-Key": host["master_api_key"]}
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.delete(
            f"{host['master_endpoint']}/master/containers/{container_name}",
            headers=headers,
        )


@router.post("/provision", status_code=201)
async def provision_context(
    body: CreateContextBody,
    user: dict = Depends(get_current_user),
    profile: dict = Depends(require_plan),
):
    plan = plan_for_role(profile.get("role"))
    workspace_id = user["id"]
    sb = get_supabase()

    # Enforce the per-plan context cap.
    if not is_unlimited(plan.max_contexts):
        cnt = (
            sb.table("containers")
            .select("id", count="exact")
            .eq("workspace_id", workspace_id)
            .neq("status", "stopped")
            .execute()
        ).count or 0
        if cnt >= plan.max_contexts:
            raise HTTPException(
                status_code=403,
                detail=f"Your {plan.key} plan allows up to {plan.max_contexts} contexts. "
                f"Delete one or upgrade your plan.",
            )

    context_id = str(uuid.uuid4())
    container_name = naming.context_container_name(workspace_id, body.name, context_id)
    collection = naming.qdrant_collection(context_id)
    caps = plan.context_caps
    # Generate the context's API key up front so the row is always complete —
    # the same key is handed to the master when the container provisions.
    api_key = secrets.token_hex(32)

    row = {
        "id": context_id,
        "workspace_id": workspace_id,
        "user_id": workspace_id,
        "name": body.name.strip(),
        "container_name": container_name,
        "qdrant_collection": collection,
        "api_key": api_key,
        "plan": plan.key,
        "ram_cap_mb": caps.ram_mb,
        "cpu_cap_vcpu": caps.cpu_vcpu,
        "disk_cap_gb": caps.disk_gb,
        "status": "provisioning",
        "port": 8000,
    }
    sb.table("containers").insert(row).execute()
    sb.table("audit_logs").insert(
        {"user_id": workspace_id, "action": "context.provision", "resource_id": context_id}
    ).execute()

    asyncio.create_task(
        _provision_bg(
            context_id, workspace_id, profile.get("role"), body.name,
            container_name, collection, body.region, api_key,
        )
    )
    logger.info("context_provision_started id=%s ws=%s", context_id, workspace_id)
    return _public(row)


async def _provision_bg(context_id, workspace_id, role, name, container_name, collection, region, api_key):
    sb = get_supabase()
    plan = plan_for_role(role)
    caps = plan.context_caps
    host: dict | None = None
    try:
        host = await scheduler.place_or_provision(
            plan=plan, region=region, workspace_id=workspace_id, caps=caps
        )
        labels = naming.docker_labels(
            role="context",
            workspace_id=workspace_id,
            context_id=context_id,
            context_name=name,
            plan=plan.key,
            host_pool=host.get("pool"),
        )
        payload = {
            "container_name": container_name,
            "display_name": name,
            "api_key": api_key,
            "user_id": workspace_id,
            "workspace_id": workspace_id,
            "plan": plan.key,
            "caps": caps.as_payload(),
            "labels": labels,
            "qdrant_collection": collection,
            "container_type": "user",
            # Extraction creds live on the (static) backend and are pushed down
            # per-context so masters/containers never store them.
            "extraction": settings.extraction_payload,
        }
        result = await _master_provision(host, payload)
        sb.table("containers").update(
            {
                "host_id": host["id"],
                "api_key": api_key,
                "port": result.get("port", 8000),
                "status": "running" if result.get("healthy") else "starting",
            }
        ).eq("id", context_id).execute()
        logger.info("context_provisioned id=%s host=%s", context_id, host["id"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("context_provision_failed id=%s", context_id)
        # Release the slot we reserved if we got as far as placement.
        if host is not None:
            try:
                scheduler.release_capacity(host["id"], caps)
            except Exception:  # noqa: BLE001
                pass
        sb.table("containers").update(
            {"status": "error", "error_message": str(exc)[:500]}
        ).eq("id", context_id).execute()


@router.get("")
async def list_contexts(user: dict = Depends(get_current_user)):
    sb = get_supabase()
    rows = (
        sb.table("containers")
        .select("*")
        .eq("workspace_id", user["id"])
        .neq("status", "stopped")
        .order("created_at", desc=True)
        .execute()
    ).data or []
    return [_public(r) for r in rows]


@router.get("/{context_id}/status")
async def context_status(context_id: str, user: dict = Depends(get_current_user)):
    sb = get_supabase()
    r = (
        sb.table("containers")
        .select("id, status, error_message, host_id, qdrant_collection")
        .eq("id", context_id)
        .eq("workspace_id", user["id"])
        .maybe_single()
        .execute()
    )
    if not r.data:
        raise HTTPException(status_code=404, detail="Context not found")
    return r.data


@router.delete("/{context_id}", status_code=204)
async def delete_context(context_id: str, user: dict = Depends(get_current_user)):
    sb = get_supabase()
    r = (
        sb.table("containers")
        .select("*")
        .eq("id", context_id)
        .eq("workspace_id", user["id"])
        .maybe_single()
        .execute()
    )
    if not r.data:
        raise HTTPException(status_code=404, detail="Context not found")
    ctx = r.data

    if ctx.get("host_id"):
        host = (
            sb.table("hosts").select("*").eq("id", ctx["host_id"]).maybe_single().execute()
        ).data
        if host:
            try:
                await _master_delete(host, ctx["container_name"])
            except Exception:  # noqa: BLE001
                logger.exception("master_delete_failed context=%s", context_id)
            caps = ResourceCaps(
                ram_mb=ctx.get("ram_cap_mb") or 0,
                cpu_vcpu=float(ctx.get("cpu_cap_vcpu") or 0),
                disk_gb=ctx.get("disk_cap_gb") or 0,
            )
            scheduler.release_capacity(host["id"], caps)

    sb.table("containers").update({"status": "stopped"}).eq("id", context_id).execute()
    sb.table("audit_logs").insert(
        {"user_id": user["id"], "action": "context.delete", "resource_id": context_id}
    ).execute()
