import asyncio
import time

import httpx
import pytest
from helpers import PREFIX, bearer
from pydantic import SecretStr

from agents_app_connector.db import conversations, devices, events


@pytest.fixture
async def live(make_app, url):
    """A device app with its lifespan running (hub listening) and a short poll interval."""
    app = make_app(database_url=SecretStr(url), poll_seconds=0.2)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://dev" + PREFIX, timeout=20
        ) as client,
    ):
        yield client


@pytest.fixture
async def cid(conn, token):
    device = await devices.by_token(conn, token)
    return await conversations.create(conn, device.id, "sketch")


async def add(conn, cid, type_="spec", **payload):
    return await events.append(conn, cid, type_, payload)


async def test_long_poll_returns_on_new_event(live, conn, token, cid):
    async def later():
        await asyncio.sleep(0.2)
        await add(conn, cid, "spec", note="x")

    task = asyncio.create_task(later())
    start = time.monotonic()
    r = await live.get(f"/conversations/{cid}/events?after=0&wait=5", headers=bearer(token))
    assert time.monotonic() - start < 2
    await task
    body = r.json()
    assert [e["type"] for e in body["events"]] == ["spec"]
    e = body["events"][0]
    assert e["note"] == "x" and e["cursor"] == body["cursor"] and e["created_at"]


async def test_long_poll_times_out_empty(live, token, cid):
    r = await live.get(f"/conversations/{cid}/events?wait=1", headers=bearer(token))
    assert r.status_code == 200 and r.json() == {"events": [], "cursor": 0}


async def test_cursor_catch_up(live, conn, token, cid):
    a = await add(conn, cid, "sketch_status")
    b = await add(conn, cid, "spec")
    r = await live.get(f"/conversations/{cid}/events?after={a}", headers=bearer(token))
    assert [e["cursor"] for e in r.json()["events"]] == [b] and r.json()["cursor"] == b
    r = await live.get(f"/conversations/{cid}/events?after={b}", headers=bearer(token))
    assert r.json() == {"events": [], "cursor": b}


async def test_wait_above_max_and_unknown_conversation(live, token, cid, settings):
    r = await live.get(f"/conversations/{cid}/events?wait=999", headers=bearer(token))
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    import uuid

    r = await live.get(f"/conversations/{uuid.uuid4()}/events", headers=bearer(token))
    assert r.status_code == 404 and r.json()["error"] == "not_found"


async def _stream(live, token, cid, revoke_after, conn, headers=None, query=""):
    """Read a stream to its end; it ends when the token is revoked."""

    async def revoke():
        await asyncio.sleep(revoke_after)
        await conn.execute("UPDATE devices SET revoked_at = now()")

    task = asyncio.create_task(revoke())
    r = await live.get(
        f"/conversations/{cid}/stream{query}", headers={**bearer(token), **(headers or {})}
    )
    await task
    return r


def _parse(text):
    out = []
    for block in text.split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if ": " in line and line[0] != ":"
        )
        if fields:
            out.append(fields)
    return out


async def test_sse_streams_and_resumes_with_last_event_id(live, conn, token, cid):
    a = await add(conn, cid, "sketch_status", n=1)
    b = await add(conn, cid, "spec", n=2)
    r = await _stream(live, token, cid, 0.6, conn, query="?after=0")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    got = _parse(r.text)
    assert [(g["id"], g["event"]) for g in got] == [(str(a), "sketch_status"), (str(b), "spec")]
    assert '"n":2' in got[1]["data"].replace(" ", "")
    await conn.execute("UPDATE devices SET revoked_at = NULL")
    c = await add(conn, cid, "message", n=3)
    r = await _stream(live, token, cid, 0.6, conn, headers={"Last-Event-ID": str(b)})
    assert [g["id"] for g in _parse(r.text)] == [str(c)]


async def test_revoked_token_ends_stream_and_poll_401(live, conn, token, cid):
    r = await _stream(live, token, cid, 0.5, conn)
    assert r.status_code == 200  # the stream ended on its own after the revoke
    for path in (f"/conversations/{cid}/stream", f"/conversations/{cid}/events?wait=0"):
        again = await live.get(path, headers=bearer(token))
        assert again.status_code == 401 and again.json()["error"] == "unauthorized"


async def test_revoked_during_long_poll_answers_401(live, conn, token, cid):
    async def revoke():
        await asyncio.sleep(0.4)
        await conn.execute("UPDATE devices SET revoked_at = now()")

    task = asyncio.create_task(revoke())
    r = await live.get(f"/conversations/{cid}/events?wait=10", headers=bearer(token))
    await task
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
