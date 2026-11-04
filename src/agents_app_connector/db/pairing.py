"""Pairing codes (6 digits, stored as sha256 only). Queue table: the admin inserts rows, the app
changes their status only through status-guarded UPDATEs.

A code is redeemable only while it is `pending`, unexpired and the newest code of its device;
older pending rows of a device are marked `superseded` by the app sweep (display only).
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg

CODE_DIGITS = 6
LOCK_KEY = "agents-app-connector-pairing-codes"
MAX_COLLISION_TRIES = 20


class TooManyPending(Exception):
    """Too many devices are waiting for a code."""


def new_code() -> str:
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


@dataclass(frozen=True)
class IssuedCode:
    code: str
    expires_at: datetime


async def issue(
    conn: psycopg.AsyncConnection, device_id: uuid.UUID, ttl_seconds: int, max_pending: int
) -> IssuedCode:
    """Admin: insert a new code for a device. Call inside a transaction (advisory xact lock).

    Raises TooManyPending when `max_pending` other devices already wait on an unexpired code.
    The plaintext is returned once and never stored.
    """
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (LOCK_KEY,))
    cur = await conn.execute(
        "SELECT count(DISTINCT device_id) FROM pairing_codes "
        "WHERE status = 'pending' AND expires_at > now() AND device_id <> %s",
        (device_id,),
    )
    if (await cur.fetchone())[0] >= max_pending:
        raise TooManyPending
    for _ in range(MAX_COLLISION_TRIES):
        code = new_code()
        digest = hash_code(code)
        cur = await conn.execute(
            "SELECT 1 FROM pairing_codes "
            "WHERE code_sha256 = %s AND status = 'pending' AND expires_at > now()",
            (digest,),
        )
        if await cur.fetchone() is None:
            break
    else:
        raise RuntimeError("no free pairing code")
    cur = await conn.execute(
        "INSERT INTO pairing_codes (id, device_id, code_sha256, expires_at) "
        "VALUES (%s, %s, %s, now() + make_interval(secs => %s)) RETURNING expires_at",
        (uuid.uuid4(), device_id, digest, ttl_seconds),
    )
    return IssuedCode(code=code, expires_at=(await cur.fetchone())[0])


REDEEM_SQL = """
UPDATE pairing_codes c SET status = 'redeemed', redeemed_at = now(), redeemed_ip = %s
FROM devices d
WHERE c.code_sha256 = %s AND c.status = 'pending' AND c.expires_at > now()
  AND d.id = c.device_id AND d.revoked_at IS NULL AND d.plugin = %s
  AND NOT EXISTS (SELECT 1 FROM device_tokens t WHERE t.device_id = c.device_id)
  AND c.id = (SELECT n.id FROM pairing_codes n WHERE n.device_id = c.device_id
              ORDER BY n.created_at DESC, n.id DESC LIMIT 1)
RETURNING c.device_id
"""


async def redeem(
    conn: psycopg.AsyncConnection, code: str, plugin: str, client_ip: str
) -> uuid.UUID | None:
    """App: atomically take a pending code; the device id, or None for any failure cause.

    Status-guarded: of two concurrent redeems of one code exactly one gets the row.
    """
    cur = await conn.execute(REDEEM_SQL, (client_ip, hash_code(code), plugin))
    row = await cur.fetchone()
    return row[0] if row else None


async def record_failure(conn: psycopg.AsyncConnection, max_failures: int) -> int:
    """App: count a failed exchange against every pending code; lock those at the budget.

    Returns how many codes were locked by this failure.
    """
    cur = await conn.execute(
        "UPDATE pairing_codes SET failed_attempts = failed_attempts + 1, "
        "status = CASE WHEN failed_attempts + 1 >= %s THEN 'locked' ELSE status END "
        "WHERE status = 'pending' AND expires_at > now() "
        "RETURNING status",
        (max_failures,),
    )
    return sum(1 for (status,) in await cur.fetchall() if status == "locked")


async def sweep(conn: psycopg.AsyncConnection) -> int:
    """App: mark expired and superseded pending codes (display and retention only)."""
    cur = await conn.execute(
        "UPDATE pairing_codes c SET status = CASE WHEN c.expires_at <= now() "
        "THEN 'expired' ELSE 'superseded' END "
        "WHERE c.status = 'pending' AND (c.expires_at <= now() OR EXISTS ("
        "SELECT 1 FROM pairing_codes n WHERE n.device_id = c.device_id AND "
        "(n.created_at, n.id) > (c.created_at, c.id)))"
    )
    return cur.rowcount


async def purge(conn: psycopg.AsyncConnection, cutoff: datetime) -> int:
    """App: delete code rows that are no longer pending and were created before `cutoff`."""
    cur = await conn.execute(
        "DELETE FROM pairing_codes WHERE status <> 'pending' AND created_at < %s", (cutoff,)
    )
    return cur.rowcount
