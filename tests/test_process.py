import asyncio
import logging
import socket

import httpx
import pytest
from pydantic import SecretStr

from agents_app_connector import process
from agents_app_connector.process import run_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def app_settings(settings, url):
    return settings.model_copy(
        update={
            "database_url": SecretStr(url),
            "device_port": free_port(),
            "mcp_port": free_port(),
            "poll_seconds": 0.1,
            "http_host": "127.0.0.1",
        }
    )


async def wait_ready(port: int) -> None:
    async with httpx.AsyncClient() as c:
        for _ in range(100):
            try:
                if (await c.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)
    raise AssertionError("device app did not come up")


async def test_serves_device_app_loops_and_stops(app_settings, orchestrator, monkeypatch):
    ticks = {"worker": 0, "tracker": 0, "retention": 0}

    class Spy:
        def __init__(self, name):
            self.name = name

        async def tick(self):
            ticks[self.name] += 1

    monkeypatch.setattr(process, "Worker", lambda *a, **k: Spy("worker"))
    monkeypatch.setattr(process, "Tracker", lambda *a, **k: Spy("tracker"))
    monkeypatch.setattr(process, "Retention", lambda *a, **k: Spy("retention"))
    stop = asyncio.Event()
    task = asyncio.create_task(run_app(app_settings, orchestrator=orchestrator, stop=stop))
    await wait_ready(app_settings.device_port)
    await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert all(n >= 2 for n in ticks.values()), ticks


async def test_retention_gets_settings_retention_days(app_settings, orchestrator, monkeypatch):
    seen = {}

    class Idle:
        async def tick(self):
            return None

    def retention(pool, store, **kw):
        seen.update(kw, root=store.root)
        return Idle()

    monkeypatch.setattr(process, "Retention", retention)
    settings = app_settings.model_copy(update={"retention_days": 7})
    stop = asyncio.Event()
    task = asyncio.create_task(run_app(settings, orchestrator=orchestrator, stop=stop))
    await wait_ready(settings.device_port)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert seen["retention_days"] == 7
    assert str(seen["root"]) == settings.data_dir


async def test_tick_error_is_logged_and_loop_continues(
    app_settings, orchestrator, monkeypatch, caplog
):
    calls = {"n": 0}

    class Boom:
        async def tick(self):
            calls["n"] += 1
            raise RuntimeError("boom")

    monkeypatch.setattr(process, "Worker", lambda *a, **k: Boom())
    stop = asyncio.Event()
    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(run_app(app_settings, orchestrator=orchestrator, stop=stop))
        await wait_ready(app_settings.device_port)
        await asyncio.sleep(0.6)
        stop.set()
        await asyncio.wait_for(task, 10)
    assert calls["n"] >= 2
    assert "boom" in caplog.text


async def test_second_instance_waits_for_singleton(app_settings, orchestrator):
    first_stop, second_stop = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(run_app(app_settings, orchestrator=orchestrator, stop=first_stop))
    await wait_ready(app_settings.device_port)
    second_settings = app_settings.model_copy(update={"device_port": free_port()})
    second = asyncio.create_task(
        run_app(second_settings, orchestrator=orchestrator, stop=second_stop)
    )
    await asyncio.sleep(0.8)
    async with httpx.AsyncClient() as c:
        with pytest.raises(httpx.TransportError):
            await c.get(f"http://127.0.0.1:{second_settings.device_port}/healthz")
    first_stop.set()
    await asyncio.wait_for(first, 10)
    await wait_ready(second_settings.device_port)
    second_stop.set()
    await asyncio.wait_for(second, 10)


async def test_serves_translator_tools_on_the_mcp_port(app_settings, orchestrator):
    stop = asyncio.Event()
    task = asyncio.create_task(run_app(app_settings, orchestrator=orchestrator, stop=stop))
    await wait_ready(app_settings.device_port)
    async with httpx.AsyncClient() as c:
        r = await c.post(f"http://127.0.0.1:{app_settings.mcp_port}/mcp", json={})
    stop.set()
    await asyncio.wait_for(task, 10)
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
