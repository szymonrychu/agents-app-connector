"""Retention: delete expired sketch files and old finished rows. Specs are kept until their
conversation is deleted (spec 3.8); conversations, inbound rows and attachment rows stay (a
purged attachment row is what makes GET /attachments/{id} answer 410)."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg_pool import AsyncConnectionPool

from agents_app_connector.clock import utcnow
from agents_app_connector.db import attachments
from agents_app_connector.storage import AttachmentStore

log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30


@dataclass(frozen=True)
class PurgeResult:
    files: int
    bytes: int


async def purge(
    pool: AsyncConnectionPool,
    store: AttachmentStore,
    now: datetime,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> PurgeResult:
    """One pass. Per attachment: delete the file, then mark the row purged (a crash between the
    two is retried next pass, deleting a missing file is a no-op). Then delete events of deleted
    conversations, finished runs and finished hand-offs older than `retention_days`."""
    cutoff = now - timedelta(days=retention_days)
    files = 0
    size = 0
    async with pool.connection() as conn:
        expired = await attachments.expired(conn, now)
    for row in expired:
        await asyncio.to_thread(store.delete, row.path)
        async with pool.connection() as conn:
            if await attachments.mark_purged(conn, row.id):
                files += 1
                size += row.size
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM events e USING conversations c WHERE e.conversation_id = c.id "
            "AND c.state = 'deleted' AND e.created_at < %s",
            (cutoff,),
        )
        await conn.execute(
            "DELETE FROM runs WHERE status <> 'in-progress' AND finished_at < %s", (cutoff,)
        )
        await conn.execute(
            "DELETE FROM handoffs WHERE status IN ('handled', 'failed') AND created_at < %s",
            (cutoff,),
        )
    return PurgeResult(files=files, bytes=size)


class Retention:
    """Runs `purge` at most once per `interval`; call `tick` as often as convenient."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        store: AttachmentStore,
        *,
        now: Callable[[], datetime] = utcnow,
        interval: timedelta = timedelta(days=1),
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ):
        self._pool = pool
        self._store = store
        self._now = now
        self._interval = interval
        self._retention_days = retention_days
        self._last: datetime | None = None

    async def tick(self) -> PurgeResult | None:
        now = self._now()
        if self._last is not None and now - self._last < self._interval:
            return None
        self._last = now
        result = await purge(self._pool, self._store, now, retention_days=self._retention_days)
        log.info("retention purge: %d files, %d bytes", result.files, result.bytes)
        return result
