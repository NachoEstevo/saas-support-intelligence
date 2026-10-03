from contextlib import asynccontextmanager

import httpx
import pytest

from app.main import create_app
from app.schemas import SupportResponse
from tests.fakes import (
    FakeRetriever,
    ScriptedModel,
    case_messages,
    case_response,
    knowledge_messages,
)

pytestmark = pytest.mark.integration


@asynccontextmanager
async def client_for(settings, model=None, retriever=None):
    app = create_app(
        settings, model or ScriptedModel([]), retriever or FakeRetriever(), start_workers=False
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, app


def customer(settings, tenant="a"):
    return {"X-API-Key": getattr(settings, f"customer_{tenant}_key").get_secret_value()}


def approver(settings, tenant="a"):
    return {"X-API-Key": getattr(settings, f"approver_{tenant}_key").get_secret_value()}


async def test_auth_validation_and_account_scope(settings):
    async with client_for(settings) as (client, app):
        assert (await client.get("/health")).status_code == 200
        assert (await client.post("/conversations")).status_code == 401
        assert (await client.post("/conversations", headers=approver(settings))).status_code == 403
        conversation = (await client.post("/conversations", headers=customer(settings))).json()
        path = f"/conversations/{conversation['id']}/messages"
        for message in [{"message": " "}, {"message": "Hola", "tenant_id": "demo-b"}]:
            assert (
                await client.post(path, json=message, headers=customer(settings))
            ).status_code == 422
        assert (
            await client.post(path, json={"message": "Hola"}, headers=customer(settings, "b"))
        ).status_code == 404
        accepted = await client.post(path, json={"message": "Hola"}, headers=customer(settings))
        assert accepted.status_code == 202
        job_id = accepted.json()["id"]
        assert (
            await client.get(f"/jobs/{job_id}", headers=customer(settings, "b"))
        ).status_code == 404
        assert (
            await client.post(
                f"/jobs/{job_id}/approve", json={"approved": True}, headers=customer(settings)
            )
        ).status_code == 403
        assert (
            await client.post(
                f"/jobs/{job_id}/approve", json={"approved": "true"}, headers=approver(settings)
            )
        ).status_code == 422
        assert (
            await client.post(
                f"/jobs/{job_id}/approve", json={"approved": True}, headers=approver(settings)
            )
        ).status_code == 409
        assert (
            await client.post(path, json={"message": "Otra"}, headers=customer(settings))
        ).status_code == 409


async def test_http_job_completes_and_new_app_keeps_conversation(settings):
    model = ScriptedModel(
        ["knowledge", "operations", "synthesis"],
        [*knowledge_messages(), *case_messages()],
        [case_response()],
    )
    async with client_for(settings, model) as (client, app):
        conversation = (await client.post("/conversations", headers=customer(settings))).json()
        response = await client.post(
            f"/conversations/{conversation['id']}/messages",
            headers=customer(settings),
            json={"message": "Qué falta en CASE-101"},
        )
        job_id = response.json()["id"]
        job = await app.state.store.claim(settings.job_timeout_seconds)
        await app.state.workers.execute(job)
        result = await client.get(f"/jobs/{job_id}", headers=customer(settings))
        assert result.json()["status"] == "DONE"
        assert result.json()["response"]["missing_documents"] == ["domicilio"]
    async with client_for(settings) as (client, app):
        assert (await client.get(f"/jobs/{job_id}", headers=customer(settings))).json()[
            "status"
        ] == "DONE"
        snapshot = await app.state.graph.aget_state(
            {"configurable": {"thread_id": f"demo-a:{conversation['id']}"}}
        )
        assert snapshot.values["last_case_id"] == "CASE-101"
        await app.state.graph.checkpointer.adelete_thread(f"demo-a:{conversation['id']}")


async def test_worker_background_loop_handles_accepted_job(settings):
    model = ScriptedModel(
        ["synthesis"], responses=[SupportResponse(status="needs_information", answer="Qué caso?")]
    )
    async with client_for(settings, model) as (client, app):
        import asyncio

        conversation = (await client.post("/conversations", headers=customer(settings))).json()
        response = await client.post(
            f"/conversations/{conversation['id']}/messages",
            headers=customer(settings),
            json={"message": "Qué falta"},
        )
        app.state.workers.start()
        for _ in range(100):
            result = (
                await client.get(f"/jobs/{response.json()['id']}", headers=customer(settings))
            ).json()
            if result["status"] == "DONE":
                break
            await asyncio.sleep(0.02)
        assert result["status"] == "DONE"


async def test_health_detects_unavailable_knowledge(settings):
    retriever = FakeRetriever()
    async with client_for(settings, retriever=retriever) as (client, app):

        async def unavailable():
            raise ValueError("Index changed")

        retriever.ready = unavailable
        assert (await client.get("/health")).status_code == 503
