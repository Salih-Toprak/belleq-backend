"""Naming, tagging, and metadata conventions for every belleq resource.

ONE place defines how things are named so a shared host (many workspaces) stays
fully attributable — for billing, cleanup, and debugging.

Conventions
-----------
Docker container names : belleq-<role>[-<ws_slug>-<ctx_slug>-<short>]
Docker labels (keys)   : belleq.<key>  (dot-namespaced)
AWS EC2 tags (keys)    : belleq:<key>  (colon-namespaced)
Qdrant collection      : c_<ctx_short>
Docker volume          : <container_name>-data

Filtering examples
------------------
  docker ps --filter label=belleq.workspace-id=<uuid>     # one customer's footprint
  aws ec2 ... --filters Name=tag:belleq:pool,Values=<pool>
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

MANAGED_BY = "belleq-platform"


def _hex(id_: str, n: int) -> str:
    """First n hex chars of a uuid-ish id (dashes stripped)."""
    return (id_ or "").replace("-", "").lower()[:n] or "x" * n


def slugify(text: str, max_len: int = 18) -> str:
    """Lowercase, hyphenated, ascii slug safe for Docker names and labels."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:max_len].strip("-") or "x"


# ── Slugs / short ids ────────────────────────────────────────────────

def workspace_slug(workspace_id: str) -> str:
    """Stable human-readable workspace handle, e.g. ws_3a9f2c."""
    return f"ws_{_hex(workspace_id, 6)}"


def context_short(context_id: str) -> str:
    return _hex(context_id, 6)


# ── Docker names ─────────────────────────────────────────────────────

def context_container_name(workspace_id: str, context_name: str, context_id: str) -> str:
    """e.g. belleq-ctx-ws3a9f2c-design-4f2a1b (unique per host)."""
    ws = workspace_slug(workspace_id).replace("_", "")
    return f"belleq-ctx-{ws}-{slugify(context_name, 16)}-{context_short(context_id)}"


def context_volume_name(container_name: str) -> str:
    return f"{container_name}-data"


def master_container_name() -> str:
    return "belleq-master"


def qdrant_container_name() -> str:
    return "belleq-qdrant"


# ── Qdrant ───────────────────────────────────────────────────────────

def qdrant_collection(context_id: str) -> str:
    """Globally-unique collection name on the shared qdrant: c_<12 hex>."""
    return f"c_{_hex(context_id, 12)}"


# ── Docker labels ────────────────────────────────────────────────────

def docker_labels(
    *,
    role: str,  # master | qdrant | context
    workspace_id: str | None = None,
    context_id: str | None = None,
    context_name: str | None = None,
    plan: str | None = None,
    host_pool: str | None = None,
) -> dict[str, str]:
    """Labels stamped on every container. Keys are belleq.<key> (dot-namespaced)."""
    labels: dict[str, str] = {
        "belleq.role": role,
        "belleq.managed-by": MANAGED_BY,
        "belleq.created-at": datetime.now(timezone.utc).isoformat(),
    }
    if workspace_id:
        labels["belleq.workspace-id"] = workspace_id
        labels["belleq.workspace-slug"] = workspace_slug(workspace_id)
    if context_id:
        labels["belleq.context-id"] = context_id
    if context_name:
        labels["belleq.context-name"] = context_name
    if plan:
        labels["belleq.plan"] = plan
    if host_pool:
        labels["belleq.host-pool"] = host_pool
    return labels


# ── AWS EC2 tags ─────────────────────────────────────────────────────

def host_name(
    *,
    host_type: str,  # shared | dedicated
    pool: str | None = None,
    plan: str | None = None,
    workspace_id: str | None = None,
) -> str:
    """EC2 Name tag. Shared: belleq-pool-<pool>. Dedicated: belleq-<plan>-<ws_slug>."""
    if host_type == "shared":
        return f"belleq-pool-{pool or 'default'}"
    ws = workspace_slug(workspace_id or "")
    return f"belleq-{plan or 'dedicated'}-{ws}"


def ec2_tags(
    *,
    host_type: str,  # shared | dedicated
    plan: str | None = None,
    pool: str | None = None,
    workspace_id: str | None = None,
) -> list[dict[str, str]]:
    """EC2 tag list. Keys are belleq:<key> (colon-namespaced)."""
    role = "shared-pool" if host_type == "shared" else "dedicated"
    tags: list[dict[str, str]] = [
        {"Key": "Name", "Value": host_name(host_type=host_type, pool=pool, plan=plan, workspace_id=workspace_id)},
        {"Key": "belleq:role", "Value": role},
        {"Key": "belleq:managed-by", "Value": MANAGED_BY},
    ]
    if plan:
        tags.append({"Key": "belleq:plan", "Value": plan})
    if pool:
        tags.append({"Key": "belleq:pool", "Value": pool})
    if workspace_id and host_type == "dedicated":
        tags.append({"Key": "belleq:workspace-id", "Value": workspace_id})
    return tags
