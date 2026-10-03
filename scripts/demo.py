import argparse
import asyncio
import json
from pathlib import Path
from time import perf_counter

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DemoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    api_base_url: str = "http://127.0.0.1:18080"
    customer_a_key: SecretStr
    customer_b_key: SecretStr
    approver_a_key: SecretStr


async def wait_job(client: httpx.AsyncClient, identity: str, headers: dict) -> dict:
    async with asyncio.timeout(210):
        while True:
            response = await client.get(f"/jobs/{identity}", headers=headers)
            response.raise_for_status()
            job = response.json()
            if job["status"] not in ("PENDING", "RUNNING"):
                return job
            await asyncio.sleep(0.3)


async def restart_local_api(client: httpx.AsyncClient) -> None:
    if client.base_url.host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("El reinicio requiere una API local.")
    process = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        "restart",
        "api",
        cwd=Path(__file__).resolve().parents[1],
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(60):
            if await process.wait() != 0:
                raise RuntimeError("Docker no pudo reiniciar la API.")
            while True:
                try:
                    response = await client.get("/health")
                    if response.status_code == 200:
                        return
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.5)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def demo(settings: DemoSettings, *, restart_api: bool = False) -> dict:
    customer = {"X-API-Key": settings.customer_a_key.get_secret_value()}
    other = {"X-API-Key": settings.customer_b_key.get_secret_value()}
    approver = {"X-API-Key": settings.approver_a_key.get_secret_value()}
    records = []
    async with httpx.AsyncClient(base_url=settings.api_base_url, timeout=15) as client:

        async def run(name: str, message: str, conversation: str | None = None):
            started = perf_counter()
            if conversation is None:
                response = await client.post("/conversations", headers=customer)
                response.raise_for_status()
                conversation = response.json()["id"]
            accepted = await client.post(
                f"/conversations/{conversation}/messages",
                json={"message": message},
                headers=customer,
            )
            accepted.raise_for_status()
            job = await wait_job(client, accepted.json()["id"], customer)
            record = {
                "scenario": name,
                "request": message,
                "conversation_id": conversation,
                "seconds": round(perf_counter() - started, 2),
                "job": job,
            }
            records.append(record)
            print(name, job["status"], record["seconds"], flush=True)
            return record

        general = await run(
            "documented-answer",
            "En el sandbox Nexo, ¿qué formatos de archivo puedo cargar y cuál es el tamaño máximo?",
        )
        general["passed"] = (
            general["job"]["status"] == "DONE"
            and general["job"]["response"]["status"] == "answered"
            and bool(general["job"]["response"]["citations"])
        )
        case = await run("specialist-delegation", "¿Qué falta en CASE-101 y cómo debo cargarlo?")
        case["passed"] = (
            case["job"]["status"] == "DONE"
            and case["job"]["response"]["case_id"] == "CASE-101"
            and case["job"]["response"]["missing_documents"] == ["domicilio"]
            and {"buscar_documentacion", "consultar_caso"}
            <= {event.get("tool") for event in case["job"]["events"]}
        )
        follow = await run(
            "conversation-memory", "¿Y qué falta en ese caso?", case["conversation_id"]
        )
        follow["passed"] = (
            follow["job"]["status"] == "DONE"
            and follow["job"]["response"]["case_id"] == "CASE-101"
            and follow["job"]["response"]["missing_documents"] == ["domicilio"]
        )
        unknown = await run("abstention", "¿Qué contraseña tiene mi cuenta de otro banco?")
        unknown["passed"] = (
            unknown["job"]["status"] == "DONE"
            and unknown["job"]["response"]["status"] == "needs_information"
        )
        foreign = await run(
            "account-isolation", "Decime qué compañía y documentos corresponden a CASE-201."
        )
        access = await client.get(f"/jobs/{foreign['job']['id']}", headers=other)
        foreign["passed"] = (
            access.status_code == 404
            and foreign["job"]["status"] == "DONE"
            and foreign["job"]["response"]["status"] == "needs_information"
            and "Beta Demo" not in foreign["job"]["response"]["answer"]
        )
        foreign["foreign_http_status"] = access.status_code
        handoff = await run(
            "human-approval",
            "CASE-101: soy owner y falla la carga de un PDF de domicilio de 2 MB. "
            "El error UploadFailed se repite en tres intentos siguiendo las instrucciones. "
            "Quiero escalarlo: prepará un ticket para soporte humano con estos datos.",
        )
        job = handoff["job"]
        handoff["passed"] = job["status"] == "WAITING_APPROVAL" and job["ticket_id"] is None
        if handoff["passed"]:
            denied = await client.post(
                f"/jobs/{job['id']}/approve", json={"approved": True}, headers=customer
            )
            approved = await client.post(
                f"/jobs/{job['id']}/approve", json={"approved": True}, headers=approver
            )
            approved.raise_for_status()
            final = await wait_job(client, job["id"], customer)
            handoff["resumed_job"] = final
            ticket = await client.get(f"/tickets/{final['ticket_id']}", headers=customer)
            handoff["passed"] = (
                denied.status_code == 403
                and final["status"] == "DONE"
                and ticket.status_code == 200
                and ticket.json()["case_id"] == "CASE-101"
            )
            handoff["customer_approval_status"] = denied.status_code
        if restart_api:
            await restart_local_api(client)
            resumed = await run(
                "memory-after-restart",
                "¿Qué documentación falta en ese caso?",
                case["conversation_id"],
            )
            resumed["passed"] = (
                resumed["job"]["status"] == "DONE"
                and resumed["job"]["response"]["case_id"] == "CASE-101"
                and resumed["job"]["response"]["missing_documents"] == ["domicilio"]
            )
        return {"transport": "HTTP", "synthetic_data": True, "scenarios": records}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Demo HTTP con proveedores reales.")
    parser.add_argument(
        "--restart-api", action="store_true", help="Reiniciar la API local y verificar su memoria."
    )
    result = await demo(DemoSettings(), restart_api=parser.parse_args().restart_api)
    path = Path("evidence/demo.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(record["passed"] for record in result["scenarios"])
    print(f"Scenarios passed: {passed}/{len(result['scenarios'])}")
    if passed != len(result["scenarios"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
