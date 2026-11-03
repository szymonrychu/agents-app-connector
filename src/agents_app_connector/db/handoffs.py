"""Hand-offs of a spec to an agent session (queue). Insert by the app api, transitions by the
app worker, always through agents_common.db.queue.transition."""

import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg
from agents_common.db.queue import transition
from psycopg.rows import class_row

COLUMNS = (
    "id, conversation_id, device_id, client_id, fingerprint, spec_version, target_session_id, "
    "target_preset, status, reason, run_id, session_slug, created_at"
)


@dataclass(frozen=True)
class HandoffRow:
    id: uuid.UUID
    conversation_id: uuid.UUID
    device_id: uuid.UUID
    client_id: uuid.UUID
    fingerprint: str
    spec_version: int
    target_session_id: uuid.UUID | None
    target_preset: str | None
    status: str
    reason: str | None
    run_id: int | None
    session_slug: str | None
    created_at: datetime


def planned_slug(handoff_id: uuid.UUID) -> str:
    """Slug of the session a preset hand-off creates."""
    return f"app-handoff-{handoff_id.hex[:12]}"


async def insert(
    conn: psycopg.AsyncConnection,
    *,
    id: uuid.UUID,
    conversation_id: uuid.UUID,
    device_id: uuid.UUID,
    client_id: uuid.UUID,
    fingerprint: str,
    spec_version: int,
    target_session_id: uuid.UUID | None,
    target_preset: str | None,
    session_slug: str,
) -> None:
    await conn.execute(
        "INSERT INTO handoffs (id, conversation_id, device_id, client_id, fingerprint, "
        "spec_version, target_session_id, target_preset, session_slug) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            id,
            conversation_id,
            device_id,
            client_id,
            fingerprint,
            spec_version,
            target_session_id,
            target_preset,
            session_slug,
        ),
    )


async def _one(conn: psycopg.AsyncConnection, where: str, params: tuple) -> HandoffRow | None:
    async with conn.cursor(row_factory=class_row(HandoffRow)) as cur:
        await cur.execute(f"SELECT {COLUMNS} FROM handoffs WHERE {where}", params)
        return await cur.fetchone()


async def by_client_id(
    conn: psycopg.AsyncConnection, device_id: uuid.UUID, client_id: uuid.UUID
) -> HandoffRow | None:
    return await _one(conn, "device_id = %s AND client_id = %s", (device_id, client_id))


async def by_run(conn: psycopg.AsyncConnection, run_id: int) -> HandoffRow | None:
    return await _one(conn, "run_id = %s", (run_id,))


async def queued(conn: psycopg.AsyncConnection) -> list[HandoffRow]:
    """Queued hand-offs, oldest first."""
    async with conn.cursor(row_factory=class_row(HandoffRow)) as cur:
        await cur.execute(
            f"SELECT {COLUMNS} FROM handoffs WHERE status = 'queued' ORDER BY created_at, id"
        )
        return await cur.fetchall()


async def claim(conn: psycopg.AsyncConnection, handoff_id: uuid.UUID, run_id: int) -> bool:
    """Guarded queued -> in-progress."""
    return await transition(conn, "handoffs", handoff_id, "queued", "in-progress", run_id=run_id)


async def fail_queued(conn: psycopg.AsyncConnection, handoff_id: uuid.UUID, reason: str) -> bool:
    return await transition(conn, "handoffs", handoff_id, "queued", "failed", reason=reason)


async def finish_run(
    conn: psycopg.AsyncConnection, run_id: int, status: str, reason: str | None
) -> HandoffRow | None:
    """Settle the in-progress hand-off of a run; the settled row, None when there is none."""
    row = await _one(conn, "run_id = %s AND status = 'in-progress' FOR UPDATE", (run_id,))
    if row is None or not await transition(
        conn, "handoffs", row.id, "in-progress", status, reason=reason
    ):
        return None
    return row
