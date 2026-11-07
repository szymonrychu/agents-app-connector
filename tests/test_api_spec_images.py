"""The spec images route: region PNGs a spec references as images/<region id>.png."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from helpers import bearer, layout, paired, region_png
from test_api_sketches_layout import BASE, BASE_LAYOUT, conv_with_base, image_op, send, strike

from agents_app_connector.db import specs
from agents_app_connector.retention import purge


async def with_image(device_api, token, conn):
    cid = await conv_with_base(device_api, token, conn)
    region_id = uuid.uuid4()
    data = region_png(320, 240)
    r = await send(
        device_api,
        token,
        cid,
        layout(ops=[image_op(region_id=region_id)]),
        regions={(0, 0): data},
    )
    assert r.status_code == 202, r.text
    return cid, region_id, data


def image_path(cid, region_id):
    return f"/conversations/{cid}/images/{region_id}.png"


async def test_image_served_with_etag_and_304(device_api, token, conn):
    cid, region_id, data = await with_image(device_api, token, conn)
    r = await device_api.get(image_path(cid, region_id), headers=bearer(token))
    etag = f'"{hashlib.sha256(data).hexdigest()}"'
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-type"] == "image/png" and r.headers["etag"] == etag
    r = await device_api.get(
        image_path(cid, region_id), headers={**bearer(token), "If-None-Match": etag}
    )
    assert r.status_code == 304 and r.content == b""


async def test_other_device_404(device_api, token, conn):
    cid, region_id, _ = await with_image(device_api, token, conn)
    other = (await paired(conn, "sketch", "other"))[1]
    r = await device_api.get(image_path(cid, region_id), headers=bearer(other))
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = await device_api.get(image_path(cid, region_id))
    assert r.status_code == 401


async def test_unknown_region_404(device_api, token, conn):
    cid, _, _ = await with_image(device_api, token, conn)
    r = await device_api.get(image_path(cid, uuid.uuid4()), headers=bearer(token))
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = await device_api.get(f"/conversations/{cid}/images/nope.png", headers=bearer(token))
    assert r.status_code in (404, 422)


async def test_image_survives_retention(device_api, token, conn, pool, store):
    cid, region_id, data = await with_image(device_api, token, conn)
    await purge(pool, store, datetime.now(UTC) + timedelta(days=3650))
    r = await device_api.get(image_path(cid, region_id), headers=bearer(token))
    assert r.status_code == 200 and r.content == data


async def test_image_deleted_with_conversation(device_api, token, conn):
    cid, region_id, _ = await with_image(device_api, token, conn)
    r = await device_api.delete(f"/conversations/{cid}", headers=bearer(token))
    assert r.status_code == 204
    r = await device_api.get(image_path(cid, region_id), headers=bearer(token))
    assert r.status_code == 404
    cur = await conn.execute("SELECT count(*) FROM spec_images")
    assert (await cur.fetchone())[0] == 0


async def test_upload_after_revert_is_stale_base(device_api, token, conn):
    """Review Focus 1: a revert makes a newer version, so ops on the old base are 409
    stale_base, never 422 or a misapplied op."""
    cid = await conv_with_base(device_api, token, conn)
    await conn.execute("UPDATE inbound SET status = 'handled'")
    await specs.add(conn, uuid.UUID(cid), BASE + "more\n", None, [], None, layout=BASE_LAYOUT)
    r = await device_api.post(
        f"/conversations/{cid}/spec/1/revert", json={"base_version": 2}, headers=bearer(token)
    )
    assert r.status_code == 201, r.text
    r = await send(device_api, token, cid, layout(ops=[strike(quote="milo")]), base=2)
    assert r.status_code == 409 and r.json()["error"] == "stale_base"
    r = await send(device_api, token, cid, layout(ops=[strike()]), base=3)
    assert r.status_code == 202, r.text
