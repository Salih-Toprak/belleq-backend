import asyncio
import logging
from datetime import datetime, timezone

import httpx

from config import settings
from database import get_supabase

logger = logging.getLogger(__name__)


async def poll_until_ready(environment_id: str):
    interval = settings.INTERNAL_POLL_INTERVAL
    timeout = settings.INTERNAL_POLL_TIMEOUT
    elapsed = 0

    sb = get_supabase()
    result = sb.table("environments").select("public_ip, master_port").eq("id", environment_id).maybe_single().execute()
    if not result.data:
        logger.error("Poller: environment %s not found", environment_id)
        return

    public_ip = result.data["public_ip"]
    master_port = result.data["master_port"]

    if not public_ip:
        logger.error("Poller: environment %s has no public IP", environment_id)
        return

    url = f"http://{public_ip}:{master_port}/health"
    logger.info("Poller: starting for environment %s at %s", environment_id, url)

    while elapsed < timeout:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    logger.info("Poller: environment %s is ready", environment_id)
                    sb.table("environments").update({
                        "status": "ready",
                        "ready_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("id", environment_id).execute()
                    return
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, OSError):
            pass

        logger.info("Poller: environment %s not ready yet (%ds elapsed)", environment_id, elapsed)
        await asyncio.sleep(interval)
        elapsed += interval

    logger.error("Poller: environment %s timed out after %ds", environment_id, timeout)
    sb.table("environments").update({
        "status": "error",
        "error_message": "Timed out waiting for master container",
    }).eq("id", environment_id).execute()
