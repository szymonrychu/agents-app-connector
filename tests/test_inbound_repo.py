import uuid

from helpers import paired

from agents_app_connector.db import conversations, inbound


def test_fingerprint_canonical():
    a = inbound.fingerprint({"b": 1, "a": 2}, ["x", "y"])
    assert a == inbound.fingerprint({"a": 2, "b": 1}, ["x", "y"])
    assert a != inbound.fingerprint({"a": 2, "b": 1}, ["y", "x"])
    assert a != inbound.fingerprint({"a": 2, "b": 2}, ["x", "y"])
    assert len(a) == 64


async def make(conn, conv, dev, kind="message"):
    iid = uuid.uuid4()
    await inbound.insert(
        conn,
        id=iid,
        conversation_id=conv,
        device_id=dev,
        client_id=uuid.uuid4(),
        fingerprint="f",
        kind=kind,
        text="t",
        meta={},
    )
    return iid


async def setup(conn):
    dev, _ = await paired(conn, "sketch", "d")
    return dev.id, await conversations.create(conn, dev.id, "sketch")


async def test_insert_and_by_client_id(conn):
    dev, conv = await setup(conn)
    iid = uuid.uuid4()
    cid = uuid.uuid4()
    await inbound.insert(
        conn, id=iid, conversation_id=conv, device_id=dev, client_id=cid,
        fingerprint="fp", kind="message", text="hi", meta={"a": 1},
    )  # fmt: skip
    row = await inbound.by_client_id(conn, dev, cid)
    assert row.id == iid and row.status == "queued" and row.meta == {"a": 1}
    assert await inbound.by_client_id(conn, dev, uuid.uuid4()) is None


async def test_claim_oldest_guarded_and_finish_requeue(conn):
    dev, conv = await setup(conn)
    first = await make(conn, conv, dev)
    await make(conn, conv, dev)
    row = await inbound.claim_oldest(conn, conv, 7)
    assert row.id == first and row.status == "in-progress" and row.run_id == 7
    assert await inbound.requeue(conn, 7) == 1
    assert (await inbound.claim_oldest(conn, conv, 8)).id == first
    assert await inbound.finish(conn, 8, "handled", None) == 1
    assert await inbound.finish(conn, 8, "failed", "x") == 0
    assert await inbound.claim_oldest(conn, uuid.uuid4(), 9) is None


async def test_next_queued_rules(conn):
    dev, a = await setup(conn)
    b = await conversations.create(conn, dev, "sketch")
    c = await conversations.create(conn, dev, "sketch")
    d = await conversations.create(conn, dev, "sketch")
    await make(conn, a, dev)
    await make(conn, b, dev)
    await make(conn, c, dev)
    await make(conn, d, dev)
    assert await inbound.next_queued(conn) == [a, b, c, d]
    await inbound.claim_oldest(conn, a, 1)
    await make(conn, a, dev)  # a has in-progress row
    await conn.execute(
        "INSERT INTO holds (conversation_id, until) VALUES (%s, now() + interval '1 hour')", (b,)
    )
    await conversations.mark_deleted(conn, c)
    assert await inbound.next_queued(conn) == [d]
    await conn.execute("UPDATE holds SET until = now() - interval '1 minute'")
    assert await inbound.next_queued(conn) == [b, d]


async def test_status_changes_are_guarded(conn):
    dev, conv = await setup(conn)
    first = await make(conn, conv, dev)
    second = await make(conn, conv, dev)
    await inbound.claim_oldest(conn, conv, 5)  # queued -> in-progress (oldest)
    await inbound.finish(conn, 5, "handled", None)
    # a handled row is not requeued or refinished by run id
    assert await inbound.requeue(conn, 5) == 0
    assert await inbound.finish(conn, 5, "failed", "x") == 0
    row = await conn.execute("SELECT status FROM inbound WHERE run_id = 5")
    assert (await row.fetchone())[0] == "handled"
    # fail_queued_rows only touches queued rows
    failed = await inbound.fail_queued_rows(conn, conv, "no_preset")
    assert [r.id for r in failed] == [second] and first != second
    assert all(r.status == "failed" and r.reason == "no_preset" for r in failed)
    cur = await conn.execute("SELECT count(*) FROM inbound WHERE status = 'handled'")
    assert (await cur.fetchone())[0] == 1
    assert await inbound.fail_queued_rows(conn, conv, "again") == []
