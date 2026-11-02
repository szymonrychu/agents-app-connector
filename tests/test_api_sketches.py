import hashlib
import json
import uuid

import httpx
import pytest
from helpers import PREFIX, bearer, count, files, meta, png, post, stored_files

from agents_app_connector.db import devices
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
    device = await devices.by_token(conn, token)
    m = meta()
    client_id = json.loads(m)["client_id"]
    await conn.execute(
        "SELECT pg_advisory_lock(hashtextextended(%s::text || %s::text, 0))",
        (device.id, client_id),
    )
    r = await post(device_api, token, m)
    assert r.status_code == 409 and r.json()["error"] == "in_progress"
    assert await count(conn, "inbound") == 0


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
        {"page": -1},
        {"conversation": "other"},
        {"client_id": "nope"},
        {"canvas": {"w": 1, "h": 1}},
        {"canvas": {"w": "400", "h": 300, "dpi": 300}},
        {"canvas": {"w": 400, "h": -3, "dpi": 300}},
        {"canvas": {"w": 400, "h": 300, "dpi": 0}},
        {"canvas": {"w": 400, "h": 300, "dpi": -1}},
        {"canvas": {"w": 400.5, "h": 300, "dpi": 300}},
        {"canvas": {"w": 400, "h": 300, "dpi": True}},
    ],
)
async def test_meta_invalid_422(device_api, token, conn, bad):
    r = await post(device_api, token, meta(**bad))
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    assert await count(conn, "inbound") == 0


@pytest.mark.parametrize("dpi", ["NaN", "Infinity", "-Infinity"])
async def test_meta_non_finite_dpi_422(device_api, token, dpi):
    raw = json.dumps({"client_id": str(uuid.uuid4()), "canvas": {"w": 4, "h": 3, "dpi": 0}})
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
