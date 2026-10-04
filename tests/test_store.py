import asyncio
from itertools import count
from types import SimpleNamespace

import pytest

from app.schemas import TicketDraft
from app.store import ConversationBusy, InvalidJobState, NotFound

pytestmark = pytest.mark.integration


async def test_tenant_cannot_read_or_submit_to_other_conversation(store):
    conversation = await store.create_conversation("demo-a")
    with pytest.raises(NotFound):
        await store.get_conversation(conversation.id, "demo-b")
    with pytest.raises(NotFound):
        await store.submit(conversation.id, "demo-b", "Hola")


async def test_only_one_job_can_own_a_conversation(store):
    conversation = await store.create_conversation("demo-a")
    results = await asyncio.gather(
        *(store.submit(conversation.id, "demo-a", "Hola") for _ in range(3)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ConversationBusy) for result in results) == 2
    job = await store.claim(30)
    assert job.status == "RUNNING"
    assert await store.claim(30) is None
    await store.finish(job.model_copy(update={"status": "DONE"}))
    assert (await store.submit(conversation.id, "demo-a", "Siguiente")).id != job.id


async def test_approval_is_atomic_and_scoped(store):
    conversation = await store.create_conversation("demo-a")
    await store.submit(conversation.id, "demo-a", "Crear ticket")
    job = await store.claim(30)
    await store.finish(job.model_copy(update={"status": "WAITING_APPROVAL"}))
    with pytest.raises(NotFound):
        await store.approve(job.id, "demo-b", True)
    results = await asyncio.gather(
        store.approve(job.id, "demo-a", True),
        store.approve(job.id, "demo-a", True),
        return_exceptions=True,
    )
    assert sum(isinstance(result, InvalidJobState) for result in results) == 1
    resumed = await store.claim(30)
    assert resumed.resume and resumed.approved
    assert await store.claim(30) is None


async def test_ticket_requires_approval_and_is_idempotent(store):
    conversation = await store.create_conversation("demo-a")
    await store.submit(conversation.id, "demo-a", "Crear ticket")
    job = await store.claim(30)
    draft = TicketDraft(subject="Error de carga", description="La carga falla en la demo.")
    with pytest.raises(InvalidJobState):
        await store.create_ticket(job.id, "demo-a", draft)
    await store.finish(job.model_copy(update={"status": "WAITING_APPROVAL"}))
    await store.approve(job.id, "demo-a", True)
    await store.claim(30)
    first = await store.create_ticket(job.id, "demo-a", draft)
    second = await store.create_ticket(job.id, "demo-a", draft)
    assert first.id == second.id
    with pytest.raises(NotFound):
        await store.get_ticket(first.id, "demo-b")


async def test_expired_worker_releases_conversation(store):
    conversation = await store.create_conversation("demo-a")
    job = await store.submit(conversation.id, "demo-a", "Consulta")
    await store.claim(30)
    await store.redis.zadd(store.key("active", "jobs"), {str(job.id): 0})
    assert await store.recover() == 1
    assert (await store.get_job(job.id, "demo-a")).error == "WORKER_LOST"
    await store.submit(conversation.id, "demo-a", "Reintentar")


async def test_rejected_job_unlocks_without_creating_ticket(store):
    conversation = await store.create_conversation("demo-a")
    await store.submit(conversation.id, "demo-a", "Crear ticket")
    job = await store.claim(30)
    await store.finish(job.model_copy(update={"status": "WAITING_APPROVAL"}))
    await store.approve(job.id, "demo-a", False)
    resumed = await store.claim(30)
    assert resumed.approved is False
    await store.finish(resumed.model_copy(update={"status": "REJECTED"}))
    await store.submit(conversation.id, "demo-a", "Otra consulta")


@pytest.mark.parametrize("failure", ["finish", "recover"])
async def test_ticket_is_visible_even_if_execution_fails_after_creation(store, failure):
    conversation = await store.create_conversation("demo-a")
    await store.submit(conversation.id, "demo-a", "Crear ticket")
    job = await store.claim(30)
    await store.finish(job.model_copy(update={"status": "WAITING_APPROVAL"}))
    await store.approve(job.id, "demo-a", True)
    resumed = await store.claim(30)
    ticket = await store.create_ticket(
        job.id, "demo-a", TicketDraft(subject="Error de carga", description="La carga falla.")
    )
    if failure == "finish":
        await store.finish(
            resumed.model_copy(update={"status": "FAILED", "error": "EXECUTION_ERROR"})
        )
    else:
        await store.redis.zadd(store.key("active", "jobs"), {str(job.id): 0})
        await store.recover()
    failed = await store.get_job(job.id, "demo-a")
    assert failed.status == "FAILED"
    assert failed.error == "TICKET_CREATED_EXECUTION_INCOMPLETE"
    assert failed.ticket_id == ticket.id
    assert (await store.get_ticket(failed.ticket_id, "demo-a")).id == ticket.id


async def test_history_keeps_order_and_lists_are_bounded(store, monkeypatch):
    clock = count(start=1_800_000_000)
    monkeypatch.setattr("app.store.time", SimpleNamespace(time=lambda: float(next(clock))))
    conversations = [await store.create_conversation("demo-a") for _ in range(52)]
    listed = await store.list_conversations("demo-a")
    assert len(listed) == 50
    assert listed[0].id == conversations[-1].id
    assert conversations[0].id not in {item.id for item in listed}
    conversation = conversations[-1]
    jobs = []
    for number in range(103):
        job = await store.submit(conversation.id, "demo-a", f"Consulta {number}")
        claimed = await store.claim(30)
        await store.finish(claimed.model_copy(update={"status": "DONE"}))
        jobs.append(job)
    history = await store.list_conversation_jobs(conversation.id, "demo-a")
    assert [item.id for item in history] == [item.id for item in jobs[-100:]]
    assert (await store.list_conversations("demo-a"))[0].title == "Consulta 0"
    with pytest.raises(NotFound):
        await store.list_conversation_jobs(conversation.id, "demo-b")


async def test_concurrent_approval_removes_inbox_once(store):
    conversation = await store.create_conversation("demo-a")
    await store.submit(conversation.id, "demo-a", "Ticket")
    job = await store.claim(30)
    await store.finish(job.model_copy(update={"status": "WAITING_APPROVAL"}))
    assert [item.id for item in await store.list_approvals("demo-a")] == [job.id]
    await asyncio.gather(
        store.approve(job.id, "demo-a", True),
        store.approve(job.id, "demo-a", False),
        return_exceptions=True,
    )
    assert await store.list_approvals("demo-a") == []
    assert (await store.claim(30)).id == job.id
    assert await store.claim(30) is None
