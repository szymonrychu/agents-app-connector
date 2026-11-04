import uuid
from datetime import UTC, datetime, timedelta

import pytest
from helpers import only, rows

from agents_app_connector.db import conversations, specs
from agents_app_connector.tracker import Tracker
from agents_app_connector.translator_tools import TranslatorTools
from agents_app_connector.worker import Worker

DOC = "LOGIN\n\n[ user ]"
REPLY = "Done, the document is stored."


@pytest.fixture
def worker(orchestrator, pool, settings, store, preset):
    return Worker(orchestrator, pool, settings, store)


@pytest.fixture
def clock():
    return {"now": datetime.now(UTC)}


@pytest.fixture
def tracker(orchestrator, pool, settings, clock):
    return Tracker(orchestrator, pool, settings, now=lambda: clock["now"])


@pytest.fixture
def tools(pool, settings):
    return TranslatorTools(pool, settings)


def session_of(fake, mid):
    return uuid.UUID(fake.messages[mid]["session_id"])


async def write(tools, fake, mid, markdown=DOC):
    """The translator calls document_write during the turn of message `mid`."""
    return (await tools.write_document(session_of(fake, mid), markdown))["version"]


async def ask(tools, fake, mid, text="Which box?"):
    """The translator calls feedback_add during the turn of message `mid`."""
    return (await tools.add_feedback(session_of(fake, mid), "question", text, None))["id"]


async def events_of(conn, conv):
    return await rows(
        conn, "SELECT type, payload FROM events WHERE conversation_id = %s ORDER BY id", conv
    )


async def start(worker, fake, upload):
    sketch_id, conv = await upload()
    await worker.tick()
    return sketch_id, conv, max(fake.messages)


async def test_completed_turn_with_document_settles_run_and_links_feedback(
    worker, tracker, tools, fake, upload, conn, pool
):
    sketch_id, conv, mid = await start(worker, fake, upload)
    fid = await ask(tools, fake, mid)
    assert await write(tools, fake, mid) == 1
    fake.complete_turn(mid, REPLY)
    await tracker.tick()
    async with pool.connection() as c:
        spec = await specs.get(c, conv, 1)
    assert spec.markdown == DOC
    assert [str(i) for i in spec.sketch_ids] == [sketch_id] and str(spec.inbound_id) == sketch_id
    evs = await events_of(conn, conv)
    assert [(e[0], e[1].get("status")) for e in evs] == [
        ("sketch_status", "queued"),
        ("sketch_status", "translating"),
        ("feedback", None),
        ("spec", None),
        ("sketch_status", "done"),
    ]
    assert evs[3][1] == {"version": 1}
    assert evs[4][1] == {"sketch_id": sketch_id, "status": "done", "version": 1}
    assert await rows(conn, "SELECT id, spec_version FROM feedback") == [(fid, 1)]
    assert await rows(conn, "SELECT status FROM inbound") == [("handled",)]
    assert await rows(conn, "SELECT status FROM runs") == [("handled",)]
    await tracker.tick()
    assert len(await rows(conn, "SELECT 1 FROM specs")) == 1


async def test_reply_text_is_never_stored(worker, tracker, tools, fake, upload, conn, pool):
    _, conv, mid = await start(worker, fake, upload)
    await write(tools, fake, mid, "A")
    fake.complete_turn(mid, "---\nid: x\n---\n# Something else entirely")
    await tracker.tick()
    assert await rows(conn, "SELECT version, markdown FROM specs") == [(1, "A")]


async def test_two_writes_in_one_turn_done_carries_the_last_version(
    worker, tracker, tools, fake, upload, conn
):
    sketch_id, conv, mid = await start(worker, fake, upload)
    await write(tools, fake, mid, "A")
    await write(tools, fake, mid, "B")
    fake.complete_turn(mid, REPLY)
    await tracker.tick()
    assert (await events_of(conn, conv))[-1] == (
        "sketch_status",
        {"sketch_id": sketch_id, "status": "done", "version": 2},
    )


async def test_follow_up_document_creates_v2(worker, tracker, tools, fake, upload, say, conn, pool):
    sketch_id, conv, mid = await start(worker, fake, upload)
    await write(tools, fake, mid)
    fake.complete_turn(mid, REPLY)
    await tracker.tick()
    message_id = await say(conv, "make it blue")
    await worker.tick()
    follow = max(fake.messages)
    await write(tools, fake, follow, DOC + " v2")
    fake.complete_turn(follow, REPLY)
    await tracker.tick()
    async with pool.connection() as c:
        v2 = await specs.latest(c, conv)
    assert v2.version == 2 and v2.markdown.endswith("v2")
    assert [str(i) for i in v2.sketch_ids] == [sketch_id] and str(v2.inbound_id) == message_id
    last = (await events_of(conn, conv))[-1]
    assert last == ("message", {"message_id": message_id, "status": "done", "version": 2})


async def test_feedback_only_turn_is_done_without_a_version(
    worker, tracker, tools, fake, upload, say, conn
):
    _, conv, mid = await start(worker, fake, upload)
    await write(tools, fake, mid)
    fake.complete_turn(mid, REPLY)
    await tracker.tick()
    message_id = await say(conv, "what does the arrow mean?")
    await worker.tick()
    follow = max(fake.messages)
    await ask(tools, fake, follow, "Which arrow?")
    fake.complete_turn(follow, REPLY)
    await tracker.tick()
    assert (await events_of(conn, conv))[-1] == (
        "message",
        {"message_id": message_id, "status": "done"},
    )
    assert await rows(conn, "SELECT kind, status FROM runs ORDER BY id") == [
        ("first", "handled"),
        ("follow", "handled"),
    ]
    assert await rows(conn, "SELECT text, spec_version FROM feedback") == [("Which arrow?", None)]
    assert len(await rows(conn, "SELECT 1 FROM specs")) == 1


async def test_turn_without_document_or_feedback_fails_no_document(
    worker, tracker, fake, upload, conn
):
    sketch_id, conv, mid = await start(worker, fake, upload)
    fake.complete_turn(mid, "---\nid: a\n---\n# A whole spec in the reply")
    await tracker.tick()
    assert await rows(conn, "SELECT status, reason FROM runs") == [("failed", "no_document")]
    assert await rows(conn, "SELECT status, reason FROM inbound") == [("failed", "no_document")]
    assert await rows(conn, "SELECT 1 FROM specs") == []
    assert (await events_of(conn, conv))[-1] == (
        "sketch_status",
        {"sketch_id": sketch_id, "status": "failed", "reason": "no_document"},
    )


async def test_turn_failed_marks_failed(worker, tracker, fake, upload, conn):
    sketch_id, conv, mid = await start(worker, fake, upload)
    fake.fail_turn(mid)
    await tracker.tick()
    assert await rows(conn, "SELECT status, reason FROM runs") == [("failed", "message_failed")]
    assert await rows(conn, "SELECT status, reason FROM inbound") == [("failed", "message_failed")]
    assert (await events_of(conn, conv))[-1] == (
        "sketch_status",
        {"sketch_id": sketch_id, "status": "failed", "reason": "message_failed"},
    )
    assert await rows(conn, "SELECT 1 FROM specs") == []
    assert await rows(conn, "SELECT 1 FROM bindings WHERE released_at IS NULL") != []


async def test_turn_status_failed(worker, tracker, fake, upload, conn):
    _, _, mid = await start(worker, fake, upload)
    fake.messages[mid]["turn"] = {"status": "failed"}
    await tracker.tick()
    assert await rows(conn, "SELECT reason FROM runs") == [("turn_failed",)]


async def test_timeout(worker, tracker, fake, upload, conn, clock):
    _, conv, _ = await start(worker, fake, upload)
    await tracker.tick()
    assert await rows(conn, "SELECT status FROM runs") == [("in-progress",)]
    (started,) = only(await rows(conn, "SELECT started_at FROM runs"))
    clock["now"] = started + timedelta(seconds=1801)
    await tracker.tick()
    assert await rows(conn, "SELECT status, reason FROM runs") == [("failed", "timeout")]
    assert (await events_of(conn, conv))[-1][1]["status"] == "failed"


async def test_first_run_message_gone_fails_session_not_found(worker, tracker, fake, upload, conn):
    _, _, mid = await start(worker, fake, upload)
    del fake.messages[mid]
    await tracker.tick()
    assert await rows(conn, "SELECT status, reason FROM runs") == [("failed", "session_not_found")]
    assert await rows(conn, "SELECT status FROM inbound") == [("failed",)]


async def test_follow_session_gone_requeues_and_releases_binding(
    worker, tracker, tools, fake, upload, say, conn
):
    _, conv, mid = await start(worker, fake, upload)
    await write(tools, fake, mid)
    fake.complete_turn(mid, REPLY)
    await tracker.tick()
    message_id = await say(conv, "more")
    await worker.tick()
    follow = max(fake.messages)
    fake.remove_session(only(fake.sessions))
    await tracker.tick()
    assert await rows(conn, "SELECT kind, status, reason FROM runs ORDER BY id") == [
        ("first", "handled", None),
        ("follow", "failed", "session_gone"),
    ]
    assert await rows(conn, "SELECT status FROM inbound WHERE id = %s", message_id) == [("queued",)]
    assert await rows(conn, "SELECT 1 FROM bindings WHERE released_at IS NULL") == []
    assert (await events_of(conn, conv))[-1][1] == {
        "message_id": message_id,
        "status": "queued",
        "reason": "session_gone",
    }
    assert follow not in fake.messages
    await worker.tick()
    assert len(fake.sessions) == 1
    assert "Previous spec version 1" in max(fake.messages.values(), key=lambda m: m["id"])["text"]


async def test_follow_message_cancelled_requeues(worker, tracker, tools, fake, upload, say, conn):
    _, conv, mid = await start(worker, fake, upload)
    await write(tools, fake, mid)
    fake.complete_turn(mid, REPLY)
    await tracker.tick()
    await say(conv, "more")
    await worker.tick()
    fake.messages[max(fake.messages)]["status"] = "cancelled"
    await tracker.tick()
    assert await rows(conn, "SELECT status FROM inbound WHERE kind = 'message'") == [("queued",)]
    assert await rows(conn, "SELECT 1 FROM bindings WHERE released_at IS NULL") == []


async def test_orchestrator_unavailable_retries_next_tick(
    worker, tracker, tools, fake, upload, conn
):
    _, _, mid = await start(worker, fake, upload)
    await write(tools, fake, mid)
    fake.complete_turn(mid, REPLY)
    fake.fail_next("get_message", 503)
    await tracker.tick()
    assert await rows(conn, "SELECT status FROM runs") == [("in-progress",)]
    assert await rows(conn, "SELECT status FROM inbound") == [("in-progress",)]
    await tracker.tick()
    assert await rows(conn, "SELECT status FROM runs") == [("handled",)]
    assert len(await rows(conn, "SELECT 1 FROM specs")) == 1


async def test_deleted_conversation_fails_run_without_spec_or_events(
    worker, tracker, fake, upload, conn, pool
):
    _, conv, mid = await start(worker, fake, upload)
    before = len(await events_of(conn, conv))
    async with pool.connection() as c:
        await conversations.mark_deleted(c, conv)
    fake.complete_turn(mid, REPLY)
    await tracker.tick()
    assert await rows(conn, "SELECT status, reason FROM runs") == [
        ("failed", "conversation_deleted")
    ]
    assert await rows(conn, "SELECT status, reason FROM inbound") == [
        ("failed", "conversation_deleted")
    ]
    assert await rows(conn, "SELECT 1 FROM specs") == []
    assert len(await events_of(conn, conv)) == before
    assert await rows(conn, "SELECT 1 FROM bindings WHERE released_at IS NULL") == []


async def test_handoff_runs_are_not_tracked(tracker, conn):
    assert await tracker.tick() is None
