"""Spec versions per conversation. Writer: the app component."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb


async def latest_version(conn: psycopg.AsyncConnection, conversation_id: uuid.UUID) -> int | None:
    cur = await conn.execute(
        "SELECT max(version) FROM specs WHERE conversation_id = %s", (conversation_id,)
    )
    return (await cur.fetchone())[0]


async def delete_for(conn: psycopg.AsyncConnection, conversation_id: uuid.UUID) -> int:
    cur = await conn.execute("DELETE FROM specs WHERE conversation_id = %s", (conversation_id,))
    return cur.rowcount


@dataclass(frozen=True)
class SpecRow:
    conversation_id: uuid.UUID
    version: int
    markdown: str
    inbound_id: uuid.UUID | None
    sketch_ids: list[uuid.UUID]
    run_id: int | None
    created_at: datetime
    layout: dict | None = None


_COLUMNS = "conversation_id, version, markdown, inbound_id, sketch_ids, run_id, created_at, layout"


async def add(
    conn: psycopg.AsyncConnection,
    conversation_id: uuid.UUID,
    markdown: str,
    inbound_id: uuid.UUID | None,
    sketch_ids: Sequence[uuid.UUID],
    run_id: int | None,
    layout: dict | None = None,
) -> int:
    """Store the next version (1 for the first) and return it. One run per conversation is in
    progress at a time; the primary key rejects a racing second writer."""
    cur = await conn.execute(
        "INSERT INTO specs "
        "(conversation_id, version, markdown, inbound_id, sketch_ids, run_id, layout) "
        "SELECT %s, coalesce(max(version), 0) + 1, %s, %s, %s, %s, %s "
        "FROM specs WHERE conversation_id = %s RETURNING version",
        (
            conversation_id,
            markdown,
            inbound_id,
            list(sketch_ids),
            run_id,
            None if layout is None else Jsonb(layout),
            conversation_id,
        ),
    )
    return (await cur.fetchone())[0]


async def max_line_number(conn: psycopg.AsyncConnection, conversation_id: uuid.UUID) -> int:
    """The largest N of every line id `L<N>` in every layout of the conversation, 0 when none.
    Line ids are never reused, so new ones continue after this (also after a revert)."""
    cur = await conn.execute(
        "SELECT coalesce(max(substr(l->>'line_id', 2)::bigint), 0) FROM specs s, "
        "jsonb_array_elements(coalesce(s.layout->'lines', '[]'::jsonb)) l "
        "WHERE s.conversation_id = %s",
        (conversation_id,),
    )
    return int((await cur.fetchone())[0])


async def reported_op_ids(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID
) -> set[uuid.UUID]:
    """Every op id an earlier version's layout reported (applied or rejected)."""
    cur = await conn.execute(
        "SELECT DISTINCT o->>'id' FROM specs s, "
        "jsonb_array_elements(coalesce(s.layout->'ops', '[]'::jsonb)) o "
        "WHERE s.conversation_id = %s",
        (conversation_id,),
    )
    return {uuid.UUID(r[0]) for r in await cur.fetchall()}


async def last_of_run(conn: psycopg.AsyncConnection, run_id: int) -> int | None:
    """The newest version the run wrote, None when it wrote none."""
    cur = await conn.execute("SELECT max(version) FROM specs WHERE run_id = %s", (run_id,))
    return (await cur.fetchone())[0]


async def latest(conn: psycopg.AsyncConnection, conversation_id: uuid.UUID) -> SpecRow | None:
    """The newest spec of the conversation."""
    async with conn.cursor(row_factory=class_row(SpecRow)) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM specs WHERE conversation_id = %s "
            "ORDER BY version DESC LIMIT 1",
            (conversation_id,),
        )
        return await cur.fetchone()


async def get(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID, version: int
) -> SpecRow | None:
    async with conn.cursor(row_factory=class_row(SpecRow)) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM specs WHERE conversation_id = %s AND version = %s",
            (conversation_id, version),
        )
        return await cur.fetchone()
