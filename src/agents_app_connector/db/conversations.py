"""Conversations. Writer: the app component."""

import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row


@dataclass(frozen=True)
class ConversationRow:
    id: uuid.UUID
    device_id: uuid.UUID
    plugin: str
    title: str | None
    state: str
    created_at: datetime
    updated_at: datetime
    latest_spec_version: int | None = None
    latest_feedback_id: int | None = None


_SELECT = (
    "SELECT c.id, c.device_id, c.plugin, c.title, c.state, c.created_at, c.updated_at, "
    "(SELECT max(s.version) FROM specs s WHERE s.conversation_id = c.id) AS latest_spec_version, "
    "(SELECT max(f.id) FROM feedback f WHERE f.conversation_id = c.id) AS latest_feedback_id "
    "FROM conversations c"
)


async def create(
    conn: psycopg.AsyncConnection, device_id: uuid.UUID, plugin: str, title: str | None = None
) -> uuid.UUID:
    conversation_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO conversations (id, device_id, plugin, title) VALUES (%s, %s, %s, %s)",
        (conversation_id, device_id, plugin, title),
    )
    return conversation_id


async def current_or_new(
    conn: psycopg.AsyncConnection, device_id: uuid.UUID, plugin: str
) -> uuid.UUID:
    """The newest open conversation of the device for the plugin, or a new one."""
    cur = await conn.execute(
        "SELECT id FROM conversations WHERE device_id = %s AND plugin = %s AND state = 'open' "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (device_id, plugin),
    )
    row = await cur.fetchone()
    return row[0] if row else await create(conn, device_id, plugin)


async def get_owned(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID, device_id: uuid.UUID
) -> ConversationRow | None:
    """An open conversation of this device; another device's or a deleted one gives None."""
    async with conn.cursor(row_factory=class_row(ConversationRow)) as cur:
        await cur.execute(
            f"{_SELECT} WHERE c.id = %s AND c.device_id = %s AND c.state = 'open'",
            (conversation_id, device_id),
        )
        return await cur.fetchone()


async def list_for(conn: psycopg.AsyncConnection, device_id: uuid.UUID) -> list[ConversationRow]:
    async with conn.cursor(row_factory=class_row(ConversationRow)) as cur:
        await cur.execute(
            f"{_SELECT} WHERE c.device_id = %s AND c.state = 'open' "
            "ORDER BY c.created_at DESC, c.id DESC",
            (device_id,),
        )
        return await cur.fetchall()


async def lock(conn: psycopg.AsyncConnection, conversation_id: uuid.UUID) -> None:
    """Row lock until the transaction ends: serialises the spec version writers of one
    conversation (revert, upload base check)."""
    await conn.execute("SELECT 1 FROM conversations WHERE id = %s FOR UPDATE", (conversation_id,))


async def touch(conn: psycopg.AsyncConnection, conversation_id: uuid.UUID) -> None:
    await conn.execute(
        "UPDATE conversations SET updated_at = now() WHERE id = %s", (conversation_id,)
    )


async def mark_deleted(conn: psycopg.AsyncConnection, conversation_id: uuid.UUID) -> bool:
    cur = await conn.execute(
        "UPDATE conversations SET state = 'deleted', updated_at = now() "
        "WHERE id = %s AND state = 'open'",
        (conversation_id,),
    )
    return cur.rowcount == 1


async def get(conn: psycopg.AsyncConnection, conversation_id: uuid.UUID) -> ConversationRow | None:
    async with conn.cursor(row_factory=class_row(ConversationRow)) as cur:
        await cur.execute(f"{_SELECT} WHERE c.id = %s", (conversation_id,))
        return await cur.fetchone()
