import asyncio
from collections.abc import AsyncIterator
from time import monotonic
from uuid import UUID

from fastapi import Request
from redis.exceptions import RedisError

from app.store import Store


async def job_events(
    store: Store, job_id: UUID, tenant_id: str, request: Request, timeout: int
) -> AsyncIterator[str]:
    deadline = monotonic() + timeout
    heartbeat = monotonic()
    previous = ""
    while monotonic() < deadline and not await request.is_disconnected():
        try:
            job = await store.get_job(job_id, tenant_id)
        except RedisError:
            yield 'event: error\ndata: {"message":"Storage unavailable"}\n\n'
            return
        payload = job.model_dump_json()
        if payload != previous:
            yield f"event: job\ndata: {payload}\n\n"
            previous = payload
        if job.status not in ("PENDING", "RUNNING"):
            return
        if monotonic() - heartbeat >= 10:
            yield ": heartbeat\n\n"
            heartbeat = monotonic()
        await asyncio.sleep(0.15)
