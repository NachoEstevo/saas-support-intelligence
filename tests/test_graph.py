import json

import pytest
from langchain_core.messages import AIMessage

from app.config import BASE_DIR
from app.graph import build_graph
from app.persistence import open_checkpointer
from app.schemas import SupportResponse, TicketDraft
from app.support import SupportService
from app.worker import Workers
from tests.fakes import (
    SOURCE,
    FakeRetriever,
    ScriptedModel,
    call,
    case_messages,
    case_response,
    knowledge_messages,
)

pytestmark = pytest.mark.integration


async def run_job(store, settings, saver, model, conversation=None, message="Ver CASE-101"):
    support = SupportService(store)
    await support.seed(BASE_DIR / "data" / "cases.json")
    graph = build_graph(model, FakeRetriever(), support, store, saver)
    conversation = conversation or await store.create_conversation("demo-a")
    submitted = await store.submit(conversation.id, "demo-a", message)
    job = await store.claim(settings.job_timeout_seconds)
    await Workers(store, graph, settings).execute(job)
    return await store.get_job(submitted.id, "demo-a"), conversation, graph


async def test_delegation_calls_both_specialists_and_validates(store, settings, saver):
    model = ScriptedModel(
        ["knowledge", "operations", "synthesis"],
        [*knowledge_messages(), *case_messages()],
        [case_response()],
    )
    job, conversation, graph = await run_job(store, settings, saver, model)
    assert job.status == "DONE"
    assert job.response.missing_documents == ["domicilio"]
    assert [event["tool"] for event in job.events if "tool" in event] == [
        "buscar_documentacion",
        "consultar_caso",
    ]
    assert {"agent": "validation", "status": "accepted"} in job.events
    assert job.trace_id is not None
    state = await graph.aget_state({"configurable": {"thread_id": f"demo-a:{conversation.id}"}})
    assert state.values["contributions"]["operations"].case_result.case.tenant_id == "demo-a"


async def test_checkpoint_survives_a_new_saver_and_graph(store, settings):
    conversation = await store.create_conversation("demo-a")
    thread_id = f"demo-a:{conversation.id}"
    async with open_checkpointer(settings.redis_url) as first:
        model = ScriptedModel(
            ["knowledge", "operations", "synthesis"],
            [*knowledge_messages(), *case_messages()],
            [case_response()],
        )
        job, _, _ = await run_job(store, settings, first, model, conversation)
        assert job.status == "DONE"
    async with open_checkpointer(settings.redis_url) as second:
        model = ScriptedModel(
            ["knowledge", "operations", "synthesis"],
            [*knowledge_messages(), *case_messages()],
            [case_response()],
        )
        job, _, graph = await run_job(
            store, settings, second, model, conversation, message="¿Y qué falta en ese caso?"
        )
        assert job.status == "DONE"
        supervisor_input = json.loads(model.inputs[0][-1].content)
        assert supervisor_input["last_case_id"] == "CASE-101"
        assert len(supervisor_input["history"]) == 3
        state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        assert len(state.values["messages"]) == 4
        await second.adelete_thread(thread_id)


async def test_invalid_citations_refine_once_then_fail_closed(store, settings, saver):
    invalid = SupportResponse(status="answered", answer="Inventado", citations=["not-retrieved"])
    model = ScriptedModel(["synthesis", "synthesis"], responses=[invalid, invalid])
    job, _, _ = await run_job(store, settings, saver, model)
    assert job.status == "DONE"
    assert job.response.status == "needs_information"
    assert not job.response.citations
    assert sum(event.get("status") == "rejected" for event in job.events) == 2


async def test_unbounded_supervisor_is_stopped(store, settings, saver):
    model = ScriptedModel(["knowledge"] * 8, knowledge_messages() * 8)
    job, _, _ = await run_job(store, settings, saver, model)
    assert job.status == "DONE"
    assert job.response.status == "needs_information"
    assert "límite" in job.response.answer


async def test_tool_can_retry_after_invalid_arguments(store, settings, saver):
    model = ScriptedModel(
        ["knowledge", "operations", "synthesis"],
        [*knowledge_messages(), call("consultar_caso", case_id="wrong"), *case_messages()],
        [case_response()],
    )
    job, _, _ = await run_job(store, settings, saver, model)
    assert job.status == "DONE"
    assert [event["status"] for event in job.events if event.get("tool") == "consultar_caso"] == [
        "error",
        "ok",
    ]


@pytest.mark.parametrize("approved", [True, False])
async def test_human_approval_resumes_only_ticket_node(store, settings, saver, approved):
    draft = TicketDraft(
        subject="Error de carga",
        description="La carga del documento falla y requiere soporte.",
        case_id="CASE-101",
    )
    model = ScriptedModel(
        ["knowledge", "operations", "synthesis"],
        [
            *knowledge_messages(),
            call("consultar_caso", case_id="CASE-101"),
            call("preparar_ticket", **draft.model_dump()),
            AIMessage("Borrador preparado"),
        ],
        [
            SupportResponse(
                status="handoff",
                answer="a" * 4000,
                citations=[SOURCE.id],
                case_id="CASE-101",
                missing_documents=["domicilio"],
                ticket=draft,
            )
        ],
    )
    job, _, graph = await run_job(store, settings, saver, model)
    assert job.status == "WAITING_APPROVAL"
    assert job.ticket_id is None
    await store.approve(job.id, "demo-a", approved)
    resumed = await store.claim(settings.job_timeout_seconds)
    async with open_checkpointer(settings.redis_url) as restarted_saver:
        restarted = build_graph(
            model, FakeRetriever(), SupportService(store), store, restarted_saver
        )
        await Workers(store, restarted, settings).execute(resumed)
    final = await store.get_job(job.id, "demo-a")
    assert final.status == ("DONE" if approved else "REJECTED")
    assert not model.routes and not model.messages and not model.responses
    if approved:
        ticket = await store.get_ticket(final.ticket_id, "demo-a")
        assert ticket.case_id == "CASE-101"
    else:
        assert final.ticket_id is None


async def test_unexpected_model_failure_is_a_controlled_job_error(store, settings, saver):
    model = ScriptedModel([])
    job, _, _ = await run_job(store, settings, saver, model)
    assert job.status == "FAILED"
    assert job.error == "EXECUTION_ERROR"
    assert job.response is None
