"""The translator's MCP tools: identity, auth, turn scoping, versions, feedback and caps."""

import uuid

import httpx
import pytest
from helpers import only, rows

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
    """A fake identity: token -> session id, plus a switch that makes the orchestrator fail."""

    def __init__(self):
        self.tokens: dict[str, uuid.UUID] = {}
        self.down = False

    async def resolve(self, token):
        if self.down:
            raise IdentityUnavailable("HTTP 503")
        return self.tokens.get(token)


@pytest.fixture
def sessions():
    return Sessions()


class McpClient:
    """Posts to the MCP app, entering its lifespan per request: pytest-asyncio sets fixtures
    up and tears them down in different tasks, which the anyio task group refuses."""

    def __init__(self, app):
        self.app = app

    async def post(self, path, **kwargs):
        async with (
            self.app.router.lifespan_context(self.app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://mcp"
            ) as c,
        ):
            return await c.post(path, **kwargs)


@pytest.fixture
def mcp(pool, settings, sessions):
    return McpClient(build_mcp_app(pool, settings, sessions.resolve))


async def rpc(client, token, method, params=None):
    headers = {**HEADERS, "mcp-protocol-version": PROTOCOL}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
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


def me_transport(calls, status=200, body=None):
    def handler(request):
        calls.append(request.headers["authorization"])
        if isinstance(status, Exception):
            raise status
        return httpx.Response(status, json=body if body is not None else {})

    return httpx.MockTransport(handler)


async def test_identity_resolves_and_caches_by_token():
    sid = uuid.uuid4()
    calls = []
    now = {"t": 0.0}
    client = httpx.AsyncClient(
        base_url="http://orch-agent", transport=me_transport(calls, body={"session_id": str(sid)})
    )
    ident = OrchestratorIdentity(client, ttl=60, clock=lambda: now["t"])
    token = "secret-token-xyz"
    assert await ident.resolve(token) == sid
    assert await ident.resolve(token) == sid
    assert calls == [f"Bearer {token}"]
    now["t"] = 61
    assert await ident.resolve(token) == sid
    assert len(calls) == 2
    assert token not in repr(ident._cache)
    await client.aclose()


async def test_identity_refused_token_is_none_and_not_cached():
    calls = []
    client = httpx.AsyncClient(base_url="http://o", transport=me_transport(calls, status=401))
    ident = OrchestratorIdentity(client)
    assert await ident.resolve("bad") is None
    assert await ident.resolve("bad") is None
    assert len(calls) == 2
    await client.aclose()


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, {}),
        (200, {"nope": 1}),
        (200, {"session_id": "not-a-uuid"}),
        (httpx.ConnectError("x"), None),
    ],
)
async def test_identity_failure_is_unavailable(status, body):
    client = httpx.AsyncClient(base_url="http://o", transport=me_transport([], status, body))
    with pytest.raises(IdentityUnavailable):
        await OrchestratorIdentity(client).resolve("t")
    await client.aclose()


# auth


async def test_missing_or_unknown_bearer_is_401(mcp):
    for token in (None, "unknown"):
        r = await rpc(mcp, token, "tools/list")
        assert r.status_code == 401 and r.json()["error"] == "unauthorized"


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
    assert set(doc["inputSchema"]["properties"]) == {"markdown"}
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
    result = await call(mcp, token, "document_write", markdown="BOX A\n\n[[illegible]]")
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "stored": True,
        "version": 1,
        "chars": 20,
        "illegible": 1,
    }
    async with pool.connection() as c:
        spec = await specs.get(c, conv, 1)
    assert spec.markdown == "BOX A\n\n[[illegible]]"
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


async def test_feedback_cap_per_run(pool, settings, sessions, translator, conn):
    c = McpClient(
        build_mcp_app(
            pool, settings.model_copy(update={"max_feedback_per_run": 2}), sessions.resolve
        )
    )
    token, _, _ = await translator()
    for _ in range(2):
        assert (await call(c, token, "feedback_add", kind="note", text="n"))["isError"] is False
    assert error_text(await call(c, token, "feedback_add", kind="note", text="n")).startswith(
        "feedback_limit"
    )
    assert len(await rows(conn, "SELECT 1 FROM feedback")) == 2
