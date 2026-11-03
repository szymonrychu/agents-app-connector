import uuid

import httpx
import pytest
from helpers import PREFIX, bearer, count, files, meta, only, post, rows, stored_files

from agents_app_connector.db import devices
from agents_app_connector.worker import Worker


async def msg(api, token, cid, text="hello", client_id=None):
    return await api.post(
        f"/conversations/{cid}/messages",
        json={"client_id": client_id or str(uuid.uuid4()), "text": text},
        headers=bearer(token),
    )


async def test_conversation_current_reuses_and_new_creates(device_api, token):
    a = (await post(device_api, token)).json()["conversation_id"]
    b = (await post(device_api, token)).json()["conversation_id"]
    c = (await post(device_api, token, meta(conversation="new"))).json()["conversation_id"]
    d = (await post(device_api, token)).json()["conversation_id"]
    assert a == b and c != a and d == c


async def test_create_and_list_conversations(device_api, token, conn):
    r = await device_api.post("/conversations", headers=bearer(token))
    assert r.status_code == 201
    cid = r.json()["id"]
    rows = (await device_api.get("/conversations", headers=bearer(token))).json()
    assert [x["id"] for x in rows] == [cid]
    assert rows[0]["title"] is None and rows[0]["latest_spec_version"] is None
    assert rows[0]["created_at"]
    await conn.execute(
        "INSERT INTO specs (conversation_id, version, markdown) VALUES (%s, 1, 'a'), (%s, 2, 'b')",
        (cid, cid),
    )
    rows = (await device_api.get("/conversations", headers=bearer(token))).json()
    assert rows[0]["latest_spec_version"] == 2


async def test_message_202_idempotent(device_api, token, conn):
    cid = (await device_api.post("/conversations", headers=bearer(token))).json()["id"]
    client_id = str(uuid.uuid4())
    r = await msg(device_api, token, cid, client_id=client_id)
    assert r.status_code == 202
    again = await msg(device_api, token, cid, client_id=client_id)
    assert again.status_code == 200 and again.json() == r.json()
    other = await msg(device_api, token, cid, text="different", client_id=client_id)
    assert other.status_code == 422 and other.json()["error"] == "idempotency_mismatch"
    row = await (await conn.execute("SELECT id, kind, text, status FROM inbound")).fetchall()
    assert [(str(i), k, t, s) for i, k, t, s in row] == [
        (r.json()["message_id"], "message", "hello", "queued")
    ]
    ev = await (await conn.execute("SELECT type, payload FROM events")).fetchall()
    assert ev == [("message", {"message_id": r.json()["message_id"], "status": "queued"})]


async def test_message_text_bounds(make_app, token):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(max_text_chars=5)),
        base_url="http://dev" + PREFIX,
    ) as c:
        cid = (await c.post("/conversations", headers=bearer(token))).json()["id"]
        assert (await msg(c, token, cid, text="")).status_code == 422
        assert (await msg(c, token, cid, text="123456")).status_code == 422
        assert (await msg(c, token, cid, text="12345")).status_code == 202


async def test_message_needs_owned_conversation_404(device_api, token, conn):
    _, other = await devices.create(conn, "sketch", "other")
    cid = (await device_api.post("/conversations", headers=bearer(other))).json()["id"]
    r = await msg(device_api, token, cid)
    assert r.status_code == 404
    assert (await msg(device_api, token, str(uuid.uuid4()))).status_code == 404
    assert await count(conn, "inbound") == 0


async def test_other_devices_conversation_hidden_and_undeletable(device_api, token, conn):
    _, other = await devices.create(conn, "sketch", "other")
    cid = (await device_api.post("/conversations", headers=bearer(other))).json()["id"]
    assert (await device_api.get("/conversations", headers=bearer(token))).json() == []
    assert (
        await device_api.delete(f"/conversations/{cid}", headers=bearer(token))
    ).status_code == 404


async def test_delete_conversation_removes_files_and_specs(device_api, token, conn, store):
    r = (await post(device_api, token, fl=files(with_strokes=True))).json()
    cid = r["conversation_id"]
    await conn.execute(
        "INSERT INTO specs (conversation_id, version, markdown) VALUES (%s, 1, 'a')", (cid,)
    )
    assert len(stored_files(store)) == 2
    d = await device_api.delete(f"/conversations/{cid}", headers=bearer(token))
    assert d.status_code == 204
    assert stored_files(store) == []
    assert await count(conn, "attachments") == 0 and await count(conn, "specs") == 0
    row = await (await conn.execute("SELECT status, reason FROM inbound")).fetchone()
    assert row == ("failed", "conversation_deleted")
    state = await (await conn.execute("SELECT state FROM conversations")).fetchone()
    assert state == ("deleted",)
    assert (await device_api.get("/conversations", headers=bearer(token))).json() == []
    assert (
        await device_api.delete(f"/conversations/{cid}", headers=bearer(token))
    ).status_code == 404
    assert (await msg(device_api, token, cid)).status_code == 404


async def _bound(device_api, token, worker, upload, fake):
    _, cid = await upload()
    await worker.tick()
    return cid, only(fake.sessions.values())


@pytest.fixture
def worker(orchestrator, pool, settings, store, preset):
    return Worker(orchestrator, pool, settings, store)


async def test_delete_closes_bound_session_and_releases_binding(
    device_api, token, worker, upload, fake, conn
):
    cid, session = await _bound(device_api, token, worker, upload, fake)
    d = await device_api.delete(f"/conversations/{cid}", headers=bearer(token))
    assert d.status_code == 204
    assert fake.discarded == [session["id"]]
    (b,) = await rows(conn, "SELECT released_at, reason FROM bindings")
    assert b[0] is not None and b[1] == "conversation_deleted"
    (run,) = await rows(conn, "SELECT status, reason FROM runs")
    assert run == ("failed", "conversation_deleted")
    (row,) = await rows(conn, "SELECT status, reason FROM inbound")
    assert row == ("failed", "conversation_deleted")


async def test_delete_without_binding_calls_nothing(device_api, token, fake):
    cid = (await device_api.post("/conversations", headers=bearer(token))).json()["id"]
    assert (
        await device_api.delete(f"/conversations/{cid}", headers=bearer(token))
    ).status_code == 204
    assert fake.discarded == []


async def test_delete_never_discards_handoff_target(device_api, token, worker, upload, fake, conn):
    cid, session = await _bound(device_api, token, worker, upload, fake)
    other = str(uuid.uuid4())
    fake.sessions[other] = {**session, "id": other, "slug": "app-handoff-abc"}
    (device_id,) = await (await conn.execute("SELECT id FROM devices")).fetchone()
    await conn.execute(
        "INSERT INTO handoffs (id, conversation_id, device_id, client_id, fingerprint, "
        "spec_version, target_session_id, session_slug) "
        "VALUES (%s, %s, %s, %s, 'x', 1, %s, 'app-handoff-abc')",
        (uuid.uuid4(), cid, device_id, uuid.uuid4(), other),
    )
    d = await device_api.delete(f"/conversations/{cid}", headers=bearer(token))
    assert d.status_code == 204
    assert fake.discarded == [session["id"]]


@pytest.mark.parametrize("status", [404, 503])
async def test_delete_still_204_when_orchestrator_refuses(
    device_api, token, worker, upload, fake, conn, status
):
    cid, _ = await _bound(device_api, token, worker, upload, fake)
    fake.fail_next("discard_session", status)
    d = await device_api.delete(f"/conversations/{cid}", headers=bearer(token))
    assert d.status_code == 204
    (b,) = await rows(conn, "SELECT reason FROM bindings")
    assert b == ("conversation_deleted",)
