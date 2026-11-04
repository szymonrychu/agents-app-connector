import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from agents_app_connector.db import device_tokens, devices, pairing

MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "src/agents_app_connector/migrations/0003_pairing_codes.sql"
)


async def device_with_code(conn, name="d", ttl=600, max_pending=3):
    dev = await devices.create(conn, "sketch", name)
    issued = await pairing.issue(conn, dev.id, ttl, max_pending)
    return dev, issued


def test_codes_are_six_digits_and_random():
    codes = {pairing.new_code() for _ in range(50)}
    assert all(re.fullmatch(r"[0-9]{6}", c) for c in codes) and len(codes) > 1
    assert "secrets.randbelow" in Path(pairing.__file__).read_text()


async def test_issue_stores_hash_only(conn):
    dev, issued = await device_with_code(conn)
    rows = await (await conn.execute("SELECT code_sha256, status FROM pairing_codes")).fetchall()
    assert rows == [(pairing.hash_code(issued.code), "pending")]
    assert issued.expires_at > datetime.now(UTC) + timedelta(seconds=590)
    assert dev.id


async def test_redeem_is_single_use(conn):
    dev, issued = await device_with_code(conn)
    assert await pairing.redeem(conn, issued.code, "sketch", "10.0.0.1") == dev.id
    assert await pairing.redeem(conn, issued.code, "sketch", "10.0.0.1") is None
    row = await (
        await conn.execute("SELECT status, redeemed_ip, redeemed_at FROM pairing_codes")
    ).fetchone()
    assert row[0] == "redeemed" and row[1] == "10.0.0.1" and row[2] is not None


async def test_concurrent_redeems_give_exactly_one_winner(url):
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as setup:
        dev, issued = await device_with_code(setup)

    async def attempt():
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as c:
            return await pairing.redeem(c, issued.code, "sketch", "1.1.1.1")

    results = await asyncio.gather(*(attempt() for _ in range(8)))
    assert results.count(dev.id) == 1 and results.count(None) == 7


async def test_redeem_refuses_expired_revoked_other_plugin_and_paired(conn):
    dev, issued = await device_with_code(conn, "a", ttl=60)
    await conn.execute("UPDATE pairing_codes SET expires_at = now() - interval '1 second'")
    assert await pairing.redeem(conn, issued.code, "sketch", "ip") is None

    dev2, issued2 = await device_with_code(conn, "b")
    await devices.revoke(conn, dev2.id)
    assert await pairing.redeem(conn, issued2.code, "sketch", "ip") is None

    dev3, issued3 = await device_with_code(conn, "c")
    assert await pairing.redeem(conn, issued3.code, "other", "ip") is None

    dev4, issued4 = await device_with_code(conn, "d")
    await device_tokens.issue(conn, dev4.id)
    assert await pairing.redeem(conn, issued4.code, "sketch", "ip") is None


async def test_only_the_newest_code_of_a_device_redeems(conn):
    dev, first = await device_with_code(conn)
    await conn.execute("UPDATE pairing_codes SET created_at = now() - interval '1 minute'")
    second = await pairing.issue(conn, dev.id, 600, 3)
    assert await pairing.redeem(conn, first.code, "sketch", "ip") is None
    assert await pairing.redeem(conn, second.code, "sketch", "ip") == dev.id


async def test_failures_lock_a_pending_code_durably(conn):
    dev, issued = await device_with_code(conn)
    for _ in range(4):
        assert await pairing.record_failure(conn, 5) == 0
    assert await pairing.record_failure(conn, 5) == 1
    status = (await (await conn.execute("SELECT status FROM pairing_codes")).fetchone())[0]
    assert status == "locked"
    assert await pairing.redeem(conn, issued.code, "sketch", "ip") is None


async def test_issue_limits_pending_devices(conn):
    for i in range(3):
        await device_with_code(conn, f"d{i}")
    extra = await devices.create(conn, "sketch", "extra")
    with pytest.raises(pairing.TooManyPending):
        await pairing.issue(conn, extra.id, 600, 3)
    # a device that already waits may ask again
    first = (await devices.list_all(conn))[0]
    await pairing.issue(conn, first.id, 600, 3)


async def test_issue_regenerates_on_collision_with_a_live_code(conn, monkeypatch):
    codes = iter(["111111", "111111", "222222"])
    monkeypatch.setattr(pairing, "new_code", lambda: next(codes))
    _, first = await device_with_code(conn, "a")
    _, second = await device_with_code(conn, "b")
    assert (first.code, second.code) == ("111111", "222222")


async def test_sweep_marks_expired_and_superseded_and_purge_deletes(conn):
    dev, _ = await device_with_code(conn, "a")
    await conn.execute("UPDATE pairing_codes SET created_at = now() - interval '1 minute'")
    await pairing.issue(conn, dev.id, 600, 3)
    dev2, _ = await device_with_code(conn, "b")
    await conn.execute(
        "UPDATE pairing_codes SET expires_at = now() - interval '1 second' WHERE device_id = %s",
        (dev2.id,),
    )
    assert await pairing.sweep(conn) == 2
    cur = await conn.execute("SELECT status FROM pairing_codes")
    assert sorted(r[0] for r in await cur.fetchall()) == ["expired", "pending", "superseded"]
    assert await pairing.purge(conn, datetime.now(UTC) + timedelta(days=1)) == 2
    assert (await (await conn.execute("SELECT count(*) FROM pairing_codes")).fetchone())[0] == 1


async def test_migration_moves_existing_token_hashes(conn):
    """Replay 0003 on the 0002 shape: existing devices keep their token hash."""
    await conn.execute("DROP TABLE pairing_codes, device_tokens")
    await conn.execute("ALTER TABLE devices ADD COLUMN token_sha256 text")
    did, digest = uuid.uuid4(), "ab" * 32
    await conn.execute(
        "INSERT INTO devices (id, plugin, name, token_sha256) VALUES (%s, 'sketch', 'old', %s)",
        (did, digest),
    )
    await conn.execute(MIGRATION.read_text(encoding="ascii"))
    row = await (await conn.execute("SELECT device_id, token_sha256 FROM device_tokens")).fetchone()
    assert row == (did, digest)
    cols = await (
        await conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'devices'"
        )
    ).fetchall()
    assert "token_sha256" not in {c[0] for c in cols}
