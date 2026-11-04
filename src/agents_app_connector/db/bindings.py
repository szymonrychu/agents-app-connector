"""Conversation to session bindings. Writer: the app worker.

A binding is created as a reservation (no session) by the first run of a conversation, filled
with the run's session, and released when the session is gone. One active binding per
conversation (unique index).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row


@dataclass(frozen=True)
class BindingRow:
    id: int
    conversation_id: uuid.UUID
    run_id: int | None
    session_id: uuid.UUID | None
    slug: str | None
    created_at: datetime
    released_at: datetime | None
    reason: str | None


COLUMNS = "id, conversation_id, run_id, session_id, slug, created_at, released_at, reason"


async def active(conn: psycopg.AsyncConnection, conversation_id: uuid.UUID) -> BindingRow | None:
    """The active binding of the conversation, a reservation (no session yet) included."""
    async with conn.cursor(row_factory=class_row(BindingRow)) as cur:
        await cur.execute(
            f"SELECT {COLUMNS} FROM bindings WHERE conversation_id = %s AND released_at IS NULL",
            (conversation_id,),
        )
        return await cur.fetchone()


async def active_session_ids(conn: psycopg.AsyncConnection) -> list[uuid.UUID]:
    """The sessions of every active binding that has one, oldest binding first."""
    cur = await conn.execute(
        "SELECT session_id FROM bindings "
        "WHERE released_at IS NULL AND session_id IS NOT NULL ORDER BY id"
    )
    return [row[0] for row in await cur.fetchall()]


async def reserve(conn: psycopg.AsyncConnection, conversation_id: uuid.UUID, run_id: int) -> bool:
    """Reserve an unbound conversation for a first run; False when it is bound."""
    cur = await conn.execute(
        "INSERT INTO bindings (conversation_id, run_id) VALUES (%s, %s) "
        "ON CONFLICT (conversation_id) WHERE released_at IS NULL DO NOTHING",
        (conversation_id, run_id),
    )
    return cur.rowcount == 1


async def fill(
    conn: psycopg.AsyncConnection, run_id: int, session_id: uuid.UUID | str, slug: str
) -> bool:
    """Fill the run's active reservation with its session; True again for the same session."""
    cur = await conn.execute(
        "UPDATE bindings SET session_id = %(s)s, slug = %(slug)s "
        "WHERE run_id = %(run)s AND released_at IS NULL "
        "AND (session_id IS NULL OR session_id = %(s)s)",
        {"s": session_id, "slug": slug, "run": run_id},
    )
    return cur.rowcount == 1


async def release(conn: psycopg.AsyncConnection, binding_id: int, reason: str) -> bool:
    cur = await conn.execute(
        "UPDATE bindings SET released_at = now(), reason = %s "
        "WHERE id = %s AND released_at IS NULL",
        (reason, binding_id),
    )
    return cur.rowcount == 1


async def release_reservation(conn: psycopg.AsyncConnection, run_id: int, reason: str) -> bool:
    """Release the run's active reservation while it still has no session."""
    cur = await conn.execute(
        "UPDATE bindings SET released_at = now(), reason = %s "
        "WHERE run_id = %s AND session_id IS NULL AND released_at IS NULL",
        (reason, run_id),
    )
    return cur.rowcount == 1
