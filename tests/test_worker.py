import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from helpers import files, meta, only, pages, png, post, rows

from agents_app_connector.db import bindings, conversations, inbound
from agents_app_connector.run_store import AppRunStore
from agents_app_connector.worker import Worker

PNG = png()


@pytest.fixture
def worker(orchestrator, pool, settings, store, preset):
    return Worker(orchestrator, pool, settings, store)


async def finish_all(pool):
    """Stand-in for the reply tracker: settle every in-progress run as handled."""
    store = AppRunStore(pool)
    async with pool.connection() as conn:
        ids = [r[0] for r in await rows(conn, "SELECT id FROM runs WHERE status = 'in-progress'")]
    for run_id in ids:
        await store.finish(run_id, "handled", None)


async def test_first_run_creates_session_with_attachments(worker, fake, upload, conn):
    sketch_id, _ = await upload(caption="login")
    await worker.tick()
    msg = only(fake.messages.values())
    atts = fake.attachments[msg["id"]]
    assert [a.name.split("-")[0] for a in atts] == ["sketch", "strokes"]
    assert atts[0].data == PNG
    assert "<!-- agents-app:" in msg["text"]
    assert "Caption: login" in msg["text"]
    session = only(fake.sessions.values())
    assert session["slug"].startswith("app-")
    (run,) = await rows(conn, "SELECT kind, slug, session_id, message_id, inbound_id FROM runs")
    assert run[0] == "first" and run[1] == session["slug"] and str(run[2]) == session["id"]
    assert run[3] == str(msg["id"]) and str(run[4]) == sketch_id
    (row,) = await rows(conn, "SELECT status FROM inbound")
    assert row[0] == "in-progress"
    (b,) = await rows(conn, "SELECT session_id, slug FROM bindings WHERE released_at IS NULL")
    assert str(b[0]) == session["id"] and b[1] == session["slug"]


async def test_translating_event_emitted(worker, upload, say, conn):
    sketch_id, conv = await upload()
    await worker.tick()
    await finish_all(worker._pool)
    message_id = await say(conv, "make it blue")
    await worker.tick()
    evs = await rows(
        conn,
        "SELECT type, payload FROM events WHERE payload->>'status' = 'translating' ORDER BY id",
    )
    assert evs[0] == ("sketch_status", {"sketch_id": sketch_id, "status": "translating"})
    assert evs[1] == ("message", {"message_id": message_id, "status": "translating"})


async def test_second_sketch_follows_same_session(worker, fake, upload, pool, conn):
    await upload()
    await worker.tick()
    await finish_all(pool)
    await upload()
    await worker.tick()
    assert len(fake.sessions) == 1 and len(fake.messages) == 2
    kinds = await rows(conn, "SELECT kind FROM runs ORDER BY id")
    assert kinds == [("first",), ("follow",)]
    second = list(fake.messages.values())[1]
    assert len(fake.attachments[second["id"]]) == 2


async def test_one_row_in_progress_blocks_next(worker, fake, upload):
    await upload()
    await upload()
    await worker.tick()
    await worker.tick()
    assert len(fake.messages) == 1


async def test_second_tick_while_run_in_progress_creates_no_run(worker, fake, upload, conn):
    sketch_id, _ = await upload()
    await worker.tick()
    await asyncio.gather(worker.tick(), worker.tick())
    await worker.tick()
    (row,) = await rows(conn, "SELECT status FROM inbound WHERE id = %s", sketch_id)
    assert row == ("in-progress",)
    run_rows = await rows(conn, "SELECT id FROM runs WHERE inbound_id = %s", sketch_id)
    assert len(run_rows) == 1
    assert len(fake.messages) == 1


async def test_retry_after_crash_posts_once(worker, fake, upload, conn):
    await upload()
    fake.fail_next("post_message", 503)
    await worker.tick()
    assert not fake.messages and len(fake.sessions) == 1
    await worker.tick()
    await worker.tick()
    assert len(fake.messages) == 1 and len(fake.sessions) == 1
    assert len(await rows(conn, "SELECT 1 FROM runs")) == 1


async def test_retry_after_create_failure_posts_once(worker, fake, upload, conn):
    await upload()
    fake.fail_next("create_session", 503)
    await worker.tick()
    assert not fake.sessions
    await worker.tick()
    assert len(fake.sessions) == 1 and len(fake.messages) == 1
    assert len(await rows(conn, "SELECT 1 FROM runs")) == 1


async def test_concurrent_ticks_create_one_run_per_row(worker, fake, upload, conn):
    await upload()
    await asyncio.gather(worker.tick(), worker.tick())
    await worker.tick()
    assert len(await rows(conn, "SELECT 1 FROM runs")) == 1
    assert len(fake.messages) == 1


async def test_claim_is_status_guarded(pool, upload, conn):
    _, conv_id = await upload()
    import uuid

    first = await inbound.claim_oldest(conn, uuid.UUID(conv_id), 1)
    assert first is not None
    assert await inbound.claim_oldest(conn, uuid.UUID(conv_id), 2) is None


async def test_gone_session_restarts_with_previous_spec(worker, fake, upload, say, pool, conn):
    sketch_id, conv = await upload()
    await worker.tick()
    await conn.execute(
        "INSERT INTO specs (conversation_id, version, markdown, inbound_id, sketch_ids) "
        "VALUES (%s, 1, '# Login spec', %s, %s)",
        (conv, sketch_id, [sketch_id]),
    )
    await finish_all(pool)
    first_session = only(fake.sessions.values())["id"]
    fake.discard_session(first_session)
    await say(conv, "also add a logo")
    await worker.tick()
    assert len(fake.sessions) == 2
    new = [s for s in fake.sessions.values() if s["id"] != first_session][0]
    msg = [m for m in fake.messages.values() if m["session_id"] == new["id"]][0]
    assert "Previous spec version 1" in msg["text"] and "# Login spec" in msg["text"]
    assert "User feedback: also add a logo" in msg["text"]
    atts = fake.attachments[msg["id"]]
    assert atts[0].data == PNG and atts[0].name.startswith("sketch-")
    (b,) = await rows(conn, "SELECT session_id FROM bindings WHERE released_at IS NULL")
    assert str(b[0]) == new["id"]
    released = await rows(conn, "SELECT reason FROM bindings WHERE released_at IS NOT NULL")
    assert released == [("session_gone",)]


async def test_session_gone_between_sketches_carries_latest_spec_and_sketch(
    worker, fake, upload, pool, conn
):
    sketch_id, conv = await upload()
    await worker.tick()
    await conn.execute(
        "INSERT INTO specs (conversation_id, version, markdown, inbound_id, sketch_ids) "
        "VALUES (%s, 1, '# Spec one', %s, %s)",
        (conv, sketch_id, [sketch_id]),
    )
    await finish_all(pool)
    fake.remove_session(only(fake.sessions.values())["id"])
    png2 = png(300, 200)
    sketch2, _ = await upload(png_bytes=png2)
    await worker.tick()
    (session,) = fake.sessions.values()
    msg = only(fake.messages.values())
    assert "Previous spec version 1" in msg["text"]
    atts = fake.attachments[msg["id"]]
    assert atts[0].data == png2 and sketch2 in atts[0].name


async def test_restart_skips_unreadable_sketch_file(worker, fake, upload, say, pool, store, conn):
    sketch_id, conv = await upload()
    await worker.tick()
    await conn.execute(
        "INSERT INTO specs (conversation_id, version, markdown, inbound_id, sketch_ids) "
        "VALUES (%s, 1, '# Spec one', %s, %s)",
        (conv, sketch_id, [sketch_id]),
    )
    await finish_all(pool)
    fake.discard_session(only(fake.sessions.values())["id"])
    for path in store.root.rglob("*"):
        if path.is_file():
            path.unlink()
    (store.root / "dir-instead").mkdir()
    # a path that is a directory raises IsADirectoryError, an OSError that is not FileNotFound
    await conn.execute("UPDATE attachments SET path = 'dir-instead' WHERE kind = 'sketch_strokes'")
    await say(conv, "again")
    await worker.tick()
    msg = [m for m in fake.messages.values() if m["status"] == "queued" and m["id"] == 2][0]
    assert "(sketch file no longer stored)" in msg["text"]
    assert "Previous spec version 1" in msg["text"]
    assert msg["id"] not in fake.attachments


async def test_purged_attachment_skipped(worker, fake, upload, conn):
    await upload()
    await conn.execute("UPDATE attachments SET purged_at = now() WHERE kind = 'sketch_strokes'")
    await worker.tick()
    msg = only(fake.messages.values())
    assert [a.name.split("-")[0] for a in fake.attachments[msg["id"]]] == ["sketch"]


async def test_usage_quota_holds_conversation(
    orchestrator, pool, settings, store, fake, upload, preset, conn
):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    worker = Worker(orchestrator, pool, settings, store, now=lambda: now)
    sketch_id, conv = await upload()
    resets = datetime.now(UTC) + timedelta(hours=1)
    fake.refuse_usage(resets)
    await worker.tick()
    (run,) = await rows(conn, "SELECT status, reason FROM runs")
    assert run == ("failed", "usage_quota")
    (row,) = await rows(conn, "SELECT status, run_id FROM inbound")
    assert row == ("queued", None)
    (hold,) = await rows(conn, "SELECT until FROM holds")
    assert hold[0] > now
    assert await rows(conn, "SELECT 1 FROM bindings WHERE released_at IS NULL") == []
    (ev,) = await rows(conn, "SELECT payload FROM events WHERE payload->>'reason' = 'usage_quota'")
    assert ev[0]["sketch_id"] == sketch_id and ev[0]["status"] == "queued" and ev[0]["retry_at"]
    await worker.tick()
    assert not fake.sessions and len(await rows(conn, "SELECT 1 FROM runs")) == 1
    later = Worker(orchestrator, pool, settings, store, now=lambda: hold[0] + timedelta(seconds=1))
    await later.tick()
    assert len(fake.sessions) == 1 and len(fake.messages) == 1


async def test_missing_preset_fails_rows(orchestrator, pool, settings, store, fake, upload, conn):
    fake.add_preset("other")
    sketch_id, conv = await upload()
    await upload()
    await Worker(orchestrator, pool, settings, store).tick()
    assert await rows(conn, "SELECT status, reason FROM inbound") == [("failed", "no_preset")] * 2
    assert not fake.sessions
    evs = await rows(conn, "SELECT payload FROM events WHERE payload->>'status' = 'failed'")
    assert len(evs) == 2 and evs[0][0]["reason"] == "no_preset"
    assert sketch_id in {e[0]["sketch_id"] for e in evs}


async def test_new_session_cap(
    orchestrator, pool, settings, store, fake, preset, device_api, token, conn
):
    from agents_app_connector.settings import Settings

    capped = Settings(**{**settings.model_dump(), "max_new_sessions": 1})
    worker = Worker(orchestrator, pool, capped, store)
    for _ in range(2):
        resp = await post(device_api, token, meta(conversation="new"))
        assert resp.status_code == 202
    await worker.tick()
    assert len(fake.sessions) == 1
    assert await rows(conn, "SELECT count(*) FROM runs WHERE kind = 'first'") == [(1,)]
    assert sorted(r[0] for r in await rows(conn, "SELECT status FROM inbound")) == [
        "in-progress",
        "queued",
    ]


async def test_deleted_conversation_not_dispatched(worker, fake, upload, conn):
    _, conv = await upload()
    import uuid

    await conversations.mark_deleted(conn, uuid.UUID(conv))
    await worker.tick()
    assert not fake.sessions


async def test_reserved_conversation_waits(worker, fake, upload, conn):
    _, conv = await upload()
    import uuid

    await bindings.reserve(conn, uuid.UUID(conv), 99)
    await worker.tick()
    assert not fake.sessions


async def test_delivery_to_gone_session_requeues_and_next_tick_restarts(
    worker, fake, upload, pool, conn
):
    await upload()
    await worker.tick()
    await finish_all(pool)
    await upload()
    fake.fail_next("post_message", 409)
    await worker.tick()
    assert await rows(conn, "SELECT status, reason FROM runs ORDER BY id") == [
        ("handled", None),
        ("failed", "session_gone"),
    ]
    assert await rows(conn, "SELECT status FROM inbound ORDER BY created_at") == [
        ("handled",),
        ("queued",),
    ]
    await worker.tick()
    assert len(fake.sessions) == 2


async def test_dispatch_to_missing_preset_id_fails_row(worker, fake, upload, preset, conn):
    await upload()
    del fake.presets[str(preset.id)]
    # preset list is read per tick, so the preset is "missing" and rows fail no_preset
    await worker.tick()
    assert await rows(conn, "SELECT status, reason FROM inbound") == [("failed", "no_preset")]


async def test_failing_handoff_tick_keeps_dispatching(worker, fake, upload, monkeypatch):
    async def boom():
        raise RuntimeError("poison hand-off")

    monkeypatch.setattr(worker._handoffs, "tick", boom)
    await upload(caption="login")
    await worker.tick()
    assert len(fake.messages) == 1


async def _upload_pages(device_api, token, order):
    resp = await post(
        device_api, token, meta(pages=pages(*order)), files(with_strokes=True, count=len(order))
    )
    assert resp.status_code == 202, resp.text


async def test_render_sorts_files_by_page_then_kind(worker, fake, device_api, token, conn):
    await _upload_pages(device_api, token, [2, 0, 1])
    await worker.tick()
    msg = only(fake.messages.values())
    names = [a.name.split("-")[0] for a in fake.attachments[msg["id"]]]
    assert names == ["sketch", "strokes"] * 3
    ids = [a.name for a in fake.attachments[msg["id"]]]
    by_page = {}
    for page, kind, aid in await rows(conn, "SELECT page, kind, id FROM attachments"):
        by_page[(page, kind)] = aid
    expect = []
    for page in (0, 1, 2):
        expect.append(f"sketch-{by_page[(page, 'sketch_png')]}.png")
        expect.append(f"strokes-{by_page[(page, 'sketch_strokes')]}.json")
    assert ids == expect


async def test_missing_one_page_file_marks_missing(worker, fake, device_api, token, conn):
    await _upload_pages(device_api, token, [0, 1])
    await conn.execute(
        "UPDATE attachments SET purged_at = now() WHERE kind = 'sketch_png' AND page = 1"
    )
    await worker.tick()
    msg = only(fake.messages.values())
    assert "(sketch file no longer stored)" in msg["text"]
    assert "(page 0)" in msg["text"] and "(page 1)" not in msg["text"]
