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
from rbac import require_plan, require_role
from services import connector_store, scheduler

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/contexts", tags=["contexts"])

_SECRET_FIELDS = {"api_key", "master_api_key"}


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _SECRET_FIELDS}


class CreateContextBody(BaseModel):
    name: str
    region: str = "eu-west-1"


class RenameContextBody(BaseModel):
    name: str


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


async def _rehydrate_connectors(host: dict, workspace_id: str) -> None:
    """Push the workspace's stored connectors onto its (possibly fresh) master.

    Best-effort: a connector restore failure must not fail context provisioning.
    The master stores the encrypted blobs verbatim and skips rows it already has
    a newer copy of, so this is safe to call on every provision.
    """
    try:
        rows = connector_store.list_for_workspace(workspace_id)
        if not rows:
            return
        items = [
            {
                "connector_id": r["connector_id"],
                "display_name": r.get("display_name", ""),
                "transport": r.get("transport", "streamable_http"),
                "url": r.get("url", ""),
                "command": r.get("command", ""),
                "args": r.get("args", []) or [],
                "enabled": bool(r.get("enabled", True)),
                "auth_status": r.get("auth_status", "none"),
                "tool_count": int(r.get("tool_count", 0) or 0),
                "last_status": r.get("last_status", "unknown"),
                "secrets_encrypted": r.get("secrets_encrypted", "{}") or "{}",
                "metadata": r.get("metadata", {}) or {},
                "added_at": r.get("added_at"),
                "updated_at": r.get("updated_at"),
            }
            for r in rows
        ]
        headers = {"X-Admin-Key": host["master_api_key"], "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{host['master_endpoint']}/master/mcp/connectors/import",
                json={"connectors": items},
                headers=headers,
            )
            resp.raise_for_status()
        logger.info(
            "connectors_rehydrated ws=%s host=%s count=%s",
            workspace_id, host["id"], len(items),
        )
    except Exception:  # noqa: BLE001
        logger.exception("connector_rehydrate_failed ws=%s host=%s", workspace_id, host.get("id"))


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
        # Restore the workspace's connectors onto this host's master. Crucial
        # when the host is fresh (first context, or after an empty-host teardown):
        # connectors persisted on the backend reappear automatically.
        await _rehydrate_connectors(host, workspace_id)
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


@router.post("/admin/terminate-empty-hosts")
async def admin_terminate_empty_hosts(
    min_age_minutes: int = 10,
    _: dict = Depends(require_role("admin")),
):
    """Admin: terminate every host with no active contexts (clean up idle EC2).

    ``min_age_minutes`` skips very young hosts that may be mid-provision; pass 0
    to sweep everything. New deletes already trigger this per-host automatically.
    """
    terminated = await scheduler.terminate_empty_hosts(min_age_minutes=min_age_minutes)
    return {"terminated": terminated}


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


def _owned_context_or_404(sb, context_id: str, workspace_id: str) -> dict:
    r = (
        sb.table("containers")
        .select("*")
        .eq("id", context_id)
        .eq("workspace_id", workspace_id)
        .maybe_single()
        .execute()
    )
    if not r.data:
        raise HTTPException(status_code=404, detail="Context not found")
    return r.data


@router.patch("/{context_id}")
async def rename_context(
    context_id: str,
    body: RenameContextBody,
    user: dict = Depends(get_current_user),
):
    """Rename a context. The container name + MCP endpoint (keyed by id) are
    unchanged — only the human-facing display name moves, in the DB and, best
    effort, on the host's master registry."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name cannot be empty")
    sb = get_supabase()
    ctx = _owned_context_or_404(sb, context_id, user["id"])

    sb.table("containers").update({"name": name}).eq("id", context_id).execute()

    # Best-effort: update the display name on the master registry too.
    host_id = ctx.get("host_id")
    if host_id:
        host = (
            sb.table("hosts").select("*").eq("id", host_id).maybe_single().execute()
        ).data
        if host:
            try:
                headers = {"X-Admin-Key": host["master_api_key"], "Content-Type": "application/json"}
                async with httpx.AsyncClient(timeout=15.0) as client:
                    await client.patch(
                        f"{host['master_endpoint']}/master/registry/containers/{ctx['container_name']}",
                        json={"display_name": name},
                        headers=headers,
                    )
            except Exception:  # noqa: BLE001
                logger.warning("master_rename_failed context=%s", context_id)

    sb.table("audit_logs").insert(
        {"user_id": user["id"], "action": "context.rename", "resource_id": context_id}
    ).execute()
    return _public({**ctx, "name": name})


@router.get("/{context_id}/api-key")
async def context_api_key(context_id: str, user: dict = Depends(get_current_user)):
    """Reveal the context's API key. Owner-only; deliberately excluded from the
    default (redacted) context payload, so it has its own endpoint."""
    sb = get_supabase()
    ctx = _owned_context_or_404(sb, context_id, user["id"])
    return {"id": context_id, "api_key": ctx.get("api_key", "")}


@router.post("/{context_id}/rebuild")
async def rebuild_context(context_id: str, user: dict = Depends(get_current_user)):
    """Re-pull the newest published image and recreate the container in place.

    The context keeps its id, endpoint, API key, and knowledge base (the data
    volume is preserved on the host); only the running image is refreshed.
    """
    sb = get_supabase()
    ctx = _owned_context_or_404(sb, context_id, user["id"])
    host_id = ctx.get("host_id")
    if not host_id:
        raise HTTPException(
            status_code=409,
            detail="Context has no host yet — wait for provisioning to finish before rebuilding.",
        )

    sb.table("containers").update(
        {"status": "provisioning", "error_message": None}
    ).eq("id", context_id).execute()
    sb.table("audit_logs").insert(
        {"user_id": user["id"], "action": "context.rebuild", "resource_id": context_id}
    ).execute()

    asyncio.create_task(_rebuild_bg(context_id, user["id"], ctx, host_id))
    logger.info("context_rebuild_started id=%s ws=%s", context_id, user["id"])
    return _public({**ctx, "status": "provisioning", "error_message": None})


async def _rebuild_bg(context_id: str, workspace_id: str, ctx: dict, host_id: str):
    sb = get_supabase()
    try:
        host = (
            sb.table("hosts").select("*").eq("id", host_id).maybe_single().execute()
        ).data
        if not host:
            raise RuntimeError("Host record not found for this context")

        caps = ResourceCaps(
            ram_mb=ctx.get("ram_cap_mb") or 0,
            cpu_vcpu=float(ctx.get("cpu_cap_vcpu") or 0),
            disk_gb=ctx.get("disk_cap_gb") or 0,
        )
        labels = naming.docker_labels(
            role="context",
            workspace_id=workspace_id,
            context_id=context_id,
            context_name=ctx.get("name", ""),
            plan=ctx.get("plan", ""),
            host_pool=host.get("pool"),
        )
        payload = {
            "container_name": ctx["container_name"],
            "display_name": ctx.get("name", ""),
            "api_key": ctx.get("api_key"),
            "user_id": workspace_id,
            "workspace_id": workspace_id,
            "plan": ctx.get("plan", ""),
            "caps": caps.as_payload(),
            "labels": labels,
            "qdrant_collection": ctx.get("qdrant_collection"),
            "container_type": "user",
            "extraction": settings.extraction_payload,
            "force_pull": True,
        }
        result = await _master_provision(host, payload)
        sb.table("containers").update(
            {"status": "running" if result.get("healthy") else "starting"}
        ).eq("id", context_id).execute()
        # The recreated container is a fresh master registry entry — make sure
        # the workspace's connectors are present on it again.
        await _rehydrate_connectors(host, workspace_id)
        logger.info("context_rebuilt id=%s host=%s", context_id, host_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("context_rebuild_failed id=%s", context_id)
        sb.table("containers").update(
            {"status": "error", "error_message": str(exc)[:500]}
        ).eq("id", context_id).execute()


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
    host_id = ctx.get("host_id")

    # Best-effort teardown on the host's master. Wrapped so a wedged/unreachable
    # master can never block the DB cleanup below (otherwise the row lingers and
    # the delete looks like it "didn't save").
    if host_id:
        host = (
            sb.table("hosts").select("*").eq("id", host_id).maybe_single().execute()
        ).data
        if host:
            try:
                await _master_delete(host, ctx["container_name"])
            except Exception:  # noqa: BLE001
                logger.exception("master_delete_failed context=%s", context_id)
            try:
                caps = ResourceCaps(
                    ram_mb=ctx.get("ram_cap_mb") or 0,
                    cpu_vcpu=float(ctx.get("cpu_cap_vcpu") or 0),
                    disk_gb=ctx.get("disk_cap_gb") or 0,
                )
                scheduler.release_capacity(host["id"], caps)
            except Exception:  # noqa: BLE001
                logger.exception("release_capacity_failed context=%s", context_id)

    # Hard-delete the row so the context is actually gone from the DB.
    sb.table("containers").delete().eq("id", context_id).execute()
    sb.table("audit_logs").insert(
        {"user_id": user["id"], "action": "context.delete", "resource_id": context_id}
    ).execute()

    # Cost control: if the host has no contexts left, terminate its EC2 instance
    # and remove the host row so we never pay for an empty instance.
    if host_id:
        try:
            await scheduler.terminate_host_if_empty(host_id)
        except Exception:  # noqa: BLE001
            logger.exception("terminate_host_if_empty_failed host=%s", host_id)
