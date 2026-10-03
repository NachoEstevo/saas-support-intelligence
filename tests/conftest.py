import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.config import Settings
from app.persistence import open_checkpointer
from app.store import Store


@pytest.fixture
async def store():
    redis = Redis.from_url(
        os.getenv("TEST_REDIS_URL", "redis://127.0.0.1:16389/0"),
        decode_responses=True,
    )
    await redis.ping()
    instance = Store(redis, f"test-support-{uuid4()}")
    yield instance
    keys = [key async for key in redis.scan_iter(f"{instance.prefix}:*")]
    if keys:
        await redis.delete(*keys)
    await redis.aclose()


@pytest.fixture
def settings(store):
    return Settings(
        _env_file=None,
        openai_api_key="not-a-live-key",
        llm_model="test-model",
        embedding_model="test-embedding",
        customer_a_key="customer-a-test-key",
        customer_b_key="customer-b-test-key",
        approver_a_key="approver-a-test-key",
        approver_b_key="approver-b-test-key",
        redis_url=os.getenv("TEST_REDIS_URL", "redis://127.0.0.1:16389/0"),
        redis_prefix=store.prefix,
        langsmith_tracing=False,
    )


@pytest.fixture
async def saver(settings):
    async with open_checkpointer(settings.redis_url) as instance:
        yield instance
