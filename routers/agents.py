"""Agent CRUD + run history.

Agents are scoped to a context the requesting user owns. The BYOK provider key
is Fernet-encrypted before storage and is never returned by any serializer here
(see agent_store.public_agent / crypto.py).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from crypto import encrypt_secret
from routers.agent_common import (
    AGENT_STATUSES,
    KB_SCOPES,
    KEYED_PROVIDERS,
    PROVIDERS,
    owned_context_or_404,
    validate_connector_ids,
    validate_enum,
)
from services import agent_store

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agents"])


class CreateAgentBody(BaseModel):
    name: str
    role_description: str = ""
    kb_scope: str = "scoped"
    kb_section_ids: list[str] = Field(default_factory=list)
    connector_ids: list[str] = Field(default_factory=list)
    provider: str = "belleq"
    api_key: str = ""  # BYOK plaintext; encrypted at rest, never returned
    model: str = ""
    budget_limit_usd: float | None = None
    notify_enabled: bool = False  # message me via a communication connector
    notify_connector_ids: list[str] = Field(default_factory=list)


class UpdateAgentBody(BaseModel):
    name: str | None = None
    role_description: str | None = None
    kb_scope: str | None = None
    kb_section_ids: list[str] | None = None
    connector_ids: list[str] | None = None
    provider: str | None = None
    api_key: str | None = None  # set to rotate the BYOK key; "" clears it
    model: str | None = None
    budget_limit_usd: float | None = None
    status: str | None = None
    notify_enabled: bool | None = None
    notify_connector_ids: list[str] | None = None


@router.post("/contexts/{context_id}/agents", status_code=201)
async def create_agent(
    context_id: str,
    body: CreateAgentBody,
    user: dict = Depends(get_current_user),
):
    ws = user["id"]
    owned_context_or_404(context_id, ws)
    validate_enum(body.kb_scope, KB_SCOPES, "kb_scope")
    validate_enum(body.provider, PROVIDERS, "provider")
    validate_connector_ids(ws, body.connector_ids)
    validate_connector_ids(ws, body.notify_connector_ids)
    if body.provider in KEYED_PROVIDERS and not body.api_key.strip():
        raise HTTPException(status_code=422, detail=f"{body.provider} provider requires an api_key")

    row = {
        "context_id": context_id,
        "workspace_id": ws,
        "name": body.name.strip(),
        "role_description": body.role_description,
        "kb_scope": body.kb_scope,
        "kb_section_ids": body.kb_section_ids,
        "connector_ids": body.connector_ids,
        "provider": body.provider,
        "api_key_encrypted": encrypt_secret(body.api_key) if body.provider in KEYED_PROVIDERS else None,
        "model": body.model,
        "budget_limit_usd": body.budget_limit_usd,
        "notify_enabled": body.notify_enabled,
        "notify_connector_ids": body.notify_connector_ids,
        "status": "active",
    }
    created = agent_store.create_agent(row)
    logger.info("agent_created id=%s ctx=%s ws=%s", created["id"], context_id, ws)
    return agent_store.public_agent(created)


@router.get("/contexts/{context_id}/agents")
async def list_agents(context_id: str, user: dict = Depends(get_current_user)):
    ws = user["id"]
    owned_context_or_404(context_id, ws)
    return [agent_store.public_agent(a) for a in agent_store.list_agents(context_id, ws)]


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, user: dict = Depends(get_current_user)):
    agent = agent_store.get_owned_agent(agent_id, user["id"])
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent_store.public_agent(agent)


@router.patch("/agents/{agent_id}")
async def update_agent(
    agent_id: str,
    body: UpdateAgentBody,
    user: dict = Depends(get_current_user),
):
    ws = user["id"]
    agent = agent_store.get_owned_agent(agent_id, ws)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    patch: dict = {}
    if body.name is not None:
        patch["name"] = body.name.strip()
    if body.role_description is not None:
        patch["role_description"] = body.role_description
    if body.kb_scope is not None:
        validate_enum(body.kb_scope, KB_SCOPES, "kb_scope")
        patch["kb_scope"] = body.kb_scope
    if body.kb_section_ids is not None:
        patch["kb_section_ids"] = body.kb_section_ids
    if body.connector_ids is not None:
        validate_connector_ids(ws, body.connector_ids)
        patch["connector_ids"] = body.connector_ids
    if body.provider is not None:
        validate_enum(body.provider, PROVIDERS, "provider")
        patch["provider"] = body.provider
    if body.model is not None:
        patch["model"] = body.model
    if body.budget_limit_usd is not None:
        # <= 0 means "no daily limit" (the UI sends 0 to clear an existing budget,
        # since a JSON null can't be told apart from an omitted field here).
        patch["budget_limit_usd"] = None if body.budget_limit_usd <= 0 else body.budget_limit_usd
    if body.status is not None:
        validate_enum(body.status, AGENT_STATUSES, "status")
        patch["status"] = body.status
    if body.notify_enabled is not None:
        patch["notify_enabled"] = body.notify_enabled
    if body.notify_connector_ids is not None:
        validate_connector_ids(ws, body.notify_connector_ids)
        patch["notify_connector_ids"] = body.notify_connector_ids
    # Key rotation: a provided api_key is (re)encrypted; "" clears it.
    if body.api_key is not None:
        patch["api_key_encrypted"] = encrypt_secret(body.api_key) if body.api_key.strip() else None

    # Guard: a keyed-provider agent (byok / openrouter) must end up with a key
    # (either an existing one or a new one in this patch). Prevents switching
    # belleq -> byok/openrouter with no key, which would fail at run time when the
    # executor tries to decrypt nothing.
    final_provider = patch.get("provider", agent.get("provider"))
    if final_provider in KEYED_PROVIDERS:
        has_key = bool((agent.get("api_key_encrypted") or "").strip())
        setting_key = "api_key_encrypted" in patch and bool(patch["api_key_encrypted"])
        clearing_key = "api_key_encrypted" in patch and not patch["api_key_encrypted"]
        if (not has_key and not setting_key) or clearing_key:
            raise HTTPException(status_code=422, detail=f"{final_provider} provider requires an api_key")

    updated = agent_store.update_agent(agent_id, patch)
    return agent_store.public_agent(updated or agent)


@router.delete("/agents/{agent_id}", status_code=200)
async def delete_agent(agent_id: str, user: dict = Depends(get_current_user)):
    """Soft delete: status -> archived. Also tears down any cron tasks."""
    agent = agent_store.get_owned_agent(agent_id, user["id"])
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent_store.update_agent(agent_id, {"status": "archived"})

    # Stop scheduling this agent's cron tasks.
    from services import agent_scheduler

    for t in agent_store.list_tasks(agent_id, user["id"]):
        if agent_scheduler.is_cron(t.get("trigger", "")):
            agent_scheduler.unregister_task(t["id"])
    logger.info("agent_archived id=%s ws=%s", agent_id, user["id"])
    return {"id": agent_id, "status": "archived"}


@router.get("/agents/{agent_id}/runs")
async def get_agent_runs(agent_id: str, user: dict = Depends(get_current_user)):
    agent = agent_store.get_owned_agent(agent_id, user["id"])
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent_store.list_runs_for_agent(agent_id)
