from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_supabase

router = APIRouter()


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    sb = get_supabase()
    result = sb.table("profiles").select("*").eq("id", user["id"]).maybe_single().execute()
    profile = result.data
    if not profile:
        return {"id": user["id"], "email": user["email"]}
    return profile
