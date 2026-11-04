import psycopg
import pytest
from helpers import paired

from agents_app_connector.db import conversations, feedback


async def conv(conn):
    dev, _ = await paired(conn, "sketch", "boox")
    return await conversations.create(conn, dev.id, "sketch")


async def test_add_list_after_cursor_and_count(conn):
    cid = await conv(conn)
    a = await feedback.add(conn, cid, run_id=7, inbound_id=None, kind="question", text="which?")
    b = await feedback.add(conn, cid, 7, None, "illegible", "word", where="page 1, box Login")
    other = await feedback.add(conn, cid, 8, None, "note", "x")
    assert a < b < other
    items = await feedback.after(conn, cid, 0)
    assert [(i.id, i.kind, i.text, i.where_text) for i in items] == [
        (a, "question", "which?", None),
        (b, "illegible", "word", "page 1, box Login"),
        (other, "note", "x", None),
    ]
    assert [i.id for i in await feedback.after(conn, cid, a)] == [b, other]
    assert [i.id for i in await feedback.after(conn, cid, 0, limit=1)] == [a]
    assert await feedback.count_for_run(conn, 7) == 2
    assert await feedback.count_for_run(conn, 9) == 0


async def test_set_spec_version_fills_only_the_runs_unset_items(conn):
    cid = await conv(conn)
    a = await feedback.add(conn, cid, 7, None, "note", "a")
    b = await feedback.add(conn, cid, 8, None, "note", "b")
    assert await feedback.set_spec_version(conn, 7, 3) == 1
    assert await feedback.set_spec_version(conn, 7, 4) == 0
    got = {i.id: i.spec_version for i in await feedback.after(conn, cid, 0)}
    assert got == {a: 3, b: None}


async def test_delete_for_conversation(conn):
    cid = await conv(conn)
    await feedback.add(conn, cid, 7, None, "note", "a")
    assert await feedback.delete_for(conn, cid) == 1
    assert await feedback.after(conn, cid, 0) == []


@pytest.mark.parametrize(
    ("kind", "text", "where"),
    [
        ("praise", "x", None),
        ("note", "", None),
        ("note", "x" * 2001, None),
        ("note", "x", "w" * 201),
    ],
)
async def test_constraints(conn, kind, text, where):
    cid = await conv(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        await feedback.add(conn, cid, 7, None, kind, text, where=where)


async def test_latest_feedback_id_in_conversation_rows(conn):
    cid = await conv(conn)
    assert (await conversations.get(conn, cid)).latest_feedback_id is None
    fid = await feedback.add(conn, cid, 7, None, "note", "a")
    assert (await conversations.get(conn, cid)).latest_feedback_id == fid
