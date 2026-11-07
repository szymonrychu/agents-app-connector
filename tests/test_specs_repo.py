import uuid

import psycopg
import pytest
from helpers import paired

from agents_app_connector.db import conversations, specs


async def conv(conn):
    dev, _ = await paired(conn, "sketch", "boox")
    return await conversations.create(conn, dev.id, "sketch")


async def test_add_versions_latest_get_delete(conn):
    cid = await conv(conn)
    ids = [uuid.uuid4(), uuid.uuid4()]
    assert await specs.latest(conn, cid) is None
    assert await specs.add(conn, cid, "a", ids[0], ids, 7) == 1
    assert await specs.add(conn, cid, "b", None, [], None) == 2
    latest = await specs.latest(conn, cid)
    assert (latest.version, latest.markdown, latest.run_id) == (2, "b", None)
    first = await specs.get(conn, cid, 1)
    assert first.sketch_ids == ids and first.inbound_id == ids[0] and first.run_id == 7
    assert await specs.last_of_run(conn, 7) == 1 and await specs.last_of_run(conn, 8) is None
    assert await specs.get(conn, cid, 3) is None
    assert await specs.delete_for(conn, cid) == 2
    assert await specs.latest(conn, cid) is None
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await specs.add(conn, uuid.uuid4(), "x", None, [], None)


async def test_add_stores_layout(conn):
    cid = await conv(conn)
    lay = {"lines": [{"line_id": "L1", "source_line": 1, "origin": None}], "ops": []}
    assert await specs.add(conn, cid, "a", None, [], None, layout=lay) == 1
    assert await specs.add(conn, cid, "b", None, [], None) == 2
    assert (await specs.get(conn, cid, 1)).layout == lay
    assert (await specs.latest(conn, cid)).layout is None


def _lines(*ids):
    return {"lines": [{"line_id": i, "source_line": n} for n, i in enumerate(ids, 1)], "ops": []}


async def test_max_line_number_scans_all_versions(conn):
    cid = await conv(conn)
    other = await conv(conn)
    assert await specs.max_line_number(conn, cid) == 0
    await specs.add(conn, cid, "a", None, [], None, layout=_lines("L1", "L12", "L3"))
    await specs.add(conn, cid, "b", None, [], None, layout=_lines("L1", "L13"))
    await specs.add(conn, cid, "c", None, [], None)
    await specs.add(conn, cid, "d", None, [], None, layout=_lines("L2"))
    await specs.add(conn, other, "x", None, [], None, layout=_lines("L99"))
    assert await specs.max_line_number(conn, cid) == 13


async def test_reported_op_ids(conn):
    cid = await conv(conn)
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert await specs.reported_op_ids(conn, cid) == set()
    lay = {"lines": [], "ops": [{"id": str(a), "status": "applied", "note": None}]}
    await specs.add(conn, cid, "a", None, [], None, layout=lay)
    lay = {"lines": [], "ops": [{"id": str(b), "status": "rejected", "note": "x"}]}
    await specs.add(conn, cid, "b", None, [], None, layout=lay)
    await specs.add(conn, cid, "c", None, [], None)
    other = await conv(conn)
    lay = {"lines": [], "ops": [{"id": str(c), "status": "applied", "note": None}]}
    await specs.add(conn, other, "x", None, [], None, layout=lay)
    assert await specs.reported_op_ids(conn, cid) == {a, b}
