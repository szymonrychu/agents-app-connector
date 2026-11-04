"""Task relay routes of the sketch plugin, against a fake taskboard over httpx.MockTransport."""

import json
import logging

import httpx
import pytest
from helpers import PREFIX, Sketch, bearer
from pydantic import SecretStr

from agents_app_connector.api.device import create_device_app
from agents_app_connector.db import devices

DELEGATE = "delegate-test-token"
UP = "/taskboard/api/v1"


class FakeTaskboard:
    """Records every upstream request and answers with a canned status and JSON body."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: object = {"ok": True}
        self.raw: bytes | None = None
        self.raises: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        if self.raw is not None:
            return httpx.Response(self.status, content=self.raw)
        return httpx.Response(self.status, json=self.body)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


@pytest.fixture
def board():
    return FakeTaskboard()


@pytest.fixture
def make_task_app(pool, settings, store, board):
    def make(configured=True, **overrides):
        extra = (
            {"taskboard_url": "http://taskboard", "taskboard_token": SecretStr(DELEGATE)}
            if configured
            else {}
        )
        client = httpx.AsyncClient(
            base_url="http://taskboard", transport=httpx.MockTransport(board.handler)
        )
        return create_device_app(
            pool,
            settings.model_copy(update={**extra, **overrides}),
            [Sketch()],
            store,
            taskboard=client if configured else None,
        )

    return make


@pytest.fixture
async def api(make_task_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_task_app()), base_url="http://dev" + PREFIX
    ) as c:
        yield c


ROUTES = [
    ("GET", "/tasks", "/tasks", None),
    ("POST", "/tasks", "/tasks", {"kind": "task", "title": "t", "client_key": "k"}),
    ("GET", "/tasks/T-12", "/tasks/T-12", None),
    ("PATCH", "/tasks/T-12", "/tasks/T-12", {"version": 3, "labels": ["a"]}),
    ("POST", "/tasks/T-12/comments", "/tasks/T-12/comments", {"body": "hi"}),
    ("PUT", "/tasks/T-12/todo", "/tasks/T-12/todo", {"items": [{"text": "x", "status": "todo"}]}),
    ("PATCH", "/tasks/T-12/todo/7", "/tasks/T-12/todo/7", {"status": "done"}),
    ("POST", "/tasks/T-12/decisions/5/answer", "/tasks/T-12/decisions/5/answer", {"choice": 0}),
]


@pytest.mark.parametrize(("method", "path", "upstream", "body"), ROUTES)
async def test_each_route_forwards_method_path_query_and_body(
    api, token, board, method, path, upstream, body
):
    board.body = {"echo": upstream}
    r = await api.request(method, path, json=body, headers=bearer(token))
    assert r.status_code == 200 and r.json() == {"echo": upstream}
    up = board.last
    assert up.method == method and up.url.path == UP + upstream
    assert (json.loads(up.content) if up.content else None) == body
    if body is not None:
        assert up.headers["content-type"] == "application/json"


async def test_query_string_passed_through(api, token, board):
    q = "label=a&label=b&status=done&cursor=c&limit=10&session=s"
    await api.get(f"/tasks?{q}", headers=bearer(token))
    assert board.last.url.query.decode() == q


async def test_only_delegate_bearer_goes_upstream(api, token, board):
    await api.post(
        "/tasks/T-1/comments",
        json={"body": "x"},
        headers={**bearer(token), "X-Orch-Session": "s", "Cookie": "c=1", "X-Custom": "1"},
    )
    up = board.last
    assert up.headers["authorization"] == f"Bearer {DELEGATE}"
    assert token not in " ".join(f"{k}={v}" for k, v in up.headers.items())
    sent = {k.lower() for k in up.headers}
    assert not sent & {"x-orch-session", "cookie", "x-custom"}
    await api.get("/tasks", headers=bearer(token))
    assert "content-type" not in board.last.headers


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, {"a": 1}),
        (201, {"key": "T-1"}),
        (404, {"error": {"code": "not_found", "message": "no task"}}),
        (409, {"error": {"code": "already_answered", "message": "x"}}),
        (412, {"error": {"code": "stale_version", "message": "x"}}),
        (422, {"error": {"code": "invalid_request", "message": "x"}}),
    ],
)
async def test_upstream_status_and_body_pass_through(api, token, board, status, body):
    board.status, board.body = status, body
    r = await api.get("/tasks/T-1", headers=bearer(token))
    assert r.status_code == status and r.json() == body


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/tasks/T-1/decisions"),
        ("POST", "/tasks/T-1/decisions/1/withdraw"),
        ("POST", "/tasks/T-1/decisions"),
        ("GET", "/tasks/x"),
        ("GET", "/tasks/stream-not"),
        ("GET", "/tasks/T-1/decisions/x/answer"),
        ("PATCH", "/tasks/T-1/todo/abc"),
        ("DELETE", "/tasks/T-1"),
        ("DELETE", "/tasks"),
        ("PUT", "/tasks/T-1"),
        ("GET", "/tasks/T-1/comments"),
    ],
)
async def test_path_outside_allowlist_is_404_not_found(api, token, board, method, path):
    r = await api.request(method, path, headers=bearer(token))
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert board.requests == []


async def test_body_over_256kib_is_413_payload_too_large(api, token, board):
    limit = 256 * 1024
    ok = await api.post("/tasks/T-1/comments", content=b"x" * limit, headers=bearer(token))
    assert ok.status_code == 200
    big = b"x" * (limit + 1)
    r = await api.post("/tasks/T-1/comments", content=big, headers=bearer(token))
    assert r.status_code == 413 and r.json()["error"] == "payload_too_large"

    async def chunks():
        yield big[: limit // 2]
        yield big[limit // 2 :]

    r = await api.post("/tasks/T-1/comments", content=chunks(), headers=bearer(token))
    assert "content-length" not in r.request.headers
    assert r.status_code == 413 and r.json()["error"] == "payload_too_large"
    assert len(board.requests) == 1


async def test_unconfigured_is_503_taskboard_unconfigured(make_task_app, token):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_task_app(configured=False)),
        base_url="http://dev" + PREFIX,
    ) as c:
        r = await c.get("/tasks", headers=bearer(token))
        assert r.status_code == 503 and r.json()["error"] == "taskboard_unconfigured"
        r = await c.post("/tasks", json={}, headers=bearer(token))
        assert r.status_code == 503 and r.json()["error"] == "taskboard_unconfigured"


@pytest.mark.parametrize(
    "fault",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.RemoteProtocolError("bad"),
    ],
)
async def test_connect_error_timeout_are_503_taskboard_unavailable(api, token, board, fault):
    board.raises = fault
    r = await api.get("/tasks", headers=bearer(token))
    assert r.status_code == 503 and r.json()["error"] == "taskboard_unavailable"


@pytest.mark.parametrize("raw", [b"<html>nope</html>", b"", b"{broken"])
async def test_non_json_upstream_is_503_taskboard_unavailable(api, token, board, raw):
    board.raw = raw
    r = await api.get("/tasks", headers=bearer(token))
    assert r.status_code == 503 and r.json()["error"] == "taskboard_unavailable"


async def test_missing_revoked_and_other_plugin_token_are_401(api, token, board, conn):
    _, other = await devices.create(conn, "other", "x")
    row = await devices.by_token(conn, token)
    for headers in ({}, bearer("unknown"), bearer(other)):
        for method, path in (("GET", "/tasks"), ("GET", "/tasks/T-1"), ("DELETE", "/tasks/x")):
            r = await api.request(method, path, headers=headers)
            assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert await devices.revoke(conn, row.id)
    r = await api.get("/tasks", headers=bearer(token))
    assert r.status_code == 401
    assert board.requests == []


async def test_rate_limited_is_429_with_retry_after(make_task_app, token):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_task_app(rate_per_minute=1)),
        base_url="http://dev" + PREFIX,
    ) as c:
        assert (await c.get("/tasks", headers=bearer(token))).status_code == 200
        r = await c.get("/tasks", headers=bearer(token))
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
        assert int(r.headers["Retry-After"]) >= 1


async def test_logs_method_path_status_only(api, token, board, caplog):
    caplog.set_level(logging.DEBUG)
    board.body = {"secret_body": "zzz"}
    await api.post(
        "/tasks/T-1/comments?label=qqq",
        json={"body": "bodytext-marker"},
        headers=bearer(token),
    )
    # httpx request lines (query included) are silenced by the CLI logging setup, see test_cli
    text = "\n".join(r.getMessage() for r in caplog.records if r.name != "httpx")
    assert "POST" in text and "/tasks/T-1/comments" in text and "200" in text
    for leak in ("bodytext-marker", "qqq", "zzz", token, DELEGATE):
        assert leak not in text
