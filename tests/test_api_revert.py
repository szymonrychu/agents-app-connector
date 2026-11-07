"""Revert a conversation to an earlier spec version (Q15 B)."""

import uuid

from helpers import bearer, layout, paired, rows
from test_api_sketches_layout import BASE, BASE_LAYOUT, conv_with_base, send
from test_translator_tools import call, mcp, sessions, worker  # noqa: F401  (fixtures)

from agents_app_connector.db import specs

V2 = BASE + "third\n"
V2_LAYOUT = {
    **BASE_LAYOUT,
    "lines": [*BASE_LAYOUT["lines"], {"line_id": "L9", "source_line": 3, "origin": None}],
}


async def two_versions(device_api, token, conn):
    cid = await conv_with_base(device_api, token, conn)
    await conn.execute("UPDATE inbound SET status = 'handled'")
    await specs.add(conn, uuid.UUID(cid), V2, None, [], None, layout=V2_LAYOUT)
    return cid


async def revert(device_api, token, cid, version, base):
    return await device_api.post(
        f"/conversations/{cid}/spec/{version}/revert",
        json={"base_version": base},
        headers=bearer(token),
    )


async def test_revert_copies_markdown_and_layout_as_new_version(device_api, token, conn):
    cid = await two_versions(device_api, token, conn)
    v1 = await specs.get(conn, uuid.UUID(cid), 1)
    r = await revert(device_api, token, cid, 1, 2)
    assert r.status_code == 201 and r.json() == {"version": 3, "reverted_from": 1}
    v3 = await specs.get(conn, uuid.UUID(cid), 3)
    assert v3.markdown == BASE and v3.sketch_ids == v1.sketch_ids
    assert v3.inbound_id is None and v3.run_id is None
    assert v3.layout == {**BASE_LAYOUT, "ops": [], "base_version": 2, "reverted_from": 1}
    r = await device_api.get(f"/conversations/{cid}/spec", headers=bearer(token))
    assert r.json()["version"] == 3 and r.json()["layout"]["reverted_from"] == 1


async def test_revert_emits_spec_event(device_api, token, conn):
    cid = await two_versions(device_api, token, conn)
    assert (await revert(device_api, token, cid, 1, 2)).status_code == 201
    ev = await rows(
        conn, "SELECT type, payload FROM events WHERE conversation_id = %s ORDER BY id", cid
    )
    assert ev[-1] == ("spec", {"version": 3})


async def test_revert_stale_base_409(device_api, token, conn):
    cid = await two_versions(device_api, token, conn)
    r = await revert(device_api, token, cid, 1, 1)
    assert r.status_code == 409 and r.json()["error"] == "stale_base"
    assert (await revert(device_api, token, cid, 1, 2)).status_code == 201
    # a replay of the same request after success
    r = await revert(device_api, token, cid, 1, 2)
    assert r.status_code == 409 and r.json()["error"] == "stale_base"
    assert await specs.latest_version(conn, uuid.UUID(cid)) == 3


async def test_revert_busy_while_turn_open_409(device_api, token, conn):
    cid = await two_versions(device_api, token, conn)
    r = await send(device_api, token, cid, layout(), base=None)
    assert r.status_code == 202
    r = await revert(device_api, token, cid, 1, 2)
    assert r.status_code == 409 and r.json()["error"] == "busy"
    await conn.execute("UPDATE inbound SET status = 'in-progress'")
    r = await revert(device_api, token, cid, 1, 2)
    assert r.status_code == 409 and r.json()["error"] == "busy"
    await conn.execute("UPDATE inbound SET status = 'failed'")
    assert (await revert(device_api, token, cid, 1, 2)).status_code == 201


async def test_revert_to_latest_422(device_api, token, conn):
    cid = await two_versions(device_api, token, conn)
    r = await revert(device_api, token, cid, 2, 2)
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    r = await device_api.post(
        f"/conversations/{cid}/spec/1/revert", json={"base_version": 0}, headers=bearer(token)
    )
    assert r.status_code == 422


async def test_revert_unknown_version_404(device_api, token, conn):
    cid = await two_versions(device_api, token, conn)
    r = await revert(device_api, token, cid, 7, 2)
    assert r.status_code == 404 and r.json()["error"] == "no_spec"


async def test_revert_other_device_404(device_api, token, conn):
    cid = await two_versions(device_api, token, conn)
    other = (await paired(conn, "sketch", "other"))[1]
    r = await revert(device_api, other, cid, 1, 2)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert await specs.latest_version(conn, uuid.UUID(cid)) == 2


async def test_line_ids_after_revert_continue_from_max(device_api, token, conn, fake, request):
    run, tokens, client = (request.getfixturevalue(n) for n in ("worker", "sessions", "mcp"))
    cid = await two_versions(device_api, token, conn)
    assert (await revert(device_api, token, cid, 1, 2)).status_code == 201
    seg = {"id": "p0-r4-0", "row": 4, "x0": 60, "x1": 900, "strokes": [2]}
    r = await send(device_api, token, cid, layout(segments=[seg]), base=3)
    assert r.status_code == 202, r.text
    await run.tick()
    session_id = uuid.UUID(fake.messages[max(fake.messages)]["session_id"])
    tokens.tokens["t"] = session_id
    result = await call(
        client,
        "t",
        "document_write",
        markdown=BASE + "new\n",
        lines=[{"n": 1, "kept": "L1"}, {"n": 2, "kept": "L2"}, {"n": 3, "segments": ["p0-r4-0"]}],
        ops=[],
    )
    assert result["isError"] is False, result
    v4 = await specs.get(conn, uuid.UUID(cid), 4)
    assert [x["line_id"] for x in v4.layout["lines"]] == ["L1", "L2", "L10"]
