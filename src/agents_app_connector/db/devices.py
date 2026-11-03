"""Paired devices. Writer: the admin component. Tokens are stored only as sha256."""

import hashlib
import secrets
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


COLUMNS = "id, plugin, name, created_at, revoked_at"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


async def create(conn: psycopg.AsyncConnection, plugin: str, name: str) -> tuple[DeviceRow, str]:
    """Insert a device; the plaintext token is returned once and never stored."""
    token = new_token()
    async with conn.cursor(row_factory=class_row(DeviceRow)) as cur:
        await cur.execute(
            f"INSERT INTO devices (id, plugin, name, token_sha256) VALUES (%s, %s, %s, %s)"
            f" RETURNING {COLUMNS}",
            (uuid.uuid4(), plugin, name, hash_token(token)),
        )
        return await cur.fetchone(), token


async def by_token(conn: psycopg.AsyncConnection, token: str) -> DeviceRow | None:
    async with conn.cursor(row_factory=class_row(DeviceRow)) as cur:
        await cur.execute(
            f"SELECT {COLUMNS} FROM devices WHERE token_sha256 = %s AND revoked_at IS NULL",
            (hash_token(token),),
        )
        return await cur.fetchone()


async def revoke(conn: psycopg.AsyncConnection, device_id: uuid.UUID) -> bool:
    """Revoke an active device; False when unknown or already revoked."""
    cur = await conn.execute(
        "UPDATE devices SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
        (device_id,),
    )
    return cur.rowcount == 1


async def list_all(conn: psycopg.AsyncConnection) -> list[DeviceRow]:
    async with conn.cursor(row_factory=class_row(DeviceRow)) as cur:
        await cur.execute(f"SELECT {COLUMNS} FROM devices ORDER BY created_at, id")
        return await cur.fetchall()
