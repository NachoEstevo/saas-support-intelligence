from pathlib import Path

import httpx
import pytest

from scripts.demo import restart_local_api


async def test_restart_waits_for_health_and_targets_only_api(monkeypatch):
    commands = []
    probes = []

    class CompletedProcess:
        returncode = 0

        async def wait(self):
            return self.returncode

    async def create_process(*args, **kwargs):
        commands.append((args, kwargs))
        return CompletedProcess()

    async def no_sleep(seconds):
        pass

    def health(request):
        probes.append(request.url.path)
        return httpx.Response(503 if len(probes) == 1 else 200)

    monkeypatch.setattr("scripts.demo.asyncio.create_subprocess_exec", create_process)
    monkeypatch.setattr("scripts.demo.asyncio.sleep", no_sleep)
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:18080", transport=httpx.MockTransport(health)
    ) as client:
        await restart_local_api(client)
    assert commands[0][0] == ("docker", "compose", "restart", "api")
    assert commands[0][1]["cwd"] == Path(__file__).resolve().parents[1]
    assert probes == ["/health", "/health"]


async def test_restart_refuses_remote_api():
    async with httpx.AsyncClient(base_url="https://support.example") as client:
        with pytest.raises(ValueError, match="local"):
            await restart_local_api(client)


async def test_restart_reports_failed_docker_command(monkeypatch):
    class FailedProcess:
        returncode = 1

        async def wait(self):
            return self.returncode

    async def create_process(*args, **kwargs):
        return FailedProcess()

    monkeypatch.setattr("scripts.demo.asyncio.create_subprocess_exec", create_process)
    async with httpx.AsyncClient(base_url="http://localhost:18080") as client:
        with pytest.raises(RuntimeError, match="reiniciar"):
            await restart_local_api(client)
