"""Spec versions per conversation. Writer: the app component."""

import uuid

import psycopg


async def latest_version(conn: psycopg.AsyncConnection, conversation_id: uuid.UUID) -> int | None:
    cur = await conn.execute(
        "SELECT max(version) FROM specs WHERE conversation_id = %s", (conversation_id,)
    )
    return (await cur.fetchone())[0]


async def delete_for(conn: psycopg.AsyncConnection, conversation_id: uuid.UUID) -> int:
    cur = await conn.execute("DELETE FROM specs WHERE conversation_id = %s", (conversation_id,))
    return cur.rowcount


async def latest(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID
) -> tuple[int, str] | None:
    """(version, markdown) of the newest spec of the conversation."""
    cur = await conn.execute(
        "SELECT version, markdown FROM specs WHERE conversation_id = %s "
        "ORDER BY version DESC LIMIT 1",
        (conversation_id,),
    )
    row = await cur.fetchone()
    return None if row is None else (row[0], row[1])
