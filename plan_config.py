"""Single source of truth for per-plan limits, resource caps, and host sizing.

Everything — scheduling, provisioning, and limit enforcement — reads from here,
so the numbers live in exactly one place. Confirmed plan numbers (2026-06):

    Plan       Contexts  KB storage  Queries/day  RAM/ctx  CPU/ctx  Disk/ctx  Hosting
    Starter    3         2 GB        500          256 MB   0.25     1 GB      shared pool (t3.large)
    Pro        10        20 GB       5,000        512 MB   0.50     2 GB      dedicated (t3.small)
    Team       25        100 GB      20,000       512 MB   0.50     2 GB      dedicated (t3.medium)
    Enterprise unlimited unlimited   unlimited    1 GB     1.00     10 GB     dedicated / own AWS
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

UNLIMITED = -1  # sentinel for "no limit" on counts/storage/queries


def is_unlimited(value: int) -> bool:
    return value == UNLIMITED


@dataclass(frozen=True)
class ResourceCaps:
    """Per-context Docker resource caps."""

    ram_mb: int
    cpu_vcpu: float
    disk_gb: int

    def as_payload(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PlanConfig:
    key: str  # canonical plan key
    max_contexts: int  # context containers per workspace
    kb_storage_gb: int  # qdrant storage per workspace (UNLIMITED allowed)
    queries_per_day: int  # knowledge queries per day
    memory_days: int  # conversation memory retention (UNLIMITED allowed)
    context_caps: ResourceCaps
    hosting: str  # "shared" | "dedicated"
    instance_type: str  # EC2 type for this plan's host(s)

    def as_payload(self) -> dict:
        """Serialisable limits payload sent to the master at provision time."""
        d = asdict(self)
        d["context_caps"] = self.context_caps.as_payload()
        return d


PLANS: dict[str, PlanConfig] = {
    "starter": PlanConfig(
        key="starter",
        max_contexts=3,
        kb_storage_gb=2,
        queries_per_day=500,
        memory_days=30,
        context_caps=ResourceCaps(ram_mb=256, cpu_vcpu=0.25, disk_gb=1),
        hosting="shared",
        instance_type="t3.large",
    ),
    "pro": PlanConfig(
        key="pro",
        max_contexts=10,
        kb_storage_gb=20,
        queries_per_day=5_000,
        memory_days=365,
        context_caps=ResourceCaps(ram_mb=512, cpu_vcpu=0.5, disk_gb=2),
        hosting="dedicated",
        instance_type="t3.small",
    ),
    "team": PlanConfig(
        key="team",
        max_contexts=25,
        kb_storage_gb=100,
        queries_per_day=20_000,
        memory_days=UNLIMITED,
        context_caps=ResourceCaps(ram_mb=512, cpu_vcpu=0.5, disk_gb=2),
        hosting="dedicated",
        instance_type="t3.medium",
    ),
    "enterprise": PlanConfig(
        key="enterprise",
        max_contexts=UNLIMITED,
        kb_storage_gb=UNLIMITED,
        queries_per_day=UNLIMITED,
        memory_days=UNLIMITED,
        context_caps=ResourceCaps(ram_mb=1024, cpu_vcpu=1.0, disk_gb=10),
        hosting="dedicated",
        instance_type="t3.large",
    ),
}

# Existing rbac/Supabase roles → canonical plan keys. (Legacy "free" maps to
# Starter; "admin" is treated as enterprise-level.)
ROLE_TO_PLAN: dict[str, str] = {
    "free": "starter",
    "starter": "starter",
    "pro": "pro",
    "team": "team",
    "enterprise": "enterprise",
    "admin": "enterprise",
}


def plan_for_role(role: str | None) -> PlanConfig:
    """Resolve a Supabase role to its PlanConfig. Defaults to Starter."""
    key = ROLE_TO_PLAN.get((role or "").lower(), "starter")
    return PLANS[key]


# ── Host capacity ────────────────────────────────────────────────────

@dataclass(frozen=True)
class InstanceSpec:
    instance_type: str
    cpu_vcpu: float
    ram_mb: int
    disk_gb: int


INSTANCE_SPECS: dict[str, InstanceSpec] = {
    "t3.small": InstanceSpec("t3.small", 2.0, 2048, 30),
    "t3.medium": InstanceSpec("t3.medium", 2.0, 4096, 40),
    "t3.large": InstanceSpec("t3.large", 2.0, 8192, 60),
}

# Reserved on a SHARED host for the master + qdrant + OS — not schedulable to
# contexts. (Dedicated hosts reserve the same; their single workspace's
# contexts share the remainder.)
HOST_RESERVE = ResourceCaps(ram_mb=1536, cpu_vcpu=0.6, disk_gb=12)


def schedulable_budget(instance_type: str) -> ResourceCaps:
    """Resources available to context containers on a host of this type."""
    spec = INSTANCE_SPECS.get(instance_type, INSTANCE_SPECS["t3.large"])
    return ResourceCaps(
        ram_mb=spec.ram_mb - HOST_RESERVE.ram_mb,
        cpu_vcpu=round(spec.cpu_vcpu - HOST_RESERVE.cpu_vcpu, 2),
        disk_gb=spec.disk_gb - HOST_RESERVE.disk_gb,
    )
