from helpers import bearer, paired

from agents_app_connector.db import conversations, feedback


async def _conv(device_api, token):
    return (await device_api.post("/conversations", headers=bearer(token))).json()["id"]


async def test_feedback_route_lists_items_after_cursor(device_api, token, conn):
    cid = await _conv(device_api, token)
    empty = await device_api.get(f"/conversations/{cid}/feedback", headers=bearer(token))
    assert empty.status_code == 200 and empty.json() == {"items": [], "cursor": 0}
    a = await feedback.add(conn, cid, 1, None, "question", "which box?", where="page 1")
    b = await feedback.add(conn, cid, 1, None, "note", "n")
    await feedback.set_spec_version(conn, 1, 2)
    r = await device_api.get(f"/conversations/{cid}/feedback", headers=bearer(token))
    body = r.json()
    assert body["cursor"] == b
    first = body["items"][0]
    assert {k: first[k] for k in ("id", "kind", "text", "where", "spec_version")} == {
        "id": a,
        "kind": "question",
        "text": "which box?",
        "where": "page 1",
        "spec_version": 2,
    }
    assert first["created_at"]
    after = await device_api.get(f"/conversations/{cid}/feedback?after={a}", headers=bearer(token))
    assert [i["id"] for i in after.json()["items"]] == [b]
    done = await device_api.get(f"/conversations/{cid}/feedback?after={b}", headers=bearer(token))
    assert done.json() == {"items": [], "cursor": b}


async def test_feedback_route_foreign_or_unknown_conversation_404(device_api, token, conn):
    other, _ = await paired(conn, "sketch", "other")
    foreign = await conversations.create(conn, other.id, "sketch")
    r = await device_api.get(f"/conversations/{foreign}/feedback", headers=bearer(token))
    assert r.status_code == 404 and r.json()["error"] == "not_found"


async def test_feedback_route_needs_token(device_api, token):
    cid = await _conv(device_api, token)
    assert (await device_api.get(f"/conversations/{cid}/feedback")).status_code == 401


async def test_list_conversations_carries_latest_feedback_id(device_api, token, conn):
    cid = await _conv(device_api, token)
    rows = (await device_api.get("/conversations", headers=bearer(token))).json()
    assert rows[0]["latest_feedback_id"] is None
    fid = await feedback.add(conn, cid, 1, None, "note", "n")
    rows = (await device_api.get("/conversations", headers=bearer(token))).json()
    assert rows[0]["latest_feedback_id"] == fid


async def test_delete_conversation_deletes_feedback(device_api, token, conn):
    cid = await _conv(device_api, token)
    await feedback.add(conn, cid, 1, None, "note", "n")
    r = await device_api.delete(f"/conversations/{cid}", headers=bearer(token))
    assert r.status_code == 204
    assert await feedback.after(conn, cid, 0) == []
