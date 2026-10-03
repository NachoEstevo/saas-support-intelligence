from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.checkpoint.redis.jsonplus_redis import JsonPlusRedisSerializer

from app.schemas import CaseResult, Source, SupportCase, SupportResponse, TicketDraft
from app.state import Contribution


@asynccontextmanager
async def open_checkpointer(redis_url: str) -> AsyncIterator[AsyncRedisSaver]:
    contracts = (Source, SupportCase, CaseResult, TicketDraft, SupportResponse, Contribution)
    serializer = JsonPlusRedisSerializer(
        allowed_json_modules=[(*model.__module__.split("."), model.__name__) for model in contracts]
    )
    async with AsyncRedisSaver.from_conn_string(redis_url) as saver:
        saver.serde = serializer
        await saver.asetup()
        yield saver
