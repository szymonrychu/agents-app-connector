"""Sketch attachment metadata. Writer: the app component. Bytes live in storage.AttachmentStore."""

import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row


@dataclass(frozen=True)
class NewAttachment:
    inbound_id: uuid.UUID
    conversation_id: uuid.UUID
    device_id: uuid.UUID
    kind: str
    mime: str
    size: int
    sha256: str
    path: str
    expires_at: datetime
    width: int | None = None
    height: int | None = None
    page: int | None = None
    id: uuid.UUID | None = None


@dataclass(frozen=True)
class AttachmentRow:
    id: uuid.UUID
    inbound_id: uuid.UUID
    conversation_id: uuid.UUID
    device_id: uuid.UUID
    kind: str
    mime: str
    size: int
    sha256: str
    path: str
    width: int | None
    height: int | None
    page: int | None
    created_at: datetime
    expires_at: datetime
    purged_at: datetime | None


COLUMNS = (
    "id, inbound_id, conversation_id, device_id, kind, mime, size, sha256, path, width, height, "
    "page, created_at, expires_at, purged_at"
)


async def insert(conn: psycopg.AsyncConnection, row: NewAttachment) -> uuid.UUID:
    attachment_id = row.id or uuid.uuid4()
    await conn.execute(
        "INSERT INTO attachments (id, inbound_id, conversation_id, device_id, kind, mime, size, "
        "sha256, path, width, height, page, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            attachment_id,
            row.inbound_id,
            row.conversation_id,
            row.device_id,
            row.kind,
            row.mime,
            row.size,
            row.sha256,
            row.path,
            row.width,
            row.height,
            row.page,
            row.expires_at,
        ),
    )
    return attachment_id


async def for_inbound(conn: psycopg.AsyncConnection, inbound_id: uuid.UUID) -> list[AttachmentRow]:
    async with conn.cursor(row_factory=class_row(AttachmentRow)) as cur:
        await cur.execute(
            f"SELECT {COLUMNS} FROM attachments WHERE inbound_id = %s ORDER BY created_at, id",
            (inbound_id,),
        )
        return await cur.fetchall()


async def get_owned(
    conn: psycopg.AsyncConnection, attachment_id: uuid.UUID, device_id: uuid.UUID
) -> AttachmentRow | None:
    async with conn.cursor(row_factory=class_row(AttachmentRow)) as cur:
        await cur.execute(
            f"SELECT {COLUMNS} FROM attachments WHERE id = %s AND device_id = %s",
            (attachment_id, device_id),
        )
        return await cur.fetchone()


async def device_usage_bytes(conn: psycopg.AsyncConnection, device_id: uuid.UUID) -> int:
    """Bytes of unpurged attachments of a device."""
    cur = await conn.execute(
        "SELECT COALESCE(SUM(size), 0) FROM attachments WHERE device_id = %s AND purged_at IS NULL",
        (device_id,),
    )
    return int((await cur.fetchone())[0])


async def expired(conn: psycopg.AsyncConnection, now: datetime) -> list[AttachmentRow]:
    async with conn.cursor(row_factory=class_row(AttachmentRow)) as cur:
        await cur.execute(
            f"SELECT {COLUMNS} FROM attachments WHERE purged_at IS NULL AND expires_at <= %s "
            "ORDER BY expires_at, id",
            (now,),
        )
        return await cur.fetchall()


async def mark_purged(conn: psycopg.AsyncConnection, attachment_id: uuid.UUID) -> bool:
    cur = await conn.execute(
        "UPDATE attachments SET purged_at = now() WHERE id = %s AND purged_at IS NULL",
        (attachment_id,),
    )
    return cur.rowcount == 1


async def delete_for_conversation(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID
) -> list[str]:
    """Delete the attachment rows of a conversation; return the stored paths to remove."""
    cur = await conn.execute(
        "DELETE FROM attachments WHERE conversation_id = %s RETURNING path, purged_at",
        (conversation_id,),
    )
    return [path for path, purged_at in await cur.fetchall() if purged_at is None]
