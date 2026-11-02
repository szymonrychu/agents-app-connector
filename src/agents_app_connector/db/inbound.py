"""Inbound sketches and messages (queue). Insert by the app api, transitions by the app worker.

Status changes go through agents_common.db.queue.transition or a status-guarded UPDATE.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import psycopg
from agents_common.db.queue import transition
from psycopg.rows import class_row
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class InboundRow:
    id: uuid.UUID
    conversation_id: uuid.UUID
    device_id: uuid.UUID
    client_id: uuid.UUID
    fingerprint: str
    kind: str
    text: str | None
    meta: dict
    status: str
    reason: str | None
    run_id: int | None
    created_at: datetime


COLUMNS = (
    "id, conversation_id, device_id, client_id, fingerprint, kind, text, meta, status, reason, "
    "run_id, created_at"
)


def fingerprint(meta: dict, part_digests: Sequence[str]) -> str:
    """sha256 of the canonical JSON of the meta plus the part digests in order."""
    canonical = json.dumps(
        {"meta": meta, "parts": list(part_digests)}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def try_lock_client(
    conn: psycopg.AsyncConnection, device_id: uuid.UUID, client_id: uuid.UUID
) -> bool:
    """Transaction-scoped advisory lock on (device, client id); False when another holds it."""
    cur = await conn.execute(
        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s::text || %s::text, 0))",
        (device_id, client_id),
    )
    return (await cur.fetchone())[0]


async def insert(
    conn: psycopg.AsyncConnection,
    *,
    id: uuid.UUID,
    conversation_id: uuid.UUID,
    device_id: uuid.UUID,
    client_id: uuid.UUID,
    fingerprint: str,
    kind: str,
    text: str | None,
    meta: dict,
) -> None:
    await conn.execute(
        "INSERT INTO inbound (id, conversation_id, device_id, client_id, fingerprint, kind, text, "
        "meta) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (id, conversation_id, device_id, client_id, fingerprint, kind, text, Jsonb(meta)),
    )


async def by_client_id(
    conn: psycopg.AsyncConnection, device_id: uuid.UUID, client_id: uuid.UUID
) -> InboundRow | None:
    async with conn.cursor(row_factory=class_row(InboundRow)) as cur:
        await cur.execute(
            f"SELECT {COLUMNS} FROM inbound WHERE device_id = %s AND client_id = %s",
            (device_id, client_id),
        )
        return await cur.fetchone()


async def next_queued(
    conn: psycopg.AsyncConnection, now: datetime | None = None
) -> list[uuid.UUID]:
    """Open conversations with a queued row, no in-progress row and no hold active at `now`
    (the database clock when omitted), oldest first."""
    cur = await conn.execute(
        "SELECT q.conversation_id FROM inbound q "
        "JOIN conversations c ON c.id = q.conversation_id AND c.state = 'open' "
        "WHERE q.status = 'queued' "
        "AND NOT EXISTS (SELECT 1 FROM inbound p WHERE p.conversation_id = q.conversation_id "
        "AND p.status = 'in-progress') "
        "AND NOT EXISTS (SELECT 1 FROM holds h WHERE h.conversation_id = q.conversation_id "
        "AND h.until > coalesce(%s, now())) "
        "GROUP BY q.conversation_id ORDER BY min(q.created_at), q.conversation_id",
        (now,),
    )
    return [r[0] for r in await cur.fetchall()]


async def claim_oldest(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID, run_id: int
) -> InboundRow | None:
    """Guarded queued -> in-progress for the oldest queued row of the conversation."""
    cur = await conn.execute(
        "SELECT id FROM inbound WHERE conversation_id = %s AND status = 'queued' "
        "ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED",
        (conversation_id,),
    )
    row = await cur.fetchone()
    if row is None or not await transition(
        conn, "inbound", row[0], "queued", "in-progress", run_id=run_id
    ):
        return None
    async with conn.cursor(row_factory=class_row(InboundRow)) as cur2:
        await cur2.execute(f"SELECT {COLUMNS} FROM inbound WHERE id = %s", (row[0],))
        return await cur2.fetchone()


async def finish(
    conn: psycopg.AsyncConnection, run_id: int, status: str, reason: str | None
) -> int:
    """Settle the in-progress row of a run as handled or failed."""
    n = 0
    for row_id in await _ids(conn, "run_id = %s AND status = 'in-progress'", (run_id,)):
        n += await transition(conn, "inbound", row_id, "in-progress", status, reason=reason)
    return n


async def requeue(conn: psycopg.AsyncConnection, run_id: int) -> int:
    n = 0
    for row_id in await _ids(conn, "run_id = %s AND status = 'in-progress'", (run_id,)):
        n += await transition(conn, "inbound", row_id, "in-progress", "queued", run_id=None)
    return n


async def _ids(conn: psycopg.AsyncConnection, where: str, params: tuple) -> list[uuid.UUID]:
    cur = await conn.execute(f"SELECT id FROM inbound WHERE {where} FOR UPDATE", params)
    return [r[0] for r in await cur.fetchall()]


async def fail_queued_for(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID, reason: str
) -> int:
    return len(await fail_queued_rows(conn, conversation_id, reason))


async def by_run(conn: psycopg.AsyncConnection, run_id: int) -> InboundRow | None:
    """The in-progress row claimed by the run."""
    async with conn.cursor(row_factory=class_row(InboundRow)) as cur:
        await cur.execute(
            f"SELECT {COLUMNS} FROM inbound WHERE run_id = %s AND status = 'in-progress'",
            (run_id,),
        )
        return await cur.fetchone()


async def latest_sketch(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID
) -> InboundRow | None:
    """The newest sketch row of the conversation, whatever its status."""
    async with conn.cursor(row_factory=class_row(InboundRow)) as cur:
        await cur.execute(
            f"SELECT {COLUMNS} FROM inbound WHERE conversation_id = %s AND kind = 'sketch' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (conversation_id,),
        )
        return await cur.fetchone()


async def fail_queued_rows(
    conn: psycopg.AsyncConnection, conversation_id: uuid.UUID, reason: str
) -> list[InboundRow]:
    """Fail every queued row of the conversation (guarded) and return them."""
    failed: list[InboundRow] = []
    where = "conversation_id = %s AND status = 'queued' ORDER BY created_at, id"
    ids = await _ids(conn, where, (conversation_id,))
    for row_id in ids:
        if await transition(conn, "inbound", row_id, "queued", "failed", reason=reason):
            async with conn.cursor(row_factory=class_row(InboundRow)) as cur:
                await cur.execute(f"SELECT {COLUMNS} FROM inbound WHERE id = %s", (row_id,))
                failed.append(await cur.fetchone())
    return failed
