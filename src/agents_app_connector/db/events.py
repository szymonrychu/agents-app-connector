"""Conversation events, cursor-polled by the device API. Writer: the app component."""

import json
import uuid

import psycopg
from agents_common.db.pool import notify

CHANNEL = "app_events"


async def append(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID, type_: str, payload: dict
) -> int:
    cur = await conn.execute(
        "INSERT INTO events (conversation_id, type, payload) VALUES (%s, %s, %s) RETURNING id",
        (conversation_id, type_, json.dumps(payload)),
    )
    event_id = (await cur.fetchone())[0]
    await notify(conn, CHANNEL, str(conversation_id))
    return event_id
