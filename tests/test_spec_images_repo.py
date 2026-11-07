import uuid

from helpers import paired

from agents_app_connector.db import attachments, conversations, inbound, spec_images


async def _image(conn, dev, cid):
    iid = uuid.uuid4()
    await inbound.insert(
        conn,
        id=iid,
        conversation_id=cid,
        device_id=dev.id,
        client_id=uuid.uuid4(),
        fingerprint="f",
        kind="sketch",
        text=None,
        meta={},
    )
    att = await attachments.insert(
        conn,
        attachments.NewAttachment(
            inbound_id=iid,
            conversation_id=cid,
            device_id=dev.id,
            kind="spec_image",
            mime="image/png",
            size=3,
            sha256="b" * 64,
            path="p",
            expires_at=None,
        ),
    )
    return att


async def test_add_get_missing(conn):
    dev, _ = await paired(conn, "sketch", "boox")
    cid = await conversations.create(conn, dev.id, "sketch")
    other = await conversations.create(conn, dev.id, "sketch")
    region, unknown = uuid.uuid4(), uuid.uuid4()
    att = await _image(conn, dev, cid)
    await spec_images.add(conn, cid, region, att)
    row = await spec_images.get(conn, cid, region)
    assert row.id == att and row.kind == "spec_image" and row.expires_at is None
    assert await spec_images.get(conn, other, region) is None
    assert await spec_images.get(conn, cid, unknown) is None
    assert await spec_images.missing(conn, cid, [region, unknown]) == {unknown}
    assert await spec_images.missing(conn, other, [region]) == {region}
    assert await spec_images.missing(conn, cid, []) == set()


async def test_cascade_with_conversation(conn):
    dev, _ = await paired(conn, "sketch", "boox")
    cid = await conversations.create(conn, dev.id, "sketch")
    region = uuid.uuid4()
    await spec_images.add(conn, cid, region, await _image(conn, dev, cid))
    await attachments.delete_for_conversation(conn, cid)
    assert await spec_images.get(conn, cid, region) is None
    cid = await conversations.create(conn, dev.id, "sketch")
    await spec_images.add(conn, cid, region, await _image(conn, dev, cid))
    await conn.execute("DELETE FROM conversations WHERE id = %s", (cid,))
    cur = await conn.execute("SELECT count(*) FROM spec_images")
    assert (await cur.fetchone())[0] == 0
