import asyncio
import logging
from time import perf_counter
from uuid import uuid4

from langchain_core.messages import RemoveMessage
from langgraph.types import Command
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import ValidationError
from redis.exceptions import RedisError

from app.config import Settings
from app.graph import turn_input
from app.schemas import Job
from app.state import ExecutionContext
from app.store import InvalidJobState, Store

logger = logging.getLogger(__name__)


def _failure_code(error: APIError | TimeoutError | ValidationError) -> str:
    if isinstance(error, (APITimeoutError, TimeoutError)):
        return "TIMEOUT"
    if isinstance(error, (AuthenticationError, PermissionDeniedError)):
        return "PROVIDER_AUTH_ERROR"
    if isinstance(error, RateLimitError):
        return (
            "PROVIDER_QUOTA_EXCEEDED"
            if error.code == "insufficient_quota"
            else "PROVIDER_RATE_LIMIT"
        )
    if isinstance(error, APIConnectionError):
        return "PROVIDER_UNAVAILABLE"
    if isinstance(error, APIStatusError):
        return "PROVIDER_UNAVAILABLE" if error.status_code >= 500 else "PROVIDER_REQUEST_ERROR"
    if isinstance(error, ValidationError):
        return "INVALID_MODEL_OUTPUT"
    return "PROVIDER_ERROR"


class Workers:
    def __init__(self, store: Store, graph, settings: Settings):
        self.store = store
        self.graph = graph
        self.settings = settings
        self.tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self.tasks = [
            asyncio.create_task(self.loop()) for _ in range(self.settings.worker_concurrency)
        ]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def execute(self, job: Job) -> None:
        started = perf_counter()
        trace_id = uuid4()
        config = {
            "configurable": {"thread_id": f"{job.tenant_id}:{job.conversation_id}"},
            "recursion_limit": 40,
            "run_id": trace_id,
            "run_name": "saas-support",
            "tags": ["final-demo"],
            "metadata": {"job_id": str(job.id), "conversation_id": str(job.conversation_id)},
        }
        context = ExecutionContext(tenant_id=job.tenant_id, job_id=job.id)
        await self.store.mark_trace(job.id, trace_id)
        job = job.model_copy(update={"trace_id": trace_id})
        try:
            async with asyncio.timeout(self.settings.job_timeout_seconds):
                if job.resume:
                    payload = Command(resume=job.approved)
                else:
                    payload = turn_input(job.message, str(job.id))
                    previous = await self.graph.aget_state(config)
                    messages = previous.values.get("messages", [])
                    payload["messages"] = [
                        *[RemoveMessage(id=message.id) for message in messages[:-6]],
                        *payload["messages"],
                    ]
                state = await self.graph.ainvoke(payload, config=config, context=context)
            status = (
                "WAITING_APPROVAL"
                if state.get("__interrupt__")
                else "REJECTED"
                if state.get("rejected")
                else "DONE"
            )
            if not state.get("valid"):
                raise ValueError("Unvalidated graph output")
            job = job.model_copy(
                update={
                    "status": status,
                    "response": state["response"],
                    "sources": state["sources"],
                    "events": [
                        *state["events"],
                        {"agent": "workflow", "seconds": round(perf_counter() - started, 4)},
                    ],
                    "ticket_id": state.get("ticket_id"),
                }
            )
            await self.store.finish(job)
            logger.info("job=%s status=%s seconds=%.2f", job.id, status, perf_counter() - started)
        except asyncio.CancelledError:
            await asyncio.shield(self._fail(job, "WORKER_STOPPED", started))
            raise
        except (APIError, TimeoutError, ValidationError) as error:
            await self._fail(job, _failure_code(error), started)
        except Exception:
            await self._fail(job, "EXECUTION_ERROR", started)

    async def _fail(self, job: Job, code: str, started: float) -> None:
        seconds = round(perf_counter() - started, 4)
        event = {"agent": "workflow", "status": "error", "error": code, "seconds": seconds}
        await self.store.finish(
            job.model_copy(
                update={"status": "FAILED", "error": code, "events": [*job.events, event]}
            )
        )
        logger.warning("job=%s failure=%s seconds=%.2f", job.id, code, seconds)

    async def loop(self) -> None:
        while True:
            try:
                await self.store.recover()
                job = await self.store.claim(self.settings.job_timeout_seconds)
                if job:
                    await self.execute(job)
                else:
                    await asyncio.sleep(0.1)
            except (RedisError, InvalidJobState) as error:
                logger.warning("worker_failure=%s", type(error).__name__)
                await asyncio.sleep(1)
