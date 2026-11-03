"""Conversation events, cursor-polled by the device API. Writer: the app component."""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg
from agents_common.db.pool import notify
from psycopg.rows import class_row

CHANNEL = "app_events"


async def append(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID, type_: str, payload: dict
) -> int:
    # Serialise event writers of one conversation until commit, so ids become visible in order
    # and a cursor reader never skips an event committed later with a lower id.
    await conn.execute(
        "SELECT 1 FROM conversations WHERE id = %s FOR NO KEY UPDATE", (conversation_id,)
    )
    cur = await conn.execute(
        "INSERT INTO events (conversation_id, type, payload) VALUES (%s, %s, %s) RETURNING id",
        (conversation_id, type_, json.dumps(payload)),
    )
    event_id = (await cur.fetchone())[0]
    await notify(conn, CHANNEL, str(conversation_id))
    return event_id


@dataclass(frozen=True)
class EventRow:
    id: int
    conversation_id: uuid.UUID
    type: str
    payload: dict
    created_at: datetime


async def after(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID, cursor: int, limit: int = 100
) -> list[EventRow]:
    """Events of a conversation with id greater than `cursor`, oldest first."""
    async with conn.cursor(row_factory=class_row(EventRow)) as cur:
        await cur.execute(
            "SELECT id, conversation_id, type, payload, created_at FROM events "
            "WHERE conversation_id = %s AND id > %s ORDER BY id LIMIT %s",
            (conversation_id, cursor, limit),
        )
        return await cur.fetchall()
