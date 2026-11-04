"""Device bearer tokens. Writer: the app component. Only the sha256 of a token is stored."""

import hashlib
import secrets
import uuid

import psycopg
from psycopg.rows import class_row

from agents_app_connector.db.devices import COLUMNS, DeviceRow


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


async def issue(conn: psycopg.AsyncConnection, device_id: uuid.UUID) -> str:
    """Store the hash of a fresh token for a device without one; the plaintext is returned once.

    A device has at most one token: a second issue raises UniqueViolation.
    """
    token = new_token()
    await conn.execute(
        "INSERT INTO device_tokens (device_id, token_sha256) VALUES (%s, %s)",
        (device_id, hash_token(token)),
    )
    return token


async def by_token(conn: psycopg.AsyncConnection, token: str) -> DeviceRow | None:
    prefixed = ", ".join(f"d.{c.strip()}" for c in COLUMNS.split(","))
    async with conn.cursor(row_factory=class_row(DeviceRow)) as cur:
        await cur.execute(
            f"SELECT {prefixed} FROM device_tokens t JOIN devices d ON d.id = t.device_id "
            "WHERE t.token_sha256 = %s AND d.revoked_at IS NULL",
            (hash_token(token),),
        )
        return await cur.fetchone()
