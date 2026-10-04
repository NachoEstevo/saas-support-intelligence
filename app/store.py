import json
import time
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from redis.asyncio import Redis

from app.schemas import Conversation, ConversationSummary, Job, SupportCase, Ticket, TicketDraft

SUBMIT = """
if redis.call('HGET', KEYS[1], 'tenant') ~= ARGV[1] then return -1 end
if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
redis.call('SET', KEYS[2], ARGV[2])
redis.call('HSET', KEYS[3], 'payload', ARGV[3], 'status', 'PENDING',
           'tenant', ARGV[1], 'conversation', ARGV[4])
redis.call('RPUSH', KEYS[4], ARGV[2])
redis.call('ZADD', KEYS[5], ARGV[5], ARGV[2])
redis.call('ZADD', KEYS[6], ARGV[5], ARGV[4])
redis.call('HSETNX', KEYS[1], 'title', ARGV[6])
redis.call('HSET', KEYS[1], 'updated_at', ARGV[5], 'last_job', ARGV[2])
return 1
"""
CLAIM = """
local id = redis.call('LPOP', KEYS[1])
if not id then return nil end
local key = ARGV[1] .. ':job:' .. id
if redis.call('HGET', key, 'status') ~= 'PENDING' then return nil end
redis.call('HSET', key, 'status', 'RUNNING')
redis.call('HDEL', key, 'draft_answer')
redis.call('ZADD', KEYS[2], ARGV[2], id)
return id
"""
FINISH = """
if redis.call('HGET', KEYS[1], 'status') ~= 'RUNNING' then return 0 end
redis.call('HSET', KEYS[1], 'payload', ARGV[1], 'status', ARGV[2])
redis.call('HDEL', KEYS[1], 'draft_answer')
if ARGV[2] == 'FAILED' and redis.call('HEXISTS', KEYS[1], 'ticket_id') == 1 then
 redis.call('HSET', KEYS[1], 'error', 'TICKET_CREATED_EXECUTION_INCOMPLETE')
end
redis.call('ZREM', KEYS[2], ARGV[3])
if ARGV[2] == 'WAITING_APPROVAL' then
 redis.call('ZADD', KEYS[4], ARGV[4], ARGV[3])
else
 redis.call('ZREM', KEYS[4], ARGV[3])
end
if ARGV[2] ~= 'WAITING_APPROVAL' and redis.call('GET', KEYS[3]) == ARGV[3] then
 redis.call('DEL', KEYS[3])
end
return 1
"""
APPROVE = """
if redis.call('HGET', KEYS[1], 'tenant') ~= ARGV[1] then return -1 end
if redis.call('HGET', KEYS[1], 'status') ~= 'WAITING_APPROVAL' then return 0 end
redis.call('HSET', KEYS[1], 'status', 'PENDING', 'resume', 'true', 'approved', ARGV[2])
redis.call('RPUSH', KEYS[2], ARGV[3])
redis.call('ZREM', KEYS[3], ARGV[3])
return 1
"""
RECOVER = """
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
for _,id in ipairs(ids) do
 local key = ARGV[2] .. ':job:' .. id
 if redis.call('HGET', key, 'status') == 'RUNNING' then
  redis.call('HSET', key, 'status', 'FAILED', 'error', 'WORKER_LOST')
  redis.call('HDEL', key, 'draft_answer')
  if redis.call('HEXISTS', key, 'ticket_id') == 1 then
   redis.call('HSET', key, 'error', 'TICKET_CREATED_EXECUTION_INCOMPLETE')
  end
  local conversation = redis.call('HGET', key, 'conversation')
  local lock = ARGV[2] .. ':busy:' .. conversation
  if redis.call('GET', lock) == id then redis.call('DEL', lock) end
 end
 redis.call('ZREM', KEYS[1], id)
end
return #ids
"""
CREATE_TICKET = """
if redis.call('HGET', KEYS[1], 'tenant') ~= ARGV[1] or
   redis.call('HGET', KEYS[1], 'status') ~= 'RUNNING' or
   redis.call('HGET', KEYS[1], 'approved') ~= 'true' then return 0 end
redis.call('SET', KEYS[2], ARGV[2], 'NX')
redis.call('HSET', KEYS[1], 'ticket_id', ARGV[3])
return 1
"""


class NotFound(LookupError):
    pass


class ConversationBusy(RuntimeError):
    pass


class InvalidJobState(RuntimeError):
    pass


class Store:
    def __init__(self, redis: Redis, prefix: str):
        self.redis = redis
        self.prefix = prefix

    def key(self, kind: str, identity: str | UUID) -> str:
        return f"{self.prefix}:{kind}:{identity}"

    async def create_conversation(self, tenant_id: str) -> Conversation:
        conversation = Conversation(id=uuid4(), tenant_id=tenant_id)
        created_at = time.time()
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(
                self.key("conversation", conversation.id),
                mapping={
                    "tenant": tenant_id,
                    "payload": conversation.model_dump_json(),
                    "updated_at": str(created_at),
                },
            )
            pipe.zadd(self.key("conversations", tenant_id), {str(conversation.id): created_at})
            await pipe.execute()
        return conversation

    async def list_conversations(self, tenant_id: str) -> list[ConversationSummary]:
        ids = await self.redis.zrevrange(self.key("conversations", tenant_id), 0, 49)
        async with self.redis.pipeline(transaction=False) as pipe:
            for identity in ids:
                pipe.hgetall(self.key("conversation", identity))
            rows = await pipe.execute()
        owned = [row for row in rows if row and row["tenant"] == tenant_id]
        async with self.redis.pipeline(transaction=False) as pipe:
            for row in owned:
                pipe.hget(self.key("job", row.get("last_job", "")), "status")
            statuses = await pipe.execute()
        return [
            ConversationSummary(
                **Conversation.model_validate_json(row["payload"]).model_dump(),
                title=row.get("title", "Nueva conversación"),
                updated_at=float(row["updated_at"]),
                last_job_status=status,
            )
            for row, status in zip(owned, statuses, strict=True)
        ]

    async def list_conversation_jobs(self, identity: UUID, tenant_id: str) -> list[Job]:
        await self.get_conversation(identity, tenant_id)
        ids = await self.redis.zrevrange(self.key("conversation-jobs", identity), 0, 99)
        return [await self.get_job(UUID(job_id), tenant_id) for job_id in reversed(ids)]

    async def list_approvals(self, tenant_id: str) -> list[Job]:
        ids = await self.redis.zrevrange(self.key("approvals", tenant_id), 0, 49)
        jobs = [await self.get_job(UUID(job_id), tenant_id) for job_id in ids]
        return [job for job in jobs if job.status == "WAITING_APPROVAL"]

    async def get_conversation(self, identity: UUID, tenant_id: str) -> Conversation:
        data = await self.redis.hgetall(self.key("conversation", identity))
        if not data or data["tenant"] != tenant_id:
            raise NotFound("Conversation not found")
        return Conversation.model_validate_json(data["payload"])

    async def submit(self, identity: UUID, tenant_id: str, message: str) -> Job:
        job = Job(id=uuid4(), conversation_id=identity, tenant_id=tenant_id, message=message)
        result = await self.redis.eval(
            SUBMIT,
            6,
            self.key("conversation", identity),
            self.key("busy", identity),
            self.key("job", job.id),
            self.key("queue", "pending"),
            self.key("conversation-jobs", identity),
            self.key("conversations", tenant_id),
            tenant_id,
            str(job.id),
            job.model_dump_json(),
            str(identity),
            time.time(),
            " ".join(message.split())[:80],
        )
        if result == -1:
            raise NotFound("Conversation not found")
        if result == 0:
            raise ConversationBusy("Conversation already has an active job")
        return job

    async def get_job(self, identity: UUID, tenant_id: str | None = None) -> Job:
        data = await self.redis.hgetall(self.key("job", identity))
        if not data or (tenant_id is not None and data["tenant"] != tenant_id):
            raise NotFound("Job not found")
        job = Job.model_validate_json(data["payload"])
        updates = {
            key: data[key] for key in ("status", "error", "trace_id", "ticket_id") if key in data
        }
        for key in ("resume", "approved"):
            if key in data:
                updates[key] = json.loads(data[key])
        updates["draft_answer"] = (
            data.get("draft_answer", "") if data["status"] == "RUNNING" else ""
        )
        return Job.model_validate(job.model_dump() | updates)

    async def claim(self, timeout: int) -> Job | None:
        identity = await self.redis.eval(
            CLAIM,
            2,
            self.key("queue", "pending"),
            self.key("active", "jobs"),
            self.prefix,
            time.time() + timeout + 15,
        )
        return await self.get_job(UUID(identity)) if identity else None

    async def finish(self, job: Job) -> None:
        transitioned = await self.redis.eval(
            FINISH,
            4,
            self.key("job", job.id),
            self.key("active", "jobs"),
            self.key("busy", job.conversation_id),
            self.key("approvals", job.tenant_id),
            job.model_dump_json(),
            job.status,
            str(job.id),
            time.time(),
        )
        if not transitioned:
            raise InvalidJobState("Job is no longer running")

    async def mark_trace(self, identity: UUID, trace_id: UUID) -> None:
        await self.redis.hset(self.key("job", identity), "trace_id", str(trace_id))

    async def update_draft(self, identity: UUID, answer: str) -> None:
        await self.redis.eval(
            "if redis.call('HGET', KEYS[1], 'status') == 'RUNNING' then "
            "return redis.call('HSET', KEYS[1], 'draft_answer', ARGV[1]) end return 0",
            1,
            self.key("job", identity),
            answer,
        )

    async def approve(self, identity: UUID, tenant_id: str, approved: bool) -> Job:
        transitioned = await self.redis.eval(
            APPROVE,
            3,
            self.key("job", identity),
            self.key("queue", "pending"),
            self.key("approvals", tenant_id),
            tenant_id,
            json.dumps(approved),
            str(identity),
        )
        if transitioned == -1:
            raise NotFound("Job not found")
        if not transitioned:
            raise InvalidJobState("Job is not awaiting approval")
        return await self.get_job(identity, tenant_id)

    async def recover(self) -> int:
        return await self.redis.eval(
            RECOVER,
            1,
            self.key("active", "jobs"),
            time.time(),
            self.prefix,
        )

    async def seed_cases(self, cases: list[SupportCase]) -> None:
        for case in cases:
            await self.redis.set(
                self.key("case", f"{case.tenant_id}:{case.case_id}"),
                case.model_dump_json(),
                nx=True,
            )

    async def get_case(self, tenant_id: str, case_id: str) -> SupportCase | None:
        raw = await self.redis.get(self.key("case", f"{tenant_id}:{case_id}"))
        return SupportCase.model_validate_json(raw) if raw else None

    async def create_ticket(self, job_id: UUID, tenant_id: str, draft: TicketDraft) -> Ticket:
        if draft.case_id and await self.get_case(tenant_id, draft.case_id) is None:
            raise NotFound("Case not found")
        identity = uuid5(NAMESPACE_URL, f"{self.prefix}/{tenant_id}/{job_id}")
        ticket = Ticket(id=identity, tenant_id=tenant_id, **draft.model_dump())
        created = await self.redis.eval(
            CREATE_TICKET,
            2,
            self.key("job", job_id),
            self.key("ticket", identity),
            tenant_id,
            ticket.model_dump_json(),
            str(identity),
        )
        if not created:
            raise InvalidJobState("Ticket requires an approved running job")
        stored = await self.get_ticket(identity, tenant_id)
        if stored != ticket:
            raise InvalidJobState("A different ticket already exists for this job")
        return stored

    async def get_ticket(self, identity: UUID, tenant_id: str) -> Ticket:
        raw = await self.redis.get(self.key("ticket", identity))
        if not raw:
            raise NotFound("Ticket not found")
        ticket = Ticket.model_validate_json(raw)
        if ticket.tenant_id != tenant_id:
            raise NotFound("Ticket not found")
        return ticket
