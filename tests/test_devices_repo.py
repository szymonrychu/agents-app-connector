import hashlib

from helpers import paired

from agents_app_connector.db import device_tokens, devices


async def test_create_has_no_token_and_is_pending(conn):
    row = await devices.create(conn, "sketch", "note-air")
    assert row.name == "note-air" and row.plugin == "sketch" and row.revoked_at is None
    assert (await (await conn.execute("SELECT count(*) FROM device_tokens")).fetchone())[0] == 0
    info = (await devices.list_all(conn))[0]
    assert info.state == "pending" and info.paired_at is None and info.code_status is None


async def test_token_stored_hashed_only(conn):
    row, token = await paired(conn, "sketch", "note-air")
    stored = await (await conn.execute("SELECT token_sha256 FROM device_tokens")).fetchone()
    assert stored[0] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in stored[0]
    assert (await devices.list_all(conn))[0].state == "active"
    assert row.name == "note-air"


async def test_by_token_finds_active_device(conn):
    row, token = await paired(conn, "sketch", "a")
    found = await device_tokens.by_token(conn, token)
    assert found is not None and found.id == row.id
    assert await device_tokens.by_token(conn, token + "x") is None


async def test_by_token_ignores_revoked(conn):
    row, token = await paired(conn, "sketch", "a")
    assert await devices.revoke(conn, row.id) is True
    assert await device_tokens.by_token(conn, token) is None
    assert await devices.revoke(conn, row.id) is False
    assert (await devices.list_all(conn))[0].state == "revoked"


async def test_list_all_includes_revoked(conn):
    a, _ = await paired(conn, "sketch", "a")
    b, _ = await paired(conn, "sketch", "b")
    await devices.revoke(conn, a.id)
    rows = await devices.list_all(conn)
    assert [r.name for r in rows] == ["a", "b"]
    assert rows[0].revoked_at is not None and rows[1].revoked_at is None


def test_new_token_is_random_and_long():
    assert device_tokens.new_token() != device_tokens.new_token()
    assert len(device_tokens.new_token()) >= 43
    assert device_tokens.hash_token("x") == hashlib.sha256(b"x").hexdigest()
