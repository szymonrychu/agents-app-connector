"""Devices. Writer: the admin component. Token hashes live in device_tokens (writer: app)."""

import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row


@dataclass(frozen=True)
class DeviceRow:
    id: uuid.UUID
    plugin: str
    name: str
    created_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class DeviceInfo:
    """A device with its pairing state: `pending` (no token yet), `active` or `revoked`."""

    id: uuid.UUID
    plugin: str
    name: str
    created_at: datetime
    revoked_at: datetime | None
    paired_at: datetime | None
    paired_ip: str | None
    code_status: str | None
    code_expires_at: datetime | None

    @property
    def state(self) -> str:
        if self.revoked_at is not None:
            return "revoked"
        return "active" if self.paired_at is not None else "pending"


COLUMNS = "id, plugin, name, created_at, revoked_at"


async def create(conn: psycopg.AsyncConnection, plugin: str, name: str) -> DeviceRow:
    """Insert a device without a token; it is paired later by redeeming a pairing code."""
    async with conn.cursor(row_factory=class_row(DeviceRow)) as cur:
        await cur.execute(
            f"INSERT INTO devices (id, plugin, name) VALUES (%s, %s, %s) RETURNING {COLUMNS}",
            (uuid.uuid4(), plugin, name),
        )
        return await cur.fetchone()


async def revoke(conn: psycopg.AsyncConnection, device_id: uuid.UUID) -> bool:
    """Revoke an active device; False when unknown or already revoked."""
    cur = await conn.execute(
        "UPDATE devices SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
        (device_id,),
    )
    return cur.rowcount == 1


LIST_SQL = """
SELECT d.id, d.plugin, d.name, d.created_at, d.revoked_at,
       t.issued_at AS paired_at, pc.redeemed_ip AS paired_ip,
       pc.code_status, pc.expires_at AS code_expires_at
FROM devices d
LEFT JOIN device_tokens t ON t.device_id = d.id
LEFT JOIN LATERAL (
    SELECT CASE WHEN status = 'pending' AND expires_at <= now() THEN 'expired' ELSE status END
               AS code_status,
           expires_at, redeemed_ip
    FROM pairing_codes WHERE device_id = d.id ORDER BY created_at DESC, id DESC LIMIT 1
) pc ON true
"""


async def list_all(conn: psycopg.AsyncConnection) -> list[DeviceInfo]:
    async with conn.cursor(row_factory=class_row(DeviceInfo)) as cur:
        await cur.execute(LIST_SQL + "ORDER BY d.created_at, d.id")
        return await cur.fetchall()


async def get_info(conn: psycopg.AsyncConnection, device_id: uuid.UUID) -> DeviceInfo | None:
    async with conn.cursor(row_factory=class_row(DeviceInfo)) as cur:
        await cur.execute(LIST_SQL + "WHERE d.id = %s", (device_id,))
        return await cur.fetchone()
