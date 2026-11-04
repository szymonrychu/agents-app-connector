import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from helpers import paired

from agents_app_connector.db import attachments
from agents_app_connector.storage import AttachmentStore


def test_store_roundtrip_sha256(tmp_path):
    store = AttachmentStore(tmp_path)
    did, aid = uuid.uuid4(), uuid.uuid4()
    stored = store.put(did, aid, b"hello")
    assert store.read(stored.path) == b"hello"
    assert stored.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert stored.size == 5
    assert not stored.path.startswith("/")
    assert list(tmp_path.rglob("*.tmp")) == []


def test_delete_missing_is_fine(tmp_path):
    store = AttachmentStore(tmp_path)
    stored = store.put(uuid.uuid4(), uuid.uuid4(), b"x")
    store.delete(stored.path)
    store.delete(stored.path)
    with pytest.raises(FileNotFoundError):
        store.read(stored.path)


def test_path_escape_refused(tmp_path):
    with pytest.raises(ValueError):
        AttachmentStore(tmp_path).read("../etc/passwd")


async def _inbound(conn):
    dev, _ = await paired(conn, "sketch", "d")
    cid, iid = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO conversations (id, device_id, plugin) VALUES (%s, %s, 'sketch')",
        (cid, dev.id),
    )
    await conn.execute(
        "INSERT INTO inbound (id, conversation_id, device_id, client_id, fingerprint, kind) "
        "VALUES (%s, %s, %s, %s, 'f', 'sketch')",
        (iid, cid, dev.id, uuid.uuid4()),
    )
    return dev.id, cid, iid


def _new(did, cid, iid, size=10, expires=None, kind="sketch_png"):
    return attachments.NewAttachment(
        inbound_id=iid,
        conversation_id=cid,
        device_id=did,
        kind=kind,
        mime="image/png",
        size=size,
        sha256="a" * 64,
        path=f"{did}/{uuid.uuid4()}",
        expires_at=expires or datetime.now(UTC) + timedelta(days=1),
        width=300,
        height=200,
    )


async def test_insert_and_for_inbound(conn):
    did, cid, iid = await _inbound(conn)
    aid = await attachments.insert(conn, _new(did, cid, iid))
    rows = await attachments.for_inbound(conn, iid)
    assert [r.id for r in rows] == [aid]
    assert rows[0].width == 300 and rows[0].purged_at is None
    assert await attachments.for_inbound(conn, uuid.uuid4()) == []


async def test_get_owned_checks_device(conn):
    did, cid, iid = await _inbound(conn)
    aid = await attachments.insert(conn, _new(did, cid, iid))
    assert (await attachments.get_owned(conn, aid, did)).id == aid
    assert await attachments.get_owned(conn, aid, uuid.uuid4()) is None


async def test_usage_counts_unpurged_only(conn):
    did, cid, iid = await _inbound(conn)
    assert await attachments.device_usage_bytes(conn, did) == 0
    a = await attachments.insert(conn, _new(did, cid, iid, size=10))
    await attachments.insert(conn, _new(did, cid, iid, size=5, kind="sketch_strokes"))
    assert await attachments.device_usage_bytes(conn, did) == 15
    assert await attachments.mark_purged(conn, a) is True
    assert await attachments.device_usage_bytes(conn, did) == 5
    assert await attachments.mark_purged(conn, a) is False


async def test_expired_lists_unpurged_past_expiry(conn):
    did, cid, iid = await _inbound(conn)
    now = datetime.now(UTC)
    old = await attachments.insert(conn, _new(did, cid, iid, expires=now - timedelta(days=1)))
    await attachments.insert(conn, _new(did, cid, iid, expires=now + timedelta(days=1)))
    gone = await attachments.insert(conn, _new(did, cid, iid, expires=now - timedelta(days=2)))
    await attachments.mark_purged(conn, gone)
    assert [r.id for r in await attachments.expired(conn, now)] == [old]
