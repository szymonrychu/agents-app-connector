"""Uploads of notebook pages: layout.<i>.json, region.<i>.<k>.png and base_version."""

import io
import json
import os
import uuid

import httpx
from helpers import (
    NOTEBOOK_CANVAS,
    PREFIX,
    count,
    files,
    layout,
    post,
    region_png,
    rows,
    stored_files,
    strokes_n,
)
from PIL import Image

from agents_app_connector.db import specs

BASE = "buy milk\nand bread\n"
BASE_LAYOUT = {
    "lines": [
        {"line_id": "L1", "source_line": 1, "origin": None},
        {"line_id": "L2", "source_line": 2, "origin": None},
    ],
    "ops": [],
    "base_version": None,
}


def nb_meta(**over):
    base = {"client_id": str(uuid.uuid4()), "pages": [{"page": 0, "canvas": NOTEBOOK_CANVAS}]}
    return json.dumps({**base, **over})


def nb_files(lay=None, strokes=3, regions=None, page_count=1):
    fl = files(
        layouts={0: lay or layout()} if lay is not False else None,
        regions=regions,
        count=page_count,
    )
    if strokes is not None:
        for i in range(page_count):
            fl[f"strokes.{i}.json"] = (f"strokes.{i}.json", strokes_n(strokes), "application/json")
    return fl


def strike(line="L1", frm=4, to=8, quote="milk", **over):
    return {
        "id": str(uuid.uuid4()),
        "kind": "delete",
        "target": {"line_id": line, "from": frm, "to": to, "quote": quote},
        "strokes": [0],
        **over,
    }


def image_op(part="region.0.0.png", region_id=None, rect=(60, 600, 400, 300)):
    return {
        "id": str(uuid.uuid4()),
        "kind": "image",
        "strokes": [1],
        "rect": list(rect),
        "region": {"id": str(region_id or uuid.uuid4()), "part": part, "after_line": "L1"},
    }


async def conv_with_base(device_api, token, conn, versions=1):
    """A conversation with spec versions 1..n, each carrying BASE and BASE_LAYOUT."""
    r = await post(device_api, token, nb_meta(conversation="new"), nb_files(lay=False))
    assert r.status_code == 202, r.text
    cid = r.json()["conversation_id"]
    for _ in range(versions):
        await specs.add(conn, uuid.UUID(cid), BASE, None, [], None, layout=BASE_LAYOUT)
    return cid


async def send(device_api, token, cid, lay, base=1, regions=None, strokes=3, **meta_over):
    m = nb_meta(conversation_id=cid, base_version=base, **meta_over)
    if base is None:
        m = nb_meta(conversation_id=cid, **meta_over)
    return await post(device_api, token, m, nb_files(lay, strokes=strokes, regions=regions))


def err(r, status, code, text=None):
    assert r.status_code == status, r.text
    assert r.json()["error"] == code, r.text
    if text is not None:
        assert text in r.json()["detail"], r.json()["detail"]


async def test_layout_and_regions_stored(device_api, token, conn, store):
    cid = await conv_with_base(device_api, token, conn)
    region_id = uuid.uuid4()
    lay = layout(
        segments=[{"id": "p0-r4-0", "row": 4, "x0": 60, "x1": 900, "strokes": [2]}],
        runs=[{"line_id": "L1", "row": 1, "x0": 53}],
        ops=[strike(), image_op(region_id=region_id)],
    )
    before = len(stored_files(store))
    r = await send(device_api, token, cid, lay, regions={(0, 0): region_png()})
    assert r.status_code == 202, r.text
    sid = r.json()["sketch_id"]
    atts = await rows(
        conn,
        "SELECT kind, expires_at IS NULL, page, mime FROM attachments WHERE inbound_id = %s "
        "ORDER BY kind",
        sid,
    )
    assert atts == [
        ("sketch_layout", False, 0, "application/json"),
        ("sketch_png", False, 0, "image/png"),
        ("sketch_strokes", False, 0, "application/json"),
        ("spec_image", True, 0, "image/png"),
    ]
    assert len(stored_files(store)) == before + 4
    images = await rows(
        conn,
        "SELECT s.region_id, a.kind FROM spec_images s JOIN attachments a "
        "ON a.id = s.attachment_id WHERE s.conversation_id = %s",
        cid,
    )
    assert images == [(region_id, "spec_image")]
    stored = (await rows(conn, "SELECT meta FROM inbound WHERE id = %s", sid))[0][0]
    assert stored["base_version"] == 1


async def test_bad_stroke_index_422(device_api, token, conn, store):
    cid = await conv_with_base(device_api, token, conn)
    before = (await count(conn, "inbound"), len(stored_files(store)))
    r = await send(device_api, token, cid, layout(ops=[strike(strokes=[3])]), strokes=3)
    err(r, 422, "invalid_request", "layout.0.json")
    seg = {"id": "p0-r4-0", "row": 4, "x0": 60, "x1": 900, "strokes": [0]}
    r = await send(device_api, token, cid, layout(segments=[seg]), base=None, strokes=None)
    err(r, 422, "invalid_request", "stroke")
    assert (await count(conn, "inbound"), len(stored_files(store))) == before


async def test_rect_outside_canvas_422(device_api, token, conn):
    cid = await conv_with_base(device_api, token, conn)
    op = {
        "id": str(uuid.uuid4()),
        "kind": "insert",
        "at": {"line_id": "L1", "offset": 8, "mode": "inline"},
        "rect": [1300, 300, 200, 90],
        "strokes": [0],
    }
    err(await send(device_api, token, cid, layout(ops=[op])), 422, "invalid_request", "rect")
    seg = {"id": "p0-r4-0", "row": 4, "x0": 60, "x1": 1500, "strokes": [0]}
    r = await send(device_api, token, cid, layout(segments=[seg]), base=None)
    err(r, 422, "invalid_request", "p0-r4-0")


async def test_missing_region_part_422(device_api, token, conn, store):
    cid = await conv_with_base(device_api, token, conn)
    r = await send(device_api, token, cid, layout(ops=[image_op()]))
    err(r, 422, "invalid_request", "region.0.0.png")
    r = await send(
        device_api,
        token,
        cid,
        layout(ops=[image_op("region.1.0.png")]),
        regions={(0, 0): region_png()},
    )
    err(r, 422, "invalid_request", "region.1.0.png")


async def test_unreferenced_region_part_422(device_api, token, conn, store):
    cid = await conv_with_base(device_api, token, conn)
    before = len(stored_files(store))
    r = await send(device_api, token, cid, layout(ops=[strike()]), regions={(0, 0): region_png()})
    err(r, 422, "invalid_request", "region.0.0.png")
    r = await send(device_api, token, cid, False, base=None, regions={(0, 0): region_png()})
    err(r, 422, "invalid_request", "region.0.0.png")
    assert len(stored_files(store)) == before


async def test_too_many_regions_422(make_app, token, conn):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(max_regions_per_page=1)),
        base_url="http://dev" + PREFIX,
    ) as api:
        cid = await conv_with_base(api, token, conn)
        lay = layout(ops=[image_op("region.0.0.png"), image_op("region.0.1.png")])
        regions = {(0, 0): region_png(), (0, 1): region_png()}
        r = await send(api, token, cid, lay, regions=regions)
    err(r, 422, "invalid_request", "at most 1 region")


async def test_ops_without_base_version_422(device_api, token, conn):
    cid = await conv_with_base(device_api, token, conn)
    r = await send(device_api, token, cid, layout(ops=[strike()]), base=None)
    err(r, 422, "invalid_request", "base_version")


async def test_unknown_line_id_422(device_api, token, conn):
    cid = await conv_with_base(device_api, token, conn)
    r = await send(device_api, token, cid, layout(ops=[strike(line="L9")]))
    err(r, 422, "invalid_request", "L9")
    lay = layout(runs=[{"line_id": "L7", "row": 1, "x0": 53}], ops=[strike()])
    err(await send(device_api, token, cid, lay), 422, "invalid_request", "L7")


async def test_quote_mismatch_422_stale_target(device_api, token, conn, store):
    cid = await conv_with_base(device_api, token, conn)
    before = (await count(conn, "inbound"), len(stored_files(store)))
    r = await send(device_api, token, cid, layout(ops=[strike(quote="milo")]))
    err(r, 422, "stale_target")
    r = await send(device_api, token, cid, layout(ops=[strike(frm=4, to=99)]))
    err(r, 422, "stale_target")
    assert (await count(conn, "inbound"), len(stored_files(store))) == before


async def test_old_base_version_409_stale_base(device_api, token, conn, store):
    cid = await conv_with_base(device_api, token, conn, versions=2)
    before = (await count(conn, "inbound"), len(stored_files(store)))
    err(await send(device_api, token, cid, layout(ops=[strike()]), base=1), 409, "stale_base")
    err(await send(device_api, token, cid, layout(ops=[strike()]), base=3), 409, "stale_base")
    assert (await count(conn, "inbound"), len(stored_files(store))) == before
    assert (await send(device_api, token, cid, layout(ops=[strike()]), base=2)).status_code == 202


async def test_replayed_op_id_409_op_replayed(device_api, token, conn):
    cid = await conv_with_base(device_api, token, conn)
    op = strike()
    reported = {
        **BASE_LAYOUT,
        "ops": [{"id": op["id"], "status": "applied", "note": None}],
        "base_version": 1,
    }
    await specs.add(conn, uuid.UUID(cid), BASE, None, [], None, layout=reported)
    err(await send(device_api, token, cid, layout(ops=[op]), base=2), 409, "op_replayed")


def note_op():
    return {"id": str(uuid.uuid4()), "kind": "note", "rect": [990, 300, 200, 90], "strokes": [0]}


async def test_note_op_accepted_without_target(device_api, token, conn):
    cid = await conv_with_base(device_api, token, conn)
    r = await send(device_api, token, cid, layout(ops=[note_op()]))
    assert r.status_code == 202


async def test_note_op_id_replayed_after_status_report(device_api, token, conn):
    cid = await conv_with_base(device_api, token, conn)
    op = note_op()
    reported = {
        **BASE_LAYOUT,
        "ops": [{"id": op["id"], "status": "rejected", "note": "no line"}],
        "base_version": 1,
    }
    await specs.add(conn, uuid.UUID(cid), BASE, None, [], None, layout=reported)
    err(await send(device_api, token, cid, layout(ops=[op]), base=2), 409, "op_replayed")


async def test_duplicate_op_ids_422(device_api, token, conn):
    cid = await conv_with_base(device_api, token, conn)
    op = strike()
    r = await send(device_api, token, cid, layout(ops=[op, op]))
    err(r, 422, "invalid_request", op["id"])


async def test_fingerprint_covers_layout_and_region_parts(device_api, token, conn):
    cid = await conv_with_base(device_api, token, conn)
    m = nb_meta(conversation_id=cid, base_version=1)
    op = image_op()
    lay = layout(ops=[op])
    first = await post(device_api, token, m, nb_files(lay, regions={(0, 0): region_png()}))
    assert first.status_code == 202
    changed = layout(ops=[op], runs=[{"line_id": "L1", "row": 1, "x0": 53}])
    r = await post(device_api, token, m, nb_files(changed, regions={(0, 0): region_png()}))
    err(r, 422, "idempotency_mismatch")
    r = await post(device_api, token, m, nb_files(lay, regions={(0, 0): region_png(301, 200)}))
    err(r, 422, "idempotency_mismatch")


async def test_client_id_replay_200(device_api, token, conn, store):
    cid = await conv_with_base(device_api, token, conn)
    m = nb_meta(conversation_id=cid, base_version=1)
    fl = nb_files(layout(ops=[strike(), image_op()]), regions={(0, 0): region_png()})
    first = await post(device_api, token, m, fl)
    assert first.status_code == 202
    stored = len(stored_files(store))
    again = await post(device_api, token, m, fl)
    assert again.status_code == 200 and again.json() == first.json()
    assert len(stored_files(store)) == stored
    assert await count(conn, "spec_images") == 1


def noisy_png(w=600, h=600):
    img = Image.frombytes("L", (w, h), os.urandom(w * h))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def test_region_counts_toward_quota(make_app, token, conn):
    region = noisy_png()
    small = sum(len(v[1]) for v in nb_files(layout(ops=[strike()])).values())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=make_app(device_quota_bytes=3 * small + len(region) // 2)
        ),
        base_url="http://dev" + PREFIX,
    ) as api:
        cid = await conv_with_base(api, token, conn)
        r = await send(api, token, cid, layout(ops=[image_op()]), regions={(0, 0): region})
        err(r, 413, "quota_exceeded")
        r = await send(api, token, cid, layout(ops=[strike()]))
        assert r.status_code == 202, r.text


async def test_plain_upload_unchanged(device_api, token, conn, store):
    r = await post(device_api, token, fl=files(with_strokes=True))
    assert r.status_code == 202
    kinds = await rows(conn, "SELECT kind FROM attachments ORDER BY kind")
    assert kinds == [("sketch_png",), ("sketch_strokes",)]
    assert await count(conn, "spec_images") == 0
    assert len(stored_files(store)) == 2


async def test_unknown_layout_index_and_bad_json_422(device_api, token, conn):
    cid = await conv_with_base(device_api, token, conn)
    fl = nb_files(layout())
    fl["layout.1.json"] = ("layout.1.json", json.dumps(layout()).encode(), "application/json")
    r = await post(device_api, token, nb_meta(conversation_id=cid), fl)
    err(r, 422, "invalid_request", "unknown form field")
    r = await send(device_api, token, cid, b"{not json", base=None)
    err(r, 422, "invalid_request", "layout.0.json")
    r = await send(device_api, token, cid, layout() | {"kind": "notebook"}, base=None)
    err(r, 422, "invalid_request", "layout.0.json")


async def test_resent_region_points_at_the_newer_crop(device_api, token, conn):
    cid = await conv_with_base(device_api, token, conn)
    region_id = uuid.uuid4()
    for w in (300, 310):
        lay = layout(ops=[image_op(region_id=region_id)])
        r = await send(device_api, token, cid, lay, regions={(0, 0): region_png(w, 200)})
        assert r.status_code == 202, r.text
    images = await rows(
        conn,
        "SELECT a.width FROM spec_images s JOIN attachments a ON a.id = s.attachment_id "
        "WHERE s.region_id = %s",
        region_id,
    )
    assert images == [(310,)]


async def test_resent_region_deletes_the_replaced_attachment_and_file(
    device_api, token, conn, store
):
    cid = await conv_with_base(device_api, token, conn)
    region_id = uuid.uuid4()
    for w in (300, 310, 320):
        lay = layout(ops=[image_op(region_id=region_id)])
        r = await send(device_api, token, cid, lay, regions={(0, 0): region_png(w, 200)})
        assert r.status_code == 202, r.text
    kept = await rows(conn, "SELECT width, path FROM attachments WHERE kind = 'spec_image'")
    assert [w for w, _ in kept] == [320]
    assert len(stored_files(store)) == await count(conn, "attachments")


async def test_oversized_layout_part_413(make_app, token, conn):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(max_layout_bytes=1024)),
        base_url="http://dev" + PREFIX,
    ) as api:
        cid = await conv_with_base(api, token, conn)
        big = layout() | {"pad": "x" * 2000}
        r = await send(api, token, cid, big, base=None)
        err(r, 413, "too_large", "layout.0.json")
        assert await count(conn, "inbound") == 1  # only the conversation's creating upload
