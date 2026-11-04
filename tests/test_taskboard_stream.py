"""The shared upstream task-change subscriber, against a fake SSE taskboard."""

import asyncio

import httpx
import pytest

from agents_app_connector.taskboard_stream import TaskChangeRelay

STREAM_PATH = "/taskboard/api/v1/stream"


def event(key: str) -> bytes:
    return f'event: task_changed\ndata: {{"key": "{key}"}}\n\n'.encode()


class FakeSse:
    """Each upstream connection reads chunks from its own queue; None ends it."""

    def __init__(self):
        self.connections: list[asyncio.Queue] = []
        self.closed: list[asyncio.Event] = []
        self.requests: list[httpx.Request] = []
        self.fail_connects = 0
        self.connected = asyncio.Event()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_connects:
            self.fail_connects -= 1
            raise httpx.ConnectError("refused")
        chunks: asyncio.Queue = asyncio.Queue()
        closed = asyncio.Event()
        self.connections.append(chunks)
        self.closed.append(closed)
        self.connected.set()

        async def body():
            try:
                while True:
                    chunk = await chunks.get()
                    if chunk is None:
                        return
                    yield chunk
            finally:
                closed.set()

        return httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"})

    def send(self, chunk: bytes, conn: int = -1) -> None:
        self.connections[conn].put_nowait(chunk)


@pytest.fixture
def fake():
    return FakeSse()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
async def relay(fake, sleeps):
    async def sleep(delay):
        sleeps.append(delay)
        await asyncio.sleep(0)

    client = httpx.AsyncClient(
        base_url="http://taskboard", transport=httpx.MockTransport(fake.handler)
    )
    r = TaskChangeRelay(client, token="delegate-test-token", sleep=sleep)
    await r.start()
    yield r
    await r.stop()
    await client.aclose()


async def get(q: asyncio.Queue, timeout=2.0) -> str:
    return await asyncio.wait_for(q.get(), timeout)


async def test_fans_one_event_to_all_subscribers(relay, fake):
    async with relay.subscribe() as a, relay.subscribe() as b:
        await fake.connected.wait()
        fake.send(event("T-5"))
        assert await get(a) == "T-5" and await get(b) == "T-5"


async def test_single_upstream_connection_for_many_subscribers(relay, fake):
    async with relay.subscribe() as a, relay.subscribe() as b, relay.subscribe() as c:
        await fake.connected.wait()
        fake.send(event("T-1"))
        for q in (a, b, c):
            assert await get(q) == "T-1"
    assert len(fake.requests) == 1
    req = fake.requests[0]
    assert req.method == "GET" and req.url.path == STREAM_PATH
    assert req.headers["authorization"] == "Bearer delegate-test-token"


async def test_no_upstream_connection_before_first_subscriber(relay, fake):
    await asyncio.sleep(0.05)
    assert fake.requests == []


async def test_reconnects_after_upstream_error_with_backoff(relay, fake, sleeps):
    fake.fail_connects = 3
    async with relay.subscribe() as q:
        await asyncio.wait_for(fake.connected.wait(), 2)
        fake.send(event("T-9"))
        assert await get(q) == "T-9"
    assert sleeps[:3] == [1.0, 2.0, 4.0]
    assert len(fake.requests) == 4


async def test_backoff_is_capped_and_resets_after_a_connect(fake, sleeps):
    async def sleep(delay):
        sleeps.append(delay)
        await asyncio.sleep(0)

    client = httpx.AsyncClient(
        base_url="http://taskboard", transport=httpx.MockTransport(fake.handler)
    )
    relay = TaskChangeRelay(client, backoff_base=1.0, backoff_cap=3.0, sleep=sleep)
    await relay.start()
    fake.fail_connects = 4
    try:
        async with relay.subscribe() as q:
            await asyncio.wait_for(fake.connected.wait(), 2)
            assert sleeps[:4] == [1.0, 2.0, 3.0, 3.0]
            fake.send(None)  # upstream closes: reconnect with the base delay again
            fake.connected.clear()
            await asyncio.wait_for(fake.connected.wait(), 2)
            assert sleeps[4] == 1.0
            fake.send(event("T-2"))
            assert await get(q) == "T-2"
    finally:
        await relay.stop()
        await client.aclose()


async def test_last_unsubscribe_cancels_upstream_task(relay, fake):
    async with relay.subscribe():
        await fake.connected.wait()
        async with relay.subscribe():
            pass
        assert not fake.closed[0].is_set()  # one subscriber left
    await asyncio.wait_for(fake.closed[0].wait(), 2)
    assert relay.running is False
    fake.connected.clear()
    async with relay.subscribe() as q:  # lazily started again
        await asyncio.wait_for(fake.connected.wait(), 2)
        fake.send(event("T-3"))
        assert await get(q) == "T-3"
    assert len(fake.requests) == 2


async def test_stop_cancels_everything_no_leaked_tasks(fake):
    before = asyncio.all_tasks()
    client = httpx.AsyncClient(
        base_url="http://taskboard", transport=httpx.MockTransport(fake.handler)
    )
    relay = TaskChangeRelay(client)
    await relay.start()
    cm = relay.subscribe()
    await cm.__aenter__()
    await asyncio.wait_for(fake.connected.wait(), 2)
    await relay.stop()
    await asyncio.wait_for(fake.closed[0].wait(), 2)
    assert relay.running is False
    await cm.__aexit__(None, None, None)
    await client.aclose()
    assert asyncio.all_tasks() - before == set()


async def test_malformed_event_is_skipped(relay, fake):
    async with relay.subscribe() as q:
        await fake.connected.wait()
        fake.send(b"event: task_changed\ndata: {not json\n\n")
        fake.send(b'event: task_changed\ndata: {"nokey": 1}\n\n')
        fake.send(b'event: task_changed\ndata: {"key": 5}\n\n')
        fake.send(b'event: task_changed\ndata: {"key": "evil/../x"}\n\n')
        fake.send(b'event: other\ndata: {"key": "T-1"}\n\n')
        fake.send(b": keepalive\n\n")
        fake.send(event("T-7"))
        assert await get(q) == "T-7"
        assert q.empty()


async def test_event_split_across_chunks_and_crlf(relay, fake):
    async with relay.subscribe() as q:
        await fake.connected.wait()
        fake.send(b'event: task_changed\r\ndata: {"key"')
        fake.send(b': "T-8"}\r\n\r\n')
        assert await get(q) == "T-8"


async def test_slow_subscriber_does_not_block_others(relay, fake):
    async with relay.subscribe() as slow, relay.subscribe() as fast:
        await fake.connected.wait()
        total = 500
        seen: list[str] = []

        async def drain():
            while True:
                seen.append(await fast.get())

        drainer = asyncio.create_task(drain())
        for i in range(total):
            fake.send(event(f"T-{i}"))
            await asyncio.sleep(0)
        await asyncio.sleep(0.2)
        drainer.cancel()
        assert seen[-1] == f"T-{total - 1}"
        # the slow subscriber never read: its queue is bounded and keeps the newest keys
        assert slow.qsize() == slow.maxsize
        last = None
        while not slow.empty():
            last = slow.get_nowait()
        assert last == f"T-{total - 1}"
