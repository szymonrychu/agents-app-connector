import uuid

import pytest
from helpers import bearer, only, rows

from agents_app_connector.db import specs
from agents_app_connector.tracker import Tracker
from agents_app_connector.worker import Worker

SPEC = "---\nid: a\ntitle: T\n---\n# T"


@pytest.fixture
def worker(orchestrator, pool, settings, store, preset):
    return Worker(orchestrator, pool, settings, store)


@pytest.fixture
def tracker(orchestrator, pool, settings):
    return Tracker(orchestrator, pool, settings)


@pytest.fixture
def target_preset(fake):
    return fake.add_preset("review-agent")


@pytest.fixture
async def api(make_app, target_preset):
    import httpx
    from helpers import PREFIX

    app = make_app(handoff_presets=["review-agent", "missing-preset"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dev" + PREFIX
    ) as c:
        yield c


@pytest.fixture
async def conv(upload, pool):
    _, conv_id = await upload()
    return conv_id


async def add_spec(pool, conv_id, markdown=SPEC):
    async with pool.connection() as c:
        return await specs.add(c, uuid.UUID(conv_id), markdown, None, [], None)


def target_session(fake, preset, slug="review-1"):
    sid = str(uuid.uuid4())
    fake.sessions[sid] = {
        "id": sid,
        "slug": slug,
        "preset_id": str(preset.id),
        "desired": "active",
    }
    return sid


async def handoff(api, token, conv_id, session, client_id=None):
    return await api.post(
        f"/conversations/{conv_id}/handoff",
        json={"client_id": client_id or str(uuid.uuid4()), "session": session},
        headers=bearer(token),
    )


async def test_handoff_posts_spec_once_into_session(
    api, token, conv, pool, worker, tracker, fake, preset, conn
):
    await add_spec(pool, conv)
    sid = target_session(fake, preset)
    resp = await handoff(api, token, conv, "review-1")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["session_slug"] == "review-1"
    fake.fail_next("post_message", 503)
    await worker.tick()
    assert not [m for m in fake.messages.values() if m["session_id"] == sid]
    await worker.tick()
    await worker.tick()
    (msg,) = [m for m in fake.messages.values() if m["session_id"] == sid]
    assert msg["text"].startswith(
        f"Spec from the Boox sketch translator (conversation {conv}, version 1):"
    )
    assert SPEC in msg["text"]
    (run,) = await rows(conn, "SELECT id, kind FROM runs WHERE kind = 'handoff'")
    assert f"<!-- agents-app:{run[0]} -->" in msg["text"]
    assert not fake.attachments.get(msg["id"])
    fake.mark_sent(msg["id"])
    await tracker.tick()
    assert await rows(conn, "SELECT status FROM handoffs") == [("handled",)]
    evs = await rows(conn, "SELECT payload FROM events WHERE type = 'handoff' ORDER BY id")
    assert [e[0]["status"] for e in evs] == ["in-progress", "handled"]
    assert evs[0][0] == {
        "handoff_id": body["handoff_id"],
        "status": "in-progress",
        "session_slug": "review-1",
    }


async def test_handoff_to_preset_creates_session(
    api, token, conv, pool, worker, tracker, fake, target_preset, conn
):
    await add_spec(pool, conv)
    resp = await handoff(api, token, conv, {"preset": "review-agent"})
    assert resp.status_code == 202, resp.text
    slug = resp.json()["session_slug"]
    assert slug.startswith("app-handoff-")
    await worker.tick()
    session = only(s for s in fake.sessions.values() if s["slug"] == slug)
    assert session["preset_id"] == str(target_preset.id)
    (msg,) = [m for m in fake.messages.values() if m["session_id"] == session["id"]]
    assert SPEC in msg["text"]
    fake.mark_sent(msg["id"])
    await tracker.tick()
    assert await rows(conn, "SELECT status, session_slug FROM handoffs") == [("handled", slug)]


async def test_handoff_without_spec_409(api, token, conv, fake, preset):
    target_session(fake, preset)
    resp = await handoff(api, token, conv, "review-1")
    assert resp.status_code == 409
    assert resp.json()["error"] == "no_spec"


async def test_handoff_preset_not_allowed_422(api, token, conv, pool, fake):
    await add_spec(pool, conv)
    resp = await handoff(api, token, conv, {"preset": "sketch-translator"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "preset_not_allowed"


async def test_handoff_allowed_but_missing_preset_fails_no_preset(
    api, token, conv, pool, worker, conn
):
    await add_spec(pool, conv)
    resp = await handoff(api, token, conv, {"preset": "missing-preset"})
    assert resp.status_code == 202
    await worker.tick()
    assert await rows(conn, "SELECT status, reason FROM handoffs") == [("failed", "no_preset")]


async def test_handoff_unknown_session_404_and_bad_body_422(api, token, conv, pool):
    await add_spec(pool, conv)
    resp = await handoff(api, token, conv, "nope")
    assert resp.status_code == 404 and resp.json()["error"] == "unknown_session"
    resp = await handoff(api, token, conv, {"preset": "x", "other": 1})
    assert resp.status_code == 422


async def test_handoff_idempotent_200_and_mismatch_422(api, token, conv, pool, fake, preset, conn):
    await add_spec(pool, conv)
    target_session(fake, preset)
    target_session(fake, preset, "review-2")
    cid = str(uuid.uuid4())
    first = await handoff(api, token, conv, "review-1", cid)
    assert first.status_code == 202
    again = await handoff(api, token, conv, "review-1", cid)
    assert again.status_code == 200
    assert again.json() == first.json()
    other = await handoff(api, token, conv, "review-2", cid)
    assert other.status_code == 422 and other.json()["error"] == "idempotency_mismatch"
    assert await rows(conn, "SELECT count(*) FROM handoffs") == [(1,)]


async def test_targets_lists_active_sessions_and_allowed_presets(
    api, token, fake, preset, target_preset
):
    target_session(fake, target_preset)
    gone = target_session(fake, target_preset, "review-2")
    fake.sessions[gone]["desired"] = "deleting"
    target_session(fake, preset, "app-7")
    resp = await api.get("/handoff/targets", headers=bearer(token))
    assert resp.status_code == 200
    body = resp.json()
    assert [(s["slug"], s["preset_name"]) for s in body["sessions"]] == [
        ("review-1", "review-agent")
    ]
    assert body["presets"] == ["review-agent"]
    assert (await api.get("/handoff/targets")).status_code == 401


async def test_handoff_gone_session_failed_event(
    api, token, conv, pool, worker, fake, preset, conn
):
    await add_spec(pool, conv)
    sid = target_session(fake, preset)
    resp = await handoff(api, token, conv, "review-1")
    fake.remove_session(sid)
    await worker.tick()
    assert await rows(conn, "SELECT status, reason FROM handoffs") == [("failed", "session_gone")]
    (ev,) = await rows(conn, "SELECT payload FROM events WHERE type = 'handoff'")
    assert ev[0] == {
        "handoff_id": resp.json()["handoff_id"],
        "status": "failed",
        "session_slug": "review-1",
        "reason": "session_gone",
    }


async def test_handoff_session_gone_after_post_failed(
    api, token, conv, pool, worker, tracker, fake, preset, conn
):
    await add_spec(pool, conv)
    sid = target_session(fake, preset)
    await handoff(api, token, conv, "review-1")
    await worker.tick()
    fake.discard_session(sid)  # queued message cancelled
    await tracker.tick()
    assert await rows(conn, "SELECT status, reason FROM handoffs") == [("failed", "session_gone")]
    assert await rows(conn, "SELECT status, reason FROM runs WHERE kind = 'handoff'") == [
        ("failed", "session_gone")
    ]
