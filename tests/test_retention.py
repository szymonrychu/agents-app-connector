import uuid
from datetime import UTC, datetime, timedelta

import pytest
from helpers import bearer, count, stored_files

from agents_app_connector.db import attachments, events
from agents_app_connector.retention import PurgeResult, Retention, purge


def now():
    return datetime.now(UTC)


async def expire_all(conn, days=1):
    await conn.execute("UPDATE attachments SET expires_at = now() - %s * interval '1 day'", (days,))


async def test_expired_files_purged_specs_kept(pool, conn, store, upload, device_api, token):
    sketch_id, conv = await upload()
    await conn.execute(
        "INSERT INTO specs (conversation_id, version, markdown) VALUES (%s, 1, 'x')", (conv,)
    )
    await expire_all(conn)
    size = (await (await conn.execute("SELECT sum(size) FROM attachments")).fetchone())[0]
    result = await purge(pool, store, now())
    assert result == PurgeResult(files=2, bytes=int(size))
    assert stored_files(store) == []
    assert await count(conn, "attachments") == 2
    cur = await conn.execute("SELECT count(*) FROM attachments WHERE purged_at IS NULL")
    assert (await cur.fetchone())[0] == 0
    assert await count(conn, "specs") == 1
    r = await device_api.get(f"/attachments/{sketch_id}", headers=bearer(token))
    assert r.status_code == 410
    assert await purge(pool, store, now()) == PurgeResult(files=0, bytes=0)


async def test_not_expired_kept(pool, conn, store, upload):
    await upload()
    assert await purge(pool, store, now()) == PurgeResult(files=0, bytes=0)
    assert len(stored_files(store)) == 2


async def test_missing_file_still_marked_purged(pool, conn, store, upload):
    await upload()
    for f in stored_files(store):
        f.unlink()
    await expire_all(conn)
    result = await purge(pool, store, now())
    assert result.files == 2


async def test_purge_frees_device_quota(pool, conn, store, upload):
    await upload()
    device_id = (await (await conn.execute("SELECT id FROM devices")).fetchone())[0]
    assert await attachments.device_usage_bytes(conn, device_id) > 0
    await expire_all(conn)
    await purge(pool, store, now())
    assert await attachments.device_usage_bytes(conn, device_id) == 0


async def test_old_rows_deleted_live_rows_kept(pool, conn, store, upload):
    _, live = await upload()
    _, dead = await upload(conv="new")
    await conn.execute("UPDATE conversations SET state = 'deleted' WHERE id = %s", (dead,))
    old = now() - timedelta(days=31)
    await conn.execute("UPDATE events SET created_at = %s", (old,))
    await events.append(conn, dead, "message", {})  # recent event of a deleted conversation
    await conn.execute(
        "INSERT INTO runs (conversation_id, kind, status, finished_at) VALUES "
        "(%s, 'first', 'handled', %s), (%s, 'first', 'handled', now()), "
        "(%s, 'first', 'in-progress', NULL)",
        (live, old, live, live),
    )
    dev = (await (await conn.execute("SELECT id FROM devices")).fetchone())[0]
    for status, created in (("handled", old), ("handled", now()), ("queued", old)):
        await conn.execute(
            "INSERT INTO handoffs (id, conversation_id, device_id, client_id, fingerprint, "
            "spec_version, target_preset, status, created_at) "
            "VALUES (%s, %s, %s, %s, 'f', 1, 'p', %s, %s)",
            (uuid.uuid4(), live, dev, uuid.uuid4(), status, created),
        )
    await purge(pool, store, now())
    cur = await conn.execute("SELECT conversation_id FROM events")
    left = {r[0] for r in await cur.fetchall()}
    assert left == {uuid.UUID(live), uuid.UUID(dead)}
    cur = await conn.execute("SELECT count(*) FROM events WHERE conversation_id = %s", (dead,))
    assert (await cur.fetchone())[0] == 1
    assert await count(conn, "runs") == 2
    cur = await conn.execute("SELECT status FROM handoffs ORDER BY created_at")
    assert sorted(r[0] for r in await cur.fetchall()) == ["handled", "queued"]
    assert await count(conn, "conversations") == 2
    assert await count(conn, "inbound") == 2


async def test_purge_once_per_day(pool, conn, store, upload):
    clock = {"now": now()}
    ret = Retention(pool, store, now=lambda: clock["now"])
    await upload()
    await expire_all(conn)
    await ret.tick()
    assert len(stored_files(store)) == 0
    await upload(conv="new")
    await expire_all(conn)
    clock["now"] += timedelta(hours=23)
    await ret.tick()
    assert len(stored_files(store)) == 2
    clock["now"] += timedelta(hours=2)
    await ret.tick()
    assert len(stored_files(store)) == 0


@pytest.mark.parametrize("days", [1, 7])
async def test_retention_days_applies_to_rows(pool, conn, store, upload, days):
    _, live = await upload()
    await conn.execute(
        "INSERT INTO runs (conversation_id, kind, status, finished_at) "
        "VALUES (%s, 'first', 'handled', now() - interval '3 days')",
        (live,),
    )
    await purge(pool, store, now(), retention_days=days)
    assert await count(conn, "runs") == (0 if days == 1 else 1)


async def test_settled_pairing_codes_age_out_and_pending_ones_stay(pool, conn, store):
    from agents_app_connector.db import devices, pairing
    from agents_app_connector.retention import PairingSweep

    old = await devices.create(conn, "sketch", "old")
    await pairing.issue(conn, old.id, 600, 3)
    await conn.execute("UPDATE pairing_codes SET expires_at = now() - interval '1 second'")
    assert await PairingSweep(pool).tick() == 1
    await conn.execute("UPDATE pairing_codes SET created_at = now() - interval '40 days'")
    live = await devices.create(conn, "sketch", "live")
    await pairing.issue(conn, live.id, 600, 3)
    await purge(pool, store, now(), retention_days=30)
    rows = await (await conn.execute("SELECT status FROM pairing_codes")).fetchall()
    assert rows == [("pending",)]
