import uuid

import pytest

from agents_app_connector.db import conversations, devices, events, holds, runs, specs
from agents_app_connector.run_store import AppRunStore


async def conv(conn):
    dev, _ = await devices.create(conn, "sketch", "d")
    return await conversations.create(conn, dev.id, "sketch")


async def test_create_set_finish_guarded(conn):
    cid = await conv(conn)
    ref = await runs.create(conn, conversation_id=cid, kind="first", preset_id=uuid.uuid4())
    assert ref.slug == f"app-{ref.id}" and ref.session_id is None
    sid = uuid.uuid4()
    assert await runs.set_session(conn, ref.id, sid) == ref.slug
    assert await runs.set_message(conn, ref.id, "7")
    assert await runs.count_first_in_progress(conn) == 1
    (row,) = await runs.in_progress(conn)
    assert row.session_id == sid and row.message_id == "7"
    assert await runs.finish(conn, ref.id, "failed", "x")
    assert not await runs.finish(conn, ref.id, "handled")
    assert await runs.set_session(conn, ref.id, uuid.uuid4()) is None
    assert not await runs.set_message(conn, ref.id, "8")
    assert await runs.in_progress(conn) == []
    with pytest.raises(ValueError):
        await runs.finish(conn, ref.id, "queued")


async def test_has_other_run(conn):
    cid = await conv(conn)
    sid = uuid.uuid4()
    a = await runs.create(conn, conversation_id=cid, kind="first", preset_id=None)
    await runs.set_session(conn, a.id, sid)
    assert not await runs.has_other_run(conn, sid, cid, a.id)
    b = await runs.create(conn, conversation_id=cid, kind="follow", preset_id=None)
    await runs.set_session(conn, b.id, sid)
    assert await runs.has_other_run(conn, sid, cid, b.id)


async def test_holds_set_and_active(conn):
    from datetime import UTC, datetime, timedelta

    cid = await conv(conn)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await holds.set(conn, cid, now + timedelta(hours=1), "usage_quota")
    assert await holds.active(conn, now) == {cid}
    await holds.set(conn, cid, now - timedelta(hours=1), "usage_quota")
    assert await holds.active(conn, now) == set()


async def test_events_after_and_specs_latest(conn):
    cid = await conv(conn)
    a = await events.append(conn, cid, "message", {"n": 1})
    b = await events.append(conn, cid, "message", {"n": 2})
    assert [e.id for e in await events.after(conn, cid, 0)] == [a, b]
    assert [e.payload for e in await events.after(conn, cid, a)] == [{"n": 2}]
    assert [e.id for e in await events.after(conn, cid, 0, limit=1)] == [a]
    assert await specs.latest(conn, cid) is None
    await conn.execute(
        "INSERT INTO specs (conversation_id, version, markdown) VALUES (%s, 1, 'a'), (%s, 2, 'b')",
        (cid, cid),
    )
    latest = await specs.latest(conn, cid)
    assert (latest.version, latest.markdown) == (2, "b")


async def test_run_store_finish_without_session_releases_reservation(pool, conn):
    from agents_app_connector.db import bindings

    cid = await conv(conn)
    ref = await runs.create(conn, conversation_id=cid, kind="first", preset_id=None)
    assert await bindings.reserve(conn, cid, ref.id)
    store = AppRunStore(pool)
    assert await store.finish(ref.id, "failed", "boom")
    assert await bindings.active(conn, cid) is None
    assert not await store.finish(ref.id, "failed", "again")
