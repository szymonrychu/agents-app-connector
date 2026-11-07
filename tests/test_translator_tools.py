"""The translator's MCP tools: identity, auth, turn scoping, versions, feedback and caps."""

import hashlib
import json
import uuid

import httpx
import pytest
from agents_common.orchestrator import OrchestratorClient
from helpers import bearer, layout, only, region_png, rows
from test_api_sketches_layout import BASE, BASE_LAYOUT, image_op, send, strike

from agents_app_connector.db import specs
from agents_app_connector.translator_tools import (
    IdentityUnavailable,
    OrchestratorIdentity,
    build_mcp_app,
)
from agents_app_connector.worker import Worker

HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
PROTOCOL = "2025-06-18"


class Sessions:
    """A fake verifier: token -> session id, plus a switch that makes the orchestrator fail."""

    def __init__(self):
        self.tokens: dict[str, uuid.UUID] = {}
        self.down = False

    async def verify(self, token, session_id):
        if self.down:
            raise IdentityUnavailable("HTTP 503")
        return self.tokens.get(token) == session_id


@pytest.fixture
def sessions():
    return Sessions()


class McpClient:
    """Posts to the MCP app, entering its lifespan per request: pytest-asyncio sets fixtures
    up and tears them down in different tasks, which the anyio task group refuses."""

    def __init__(self, app, sessions):
        self.app = app
        self.sessions = sessions

    async def post(self, path, **kwargs):
        async with (
            self.app.router.lifespan_context(self.app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://mcp"
            ) as c,
        ):
            return await c.post(path, **kwargs)


@pytest.fixture
def mcp(pool, settings, sessions, store):
    return McpClient(build_mcp_app(pool, settings, sessions.verify, store), sessions)


NO_HEADER = object()


async def rpc(client, token, method, params=None, session=None):
    """`session` is the X-Orch-Session value: the token's session by default, NO_HEADER to omit."""
    headers = {**HEADERS, "mcp-protocol-version": PROTOCOL}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if session is None:
        session = client.sessions.tokens.get(token, uuid.uuid4())
    if session is not NO_HEADER:
        headers["x-orch-session"] = str(session)
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return await client.post("/mcp", headers=headers, json=body)


async def call(client, token, name, **arguments):
    r = await rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def error_text(result):
    assert result["isError"] is True
    return result["content"][0]["text"]


@pytest.fixture
def worker(orchestrator, pool, settings, store, preset):
    return Worker(orchestrator, pool, settings, store)


@pytest.fixture
def translator(worker, fake, upload, sessions):
    """Start a first run; the session's token is `tok-<n>`. Returns (token, conversation)."""

    async def go(conv="current"):
        sketch_id, cid = await upload(conv)
        await worker.tick()
        session_id = uuid.UUID(fake.messages[max(fake.messages)]["session_id"])
        token = f"tok-{len(sessions.tokens)}"
        sessions.tokens[token] = session_id
        return token, cid, sketch_id

    return go


async def events_of(conn, conv):
    return await rows(
        conn, "SELECT type, payload FROM events WHERE conversation_id = %s ORDER BY id", conv
    )


# identity


def verify_client(calls, owner=None, status=200, body=None):
    """An OrchestratorClient on a mock service port: `owner` is the one session that accepts the
    token; every other session answers the orchestrator's 404 `not_found`."""

    def handler(request):
        calls.append((request.url.path, json.loads(request.content)["token"]))
        if isinstance(status, Exception):
            raise status
        if status != 200:
            return httpx.Response(status, json={})
        sid = request.url.path.split("/")[4]
        if str(owner) != sid:
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "x"}})
        return httpx.Response(200, json=body if body is not None else {"session_id": sid})

    return OrchestratorClient("http://orch", "svc", transport=httpx.MockTransport(handler))


async def test_identity_valid_token_is_one_verify_call_for_the_named_session():
    sid = uuid.uuid4()
    calls = []
    client = verify_client(calls, owner=sid)
    token = "secret-token-xyz"
    assert await OrchestratorIdentity(client).verify(token, sid) is True
    assert calls == [(f"/api/v1/sessions/{sid}/agent-token/verify", token)]
    await client.aclose()


async def test_identity_refused_token_is_false_and_not_cached():
    calls = []
    client = verify_client(calls, owner=None)
    ident = OrchestratorIdentity(client)
    sid = uuid.uuid4()
    assert await ident.verify("bad", sid) is False
    assert await ident.verify("bad", sid) is False
    assert len(calls) == 2
    assert ident._cache == {}
    await client.aclose()


async def test_identity_token_of_another_session_is_refused():
    sid, other = uuid.uuid4(), uuid.uuid4()
    client = verify_client([], owner=sid)
    assert await OrchestratorIdentity(client).verify("t", other) is False
    await client.aclose()


async def test_identity_cache_hit_by_token_hash_and_session_expires_after_ttl():
    sid, other = uuid.uuid4(), uuid.uuid4()
    calls = []
    now = {"t": 0.0}
    client = verify_client(calls, owner=sid)
    ident = OrchestratorIdentity(client, ttl=60, clock=lambda: now["t"])
    token = "secret-token-xyz"
    assert await ident.verify(token, sid) is True
    assert await ident.verify(token, sid) is True
    assert len(calls) == 1
    assert list(ident._cache) == [(hashlib.sha256(token.encode()).hexdigest(), sid)]
    assert token not in repr(ident._cache)
    assert await ident.verify(token, other) is False
    assert len(calls) == 2
    now["t"] = 61
    assert await ident.verify(token, sid) is True
    assert len(calls) == 3
    await client.aclose()


async def test_identity_200_for_a_different_session_is_unavailable_not_accepted():
    sid, other = uuid.uuid4(), uuid.uuid4()
    client = verify_client([], owner=sid, body={"session_id": str(other)})
    ident = OrchestratorIdentity(client)
    with pytest.raises(IdentityUnavailable):
        await ident.verify("tok-abc", sid)
    assert ident._cache == {}
    await client.aclose()


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, {}),
        (401, {}),
        (200, {"nope": 1}),
        (200, {"session_id": "not-a-uuid"}),
        (httpx.ConnectError("x"), None),
    ],
)
async def test_identity_failure_is_unavailable(status, body):
    sid = uuid.uuid4()
    client = verify_client([], owner=sid, status=status, body=body)
    with pytest.raises(IdentityUnavailable) as err:
        await OrchestratorIdentity(client).verify("tok-abc", sid)
    assert "tok-abc" not in str(err.value)
    await client.aclose()


# auth


async def test_missing_or_unknown_bearer_is_401(mcp):
    for token in (None, "unknown"):
        r = await rpc(mcp, token, "tools/list")
        assert r.status_code == 401 and r.json()["error"] == "unauthorized"


async def test_missing_session_header_is_401_even_for_a_valid_token(mcp, sessions):
    sessions.tokens["t"] = uuid.uuid4()
    r = await rpc(mcp, "t", "tools/list", session=NO_HEADER)
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert "X-Orch-Session" in r.json()["detail"]


async def test_malformed_session_header_is_401(mcp, sessions):
    sessions.tokens["t"] = uuid.uuid4()
    r = await rpc(mcp, "t", "tools/list", session="not-a-uuid")
    assert r.status_code == 401


async def test_token_of_another_session_than_the_header_is_401(mcp, sessions):
    sessions.tokens["t"] = uuid.uuid4()
    r = await rpc(mcp, "t", "tools/list", session=uuid.uuid4())
    assert r.status_code == 401


async def test_verified_header_session_is_the_caller(mcp, sessions, translator, conn):
    token, _, _ = await translator()
    result = await call(mcp, token, "feedback_add", kind="note", text="n")
    assert result["isError"] is False


async def test_authorization_header_never_reaches_fastmcp(mcp, sessions, monkeypatch):
    from fastmcp.server import dependencies

    seen = []
    real = dependencies.get_http_request

    def spy():
        request = real()
        seen.append(dict(request.headers))
        return request

    monkeypatch.setattr("agents_app_connector.translator_tools.get_http_request", spy)
    sessions.tokens["secret-xyz"] = uuid.uuid4()
    await call(mcp, "secret-xyz", "feedback_add", kind="note", text="n")
    assert seen
    assert all("authorization" not in h for h in seen)
    assert all("secret-xyz" not in v for h in seen for v in h.values())


async def test_token_never_reaches_the_logs(mcp, sessions, caplog):
    caplog.set_level("DEBUG")
    sessions.tokens["secret-xyz"] = uuid.uuid4()
    await call(mcp, "secret-xyz", "feedback_add", kind="note", text="n")
    await call(mcp, "secret-xyz", "document_write", markdown="x")
    await rpc(mcp, "wrong-secret-abc", "tools/list")
    sessions.down = True
    await rpc(mcp, "secret-xyz", "tools/list")
    assert "secret-xyz" not in caplog.text
    assert "wrong-secret-abc" not in caplog.text


async def test_identity_down_is_503(mcp, sessions):
    sessions.tokens["t"] = uuid.uuid4()
    sessions.down = True
    r = await rpc(mcp, "t", "tools/list")
    assert r.status_code == 503 and r.json()["error"] == "unavailable"


async def test_lists_exactly_the_two_tools_with_the_contract(mcp, sessions):
    sessions.tokens["t"] = uuid.uuid4()
    r = await rpc(mcp, "t", "tools/list")
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == {"document_write", "feedback_add"}
    doc = tools["document_write"]
    assert doc["inputSchema"]["required"] == ["markdown"]
    assert set(doc["inputSchema"]["properties"]) == {"markdown", "lines", "ops"}
    assert "For notebook pages also pass lines" in doc["description"]
    for word in ("faithful", "nothing else", "front matter", "Requirements", "Open questions"):
        assert word in doc["description"], word
    assert "feedback_add" in doc["description"]
    fb = tools["feedback_add"]
    assert fb["inputSchema"]["properties"]["kind"]["enum"] == [
        "question",
        "uncertain",
        "illegible",
        "note",
    ]
    assert set(fb["inputSchema"]["required"]) == {"kind", "text"}
    assert all(t["description"].isascii() for t in tools.values())


# document_write


async def test_no_active_turn_without_a_run(mcp, sessions):
    sessions.tokens["t"] = uuid.uuid4()
    result = await call(mcp, "t", "document_write", markdown="# x")
    assert error_text(result).startswith("no_active_turn")
    result = await call(mcp, "t", "feedback_add", kind="note", text="n")
    assert error_text(result).startswith("no_active_turn")


async def test_document_write_stores_next_version_with_run_and_event(mcp, translator, conn, pool):
    token, conv, sketch_id = await translator()
    result = await call(mcp, token, "document_write", markdown="BOX A\n\n[illegible]")
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "stored": True,
        "version": 1,
        "chars": 18,
        "illegible": 1,
        "lines": 3,
    }
    async with pool.connection() as c:
        spec = await specs.get(c, conv, 1)
    assert spec.markdown == "BOX A\n\n[illegible]"
    assert [str(i) for i in spec.sketch_ids] == [sketch_id] and str(spec.inbound_id) == sketch_id
    (run_id,) = only(await rows(conn, "SELECT id FROM runs"))
    assert await rows(conn, "SELECT run_id FROM specs") == [(run_id,)]
    assert (await events_of(conn, conv))[-1] == ("spec", {"version": 1})
    again = await call(mcp, token, "document_write", markdown="BOX B")
    assert again["structuredContent"]["version"] == 2
    assert await rows(conn, "SELECT status FROM runs") == [("in-progress",)]


async def test_document_write_rejects_empty_and_too_large(mcp, translator, settings, conn):
    token, _, _ = await translator()
    assert error_text(await call(mcp, token, "document_write", markdown=" \n")).startswith(
        "empty_document"
    )
    big = "x" * (settings.max_document_bytes + 1)
    assert error_text(await call(mcp, token, "document_write", markdown=big)).startswith(
        "document_too_large"
    )
    assert await rows(conn, "SELECT 1 FROM specs") == []


async def test_finished_run_or_deleted_conversation_is_no_active_turn(mcp, translator, conn):
    token, conv, _ = await translator()
    await conn.execute("UPDATE conversations SET state = 'deleted' WHERE id = %s", (conv,))
    assert error_text(await call(mcp, token, "document_write", markdown="x")).startswith(
        "no_active_turn"
    )
    await conn.execute("UPDATE conversations SET state = 'open' WHERE id = %s", (conv,))
    await conn.execute("UPDATE runs SET status = 'handled'")
    assert error_text(await call(mcp, token, "document_write", markdown="x")).startswith(
        "no_active_turn"
    )


async def test_released_binding_is_no_active_turn(mcp, translator, conn):
    token, _, _ = await translator()
    await conn.execute("UPDATE bindings SET released_at = now(), reason = 'x'")
    assert error_text(await call(mcp, token, "feedback_add", kind="note", text="n")).startswith(
        "no_active_turn"
    )


async def test_each_session_writes_only_its_own_conversation(mcp, translator, conn):
    token_a, conv_a, _ = await translator("new")
    token_b, conv_b, _ = await translator("new")
    assert conv_a != conv_b
    await call(mcp, token_a, "document_write", markdown="A")
    await call(mcp, token_b, "feedback_add", kind="question", text="B?")
    assert await rows(conn, "SELECT conversation_id::text, markdown FROM specs") == [(conv_a, "A")]
    assert await rows(conn, "SELECT conversation_id::text, text FROM feedback") == [(conv_b, "B?")]


# feedback_add


async def test_feedback_add_stores_item_and_event(mcp, translator, conn):
    token, conv, sketch_id = await translator()
    result = await call(
        mcp, token, "feedback_add", kind="question", text=" Which box? ", where="page 1"
    )
    fid = result["structuredContent"]["id"]
    assert result["structuredContent"] == {"stored": True, "id": fid}
    (run_id,) = only(await rows(conn, "SELECT id FROM runs"))
    assert await rows(
        conn, "SELECT run_id, inbound_id::text, kind, text, where_text, spec_version FROM feedback"
    ) == [(run_id, sketch_id, "question", "Which box?", "page 1", None)]
    assert (await events_of(conn, conv))[-1] == (
        "feedback",
        {"feedback_id": fid, "kind": "question"},
    )


async def test_feedback_add_validation(mcp, translator, conn):
    token, _, _ = await translator()
    bad_kind = await call(mcp, token, "feedback_add", kind="praise", text="x")
    assert bad_kind["isError"] is True
    cases = [
        ({"kind": "note", "text": "  "}, "empty_text"),
        ({"kind": "note", "text": "x" * 2001}, "text_too_long"),
        ({"kind": "note", "text": "x", "where": "w" * 201}, "where_too_long"),
    ]
    for args, code in cases:
        assert error_text(await call(mcp, token, "feedback_add", **args)).startswith(code)
    assert await rows(conn, "SELECT 1 FROM feedback") == []


async def test_feedback_cap_per_run(pool, settings, sessions, translator, conn, store):
    c = McpClient(
        build_mcp_app(
            pool,
            settings.model_copy(update={"max_feedback_per_run": 2}),
            sessions.verify,
            store,
        ),
        sessions,
    )
    token, _, _ = await translator()
    for _ in range(2):
        assert (await call(c, token, "feedback_add", kind="note", text="n"))["isError"] is False
    assert error_text(await call(c, token, "feedback_add", kind="note", text="n")).startswith(
        "feedback_limit"
    )
    assert len(await rows(conn, "SELECT 1 FROM feedback")) == 2


# notebook turns


@pytest.fixture
def notebook(worker, fake, sessions, device_api, token, conn):
    """A conversation with base v1 (BASE, L1 and L2) and a notebook turn running on it, whose
    layout has one segment, a strike on L1 and an image op. Returns (token, cid, ids)."""

    async def go():
        r = await device_api.post("/conversations", headers=bearer(token))
        cid = r.json()["id"]
        await specs.add(conn, uuid.UUID(cid), BASE, None, [], None, layout=BASE_LAYOUT)
        region_id = uuid.uuid4()
        ops = [strike(), image_op(region_id=region_id)]
        seg = {"id": "p0-r4-0", "row": 4, "x0": 60, "x1": 900, "strokes": [2]}
        lay = layout(segments=[seg], runs=[{"line_id": "L1", "row": 1, "x0": 53}], ops=ops)
        r = await send(device_api, token, cid, lay, regions={(0, 0): region_png()})
        assert r.status_code == 202, r.text
        await worker.tick()
        session_id = uuid.UUID(fake.messages[max(fake.messages)]["session_id"])
        tok = f"tok-{len(sessions.tokens)}"
        sessions.tokens[tok] = session_id
        return tok, cid, {"ops": [o["id"] for o in ops], "region": str(region_id)}

    return go


def notebook_doc(region):
    return f"buy\nand bread\nnew line\n![image](images/{region}.png)\n"


def notebook_args(ids, **over):
    args = {
        "markdown": notebook_doc(ids["region"]),
        "lines": [
            {"n": 1, "kept": "L1", "op": ids["ops"][0]},
            {"n": 2, "kept": "L2"},
            {"n": 3, "segments": ["p0-r4-0"]},
            {"n": 4, "op": ids["ops"][1]},
        ],
        "ops": [
            {"id": ids["ops"][0], "status": "applied"},
            {"id": ids["ops"][1], "status": "applied", "note": "placed after L2"},
        ],
    }
    return {**args, **over}


async def test_notebook_turn_stores_layout_with_line_ids(mcp, notebook, conn):
    token, cid, ids = await notebook()
    await specs.add(
        conn, uuid.UUID(cid), "x", None, [], None, layout={"lines": [{"line_id": "L40"}]}
    )
    result = await call(mcp, token, "document_write", **notebook_args(ids))
    assert result["isError"] is False, result
    assert result["structuredContent"]["lines"] == 4
    row = await specs.get(conn, uuid.UUID(cid), result["structuredContent"]["version"])
    assert row.layout == {
        "lines": [
            {"line_id": "L1", "source_line": 1, "origin": {"kept": "L1", "op": ids["ops"][0]}},
            {"line_id": "L2", "source_line": 2, "origin": {"kept": "L2"}},
            {"line_id": "L41", "source_line": 3, "origin": {"segments": ["p0-r4-0"]}},
            {"line_id": "L42", "source_line": 4, "origin": {"op": ids["ops"][1]}},
        ],
        "ops": [
            {"id": ids["ops"][0], "status": "applied", "note": None},
            {"id": ids["ops"][1], "status": "applied", "note": "placed after L2"},
        ],
        "base_version": 1,
        "reverted_from": None,
    }


async def test_notebook_turn_without_lines_is_layout_incomplete(mcp, notebook, conn):
    token, cid, ids = await notebook()
    result = await call(mcp, token, "document_write", markdown=notebook_doc(ids["region"]))
    text = error_text(result)
    assert text.startswith("layout_incomplete")
    for item in ("p0-r4-0", *ids["ops"]):
        assert item in text, item
    assert await specs.latest_version(conn, uuid.UUID(cid)) == 1


async def test_missing_op_status_is_layout_incomplete(mcp, notebook, conn):
    token, cid, ids = await notebook()
    args = notebook_args(ids, ops=[{"id": ids["ops"][0], "status": "rejected", "note": "x"}])
    text = error_text(await call(mcp, token, "document_write", **args))
    assert text.startswith("layout_incomplete") and ids["ops"][1] in text
    assert ids["ops"][0] not in text
    args = notebook_args(ids)
    args["lines"][2] = {"n": 3}
    text = error_text(await call(mcp, token, "document_write", **args))
    assert text.startswith("layout_incomplete") and "p0-r4-0" in text
    assert await specs.latest_version(conn, uuid.UUID(cid)) == 1


async def test_notebook_turn_bad_lines_are_layout_invalid(mcp, notebook, conn):
    token, cid, ids = await notebook()
    cases = [
        notebook_args(ids, lines=[*notebook_args(ids)["lines"], {"n": 9}]),
        notebook_args(ids, lines=[{"n": 1, "kept": "L9"}, *notebook_args(ids)["lines"][1:]]),
        notebook_args(
            ids, ops=[*notebook_args(ids)["ops"], {"id": str(uuid.uuid4()), "status": "applied"}]
        ),
        notebook_args(ids, ops=[{"id": ids["ops"][0], "status": "done"}]),
    ]
    for args in cases:
        text = error_text(await call(mcp, token, "document_write", **args))
        assert text.startswith("layout_invalid"), text
    assert await specs.latest_version(conn, uuid.UUID(cid)) == 1


async def test_unknown_image_reference(mcp, notebook, conn):
    token, cid, ids = await notebook()
    other = str(uuid.uuid4())
    args = notebook_args(ids, markdown=notebook_doc(ids["region"]) + f"![x](images/{other}.png)\n")
    args["lines"].append({"n": 5})
    text = error_text(await call(mcp, token, "document_write", **args))
    assert text.startswith("unknown_image") and other in text
    assert ids["region"] not in text
    assert await specs.latest_version(conn, uuid.UUID(cid)) == 1


async def test_plain_turn_rejects_lines_and_ops(mcp, translator, conn):
    token, conv, _ = await translator()
    for extra in ({"lines": [{"n": 1}]}, {"ops": [{"id": str(uuid.uuid4()), "status": "applied"}]}):
        result = await call(mcp, token, "document_write", markdown="a", **extra)
        assert error_text(result).startswith("layout_not_expected")
    result = await call(mcp, token, "document_write", markdown="a\nb", lines=[], ops=[])
    assert result["isError"] is False and result["structuredContent"]["lines"] == 2
    row = await specs.latest(conn, uuid.UUID(conv))
    assert row.layout is None


async def test_text_follow_up_carries_the_layout_so_the_next_upload_works(
    mcp, notebook, conn, worker, fake, sessions, say, device_api, token
):
    tok, cid, ids = await notebook()
    result = await call(mcp, tok, "document_write", **notebook_args(ids))
    assert result["isError"] is False, result
    v2 = await specs.get(conn, uuid.UUID(cid), 2)
    await conn.execute("UPDATE runs SET status = 'handled', finished_at = now()")
    await conn.execute("UPDATE inbound SET status = 'handled'")
    await say(cid, "make it shorter")
    await worker.tick()
    session_id = uuid.UUID(fake.messages[max(fake.messages)]["session_id"])
    sessions.tokens["follow"] = session_id
    short = notebook_doc(ids["region"]).replace("new line\n", "")
    result = await call(mcp, "follow", "document_write", markdown=short)
    assert result["isError"] is False, result
    v3 = await specs.get(conn, uuid.UUID(cid), 3)
    assert v3.layout["ops"] == [] and v3.layout["base_version"] == 2
    assert [x["line_id"] for x in v3.layout["lines"]] == ["L1", "L2", "L3"]
    assert [x["source_line"] for x in v3.layout["lines"]] == [1, 2, 3]
    assert v3.layout["lines"][0]["line_id"] == v2.layout["lines"][0]["line_id"]
    await conn.execute("UPDATE runs SET status = 'handled', finished_at = now()")
    await conn.execute("UPDATE inbound SET status = 'handled'")
    r = await send(device_api, token, cid, layout(runs=[{"line_id": "L1", "row": 1, "x0": 53}]), 3)
    assert r.status_code == 202, r.text


async def test_line_without_provenance_is_layout_incomplete(mcp, notebook, conn):
    token, cid, ids = await notebook()
    args = notebook_args(ids)
    args["lines"][1] = {"n": 2}
    text = error_text(await call(mcp, token, "document_write", **args))
    assert text.startswith("layout_incomplete") and "kept" in text and "2" in text
    assert await specs.latest_version(conn, uuid.UUID(cid)) == 1
    args["lines"][1] = {"n": 2, "kept": "L2"}
    assert (await call(mcp, token, "document_write", **args))["isError"] is False
