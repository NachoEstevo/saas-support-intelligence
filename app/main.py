import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import BASE_DIR, Settings
from app.graph import build_graph
from app.persistence import open_checkpointer
from app.rag import HybridRetriever
from app.schemas import (
    AcceptedJob,
    ApprovalRequest,
    Conversation,
    Health,
    Job,
    MessageRequest,
    Principal,
    Ticket,
)
from app.store import ConversationBusy, InvalidJobState, NotFound, Store
from app.support import SupportService
from app.worker import Workers


def create_app(
    settings: Settings | None = None, model=None, retriever=None, *, start_workers: bool = True
) -> FastAPI:
    settings = settings or Settings()
    settings.configure_tracing()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            redis = Redis.from_url(settings.redis_url, decode_responses=True)
            stack.push_async_callback(redis.aclose)
            await redis.ping()
            store = Store(redis, settings.redis_prefix)
            saver = await stack.enter_async_context(open_checkpointer(settings.redis_url))
            knowledge = retriever or await HybridRetriever.connect(settings)
            stack.push_async_callback(knowledge.close)
            await knowledge.ready()
            support = SupportService(store)
            await support.seed(BASE_DIR / "data" / "cases.json")
            graph = build_graph(model or settings.create_model(), knowledge, support, store, saver)
            workers = Workers(store, graph, settings)
            app.state.store = store
            app.state.graph = graph
            app.state.retriever = knowledge
            app.state.workers = workers
            if start_workers:
                workers.start()
            try:
                yield
            finally:
                await workers.stop()

    app = FastAPI(title="SaaS Support Intelligence", version="0.1.0", lifespan=lifespan)

    async def authenticate(x_api_key: str = Header(default="")) -> Principal:
        credentials = (
            (settings.customer_a_key, "demo-a", "customer"),
            (settings.customer_b_key, "demo-b", "customer"),
            (settings.approver_a_key, "demo-a", "approver"),
            (settings.approver_b_key, "demo-b", "approver"),
        )
        for key, tenant_id, role in credentials:
            if secrets.compare_digest(x_api_key, key.get_secret_value()):
                return Principal(tenant_id=tenant_id, role=role)
        raise HTTPException(401, "Invalid API credential")

    Identity = Annotated[Principal, Depends(authenticate)]

    @app.exception_handler(NotFound)
    async def not_found(request: Request, error: NotFound):
        return JSONResponse(status_code=404, content={"detail": "Resource not found"})

    @app.exception_handler(ConversationBusy)
    @app.exception_handler(InvalidJobState)
    async def conflict(request: Request, error: RuntimeError):
        return JSONResponse(
            status_code=409, content={"detail": "Operation conflicts with current state"}
        )

    @app.exception_handler(RedisError)
    async def unavailable(request: Request, error: RedisError):
        return JSONResponse(status_code=503, content={"detail": "Storage unavailable"})

    @app.get("/health", response_model=Health)
    async def health() -> Health:
        await app.state.store.redis.ping()
        try:
            await app.state.retriever.ready()
        except Exception as error:
            raise HTTPException(503, "Knowledge index unavailable") from error
        return Health()

    @app.post("/conversations", status_code=201, response_model=Conversation)
    async def create_conversation(identity: Identity) -> Conversation:
        if identity.role != "customer":
            raise HTTPException(403, "Customer credential required")
        return await app.state.store.create_conversation(identity.tenant_id)

    @app.post(
        "/conversations/{conversation_id}/messages", status_code=202, response_model=AcceptedJob
    )
    async def submit(
        conversation_id: UUID, request: MessageRequest, identity: Identity
    ) -> AcceptedJob:
        if identity.role != "customer":
            raise HTTPException(403, "Customer credential required")
        job = await app.state.store.submit(conversation_id, identity.tenant_id, request.message)
        return AcceptedJob(id=job.id, conversation_id=job.conversation_id, status=job.status)

    @app.get("/jobs/{job_id}", response_model=Job)
    async def get_job(job_id: UUID, identity: Identity) -> Job:
        return await app.state.store.get_job(job_id, identity.tenant_id)

    @app.post("/jobs/{job_id}/approve", status_code=202, response_model=AcceptedJob)
    async def approve(job_id: UUID, request: ApprovalRequest, identity: Identity) -> AcceptedJob:
        if identity.role != "approver":
            raise HTTPException(403, "Approver credential required")
        job = await app.state.store.approve(job_id, identity.tenant_id, request.approved)
        return AcceptedJob(id=job.id, conversation_id=job.conversation_id, status=job.status)

    @app.get("/tickets/{ticket_id}", response_model=Ticket)
    async def get_ticket(ticket_id: UUID, identity: Identity) -> Ticket:
        return await app.state.store.get_ticket(ticket_id, identity.tenant_id)

    return app
