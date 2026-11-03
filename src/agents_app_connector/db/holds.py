"""Per-conversation dispatch holds (usage quota back-off). Writer: the app worker."""

from __future__ import annotations

import builtins
import uuid
from datetime import datetime

import psycopg


async def set(  # noqa: A001
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID, until: datetime, reason: str | None
) -> None:
    await conn.execute(
        "INSERT INTO holds (conversation_id, until, reason) VALUES (%s, %s, %s) "
        "ON CONFLICT (conversation_id) DO UPDATE SET until = EXCLUDED.until, "
        "reason = EXCLUDED.reason",
        (conversation_id, until, reason),
    )


async def active(conn: psycopg.AsyncConnection, now: datetime) -> builtins.set[uuid.UUID]:
    """Conversations on hold at `now`."""
    cur = await conn.execute("SELECT conversation_id FROM holds WHERE until > %s", (now,))
    return {r[0] for r in await cur.fetchall()}
