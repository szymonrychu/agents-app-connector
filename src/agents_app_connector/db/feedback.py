"""Translator feedback items per conversation, kept apart from the document. Writer: the app
component (the translator's feedback_add tool runs in the app process)."""

import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row

KINDS = ("question", "uncertain", "illegible", "note")


@dataclass(frozen=True)
class FeedbackRow:
    id: int
    conversation_id: uuid.UUID
    run_id: int
    inbound_id: uuid.UUID | None
    spec_version: int | None
    kind: str
    text: str
    where_text: str | None
    created_at: datetime


_COLUMNS = (
    "id, conversation_id, run_id, inbound_id, spec_version, kind, text, where_text, created_at"
)


async def add(
    conn: psycopg.AsyncConnection,
    conversation_id: uuid.UUID,
    run_id: int,
    inbound_id: uuid.UUID | None,
    kind: str,
    text: str,
    *,
    where: str | None = None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO feedback (conversation_id, run_id, inbound_id, kind, text, where_text) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (conversation_id, run_id, inbound_id, kind, text, where),
    )
    return (await cur.fetchone())[0]


async def after(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID, cursor: int, limit: int = 100
) -> list[FeedbackRow]:
    """Items of a conversation with id greater than `cursor`, oldest first."""
    async with conn.cursor(row_factory=class_row(FeedbackRow)) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM feedback WHERE conversation_id = %s AND id > %s "
            "ORDER BY id LIMIT %s",
            (conversation_id, cursor, limit),
        )
        return await cur.fetchall()


async def count_for_run(conn: psycopg.AsyncConnection, run_id: int) -> int:
    cur = await conn.execute("SELECT count(*) FROM feedback WHERE run_id = %s", (run_id,))
    return (await cur.fetchone())[0]


async def set_spec_version(conn: psycopg.AsyncConnection, run_id: int, version: int) -> int:
    """Link the run's items that have no version yet to the version the run wrote."""
    cur = await conn.execute(
        "UPDATE feedback SET spec_version = %s WHERE run_id = %s AND spec_version IS NULL",
        (version, run_id),
    )
    return cur.rowcount


async def delete_for(conn: psycopg.AsyncConnection, conversation_id: uuid.UUID) -> int:
    cur = await conn.execute("DELETE FROM feedback WHERE conversation_id = %s", (conversation_id,))
    return cur.rowcount
