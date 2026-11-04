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
