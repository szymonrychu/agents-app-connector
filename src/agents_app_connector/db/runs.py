"""Agent runs of the connector (one per inbound row or handoff). Writer: the app worker.

Writers that take a connection never commit; the caller owns the transaction. Status changes go
through agents_common.db.queue.transition.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from agents_common.db.queue import transition
from agents_common.runs import RunRef
from psycopg.rows import class_row

COLUMNS = (
    "id, conversation_id, inbound_id, handoff_id, kind, preset_id, slug, session_id, "
    "message_id, status, reason, started_at, finished_at"
)


@dataclass(frozen=True)
class RunRow:
    id: int
    conversation_id: uuid.UUID
    inbound_id: uuid.UUID | None
    handoff_id: uuid.UUID | None
    kind: str
    preset_id: uuid.UUID | None
    slug: str | None
    session_id: uuid.UUID | None
    message_id: str | None
    status: str
    reason: str | None
    started_at: datetime
    finished_at: datetime | None


def to_ref(run: RunRow) -> RunRef:
    return RunRef(
        id=run.id,
        slug=run.slug or f"app-{run.id}",
        preset_id="" if run.preset_id is None else str(run.preset_id),
        session_id=None if run.session_id is None else str(run.session_id),
        message_id=run.message_id,
        started_at=run.started_at,
    )


async def create(
    conn: psycopg.AsyncConnection,
    *,
    conversation_id: uuid.UUID,
    kind: str,
    preset_id: uuid.UUID | str | None,
    inbound_id: uuid.UUID | None = None,
    handoff_id: uuid.UUID | None = None,
    slug: str | None = None,
) -> RunRef:
    """Insert an in-progress run with slug `slug`, `app-<id>` when omitted."""
    cur = await conn.execute(
        "INSERT INTO runs (conversation_id, inbound_id, handoff_id, kind, preset_id) "
        "VALUES (%s, %s, %s, %s, %s::uuid) RETURNING id, started_at",
        (
            conversation_id,
            inbound_id,
            handoff_id,
            kind,
            None if preset_id is None else str(preset_id),
        ),
    )
    run_id, started_at = await cur.fetchone()
    slug = slug or f"app-{run_id}"
    await conn.execute("UPDATE runs SET slug = %s WHERE id = %s", (slug, run_id))
    return RunRef(
        id=run_id,
        slug=slug,
        preset_id="" if preset_id is None else str(preset_id),
        session_id=None,
        message_id=None,
        started_at=started_at,
    )


async def attach_inbound(conn: psycopg.AsyncConnection, run_id: int, inbound_id: uuid.UUID) -> None:
    await conn.execute(
        "UPDATE runs SET inbound_id = %s WHERE id = %s AND status = 'in-progress'",
        (inbound_id, run_id),
    )


async def set_session(
    conn: psycopg.AsyncConnection, run_id: int, session_id: uuid.UUID | str
) -> str | None:
    """Store the session of an in-progress run; its slug, or None when it is not in progress."""
    cur = await conn.execute(
        "UPDATE runs SET session_id = %s::uuid WHERE id = %s AND status = 'in-progress' "
        "RETURNING coalesce(slug, 'app-' || id)",
        (str(session_id), run_id),
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def set_message(conn: psycopg.AsyncConnection, run_id: int, message_id: str) -> bool:
    cur = await conn.execute(
        "UPDATE runs SET message_id = %s WHERE id = %s AND status = 'in-progress'",
        (message_id, run_id),
    )
    return cur.rowcount == 1


async def finish(
    conn: psycopg.AsyncConnection, run_id: int, status: str, reason: str | None = None
) -> bool:
    if status not in ("handled", "failed"):
        raise ValueError(f"finish status must be handled or failed, got {status!r}")
    return await transition(
        conn, "runs", run_id, "in-progress", status, reason=reason, finished_at=datetime.now(UTC)
    )


async def in_progress(conn: psycopg.AsyncConnection) -> list[RunRow]:
    async with conn.cursor(row_factory=class_row(RunRow)) as cur:
        await cur.execute(f"SELECT {COLUMNS} FROM runs WHERE status = 'in-progress' ORDER BY id")
        return await cur.fetchall()


async def get(conn: psycopg.AsyncConnection, run_id: int) -> RunRow | None:
    async with conn.cursor(row_factory=class_row(RunRow)) as cur:
        await cur.execute(f"SELECT {COLUMNS} FROM runs WHERE id = %s", (run_id,))
        return await cur.fetchone()


async def count_first_in_progress(conn: psycopg.AsyncConnection) -> int:
    cur = await conn.execute(
        "SELECT count(*) FROM runs WHERE status = 'in-progress' AND kind = 'first'"
    )
    return (await cur.fetchone())[0]


async def has_other_run(
    conn: psycopg.AsyncConnection,
    session_id: uuid.UUID | str,
    conversation_id: uuid.UUID,
    exclude_run_id: int,
) -> bool:
    """Whether the session has a run of the conversation other than `exclude_run_id`."""
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM runs WHERE session_id = %s::uuid "
        "AND conversation_id = %s AND id <> %s)",
        (str(session_id), conversation_id, exclude_run_id),
    )
    return (await cur.fetchone())[0]
