"""Wakes event waiters: one LISTEN connection on `app_events`, with a periodic wake as fallback."""

import asyncio
import contextlib
import logging
import uuid

from agents_common.db.pool import listen
from psycopg_pool import AsyncConnectionPool

from agents_app_connector.db.events import CHANNEL

log = logging.getLogger(__name__)


class EventHub:
    """Waiters block per conversation id; NOTIFY payloads (the conversation id) wake them.

    Every `poll_seconds` all waiters wake anyway, so a missed notification or a lost LISTEN
    connection only delays an event, never loses it. Callers re-read the database after waking.
    """

    def __init__(self, dsn: str, pool: AsyncConnectionPool, poll_seconds: float):
        self._dsn = dsn
        self.pool = pool
        self._poll_seconds = poll_seconds
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._listen_loop()),
                asyncio.create_task(self._poll_loop()),
            ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    def wake(self, conversation_id: str | None = None) -> None:
        """Wake the waiters of one conversation, or all of them."""
        keys = list(self._waiters) if conversation_id is None else [conversation_id]
        for key in keys:
            for event in self._waiters.get(key, ()):
                event.set()

    async def wait_for(self, conversation_id: uuid.UUID, timeout: float) -> None:
        """Return when the conversation is signalled, or after `timeout` seconds."""
        key = str(conversation_id)
        event = asyncio.Event()
        self._waiters.setdefault(key, set()).add(event)
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout)
        finally:
            waiting = self._waiters.get(key)
            if waiting is not None:
                waiting.discard(event)
                if not waiting:
                    del self._waiters[key]

    async def _listen_loop(self) -> None:
        async for payload in listen(self._dsn, CHANNEL):
            self.wake(payload)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_seconds)
            self.wake()
