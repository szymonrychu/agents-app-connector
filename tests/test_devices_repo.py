import hashlib

from agents_app_connector.db import devices


async def test_token_stored_hashed_only(conn):
    row, token = await devices.create(conn, "sketch", "note-air")
    stored = await (await conn.execute("SELECT token_sha256 FROM devices")).fetchone()
    assert stored[0] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in stored[0]
    assert row.name == "note-air" and row.plugin == "sketch" and row.revoked_at is None


async def test_by_token_finds_active_device(conn):
    row, token = await devices.create(conn, "sketch", "a")
    found = await devices.by_token(conn, token)
    assert found is not None and found.id == row.id
    assert await devices.by_token(conn, token + "x") is None


async def test_by_token_ignores_revoked(conn):
    row, token = await devices.create(conn, "sketch", "a")
    assert await devices.revoke(conn, row.id) is True
    assert await devices.by_token(conn, token) is None
    assert await devices.revoke(conn, row.id) is False


async def test_list_all_includes_revoked(conn):
    a, _ = await devices.create(conn, "sketch", "a")
    b, _ = await devices.create(conn, "sketch", "b")
    await devices.revoke(conn, a.id)
    rows = await devices.list_all(conn)
    assert [r.name for r in rows] == ["a", "b"]
    assert rows[0].revoked_at is not None and rows[1].revoked_at is None


def test_new_token_is_random_and_long():
    assert devices.new_token() != devices.new_token()
    assert len(devices.new_token()) >= 43
    assert devices.hash_token("x") == hashlib.sha256(b"x").hexdigest()
