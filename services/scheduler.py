"""Host scheduler — decides which EC2 a new context lands on.

  Starter (shared)      -> a shared pool host with free budget, else provision a
                           new shared host (one master + qdrant, many workspaces).
  Pro/Team/Enterprise   -> the workspace's dedicated host (provision on first use).

Capacity is reserved atomically via the reserve_host_capacity RPC, so two
concurrent placements can never over-commit a host. Everything is tagged/named
through naming.py so a shared host stays attributable per workspace.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from datetime import datetime, timezone

import httpx

import naming
import plan_config as pc
from config import settings
from database import get_supabase
from services import aws

logger = logging.getLogger(__name__)

MASTER_PORT = 9000


# ── Capacity (atomic via RPC) ────────────────────────────────────────

def reserve_capacity(host_id: str, caps: pc.ResourceCaps) -> bool:
    sb = get_supabase()
    res = sb.rpc(
        "reserve_host_capacity",
        {
            "p_host_id": host_id,
            "p_cpu": caps.cpu_vcpu,
            "p_ram_mb": caps.ram_mb,
            "p_disk_gb": caps.disk_gb,
        },
    ).execute()
    return bool(res.data)


def release_capacity(host_id: str, caps: pc.ResourceCaps) -> None:
    sb = get_supabase()
    sb.rpc(
        "release_host_capacity",
        {
            "p_host_id": host_id,
            "p_cpu": caps.cpu_vcpu,
            "p_ram_mb": caps.ram_mb,
            "p_disk_gb": caps.disk_gb,
        },
    ).execute()


# ── Pool naming ──────────────────────────────────────────────────────

def _region_short(region: str) -> str:
    return region.replace("-", "")  # eu-west-1 -> euwest1


def next_pool_name(region: str) -> str:
    """Next shared-pool name in a region, e.g. euwest1-03."""
    sb = get_supabase()
    res = (
        sb.table("hosts")
        .select("id", count="exact")
        .eq("host_type", "shared")
        .eq("region", region)
        .execute()
    )
    n = (res.count or 0) + 1
    return f"{_region_short(region)}-{n:02d}"


# ── Host readiness ───────────────────────────────────────────────────

async def _poll_master_ready(endpoint: str, timeout: int = 600, interval: int = 15) -> bool:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(f"{endpoint}/health")
                if r.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(interval)
    return False


# ── Host provisioning ────────────────────────────────────────────────

async def provision_host(
    *,
    host_type: str,
    plan: pc.PlanConfig,
    region: str,
    pool: str | None = None,
    workspace_id: str | None = None,
) -> dict:
    """Launch a new EC2 host (master + qdrant), wait for health, return the row.

    Raises on failure (the hosts row is marked error).
    """
    sb = get_supabase()
    budget = pc.schedulable_budget(plan.instance_type)
    host_id = str(uuid.uuid4())
    master_api_key = secrets.token_hex(32)

    sb.table("hosts").insert(
        {
            "id": host_id,
            "host_type": host_type,
            "pool": pool,
            "workspace_id": workspace_id,
            "plan": plan.key,
            "region": region,
            "instance_type": plan.instance_type,
            "master_api_key": master_api_key,
            "cpu_budget": budget.cpu_vcpu,
            "ram_budget_mb": budget.ram_mb,
            "disk_budget_gb": budget.disk_gb,
            "status": "provisioning",
        }
    ).execute()

    name = naming.host_name(host_type=host_type, pool=pool, plan=plan.key, workspace_id=workspace_id)
    tags = naming.ec2_tags(host_type=host_type, plan=plan.key, pool=pool, workspace_id=workspace_id)

    try:
        ec2 = await aws.provision_ec2(
            instance_name=name,
            master_api_key=master_api_key,
            region=region,
            instance_type=plan.instance_type,
            tags=tags,
        )
    except Exception as exc:  # noqa: BLE001
        sb.table("hosts").update({"status": "error", "error_message": str(exc)}).eq("id", host_id).execute()
        logger.exception("host_ec2_launch_failed host=%s", host_id)
        raise

    endpoint = f"http://{ec2['public_ip']}:{MASTER_PORT}"
    sb.table("hosts").update(
        {
            "ec2_instance_id": ec2["instance_id"],
            "public_ip": ec2["public_ip"],
            "master_endpoint": endpoint,
        }
    ).eq("id", host_id).execute()

    ready = await _poll_master_ready(endpoint, timeout=settings.INTERNAL_POLL_TIMEOUT)
    if not ready:
        sb.table("hosts").update({"status": "error", "error_message": "master health timeout"}).eq("id", host_id).execute()
        raise RuntimeError(f"host {host_id} master did not become ready")

    sb.table("hosts").update(
        {"status": "ready", "ready_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", host_id).execute()

    host = sb.table("hosts").select("*").eq("id", host_id).maybe_single().execute()
    return host.data


# ── Placement ────────────────────────────────────────────────────────

def _workspace_home_host_id(workspace_id: str) -> str | None:
    """The host where this workspace already has contexts (affinity), if any."""
    sb = get_supabase()
    rows = (
        sb.table("containers")
        .select("host_id")
        .eq("workspace_id", workspace_id)
        .neq("status", "stopped")
        .not_.is_("host_id", "null")
        .limit(1)
        .execute()
    ).data or []
    return rows[0]["host_id"] if rows else None


def find_ready_host_with_capacity(
    *, plan: pc.PlanConfig, region: str, workspace_id: str, caps: pc.ResourceCaps
) -> dict | None:
    """Reserve a slot on an existing READY host, returning it, or None.

    Workspace affinity: a workspace's contexts prefer the host where it already
    has contexts, so its (workspace-level) connectors stay co-located on one
    master.
    """
    sb = get_supabase()
    q = sb.table("hosts").select("*").eq("region", region).eq("status", "ready")
    if plan.hosting == "shared":
        q = q.eq("host_type", "shared").eq("plan", plan.key)
    else:
        q = q.eq("host_type", "dedicated").eq("workspace_id", workspace_id)
    rows = q.order("created_at").execute().data or []

    # Try the workspace's existing home host first.
    home_id = _workspace_home_host_id(workspace_id)
    if home_id:
        rows.sort(key=lambda h: 0 if h["id"] == home_id else 1)

    for h in rows:
        if reserve_capacity(h["id"], caps):
            return h
    return None


async def place_or_provision(
    *, plan: pc.PlanConfig, region: str, workspace_id: str, caps: pc.ResourceCaps
) -> dict:
    """Return a ready host with a reserved slot for one context.

    Fast path: reuse an existing host. Slow path: provision a new EC2 host
    (may take minutes) — call this from a background task.
    """
    host = find_ready_host_with_capacity(
        plan=plan, region=region, workspace_id=workspace_id, caps=caps
    )
    if host is not None:
        return host

    host = await provision_host(
        host_type=plan.hosting,  # "shared" | "dedicated"
        plan=plan,
        region=region,
        pool=next_pool_name(region) if plan.hosting == "shared" else None,
        workspace_id=workspace_id if plan.hosting == "dedicated" else None,
    )
    if not reserve_capacity(host["id"], caps):
        raise RuntimeError(f"freshly provisioned host {host['id']} had no capacity")
    return host
