"""Upload to spec to feedback to hand-off, end to end: real Postgres, FakeOrchestrator, the
admin and device apps over httpx, and the worker and tracker ticked by hand."""

import asyncio
import json
import uuid

import httpx
import pytest
from helpers import PREFIX, FakeSse, bearer, event, files, meta, only, post, rows
from pydantic import SecretStr

from agents_app_connector.api.admin import PREFIX as ADMIN_PREFIX
from agents_app_connector.api.admin import create_admin_app
from agents_app_connector.api.device import create_device_app
from agents_app_connector.plugins.sketch import PLUGIN, task_routes
from agents_app_connector.tracker import Tracker
from agents_app_connector.worker import Worker

pytestmark = pytest.mark.e2e

SPEC_V1 = "---\nid: a\ntitle: T\n---\n# Box A"
SPEC_V2 = "---\nid: a\ntitle: T\n---\n# Box B"


async def pair(admin, api):
    """Create a device through the admin API and exchange its code: `(token, device_id)`."""
    created = await admin.post("/devices", json={"name": "boox"})
    assert created.status_code == 201
    exchanged = await api.post("/pair", json={"code": created.json()["code"]})
    assert exchanged.status_code == 200, exchanged.text
    return exchanged.json()["token"], created.json()["device_id"]


async def test_upload_to_spec_to_feedback_to_handoff(
    pool, settings, store, orchestrator, fake, preset, conn
):
    other_preset = fake.add_preset("review-agent")
    other = str(uuid.uuid4())
    fake.sessions[other] = {
        "id": other,
        "slug": "review-1",
        "preset_id": str(other_preset.id),
        "desired": "active",
    }
    s = settings.model_copy(update={"handoff_presets": ["review-agent"]})
    admin_app = create_admin_app(pool, s)
    device_app = create_device_app(pool, s, [PLUGIN], store, orchestrator=orchestrator)

    def worker():
        return Worker(orchestrator, pool, s, store)

    def tracker():
        return Tracker(orchestrator, pool, s)

    async with (
        device_app.router.lifespan_context(device_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=admin_app, client=("127.0.0.1", 1)),
            base_url="http://admin" + ADMIN_PREFIX,
        ) as admin,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=device_app), base_url="http://dev" + PREFIX
        ) as api,
    ):
        token, _ = await pair(admin, api)
        w, t = worker(), tracker()

        # upload, dispatch: one session, one message with two attachments
        up = await post(api, token, meta(), files(with_strokes=True))
        assert up.status_code == 202, up.text
        conv = up.json()["conversation_id"]
        await w.tick()
        session = only(x for x in fake.sessions.values() if x["preset_id"] == str(preset.id))
        (first,) = fake.messages
        assert fake.messages[first]["session_id"] == session["id"]
        assert len(fake.messages[first]["attachments"]) == 2

        # reply: spec v1 and its events
        fake.complete_turn(first, SPEC_V1)
        await t.tick()
        ev = await api.get(f"/conversations/{conv}/events?after=0", headers=bearer(token))
        assert "spec" in [e["type"] for e in ev.json()["events"]]
        spec = await api.get(f"/conversations/{conv}/spec", headers=bearer(token))
        assert spec.json()["version"] == 1 and spec.json()["markdown"] == SPEC_V1

        # feedback: follow-up in the same session, spec v2
        msg = await api.post(
            f"/conversations/{conv}/messages",
            json={"client_id": str(uuid.uuid4()), "text": "rename box A"},
            headers=bearer(token),
        )
        assert msg.status_code == 202, msg.text
        await w.tick()
        second = max(fake.messages)
        assert second != first and fake.messages[second]["session_id"] == session["id"]
        assert "rename box A" in fake.messages[second]["text"]
        fake.complete_turn(second, SPEC_V2)
        await t.tick()
        spec = await api.get(f"/conversations/{conv}/spec", headers=bearer(token))
        assert spec.json()["version"] == 2 and spec.json()["markdown"] == SPEC_V2

        # hand-off: the other session gets one message with spec v2
        ho = await api.post(
            f"/conversations/{conv}/handoff",
            json={"client_id": str(uuid.uuid4()), "session": "review-1"},
            headers=bearer(token),
        )
        assert ho.status_code == 202, ho.text
        await w.tick()
        (handoff_msg,) = [m for m in fake.messages.values() if m["session_id"] == other]
        assert SPEC_V2 in handoff_msg["text"]
        (run_id,) = (await rows(conn, "SELECT id FROM runs WHERE kind = 'handoff'"))[0]
        assert f"<!-- agents-app:{run_id} -->" in handoff_msg["text"]
        fake.mark_sent(handoff_msg["id"])
        await t.tick()
        assert await rows(conn, "SELECT status FROM handoffs") == [("handled",)]

        # restart: fresh worker and tracker post nothing new (dispatch marker survives)
        before = {k: dict(v) for k, v in fake.messages.items()}
        w2, t2 = worker(), tracker()
        await w2.tick()
        await t2.tick()
        await w2.tick()
        assert set(fake.messages) == set(before)
        assert len(fake.sessions) == 2
        assert len(await rows(conn, "SELECT 1 FROM specs")) == 2


async def test_dispatch_marker_survives_restart(
    pool, settings, store, orchestrator, fake, preset, conn
):
    """The process dies after the orchestrator accepted the message but before the run stored
    its message id: a restarted worker adopts the message carrying the run marker."""
    device_app = create_device_app(pool, settings, [PLUGIN], store, orchestrator=orchestrator)
    async with (
        device_app.router.lifespan_context(device_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_admin_app(pool, settings), client=("127.0.0.1", 1)
            ),
            base_url="http://admin" + ADMIN_PREFIX,
        ) as admin,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=device_app), base_url="http://dev" + PREFIX
        ) as api,
    ):
        token, _ = await pair(admin, api)
        assert (await post(api, token, meta(), files())).status_code == 202
        await Worker(orchestrator, pool, settings, store).tick()
        (mid,) = fake.messages
        await conn.execute("UPDATE runs SET message_id = NULL")
        await Worker(orchestrator, pool, settings, store).tick()
        await Worker(orchestrator, pool, settings, store).tick()
        assert list(fake.messages) == [mid]
        assert await rows(conn, "SELECT message_id FROM runs") == [(str(mid),)]


DELEGATE = "delegate-test-token"
UP = "/taskboard/api/v1"


class FakeTaskboard(FakeSse):
    """The delegate listener: a task list, one open decision and the SSE stream."""

    def __init__(self):
        super().__init__()
        self.answered: dict | None = None
        self.seen_auth: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen_auth.add(request.headers.get("authorization", ""))
        if request.url.path == f"{UP}/stream":
            return super().handler(request)
        if request.method == "GET" and request.url.path == f"{UP}/tasks":
            items = [{"key": "T-1", "title": "first", "status": "open", "labels": ["a"]}]
            return httpx.Response(200, json={"items": items, "next_cursor": None})
        if request.method == "POST" and request.url.path == f"{UP}/tasks/T-1/decisions/4/answer":
            if self.answered is not None:
                err = {"code": "already_answered", "message": "decision already answered"}
                return httpx.Response(409, json={"error": err})
            self.answered = json.loads(request.content)
            return httpx.Response(200, json={"key": "T-1", "decisions": [{"id": 4}]})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no"}})


async def test_e2e_task_relay(pool, settings, store, orchestrator, monkeypatch):
    monkeypatch.setattr(task_routes, "KEEPALIVE_SECONDS", 0.2)
    board = FakeTaskboard()
    taskboard = httpx.AsyncClient(
        base_url="http://taskboard", transport=httpx.MockTransport(board.handler)
    )
    s = settings.model_copy(
        update={
            "taskboard_url": "http://taskboard",
            "taskboard_token": SecretStr(DELEGATE),
            "poll_seconds": 0.2,
        }
    )
    admin_app = create_admin_app(pool, s)
    device_app = create_device_app(
        pool, s, [PLUGIN], store, orchestrator=orchestrator, taskboard=taskboard
    )
    async with (
        device_app.router.lifespan_context(device_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=admin_app, client=("127.0.0.1", 1)),
            base_url="http://admin" + ADMIN_PREFIX,
        ) as admin,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=device_app),
            base_url="http://dev" + PREFIX,
            timeout=20,
        ) as api,
    ):
        token, device_id = await pair(admin, api)

        listed = await api.get("/tasks?status=active&label=a", headers=bearer(token))
        assert listed.status_code == 200 and listed.json()["items"][0]["key"] == "T-1"

        first = await api.post(
            "/tasks/T-1/decisions/4/answer", json={"choice": 1}, headers=bearer(token)
        )
        assert first.status_code == 200 and board.answered == {"choice": 1}
        second = await api.post(
            "/tasks/T-1/decisions/4/answer", json={"choice": 1}, headers=bearer(token)
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "already_answered"

        async def feed_then_revoke():
            await asyncio.wait_for(board.connected.wait(), 5)
            board.send(event("T-1"))
            await asyncio.sleep(0.4)
            assert (await admin.delete(f"/devices/{device_id}")).status_code == 204

        feeder = asyncio.create_task(feed_then_revoke())
        stream = await asyncio.wait_for(api.get("/tasks/stream", headers=bearer(token)), 40)
        await feeder
        assert stream.status_code == 200
        assert '"key":"T-1"' in stream.text.replace(" ", "")
        gone = await api.get("/tasks", headers=bearer(token))
        assert gone.status_code == 401

    assert board.seen_auth == {f"Bearer {DELEGATE}"}
    await taskboard.aclose()
