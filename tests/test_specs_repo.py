import uuid

import psycopg
import pytest

from agents_app_connector.db import conversations, devices, specs


async def conv(conn):
    dev, _ = await devices.create(conn, "sketch", "boox")
    return await conversations.create(conn, dev.id, "sketch")


async def test_add_versions_latest_get_delete(conn):
    cid = await conv(conn)
    ids = [uuid.uuid4(), uuid.uuid4()]
    assert await specs.latest(conn, cid) is None
    assert await specs.add(conn, cid, "a", ids[0], ids, True) == 1
    assert await specs.add(conn, cid, "b", None, [], False) == 2
    latest = await specs.latest(conn, cid)
    assert (latest.version, latest.markdown, latest.front_matter) == (2, "b", False)
    first = await specs.get(conn, cid, 1)
    assert first.sketch_ids == ids and first.inbound_id == ids[0] and first.front_matter
    assert await specs.get(conn, cid, 3) is None
    assert await specs.delete_for(conn, cid) == 2
    assert await specs.latest(conn, cid) is None
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await specs.add(conn, uuid.uuid4(), "x", None, [], True)
