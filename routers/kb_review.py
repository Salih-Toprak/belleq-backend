"""Shared-KB review queue: human approval for agent ``scope="shared"`` writes.

Agents never write directly to the shared KB. A shared write is queued here; an
owner approves (upsert into the context KB) or rejects it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from routers.agent_common import owned_context_or_404
from services import agent_runner, agent_store

logger = logging.getLogger(__name__)
router = APIRouter(tags=["kb-review"])


def _owned_review_or_404(item_id: str, workspace_id: str) -> dict:
    item = agent_store.get_owned_review(item_id, workspace_id)
    if not item:
        raise HTTPException(status_code=404, detail="Review item not found")
    return item


@router.get("/contexts/{context_id}/kb-review")
async def list_review_queue(context_id: str, user: dict = Depends(get_current_user)):
    ws = user["id"]
    owned_context_or_404(context_id, ws)
    return agent_store.list_pending_reviews(context_id, ws)


@router.post("/kb-review/{item_id}/approve")
async def approve_review(item_id: str, user: dict = Depends(get_current_user)):
    ws = user["id"]
    item = _owned_review_or_404(item_id, ws)
    if item.get("status") != "pending":
        raise HTTPException(status_code=409, detail=f"Item already {item.get('status')}")

    # Upsert into the context KB via the container's KBWriter, then mark approved.
    try:
        await agent_runner.write_to_context_kb(
            item["context_id"], item.get("content", ""), item.get("tags", []) or []
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("kb_review_approve_write_failed item=%s", item_id)
        raise HTTPException(status_code=502, detail=f"KB write failed: {exc}")

    updated = agent_store.set_review_status(item_id, "approved")
    logger.info("kb_review_approved item=%s ctx=%s", item_id, item["context_id"])
    return updated or {"id": item_id, "status": "approved"}


@router.post("/kb-review/{item_id}/reject")
async def reject_review(item_id: str, user: dict = Depends(get_current_user)):
    ws = user["id"]
    item = _owned_review_or_404(item_id, ws)
    if item.get("status") != "pending":
        raise HTTPException(status_code=409, detail=f"Item already {item.get('status')}")
    updated = agent_store.set_review_status(item_id, "rejected")
    logger.info("kb_review_rejected item=%s", item_id)
    return updated or {"id": item_id, "status": "rejected"}
