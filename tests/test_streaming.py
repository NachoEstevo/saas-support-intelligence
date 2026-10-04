import asyncio
import json

import pytest
from langchain_core.messages import AIMessageChunk

from app.graph import build_graph
from app.schemas import SupportResponse
from app.streaming import job_events
from app.support import SupportService
from app.worker import Workers
from tests.fakes import FakeRetriever, ScriptedModel
from tests.test_api import client_for, customer

pytestmark = pytest.mark.integration


async def test_paragraph_is_visible_before_generation_finishes(store, settings, saver):
    generating = asyncio.Event()
    finish = asyncio.Event()
    answer = "Primer párrafo con información.\n\nSegundo párrafo completo."
    response = SupportResponse(status="needs_information", answer=answer)

    class StreamingModel(ScriptedModel):
        async def astream(self, messages):
            raw = response.model_dump_json()
            cut = raw.index("Segundo")
            yield AIMessageChunk(content=raw[:cut])
            generating.set()
            await finish.wait()
            yield AIMessageChunk(content=raw[cut:])

    model = StreamingModel(["synthesis"])
    graph = build_graph(model, FakeRetriever(), SupportService(store), store, saver)
    conversation = await store.create_conversation("demo-a")
    submitted = await store.submit(conversation.id, "demo-a", "Una pregunta")
    claimed = await store.claim(settings.job_timeout_seconds)
    execution = asyncio.create_task(Workers(store, graph, settings).execute(claimed))
    try:
        await asyncio.wait_for(generating.wait(), 5)
        for _ in range(100):
            snapshot = await store.get_job(submitted.id, "demo-a")
            if snapshot.draft_answer:
                break
            await asyncio.sleep(0.01)
        assert snapshot.status == "RUNNING"
        assert snapshot.response is None
        assert snapshot.draft_answer == "Primer párrafo con información.\n\n"
        assert not execution.done()
    finally:
        finish.set()
        await asyncio.wait_for(execution, 5)
    final = await store.get_job(submitted.id, "demo-a")
    assert final.status == "DONE"
    assert final.response.answer == answer
    assert final.draft_answer == ""


@pytest.mark.parametrize("failure", ["truncated", "network"])
async def test_incomplete_or_failed_stream_discards_draft(store, settings, saver, failure):
    class BrokenStream(ScriptedModel):
        async def astream(self, messages):
            raw = SupportResponse(
                status="needs_information", answer="Uno.\n\nDos."
            ).model_dump_json()
            yield AIMessageChunk(content=raw[:-1])
            if failure == "network":
                raise RuntimeError("private-stream-detail")

    graph = build_graph(
        BrokenStream(["synthesis"]), FakeRetriever(), SupportService(store), store, saver
    )
    conversation = await store.create_conversation("demo-a")
    await store.submit(conversation.id, "demo-a", "Consulta")
    job = await store.claim(settings.job_timeout_seconds)
    await Workers(store, graph, settings).execute(job)
    final = await store.get_job(job.id, "demo-a")
    assert final.status == "FAILED"
    assert final.draft_answer == ""
    assert not await store.redis.hexists(store.key("job", job.id), "draft_answer")
    assert final.response is None
    assert final.error == ("INVALID_MODEL_OUTPUT" if failure == "truncated" else "EXECUTION_ERROR")
    assert "private-stream-detail" not in final.model_dump_json()


async def test_stream_is_authenticated_tenant_scoped_and_final_snapshot_is_canonical(settings):
    async with client_for(settings) as (client, app):
        store = app.state.store
        conversation = await store.create_conversation("demo-a")
        await store.submit(conversation.id, "demo-a", "Consulta")
        job = await store.claim(settings.job_timeout_seconds)
        await store.update_draft(job.id, "Borrador descartado")
        await store.finish(job.model_copy(update={"status": "FAILED", "error": "TIMEOUT"}))
        path = f"/jobs/{job.id}/stream"
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers=customer(settings, "b"))).status_code == 404
        response = await client.get(path, headers=customer(settings))
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
        snapshot = json.loads(response.text.split("data: ", 1)[1])
        assert snapshot["status"] == "FAILED"
        assert snapshot["draft_answer"] == ""
        assert "Borrador descartado" not in response.text
        assert "contributions" not in response.text


async def test_disconnect_does_not_cancel_or_release_running_job(store):
    class Disconnected:
        async def is_disconnected(self):
            return True

    conversation = await store.create_conversation("demo-a")
    await store.submit(conversation.id, "demo-a", "Consulta")
    job = await store.claim(30)
    assert [item async for item in job_events(store, job.id, "demo-a", Disconnected(), 30)] == []
    assert (await store.get_job(job.id, "demo-a")).status == "RUNNING"


async def test_validation_rejection_resets_the_draft_before_refinement(
    store, settings, saver, monkeypatch
):
    writes = []
    update_draft = store.update_draft

    async def record(identity, answer):
        writes.append(answer)
        await update_draft(identity, answer)

    monkeypatch.setattr(store, "update_draft", record)
    invalid = SupportResponse(status="answered", answer="Afirmación sin evidencia.")
    valid = SupportResponse(status="needs_information", answer="Necesito más información.")
    model = ScriptedModel(["synthesis", "synthesis"], responses=[invalid, valid])
    graph = build_graph(model, FakeRetriever(), SupportService(store), store, saver)
    conversation = await store.create_conversation("demo-a")
    await store.submit(conversation.id, "demo-a", "Consulta")
    job = await store.claim(settings.job_timeout_seconds)
    await Workers(store, graph, settings).execute(job)
    rejected = writes.index(invalid.answer)
    assert writes[rejected + 1] == ""
    assert writes[-1] == valid.answer
    final = await store.get_job(job.id, "demo-a")
    assert final.status == "DONE"
    assert final.response == valid


async def test_storage_failure_ends_stream_with_a_safe_error():
    from redis.exceptions import ConnectionError

    class BrokenStore:
        async def get_job(self, *args):
            raise ConnectionError("private-redis-url")

    class Connected:
        async def is_disconnected(self):
            return False

    frames = [item async for item in job_events(BrokenStore(), None, "demo-a", Connected(), 30)]
    assert frames == ['event: error\ndata: {"message":"Storage unavailable"}\n\n']
