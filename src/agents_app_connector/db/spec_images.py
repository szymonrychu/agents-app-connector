"""Region images a spec may reference as `images/<region_id>.png`. Writer: the app component.

Each row points at a `spec_image` attachment (never expires, deleted with the conversation)."""

import uuid
from collections.abc import Iterable

import psycopg
from psycopg.rows import class_row

from agents_app_connector.db import attachments


async def add(
    conn: psycopg.AsyncConnection,
    conversation_id: uuid.UUID,
    region_id: uuid.UUID,
    attachment_id: uuid.UUID,
) -> None:
    await conn.execute(
        "INSERT INTO spec_images (conversation_id, region_id, attachment_id) VALUES (%s, %s, %s)",
        (conversation_id, region_id, attachment_id),
    )


async def put(
    conn: psycopg.AsyncConnection,
    conversation_id: uuid.UUID,
    region_id: uuid.UUID,
    attachment_id: uuid.UUID,
) -> None:
    """Add, or point an existing region id at the newer crop (a re-sent op whose earlier upload
    no version reported; a reported one is refused as op_replayed before this)."""
    await conn.execute(
        "INSERT INTO spec_images (conversation_id, region_id, attachment_id) VALUES (%s, %s, %s) "
        "ON CONFLICT (conversation_id, region_id) DO UPDATE "
        "SET attachment_id = EXCLUDED.attachment_id, created_at = now()",
        (conversation_id, region_id, attachment_id),
    )


async def get(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID, region_id: uuid.UUID
) -> attachments.AttachmentRow | None:
    cols = ", ".join(f"a.{c}" for c in attachments.COLUMNS.split(", "))
    async with conn.cursor(row_factory=class_row(attachments.AttachmentRow)) as cur:
        await cur.execute(
            f"SELECT {cols} FROM spec_images s JOIN attachments a ON a.id = s.attachment_id "
            "WHERE s.conversation_id = %s AND s.region_id = %s",
            (conversation_id, region_id),
        )
        return await cur.fetchone()


async def missing(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID, region_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    """The region ids that are not images of the conversation."""
    wanted = set(region_ids)
    if not wanted:
        return set()
    cur = await conn.execute(
        "SELECT region_id FROM spec_images WHERE conversation_id = %s AND region_id = ANY(%s)",
        (conversation_id, list(wanted)),
    )
    return wanted - {r[0] for r in await cur.fetchall()}
