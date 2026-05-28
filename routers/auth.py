from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from database import get_supabase
from rbac import get_current_profile, PLAN_LIMITS

router = APIRouter()


class SelectPlanRequest(BaseModel):
    plan: str


@router.get("/me")
async def me(profile: dict = Depends(get_current_profile)):
    """Return the current user's profile, including role/plan info."""
    role = profile.get("role")
    limits = PLAN_LIMITS.get(role) if role else None
    return {
        **profile,
        "limits": limits,
    }


@router.post("/me/plan")
async def select_plan(body: SelectPlanRequest, user: dict = Depends(get_current_user)):
    """Let a user pick their initial plan. Only works if they don't have one yet,
    or if they're upgrading (Stripe not wired — just role assignment for now)."""
    allowed = {"free", "pro", "team", "enterprise"}
    if body.plan not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid plan. Choose from: {', '.join(sorted(allowed))}")

    sb = get_supabase()
    existing = sb.table("profiles").select("id, role").eq("id", user["id"]).maybe_single().execute()

    if existing.data and existing.data.get("role"):
        # Already has a plan — for now just allow changes (Stripe will gate later)
        sb.table("profiles").update({"role": body.plan}).eq("id", user["id"]).execute()
    elif existing.data:
        # Profile row exists but no role
        sb.table("profiles").update({"role": body.plan}).eq("id", user["id"]).execute()
    else:
        # No profile row yet — create one
        sb.table("profiles").insert({
            "id": user["id"],
            "role": body.plan,
        }).execute()

    sb.table("audit_logs").insert({
        "user_id": user["id"],
        "action": "plan.select",
        "detail": f"plan={body.plan}",
    }).execute()

    # Return the updated profile
    profile = sb.table("profiles").select("*").eq("id", user["id"]).maybe_single().execute()
    limits = PLAN_LIMITS.get(body.plan)
    return {**profile.data, "email": user["email"], "limits": limits}
