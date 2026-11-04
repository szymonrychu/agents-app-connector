import uuid

import psycopg
import pytest

TABLES = {
    "devices": "writer: admin",
    "device_tokens": "writer: app",
    "pairing_codes": "queue: insert by admin, transitions by app",
    "conversations": "writer: app",
    "inbound": "queue: insert by app api, transitions by app worker",
    "attachments": "writer: app",
    "runs": "writer: app",
    "bindings": "writer: app",
    "holds": "writer: app",
    "specs": "writer: app",
    "events": "writer: app",
    "handoffs": "queue: insert by app api, transitions by app worker",
}
SHA = "a" * 64


async def _device(conn):
    did = uuid.uuid4()
    await conn.execute("INSERT INTO devices (id, plugin, name) VALUES (%s, 'sketch', 'd')", (did,))
    return did


async def _conversation(conn, did):
    cid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO conversations (id, device_id, plugin, state) "
        "VALUES (%s, %s, 'sketch', 'open')",
        (cid, did),
    )
    return cid


async def test_tables_and_writers(conn):
    cur = await conn.execute(
        "SELECT c.relname, obj_description(c.oid, 'pg_class') FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> 'schema_migrations'"
    )
    comments = dict(await cur.fetchall())
    assert {k: v for k, v in comments.items() if k in TABLES} == TABLES
    assert not {k for k in comments if k not in TABLES and not k.startswith("_")}


async def test_client_id_unique_per_device(conn):
    did = await _device(conn)
    cid = await _conversation(conn, did)
    client_id = uuid.uuid4()
    sql = (
        "INSERT INTO inbound (id, conversation_id, device_id, client_id, fingerprint, kind, "
        "status) "
        "VALUES (%s, %s, %s, %s, 'f', 'sketch', 'queued')"
    )
    await conn.execute(sql, (uuid.uuid4(), cid, did, client_id))
    with pytest.raises(psycopg.errors.UniqueViolation):
        await conn.execute(sql, (uuid.uuid4(), cid, did, client_id))


async def test_one_active_binding(conn):
    cid = await _conversation(conn, await _device(conn))
    sql = "INSERT INTO bindings (conversation_id) VALUES (%s)"
    await conn.execute(sql, (cid,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        await conn.execute(sql, (cid,))
    await conn.execute("UPDATE bindings SET released_at = now(), reason = 'x'")
    await conn.execute(sql, (cid,))


async def test_handoff_needs_one_target(conn):
    did = await _device(conn)
    cid = await _conversation(conn, did)
    sql = (
        "INSERT INTO handoffs (id, conversation_id, device_id, client_id, fingerprint, "
        "spec_version, target_session_id, target_preset, status) "
        "VALUES (%s, %s, %s, %s, 'f', 1, %s, %s, 'queued')"
    )
    sid = uuid.uuid4()
    for a, b in [(None, None), (sid, "p")]:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(sql, (uuid.uuid4(), cid, did, uuid.uuid4(), a, b))
    await conn.execute(sql, (uuid.uuid4(), cid, did, uuid.uuid4(), sid, None))
    await conn.execute(sql, (uuid.uuid4(), cid, did, uuid.uuid4(), None, "p"))


async def test_constraints_and_cascade(conn):
    did = await _device(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        await conn.execute(
            "INSERT INTO device_tokens (device_id, token_sha256) VALUES (%s, 'zz')", (did,)
        )
    cid = await _conversation(conn, did)
    iid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO inbound (id, conversation_id, device_id, client_id, fingerprint, kind, "
        "status) "
        "VALUES (%s, %s, %s, %s, 'f', 'sketch', 'queued')",
        (iid, cid, did, uuid.uuid4()),
    )
    await conn.execute(
        "INSERT INTO attachments (id, inbound_id, conversation_id, device_id, kind, mime, size, "
        "sha256, path, expires_at) "
        "VALUES (%s, %s, %s, %s, 'sketch_png', 'image/png', 1, %s, 'p', now())",
        (uuid.uuid4(), iid, cid, did, SHA),
    )
    await conn.execute(
        "INSERT INTO specs (conversation_id, version, markdown, inbound_id, sketch_ids, "
        "front_matter) VALUES (%s, 1, '# t', %s, %s, true)",
        (cid, iid, [iid]),
    )
    await conn.execute(
        "INSERT INTO events (conversation_id, type, payload) VALUES (%s, 'spec', '{}')", (cid,)
    )
    await conn.execute("DELETE FROM conversations WHERE id = %s", (cid,))
    for t in ("inbound", "attachments", "specs"):
        cur = await conn.execute(f"SELECT count(*) FROM {t}")  # noqa: S608
        assert (await cur.fetchone())[0] == 0
