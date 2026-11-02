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
