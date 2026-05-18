import logging

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

TIMEOUT = 30.0


def _base_url(env: dict) -> str:
    return f"http://{env['public_ip']}:{env['master_port']}"


def _headers(env: dict) -> dict:
    return {"X-Admin-Key": env["master_api_key"]}


async def _request(method: str, env: dict, path: str, **kwargs) -> httpx.Response:
    url = f"{_base_url(env)}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.request(method, url, headers=_headers(env), **kwargs)
            resp.raise_for_status()
            return resp
    except (httpx.ConnectError, httpx.ConnectTimeout):
        logger.error("Environment %s unreachable at %s", env["id"], env["public_ip"])
        raise HTTPException(status_code=503, detail="Environment unreachable")
    except httpx.HTTPStatusError as e:
        logger.error("Environment %s returned %s: %s", env["id"], e.response.status_code, e.response.text)
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


async def provision_container(env: dict, container_name: str, api_key: str, user_id: str) -> dict:
    logger.info("Provisioning container %s on environment %s", container_name, env["id"])
    resp = await _request("POST", env, "/master/containers/provision", json={
        "container_name": container_name,
        "api_key": api_key,
        "user_id": user_id,
    })
    return resp.json()


async def delete_container(env: dict, container_name: str) -> bool:
    logger.info("Deleting container %s on environment %s", container_name, env["id"])
    await _request("DELETE", env, f"/master/containers/{container_name}")
    return True


async def get_aggregate_stats(env: dict) -> dict:
    resp = await _request("GET", env, "/master/aggregate/stats")
    return resp.json()


async def get_aggregate_docs(env: dict) -> dict:
    resp = await _request("GET", env, "/master/aggregate/docs")
    return resp.json()


async def trigger_sync(env: dict, source_id: str | None = None) -> dict:
    body = {}
    if source_id:
        body["source_id"] = source_id
    resp = await _request("POST", env, "/master/ingestion/sync", json=body)
    return resp.json()


async def get_sources(env: dict) -> list:
    resp = await _request("GET", env, "/master/sources")
    return resp.json()


async def create_source(env: dict, source_data: dict) -> dict:
    resp = await _request("POST", env, "/master/sources", json=source_data)
    return resp.json()
