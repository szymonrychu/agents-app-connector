import asyncio
import hashlib
import json
import uuid

import httpx
import pytest
from helpers import (
    CANVAS,
    PREFIX,
    bearer,
    count,
    files,
    meta,
    pages,
    paired,
    png,
    post,
    stored_files,
)

from agents_app_connector.db import device_tokens
from agents_app_connector.plugins.sketch import router as sketch_router


async def test_upload_202_queues(device_api, token, conn, store):
    data = png()
    r = await post(device_api, token, fl=files(data, with_strokes=True))
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    row = await (
        await conn.execute("SELECT id, status, kind, conversation_id FROM inbound")
    ).fetchone()
    assert str(row[0]) == body["sketch_id"] and row[1] == "queued" and row[2] == "sketch"
    assert str(row[3]) == body["conversation_id"]
    atts = await (
        await conn.execute("SELECT id, kind, sha256, width, height FROM attachments ORDER BY kind")
    ).fetchall()
    assert [a[1] for a in atts] == ["sketch_png", "sketch_strokes"]
    assert atts[0][2] == hashlib.sha256(data).hexdigest()
    assert str(atts[0][0]) == body["sketch_id"] and (atts[0][3], atts[0][4]) == (400, 300)
    assert len(stored_files(store)) == 2
    ev = await (await conn.execute("SELECT type, payload FROM events")).fetchall()
    assert ev == [("sketch_status", {"sketch_id": body["sketch_id"], "status": "queued"})]


async def test_same_client_id_same_payload_200(device_api, token, conn, store):
    m = meta()
    first = await post(device_api, token, m)
    second = await post(device_api, token, m)
    assert second.status_code == 200 and second.json() == first.json()
    assert await count(conn, "inbound") == 1 and await count(conn, "events") == 1
    assert len(stored_files(store)) == 1


async def test_same_client_id_other_payload_422(device_api, token, conn):
    m = meta()
    assert (await post(device_api, token, m)).status_code == 202
    r = await post(device_api, token, m, files(png(500, 300)))
    assert r.status_code == 422 and r.json()["error"] == "idempotency_mismatch"
    r = await post(device_api, token, json.dumps({**json.loads(m), "caption": "x"}))
    assert r.status_code == 422 and r.json()["error"] == "idempotency_mismatch"
    assert await count(conn, "inbound") == 1


async def test_in_progress_duplicate_409(device_api, token, conn):
    device = await device_tokens.by_token(conn, token)
    m = meta()
    client_id = json.loads(m)["client_id"]
    await conn.execute(
        "SELECT pg_advisory_lock(hashtextextended(%s::text || %s::text, 0))",
        (device.id, client_id),
    )
    r = await post(device_api, token, m)
    assert r.status_code == 409 and r.json()["error"] == "in_progress"
    assert await count(conn, "inbound") == 0


async def test_concurrent_identical_uploads_one_row(device_api, token, conn):
    m = meta()
    results = await asyncio.gather(*[post(device_api, token, m) for _ in range(2)])
    codes = sorted(r.status_code for r in results)
    assert codes[0] in (200, 202) and codes[1] in (200, 202, 409)
    assert 202 in codes
    assert await count(conn, "inbound") == 1


async def test_non_png_415_nothing_stored(device_api, token, conn, store):
    r = await post(device_api, token, fl=files(b"GIF89a" + b"\x00" * 200))
    assert r.status_code == 415 and r.json()["error"] == "invalid_png"
    assert await count(conn, "inbound") == 0 and await count(conn, "attachments") == 0
    assert await count(conn, "conversations") == 0 and stored_files(store) == []


async def test_png_over_cap_413(make_app, token):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(max_png_bytes=50)),
        base_url="http://dev" + PREFIX,
    ) as c:
        r = await post(c, token)
    assert r.status_code == 413 and r.json()["error"] == "too_large"


@pytest.mark.parametrize(
    "bad",
    [
        {"extra": 1},
        {"caption": "x" * 501},
        {"pages": []},
        {"pages": [{"page": -1, "canvas": CANVAS}]},
        {"pages": [{"page": 0}]},
        {"canvas": CANVAS},
        {"page": 0},
        {"conversation": "other"},
        {"client_id": "nope"},
        {"pages": [{"page": 0, "canvas": {"w": 1, "h": 1}}]},
        {"pages": [{"page": 0, "canvas": {"w": "400", "h": 300, "dpi": 300}}]},
        {"pages": [{"page": 0, "canvas": {"w": 400, "h": -3, "dpi": 300}}]},
        {"pages": [{"page": 0, "canvas": {"w": 400, "h": 300, "dpi": 0}}]},
        {"pages": [{"page": 0, "canvas": {"w": 400, "h": 300, "dpi": -1}}]},
        {"pages": [{"page": 0, "canvas": {"w": 400.5, "h": 300, "dpi": 300}}]},
        {"pages": [{"page": 0, "canvas": {"w": 400, "h": 300, "dpi": True}}]},
    ],
)
async def test_meta_invalid_422(device_api, token, conn, bad):
    r = await post(device_api, token, meta(**bad))
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    assert await count(conn, "inbound") == 0


@pytest.mark.parametrize("dpi", ["NaN", "Infinity", "-Infinity"])
async def test_meta_non_finite_dpi_422(device_api, token, dpi):
    raw = json.dumps(
        {
            "client_id": str(uuid.uuid4()),
            "pages": [{"page": 0, "canvas": {"w": 4, "h": 3, "dpi": 0}}],
        }
    )
    raw = raw.replace('"dpi": 0', '"dpi": ' + dpi)
    r = await post(device_api, token, raw)
    assert r.status_code == 422


async def test_meta_missing_or_part_missing_422(device_api, token):
    r = await device_api.post("/sketches", files=files(), headers=bearer(token))
    assert r.status_code == 422
    r = await device_api.post("/sketches", data={"meta": meta()}, headers=bearer(token))
    assert r.status_code == 422


async def test_quota_413(make_app, token, conn, store):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(device_quota_bytes=len(png()) + 10)),
        base_url="http://dev" + PREFIX,
    ) as c:
        assert (await post(c, token)).status_code == 202
        r = await post(c, token)
    assert r.status_code == 413 and r.json()["error"] == "quota_exceeded"
    assert await count(conn, "inbound") == 1 and len(stored_files(store)) == 1


async def test_failed_transaction_removes_written_files(
    device_api, token, conn, store, monkeypatch
):
    async def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(sketch_router.events, "append", boom)
    with pytest.raises(RuntimeError):
        await post(device_api, token, fl=files(with_strokes=True))
    assert stored_files(store) == [] and await count(conn, "inbound") == 0


async def test_unauthenticated_401(device_api):
    r = await device_api.post("/sketches", data={"meta": meta()}, files=files())
    assert r.status_code == 401


async def _atts(conn):
    return await (
        await conn.execute("SELECT kind, page, id FROM attachments ORDER BY kind, page")
    ).fetchall()


async def test_upload_three_pages_one_row(device_api, token, conn, store):
    r = await post(device_api, token, meta(pages=pages(0, 1, 2)), files(with_strokes=True, count=3))
    assert r.status_code == 202
    assert await count(conn, "inbound") == 1
    atts = await _atts(conn)
    assert [(a[0], a[1]) for a in atts] == [
        ("sketch_png", 0),
        ("sketch_png", 1),
        ("sketch_png", 2),
        ("sketch_strokes", 0),
        ("sketch_strokes", 1),
        ("sketch_strokes", 2),
    ]
    assert len(stored_files(store)) == 6


async def test_pages_out_of_order_keep_their_index(device_api, token, conn):
    r = await post(device_api, token, meta(pages=pages(2, 0)), files(count=2))
    assert r.status_code == 202
    first = await (
        await conn.execute("SELECT page FROM attachments WHERE id = %s", (r.json()["sketch_id"],))
    ).fetchone()
    assert first == (2,)
    assert [a[1] for a in await _atts(conn)] == [0, 2]


async def test_duplicate_page_index_422(device_api, token, conn):
    r = await post(device_api, token, meta(pages=pages(1, 1)), files(count=2))
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    assert await count(conn, "inbound") == 0


async def test_too_many_pages_422(device_api, token, conn):
    n = 5
    r = await post(device_api, token, meta(pages=pages(*range(n))), files(count=n))
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    assert await count(conn, "inbound") == 0


async def test_missing_png_for_page_422(device_api, token, conn):
    fl = files(count=2)
    del fl["sketch.1.png"]
    r = await post(device_api, token, meta(pages=pages(0, 1)), fl)
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    assert await count(conn, "inbound") == 0


async def test_extra_png_without_page_422(device_api, token, conn):
    r = await post(device_api, token, meta(pages=pages(0)), files(count=2))
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    assert await count(conn, "inbound") == 0


async def test_old_single_page_field_names_422(device_api, token):
    fl = {"sketch.png": ("sketch.png", png(), "image/png")}
    r = await post(device_api, token, fl=fl)
    assert r.status_code == 422


async def test_total_over_max_upload_bytes_413_nothing_stored(make_app, token, conn, store):
    limit = len(png()) * 2 + 10
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(max_upload_bytes=limit)),
        base_url="http://dev" + PREFIX,
    ) as c:
        r = await post(c, token, meta(pages=pages(0, 1, 2)), files(count=3))
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    assert await count(conn, "inbound") == 0 and stored_files(store) == []


async def test_conversation_id_targets_older_conversation(device_api, token, conn):
    old = (await post(device_api, token, meta(conversation="new"))).json()["conversation_id"]
    new = (await post(device_api, token, meta(conversation="new"))).json()["conversation_id"]
    assert old != new
    r = await post(device_api, token, meta(conversation_id=old))
    assert r.status_code == 202 and r.json()["conversation_id"] == old
    row = await (
        await conn.execute(
            "SELECT conversation_id FROM inbound WHERE id = %s", (r.json()["sketch_id"],)
        )
    ).fetchone()
    assert str(row[0]) == old


async def test_conversation_id_foreign_404(device_api, token, conn):
    other = (await paired(conn, "sketch", "other"))[1]
    foreign = (await post(device_api, other, meta(conversation="new"))).json()["conversation_id"]
    for cid in (foreign, str(uuid.uuid4())):
        r = await post(device_api, token, meta(conversation_id=cid))
        assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert await count(conn, "inbound") == 1


async def test_conversation_id_with_new_422(device_api, token, conn):
    cid = (await post(device_api, token, meta(conversation="new"))).json()["conversation_id"]
    r = await post(device_api, token, meta(conversation="new", conversation_id=cid))
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    assert await count(conn, "inbound") == 1


async def test_replay_same_multi_page_payload_200(device_api, token, conn, store):
    m = meta(pages=pages(0, 1))
    first = await post(device_api, token, m, files(with_strokes=True, count=2))
    second = await post(device_api, token, m, files(with_strokes=True, count=2))
    assert first.status_code == 202 and second.status_code == 200
    assert second.json() == first.json()
    assert await count(conn, "inbound") == 1 and len(stored_files(store)) == 4


async def test_config_reports_page_limits(device_api, token):
    r = await device_api.get("/config", headers=bearer(token))
    body = r.json()
    assert (body["max_pages"], body["max_upload_bytes"], body["min_edge_px"]) == (
        4,
        8388608,
        200,
    )


async def _conv_with_spec(device_api, token, conn, version=1):
    cid = (await post(device_api, token, meta(conversation="new"))).json()["conversation_id"]
    for v in range(1, version + 1):
        await conn.execute(
            "INSERT INTO specs (conversation_id, version, markdown) VALUES (%s, %s, %s)",
            (cid, v, f"# spec {v}"),
        )
    return cid


def _annotates(version=1):
    return {"spec_version": version, "pages": [{"page": 0, "doc_page": 0, "lines": [0, 4]}]}


async def test_annotated_upload_unknown_spec_404_no_spec(device_api, token, conn, store):
    cid = await _conv_with_spec(device_api, token, conn)
    before = (await count(conn, "inbound"), len(stored_files(store)))
    r = await post(device_api, token, meta(conversation_id=cid, annotates=_annotates(2)))
    assert r.status_code == 404 and r.json()["error"] == "no_spec"
    assert r.json()["detail"] == "no such spec version"
    assert (await count(conn, "inbound"), len(stored_files(store))) == before


async def test_annotated_upload_accepted_when_spec_exists(device_api, token, conn):
    cid = await _conv_with_spec(device_api, token, conn, version=2)
    r = await post(device_api, token, meta(conversation_id=cid, annotates=_annotates(1)))
    assert r.status_code == 202
    stored = (
        await (
            await conn.execute("SELECT meta FROM inbound WHERE id = %s", (r.json()["sketch_id"],))
        ).fetchone()
    )[0]
    assert stored["annotates"] == _annotates(1)


async def test_replay_same_client_id_same_annotates_200(device_api, token, conn):
    cid = await _conv_with_spec(device_api, token, conn)
    m = meta(conversation_id=cid, annotates=_annotates(1))
    first = await post(device_api, token, m)
    second = await post(device_api, token, m)
    assert first.status_code == 202 and second.status_code == 200
    assert second.json() == first.json()


async def test_replay_with_different_annotates_422_idempotency_mismatch(device_api, token, conn):
    cid = await _conv_with_spec(device_api, token, conn, version=2)
    base = json.loads(meta(conversation_id=cid, annotates=_annotates(1)))
    assert (await post(device_api, token, json.dumps(base))).status_code == 202
    other = {**base, "annotates": _annotates(2)}
    r = await post(device_api, token, json.dumps(other))
    assert r.status_code == 422 and r.json()["error"] == "idempotency_mismatch"
    plain = {k: v for k, v in base.items() if k != "annotates"}
    r = await post(device_api, token, json.dumps(plain))
    assert r.status_code == 422 and r.json()["error"] == "idempotency_mismatch"


async def test_plain_upload_unchanged_without_annotates(device_api, token, conn):
    cid = await _conv_with_spec(device_api, token, conn)
    r = await post(device_api, token, meta(conversation_id=cid))
    assert r.status_code == 202
    stored = (
        await (
            await conn.execute("SELECT meta FROM inbound WHERE id = %s", (r.json()["sketch_id"],))
        ).fetchone()
    )[0]
    assert stored["annotates"] is None


async def test_annotated_upload_to_other_device_conversation_404_not_found(device_api, token, conn):
    other = (await paired(conn, "sketch", "other"))[1]
    foreign = await _conv_with_spec(device_api, other, conn)
    r = await post(device_api, token, meta(conversation_id=foreign, annotates=_annotates(1)))
    assert r.status_code == 404 and r.json()["error"] == "not_found"


# ---- picture regions on any page (contract 2.3.0) ----


def pic(part="region.0.0.png", rect=(60, 60, 300, 200), id=None):
    return {"id": str(id or uuid.uuid4()), "rect": list(rect), "part": part}


def pic_meta(*page_pictures, **over):
    pgs = [{"page": i, "canvas": CANVAS, "regions": p} for i, p in enumerate(page_pictures)]
    return meta(pages=pgs, **over)


async def test_plain_upload_with_pictures_stores_spec_images(device_api, token, conn):
    from helpers import region_png, rows

    p = pic()
    r = await post(device_api, token, pic_meta([p]), files(regions={(0, 0): region_png()}))
    assert r.status_code == 202, r.text
    cid = r.json()["conversation_id"]
    from agents_app_connector.db import spec_images

    assert await spec_images.get(conn, uuid.UUID(cid), uuid.UUID(p["id"])) is not None
    (row,) = await rows(conn, "SELECT kind, expires_at FROM attachments WHERE kind = 'spec_image'")
    assert row[0] == "spec_image" and row[1] is None


async def test_picture_served_by_images_route(device_api, token, conn):
    from helpers import bearer, region_png

    p = pic()
    data = region_png()
    r = await post(device_api, token, pic_meta([p]), files(regions={(0, 0): data}))
    cid = r.json()["conversation_id"]
    got = await device_api.get(f"/conversations/{cid}/images/{p['id']}.png", headers=bearer(token))
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/png"
    assert got.content == data


def _err(r, text):
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "invalid_request", r.text
    assert text in r.json()["detail"], r.json()["detail"]


async def test_plain_page_picture_cap_422(make_app, token, conn):
    from helpers import region_png

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(max_regions_per_page=1)),
        base_url="http://dev" + PREFIX,
    ) as api:
        m = pic_meta([pic(), pic("region.0.1.png")])
        r = await post(api, token, m, files(regions={(0, 0): region_png(), (0, 1): region_png()}))
    _err(r, "at most 1 region")


async def test_unreferenced_region_part_on_plain_page_422(device_api, token, conn, store):
    from helpers import region_png

    r = await post(device_api, token, meta(), files(regions={(0, 0): region_png()}))
    _err(r, "region.0.0.png: no image op or picture names it")
    assert stored_files(store) == []


async def test_picture_named_but_part_missing_422(device_api, token, conn):
    r = await post(device_api, token, pic_meta([pic()]), files())
    _err(r, "region.0.0.png is named but missing")


async def test_picture_part_of_another_page_422(device_api, token, conn):
    from helpers import region_png

    m = pic_meta([pic("region.1.0.png")], [])
    r = await post(device_api, token, m, files(count=2, regions={(1, 0): region_png()}))
    _err(r, "is not a part of page 0")


async def test_picture_rect_outside_canvas_422(device_api, token, conn):
    from helpers import region_png

    p = pic(rect=(0, 0, CANVAS["w"] + 1, 10))
    r = await post(device_api, token, pic_meta([p]), files(regions={(0, 0): region_png()}))
    _err(r, "rect outside the canvas")


async def test_part_named_by_op_and_picture_422(device_api, token, conn):
    from helpers import region_png
    from test_api_sketches_layout import conv_with_base, image_op, nb_files

    cid = await conv_with_base(device_api, token, conn)
    from helpers import NOTEBOOK_CANVAS, layout

    p = pic()
    m = json.dumps(
        {
            "client_id": str(uuid.uuid4()),
            "conversation_id": cid,
            "base_version": 1,
            "pages": [{"page": 0, "canvas": NOTEBOOK_CANVAS, "regions": [p]}],
        }
    )
    fl = nb_files(layout(ops=[image_op()]), regions={(0, 0): region_png()})
    r = await post(device_api, token, m, fl)
    _err(r, "is named twice")


async def test_duplicate_picture_id_422(device_api, token, conn):
    from helpers import region_png

    i = uuid.uuid4()
    m = pic_meta([pic(id=i), pic("region.0.1.png", id=i)])
    r = await post(
        device_api, token, m, files(regions={(0, 0): region_png(), (0, 1): region_png()})
    )
    _err(r, "duplicate op or region id")


async def test_pictures_with_a_segment_layout_and_no_base_accepted(device_api, token, conn):
    from helpers import NOTEBOOK_CANVAS, layout, region_png
    from test_api_sketches_layout import nb_files

    seg = {"id": "p0-r0-0", "row": 0, "x0": 60, "x1": 400, "strokes": [0, 1, 2]}
    m = json.dumps(
        {
            "client_id": str(uuid.uuid4()),
            "pages": [{"page": 0, "canvas": NOTEBOOK_CANVAS, "regions": [pic()]}],
        }
    )
    fl = nb_files(layout(segments=[seg]), strokes=3, regions={(0, 0): region_png()})
    r = await post(device_api, token, m, fl)
    assert r.status_code == 202, r.text


async def test_plain_upload_fingerprint_has_no_regions_key(device_api, token, conn):
    from helpers import rows

    m = meta()
    r = await post(device_api, token, m)
    assert r.status_code == 202
    (row,) = await rows(conn, "SELECT meta FROM inbound")
    assert "regions" not in row[0]["pages"][0]
    again = await post(device_api, token, m)
    assert again.status_code == 200


async def test_pictures_are_part_of_the_fingerprint(device_api, token, conn):
    from helpers import region_png

    cid = str(uuid.uuid4())
    p = pic()
    fl = files(regions={(0, 0): region_png()})
    r = await post(device_api, token, pic_meta([p], client_id=cid), fl)
    assert r.status_code == 202, r.text
    moved = {**p, "rect": [70, 60, 300, 200]}
    r = await post(device_api, token, pic_meta([moved], client_id=cid), fl)
    assert r.status_code == 422 and r.json()["error"] == "idempotency_mismatch", r.text
