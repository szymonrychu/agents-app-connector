"""The agents-common RunStore of the connector's runs.

Storing a first run's session fills the conversation's reservation with it; finishing a run
settles its in-progress inbound row and releases a reservation that never got a session, each in
one transaction, so a failed dispatch never leaves a row in progress or a conversation reserved.
Shared by the dispatch worker and the reply tracker.
"""

import logging

import psycopg
from agents_common import runs as agent_runs
from psycopg_pool import AsyncConnectionPool

from agents_app_connector.db import bindings, events, handoffs, inbound, runs

log = logging.getLogger(__name__)

RUN_FAILED = "run_failed"
SESSION_GONE = "session_gone"
# agents_common.runs.track reasons that mean the session of a follow run is gone.
_GONE_REASONS = frozenset({"session_not_found", "message_cancelled"})


def status_event(row: inbound.InboundRow, status: str, **extra) -> tuple[str, dict]:
    """The (type, payload) of a status event of an inbound row."""
    if row.kind == "sketch":
        return "sketch_status", {"sketch_id": str(row.id), "status": status, **extra}
    return "message", {"message_id": str(row.id), "status": status, **extra}


def handoff_event(row: handoffs.HandoffRow, status: str, **extra) -> dict:
    """The payload of a `handoff` event."""
    return {
        "handoff_id": str(row.id),
        "status": status,
        "session_slug": row.session_slug,
        **extra,
    }


class AppRunStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def set_session(self, run_id: agent_runs.RunId, session_id: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            slug = await runs.set_session(conn, int(run_id), session_id)
            if slug is not None and await bindings.fill(conn, int(run_id), session_id, slug):
                log.info("run %s bound its conversation to session %s", run_id, session_id)

    async def set_message(self, run_id: agent_runs.RunId, message_id: str) -> None:
        async with self._pool.connection() as conn:
            await runs.set_message(conn, int(run_id), message_id)

    async def finish(
        self, run_id: agent_runs.RunId, status: agent_runs.RunStatus, reason: str | None
    ) -> bool:
        async with self._pool.connection() as conn, conn.transaction():
            run = await runs.get(conn, int(run_id))
            gone = status == "failed" and reason in _GONE_REASONS and run and run.kind == "follow"
            if gone:
                reason = SESSION_GONE
            if not await runs.finish(conn, int(run_id), status, reason):
                return False
            row = await inbound.by_run(conn, int(run_id))
            if run is not None and run.kind == "handoff":
                await self._settle_handoff(conn, run, status, reason)
            elif gone:
                await self._session_gone(conn, run, row)
            elif row is not None:
                await inbound.finish(conn, int(run_id), status, reason)
                if status == "failed":
                    type_, payload = status_event(row, "failed", reason=reason)
                    await events.append(conn, row.conversation_id, type_, payload)
            released = await bindings.release_reservation(conn, int(run_id), RUN_FAILED)
        log.info("run %s finished %s (%s)", run_id, status, reason)
        if released:
            log.info("run %s ended without a session: reservation released", run_id)
        return True

    @staticmethod
    async def _settle_handoff(
        conn: psycopg.AsyncConnection, run: runs.RunRow, status: str, reason: str | None
    ) -> None:
        """Settle the hand-off of a run and emit its `handoff` event."""
        done = await handoffs.finish_run(conn, run.id, status, reason)
        if done is not None:
            extra = {} if reason is None else {"reason": reason}
            await events.append(
                conn, done.conversation_id, "handoff", handoff_event(done, status, **extra)
            )

    @staticmethod
    async def _session_gone(
        conn: psycopg.AsyncConnection, run: runs.RunRow, row: inbound.InboundRow | None
    ) -> None:
        """A follow run whose session is gone: the row returns to the queue and the binding is
        released, so the worker starts a fresh session with the latest spec."""
        await inbound.requeue(conn, run.id)
        binding = await bindings.active(conn, run.conversation_id)
        if binding is not None:
            await bindings.release(conn, binding.id, SESSION_GONE)
        if row is not None:
            type_, payload = status_event(row, "queued", reason=SESSION_GONE)
            await events.append(conn, run.conversation_id, type_, payload)
