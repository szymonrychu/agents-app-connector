"""One shared upstream task-change subscription fanned out to many device streams.

The taskboard delegate listener streams `task_changed` events (`data: {"key": "T-5"}`). The
relay holds a single upstream connection per process while at least one device is subscribed,
reconnecting with exponential backoff, and puts each key on every subscriber queue. Keys are
idempotent refresh hints, so a full queue drops its oldest key instead of blocking the others.
"""

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

STREAM_PATH = "/taskboard/api/v1/stream"
EVENT_NAME = "task_changed"
KEY = re.compile(r"T-\d+")
QUEUE_SIZE = 64
# The taskboard sends a keepalive comment every 15 s; three missed ones mean a dead connection.
READ_TIMEOUT_SECONDS = 45.0
CONNECT_TIMEOUT_SECONDS = 10.0


class _UpstreamError(Exception):
    pass


def _key_of(data: str) -> str | None:
    try:
        key = json.loads(data).get("key")
    except (ValueError, AttributeError):
        return None
    return key if isinstance(key, str) and KEY.fullmatch(key) else None


class TaskChangeRelay:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        token: str = "",
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._client = client
        self._token = token
        self._base = backoff_base
        self._cap = backoff_cap
        self._sleep = sleep
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._task: asyncio.Task | None = None
        self._open = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Allow subscriptions; the upstream connection itself starts with the first one."""
        self._open = True

    async def stop(self) -> None:
        self._open = False
        await self._cancel()

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[str]]:
        queue: asyncio.Queue[str] = asyncio.Queue(QUEUE_SIZE)
        self._subscribers.add(queue)
        if self._open and not self.running:
            self._task = asyncio.create_task(self._run(), name="taskboard-stream")
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
            if not self._subscribers:
                await self._cancel()

    async def _cancel(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _publish(self, key: str) -> None:
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(key)

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                async with self._client.stream(
                    "GET",
                    STREAM_PATH,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=httpx.Timeout(CONNECT_TIMEOUT_SECONDS, read=READ_TIMEOUT_SECONDS),
                ) as resp:
                    if resp.status_code != 200:
                        raise _UpstreamError(f"status {resp.status_code}")
                    failures = 0
                    await self._read(resp)
            except (httpx.HTTPError, _UpstreamError) as exc:
                log.warning("task stream: upstream %s", type(exc).__name__)
            delay = min(self._cap, self._base * 2**failures)
            failures += 1
            await self._sleep(delay)

    async def _read(self, resp: httpx.Response) -> None:
        event = ""
        data: list[str] = []
        async for line in resp.aiter_lines():
            if line == "":
                if event == EVENT_NAME and data:
                    key = _key_of("\n".join(data))
                    if key is not None:
                        self._publish(key)
                event, data = "", []
            elif line.startswith(":"):
                continue
            else:
                field, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if field == "event":
                    event = value
                elif field == "data":
                    data.append(value)
