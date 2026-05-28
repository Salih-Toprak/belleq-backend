"""
Role-based access control helpers.

Roles
─────
admin      – bypasses all gates (you assign this in Supabase manually)
free       – 1 environment, 1 container per environment
pro        – 3 environments, 5 containers per environment
team       – 5 environments, 20 containers per environment
enterprise – unlimited

A user with no profile row is treated as having no plan (role=None);
the frontend redirects them to the plan-selection screen.
"""

from fastapi import Depends, HTTPException, status

from auth import get_current_user
from database import get_supabase

# ── Plan limits ──────────────────────────────────────────────────────────────

PLAN_LIMITS: dict[str, dict] = {
    "free": {"max_environments": 1, "max_containers_per_env": 1},
    "pro": {"max_environments": 3, "max_containers_per_env": 5},
    "team": {"max_environments": 5, "max_containers_per_env": 20},
    "enterprise": {"max_environments": 999, "max_containers_per_env": 999},
    "admin": {"max_environments": 999, "max_containers_per_env": 999},
}


def _get_profile(user_id: str) -> dict | None:
    sb = get_supabase()
    result = sb.table("profiles").select("*").eq("id", user_id).maybe_single().execute()
    return result.data


# ── Dependency: resolve profile with role ────────────────────────────────────

async def get_current_profile(user: dict = Depends(get_current_user)) -> dict:
    """Return the full profile dict (id, email, role, plan, …).

    If no profile row exists yet the user just signed up and hasn't picked a
    plan — return a stub with role=None so the frontend can redirect.
    """
    profile = _get_profile(user["id"])
    if profile:
        profile["email"] = user["email"]
        return profile
    return {"id": user["id"], "email": user["email"], "role": None}


# ── Dependency: require a specific role (or admin) ───────────────────────────

def require_role(*allowed_roles: str):
    """FastAPI dependency factory.

    Usage:
        @router.post("/admin-only", dependencies=[Depends(require_role("admin"))])
    """
    async def _check(profile: dict = Depends(get_current_profile)):
        role = profile.get("role")
        if role == "admin":
            return profile
        if role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your plan does not include this feature",
            )
        return profile
    return _check


# ── Dependency: require *any* active plan ────────────────────────────────────

async def require_plan(profile: dict = Depends(get_current_profile)) -> dict:
    """Block users that haven't selected a plan yet."""
    role = profile.get("role")
    if not role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please select a plan to continue",
        )
    return profile


# ── Helpers for limit enforcement ────────────────────────────────────────────

def check_environment_limit(profile: dict):
    """Raise 403 if the user has hit their environment cap."""
    role = profile.get("role", "")
    if role == "admin":
        return
    limits = PLAN_LIMITS.get(role)
    if not limits:
        raise HTTPException(status_code=403, detail="No active plan")

    sb = get_supabase()
    result = (
        sb.table("environments")
        .select("id", count="exact")
        .eq("user_id", profile["id"])
        .neq("status", "terminated")
        .execute()
    )
    current = result.count or 0
    if current >= limits["max_environments"]:
        raise HTTPException(
            status_code=403,
            detail=f"Your {role} plan allows up to {limits['max_environments']} active environment(s). "
                   f"Delete an existing one or upgrade your plan.",
        )


def check_container_limit(profile: dict, env_id: str):
    """Raise 403 if the user has hit their per-environment container cap."""
    role = profile.get("role", "")
    if role == "admin":
        return
    limits = PLAN_LIMITS.get(role)
    if not limits:
        raise HTTPException(status_code=403, detail="No active plan")

    sb = get_supabase()
    result = (
        sb.table("containers")
        .select("id", count="exact")
        .eq("environment_id", env_id)
        .neq("status", "stopped")
        .execute()
    )
    current = result.count or 0
    if current >= limits["max_containers_per_env"]:
        raise HTTPException(
            status_code=403,
            detail=f"Your {role} plan allows up to {limits['max_containers_per_env']} container(s) per environment. "
                   f"Remove an existing one or upgrade your plan.",
        )
