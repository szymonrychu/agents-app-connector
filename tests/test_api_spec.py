import hashlib
import uuid

from helpers import bearer, paired, png


async def seed_spec(conn, cid, version, markdown, sketch_ids=()):
    await conn.execute(
        "INSERT INTO specs (conversation_id, version, markdown, sketch_ids) "
        "VALUES (%s, %s, %s, %s)",
        (cid, version, markdown, list(sketch_ids)),
    )


async def test_spec_routes_and_markdown_type(device_api, token, conn, upload):
    sketch_id, cid = await upload()
    await seed_spec(conn, cid, 1, "# one", [uuid.UUID(sketch_id)])
    await seed_spec(conn, cid, 2, "# two", [uuid.UUID(sketch_id)])
    r = await device_api.get(f"/conversations/{cid}/spec", headers=bearer(token))
    body = r.json()
    assert r.status_code == 200 and body["version"] == 2 and body["markdown"] == "# two"
    assert body["sketch_ids"] == [sketch_id] and body["created"]
    r = await device_api.get(f"/conversations/{cid}/spec/1", headers=bearer(token))
    assert r.json()["markdown"] == "# one"
    r = await device_api.get(f"/conversations/{cid}/spec/9", headers=bearer(token))
    assert r.status_code == 404 and r.json()["error"] == "no_spec"
    r = await device_api.get(f"/conversations/{cid}/spec.md", headers=bearer(token))
    assert r.headers["content-type"] == "text/markdown; charset=utf-8" and r.text == "# two"


async def test_no_spec_and_foreign_conversation(device_api, token, conn, upload):
    _, cid = await upload()
    for path in ("spec", "spec.md", "spec/1"):
        r = await device_api.get(f"/conversations/{cid}/{path}", headers=bearer(token))
        assert r.status_code == 404 and r.json()["error"] == "no_spec"
    other = (await paired(conn, "sketch", "other"))[1]
    r = await device_api.get(f"/conversations/{cid}/spec", headers=bearer(other))
    assert r.status_code == 404 and r.json()["error"] == "not_found"


async def test_attachment_etag_304_and_other_device_404(device_api, token, conn, upload):
    sketch_id, _ = await upload()
    data = png()
    r = await device_api.get(f"/attachments/{sketch_id}", headers=bearer(token))
    etag = f'"{hashlib.sha256(data).hexdigest()}"'
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-type"] == "image/png" and r.headers["etag"] == etag
    r = await device_api.get(
        f"/attachments/{sketch_id}", headers={**bearer(token), "If-None-Match": etag}
    )
    assert r.status_code == 304 and r.content == b""
    other = (await paired(conn, "sketch", "other"))[1]
    r = await device_api.get(f"/attachments/{sketch_id}", headers=bearer(other))
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = await device_api.get(f"/attachments/{uuid.uuid4()}", headers=bearer(token))
    assert r.status_code == 404


async def test_purged_attachment_is_410(device_api, token, conn, upload):
    sketch_id, _ = await upload()
    await conn.execute("UPDATE attachments SET purged_at = now()")
    r = await device_api.get(f"/attachments/{sketch_id}", headers=bearer(token))
    assert r.status_code == 410 and r.json()["error"] == "purged"


async def test_spec_json_includes_layout(device_api, token, conn, upload):
    from agents_app_connector.db import specs

    _, cid = await upload()
    lay = {
        "lines": [{"line_id": "L1", "source_line": 1, "origin": {"segments": ["p0-r1-0"]}}],
        "ops": [{"id": str(uuid.uuid4()), "status": "applied", "note": None}],
        "base_version": None,
        "reverted_from": None,
    }
    await specs.add(conn, uuid.UUID(cid), "a", None, [], None, layout=lay)
    for path in ("spec", "spec/1"):
        r = await device_api.get(f"/conversations/{cid}/{path}", headers=bearer(token))
        assert r.status_code == 200 and r.json()["layout"] == lay


async def test_spec_json_layout_null_for_plain(device_api, token, conn, upload):
    _, cid = await upload()
    await seed_spec(conn, cid, 1, "# one")
    r = await device_api.get(f"/conversations/{cid}/spec", headers=bearer(token))
    assert r.status_code == 200 and r.json()["layout"] is None
