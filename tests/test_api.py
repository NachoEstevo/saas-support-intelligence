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


async def test_non_ascii_credential_returns_unauthorized_instead_of_server_error(settings):
    app = create_app(settings, ScriptedModel([]), FakeRetriever(), start_workers=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/conversations", headers={"X-API-Key": "incorrectá".encode()}
            )
            assert response.status_code == 401


def test_openapi_describes_api_key_authentication(settings):
    app = create_app(settings, ScriptedModel([]), FakeRetriever(), start_workers=False)
    schema = app.openapi()
    security = schema["components"]["securitySchemes"]["APIKeyHeader"]
    assert security == {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    assert schema["paths"]["/jobs/{job_id}"]["get"]["security"] == [{"APIKeyHeader": []}]
    assert "security" not in schema["paths"]["/health"]["get"]


async def test_workbench_identity_history_and_inbox_are_role_and_tenant_scoped(settings):
    async with client_for(settings) as (client, app):
        assert (await client.get("/identity", headers=customer(settings))).json() == {
            "tenant_id": "demo-a",
            "role": "customer",
        }
        assert (await client.get("/identity", headers=approver(settings))).json()[
            "role"
        ] == "approver"
        assert (await client.get("/conversations")).status_code == 401
        assert (await client.get("/conversations", headers=approver(settings))).status_code == 403
        conversation = (await client.post("/conversations", headers=customer(settings))).json()
        history = f"/conversations/{conversation['id']}/jobs"
        assert (await client.get(history, headers=customer(settings, "b"))).status_code == 404
        assert (await client.get(history, headers=approver(settings))).status_code == 403
        assert (await client.get(history, headers=customer(settings))).json() == []
        accepted = await client.post(
            f"/conversations/{conversation['id']}/messages",
            headers=customer(settings),
            json={"message": "Problema con CASE-101"},
        )
        job = await app.state.store.claim(30)
        await app.state.store.finish(job.model_copy(update={"status": "WAITING_APPROVAL"}))
        listed = (await client.get("/conversations", headers=customer(settings))).json()
        assert listed[0]["title"] == "Problema con CASE-101"
        assert listed[0]["last_job_status"] == "WAITING_APPROVAL"
        assert (await client.get("/conversations", headers=customer(settings, "b"))).json() == []
        assert (await client.get(history, headers=customer(settings))).json()[0]["id"] == str(
            job.id
        )
        assert (await client.get("/approvals", headers=customer(settings))).status_code == 403
        assert (await client.get("/approvals", headers=approver(settings, "b"))).json() == []
        inbox = (await client.get("/approvals", headers=approver(settings))).json()
        assert [item["id"] for item in inbox] == [accepted.json()["id"]]
        assert (
            await client.post(
                f"/jobs/{job.id}/approve",
                headers=approver(settings),
                json={"approved": False},
            )
        ).status_code == 202
        assert (await client.get("/approvals", headers=approver(settings))).json() == []


async def test_workbench_history_survives_a_new_api_instance(settings):
    async with client_for(settings) as (client, app):
        conversation = await app.state.store.create_conversation("demo-a")
        await app.state.store.submit(conversation.id, "demo-a", "Primera consulta")
    async with client_for(settings) as (client, app):
        assert (await client.get("/conversations", headers=customer(settings))).json()[0][
            "id"
        ] == str(conversation.id)
        jobs = (
            await client.get(f"/conversations/{conversation.id}/jobs", headers=customer(settings))
        ).json()
        assert jobs[0]["message"] == "Primera consulta"


async def test_built_frontend_preserves_api_routes_auth_and_private_cache_policy(
    settings, tmp_path, monkeypatch
):
    web_dist = tmp_path / "web" / "dist"
    web_dist.mkdir(parents=True)
    (web_dist / "index.html").write_text("<html>Support lab</html>", encoding="utf-8")
    monkeypatch.setattr("app.main.BASE_DIR", tmp_path)
    app = create_app(settings, ScriptedModel([]), FakeRetriever(), start_workers=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert "Support lab" in page.text
        assert page.headers["X-Content-Type-Options"] == "nosniff"
        assert (await client.get("/docs")).status_code == 200
        identity = await client.get("/identity")
        assert identity.status_code == 401
        assert identity.headers["Cache-Control"] == "no-store"
        identity = await client.get("/identity", headers=customer(settings))
        assert identity.json() == {"tenant_id": "demo-a", "role": "customer"}
        assert identity.headers["Cache-Control"] == "no-store"
        assert (await client.get("/missing-route")).status_code == 404
