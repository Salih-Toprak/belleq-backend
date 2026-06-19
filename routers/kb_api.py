"""Public Belleq KB REST API — the universal, non-MCP integration path.

MCP isn't universal: ChatGPT (web) won't accept an SSE MCP URL, Gemini has no
custom-tool connector, and some clients expose tools inconsistently. This router
gives every provider a plain JSON REST surface for the same four operations the
MCP tools expose, authenticated by the per-context **API key** (the value shown
on the dashboard) as a bearer token:

    Authorization: Bearer <context_api_key>

    POST /v1/recall   {"limit": 10}                     -> recent saved facts
    POST /v1/query    {"query": "..."}                  -> KB search results
    POST /v1/record   {"user_message","assistant_message","conversation_id"}
    POST /v1/flush    {}                                 -> ingest buffered now

The bearer token both authenticates AND selects the context (one key = one
context), so no id is needed in the path — ideal for ChatGPT Actions / Gemini
function calling. ``GET /v1/openapi.json`` serves an import-ready Action spec.

The token is validated against Supabase ``containers.api_key`` here, so rotating
a key (see contexts.regenerate) takes effect instantly and never needs the
container restarted — the container is reached over the trusted private network.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Body, Header, HTTPException, Request
from pydantic import BaseModel, Field

from database import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["kb-rest-api"])

TIMEOUT = 30.0


class RecallBody(BaseModel):
    limit: int = Field(default=10, ge=1, le=100)


class QueryBody(BaseModel):
    query: str = Field(min_length=1)


class RecordBody(BaseModel):
    user_message: str
    assistant_message: str
    conversation_id: str = ""


def _context_for_key(api_key: str) -> dict:
    """Resolve a bearer api_key to its (active) context row, or 401/409."""
    key = (api_key or "").strip()
    if not key:
        raise HTTPException(status_code=401, detail="Missing API key")
    sb = get_supabase()
    ctx = (
        sb.table("containers")
        .select("id, container_name, host_id, status, api_key")
        .eq("api_key", key)
        .neq("status", "stopped")
        .maybe_single()
        .execute()
    ).data
    if not ctx:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if not ctx.get("host_id"):
        raise HTTPException(status_code=409, detail="Context is still provisioning")
    return ctx


def _host_for(ctx: dict) -> dict:
    sb = get_supabase()
    host = (
        sb.table("hosts").select("*").eq("id", ctx["host_id"]).maybe_single().execute()
    ).data
    if not host or not host.get("master_endpoint"):
        raise HTTPException(status_code=502, detail="Context host is unavailable")
    return host


async def _call_kb(api_key: str, op: str, payload: dict) -> dict:
    ctx = _context_for_key(api_key)
    host = _host_for(ctx)
    target = f"{host['master_endpoint']}/master/kb/{ctx['container_name']}/{op}"
    headers = {"X-Admin-Key": host["master_api_key"], "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(target, json=payload, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        logger.error("kb_api_master_unreachable ctx=%s op=%s", ctx["id"], op)
        raise HTTPException(status_code=502, detail="Memory service is unreachable")
    except httpx.HTTPError as exc:
        logger.exception("kb_api_failed ctx=%s op=%s", ctx["id"], op)
        raise HTTPException(status_code=502, detail=f"Memory service error: {exc}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:300])
    return r.json()


def _bearer(authorization: str) -> str:
    """Extract the token from an `Authorization: Bearer ...` header."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (authorization or "").strip()


@router.post("/recall", summary="Load recent memory")
async def recall(
    body: RecallBody = Body(default_factory=RecallBody),
    authorization: str = Header(default=""),
):
    """Load what Belleq remembers about the user and their recent work."""
    return await _call_kb(_bearer(authorization), "recall", {"limit": body.limit})


@router.post("/query", summary="Search memory")
async def query(
    body: QueryBody,
    authorization: str = Header(default=""),
):
    """Search the knowledge base for anything relevant to a topic."""
    return await _call_kb(_bearer(authorization), "query", {"query": body.query})


@router.post("/record", summary="Save an exchange")
async def record(
    body: RecordBody,
    authorization: str = Header(default=""),
):
    """Save a user/assistant exchange verbatim so it persists across chats."""
    return await _call_kb(
        _bearer(authorization),
        "record",
        {
            "user_message": body.user_message,
            "assistant_message": body.assistant_message,
            "conversation_id": body.conversation_id,
        },
    )


@router.post("/flush", summary="Index buffered memory now")
async def flush(authorization: str = Header(default="")):
    """Ingest buffered exchanges into the knowledge base immediately."""
    return await _call_kb(_bearer(authorization), "flush", {})


@router.get("/openapi.json", summary="OpenAPI spec for ChatGPT Actions / function calling")
async def openapi_spec(request: Request):
    """Self-describing spec a user can import into ChatGPT Actions, or hand to any
    function-calling client (Gemini, etc.). Server URL is derived from the request
    so it works on any deployment host."""
    # Behind a TLS-terminating proxy/ALB the app sees plain http, but the public
    # API is served over https — and ChatGPT Actions rejects non-https servers.
    # Honour X-Forwarded-Proto, and upgrade http->https for non-local hosts.
    host = request.headers.get("host") or request.url.hostname or ""
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if not proto:
        proto = request.url.scheme
    if proto == "http" and host and not host.startswith(("localhost", "127.0.0.1")):
        proto = "https"
    base = f"{proto}://{host}".rstrip("/")
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Belleq Memory",
            "version": "1.0.0",
            "description": (
                "Belleq is your persistent AI memory. Call recall at the start of a "
                "conversation to load prior context, query to look up specific facts, "
                "record after each exchange to save it verbatim, and flush to index "
                "saved exchanges immediately. Authenticate with your context API key "
                "as a bearer token."
            ),
        },
        "servers": [{"url": base}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"}
            }
        },
        "security": [{"bearerAuth": []}],
        "paths": {
            "/v1/recall": {
                "post": {
                    "operationId": "recallMemory",
                    "summary": "Load recent memory at the start of a conversation",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "limit": {"type": "integer", "default": 10}
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "Recent saved facts"}},
                }
            },
            "/v1/query": {
                "post": {
                    "operationId": "queryMemory",
                    "summary": "Search memory for a topic",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["query"],
                                    "properties": {"query": {"type": "string"}},
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Matching knowledge chunks"}},
                }
            },
            "/v1/record": {
                "post": {
                    "operationId": "recordExchange",
                    "summary": "Save a user/assistant exchange verbatim",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["user_message", "assistant_message"],
                                    "properties": {
                                        "user_message": {"type": "string"},
                                        "assistant_message": {"type": "string"},
                                        "conversation_id": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Record acknowledgement"}},
                }
            },
            "/v1/flush": {
                "post": {
                    "operationId": "flushMemory",
                    "summary": "Index buffered exchanges immediately",
                    "responses": {"200": {"description": "Ingestion counts"}},
                }
            },
        },
    }
