import uuid

from agents_app_connector.db import bindings, conversations, devices
from agents_app_connector.run_store import AppRunStore


async def conv(conn):
    dev, _ = await devices.create(conn, "sketch", "d")
    return await conversations.create(conn, dev.id, "sketch")


async def test_reserve_fill_release(conn):
    cid = await conv(conn)
    assert await bindings.active(conn, cid) is None
    assert await bindings.reserve(conn, cid, 1)
    assert not await bindings.reserve(conn, cid, 2)
    b = await bindings.active(conn, cid)
    assert b.session_id is None and b.run_id == 1
    sid = uuid.uuid4()
    assert await bindings.fill(conn, 1, sid, "app-1")
    assert await bindings.fill(conn, 1, sid, "app-1")
    assert not await bindings.fill(conn, 1, uuid.uuid4(), "app-1")
    assert not await bindings.release_reservation(conn, 1, "run_failed")
    b = await bindings.active(conn, cid)
    assert b.session_id == sid and b.slug == "app-1"
    assert await bindings.release(conn, b.id, "session_gone")
    assert not await bindings.release(conn, b.id, "session_gone")
    assert await bindings.active(conn, cid) is None
    assert await bindings.reserve(conn, cid, 3)


async def test_release_reservation(conn):
    cid = await conv(conn)
    await bindings.reserve(conn, cid, 5)
    assert await bindings.release_reservation(conn, 5, "run_failed")
    assert await bindings.active(conn, cid) is None


async def test_run_store_set_session_fills_binding(pool, conn):
    from agents_app_connector.db import runs

    cid = await conv(conn)
    ref = await runs.create(conn, conversation_id=cid, kind="first", preset_id=None)
    await bindings.reserve(conn, cid, ref.id)
    sid = str(uuid.uuid4())
    await AppRunStore(pool).set_session(ref.id, sid)
    b = await bindings.active(conn, cid)
    assert str(b.session_id) == sid and b.slug == ref.slug
