"""The reply tracker: settles finished translator turns.

The translator writes the document and its feedback during the turn with its MCP tools
(`translator_tools`); the reply text is never stored. Each tick polls the message of every
in-progress `first` or `follow` run once. A completed turn that wrote a version or feedback is
closed in the connector's own transaction: the run, the inbound row, the run's feedback linked
to the last version it wrote, and the `done` status event (with that version, if any), so a
crash leaves the run in progress and the next tick settles it once. A completed turn that wrote
neither fails `no_document`. Every other outcome (failed turn, failed or cancelled message,
message not found, timeout) goes to agents_common's `track` with the shared run store, whose
finish settles the row and emits the `failed` event; a follow run whose session is gone is
requeued there. A run of a deleted conversation fails `conversation_deleted` with no events. An
unavailable orchestrator leaves the run in progress for the next tick. A handoff run finishes
when its message was sent (`track_delivery`); a cancelled, failed or missing message fails it
`session_gone`.
"""

import logging
from collections.abc import Callable
from datetime import datetime

from agents_common import runs as agent_runs
from agents_common.orchestrator import (
    Message,
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
)
from psycopg_pool import AsyncConnectionPool

from agents_app_connector.clock import utcnow
from agents_app_connector.db import bindings, conversations, events, feedback, inbound, runs, specs
from agents_app_connector.run_store import AppRunStore, status_event
from agents_app_connector.settings import Settings

log = logging.getLogger(__name__)

TRACKED = ("first", "follow")
NO_DOCUMENT = "no_document"
CONVERSATION_DELETED = "conversation_deleted"
INBOUND_MISSING = "inbound_missing"
TIMEOUT = "timeout"
SESSION_GONE = "session_gone"


class _Fetched:
    """A client that answers `get_message` with the message already fetched this tick, so the
    shared `track` does not poll a second time."""

    def __init__(self, message: Message | None) -> None:
        self._message = message

    async def get_message(self, message_id: int) -> Message:
        if self._message is None:
            raise NotFound(404, "not_found", "message not found")
        return self._message


class Tracker:
    def __init__(
        self,
        orchestrator: OrchestratorClient,
        pool: AsyncConnectionPool,
        settings: Settings,
        *,
        now: Callable[[], datetime] = utcnow,
    ):
        self._orchestrator = orchestrator
        self._pool = pool
        self._settings = settings
        self._now = now
        self._runs = AppRunStore(pool)

    async def tick(self) -> None:
        async with self._pool.connection() as conn:
            active = [
                r
                for r in await runs.in_progress(conn)
                if r.message_id is not None and r.kind in (*TRACKED, "handoff")
            ]
        for run in active:
            try:
                await self._track(run)
            except OrchestratorUnavailable as exc:
                log.warning("run %s: orchestrator unavailable, retrying next tick: %s", run.id, exc)

    async def _track_handoff(self, run: runs.RunRow) -> None:
        """A hand-off is done when its message was sent; a lost message fails it."""
        outcome = await agent_runs.track_delivery(
            self._orchestrator,
            self._runs,
            runs.to_ref(run),
            timeout_seconds=self._settings.run_timeout_seconds,
            now=self._now(),
        )
        if outcome == "gone":
            await self._runs.finish(run.id, "failed", SESSION_GONE)

    async def _track(self, run: runs.RunRow) -> None:
        if run.kind == "handoff":
            await self._track_handoff(run)
            return
        async with self._pool.connection() as conn:
            conv = await conversations.get(conn, run.conversation_id)
        if conv is None or conv.state == "deleted":
            await self._fail_deleted(run)
            return
        message = await self._fetch(run)
        turn = None if message is None else message.turn
        if turn is not None and turn.status == "completed":
            # Length only: the reply may carry user content and is never stored.
            log.info(
                "run %s: turn completed, reply of %d chars", run.id, len(turn.final_text or "")
            )
            if not await self._settle(run):
                await self._runs.finish(run.id, "failed", NO_DOCUMENT)
            return
        await agent_runs.track(
            _Fetched(message),  # type: ignore[arg-type]
            self._runs,
            runs.to_ref(run),
            self._now(),
            self._settings.run_timeout_seconds,
        )

    async def _fetch(self, run: runs.RunRow) -> Message | None:
        """The run's message; None when it is gone."""
        assert run.message_id is not None
        try:
            return await self._orchestrator.get_message(int(run.message_id))
        except NotFound:
            return None
        except OrchestratorUnavailable:
            raise
        except OrchestratorError as exc:
            # Status and code only: the message text may echo request content.
            log.warning("run %s: poll refused: %s %s", run.id, exc.status, exc.code)
            if (self._now() - run.started_at).total_seconds() > self._settings.run_timeout_seconds:
                await self._runs.finish(run.id, "failed", TIMEOUT)
            raise OrchestratorUnavailable(f"poll refused: {exc.status}") from exc

    async def _settle(self, run: runs.RunRow) -> bool:
        """Close a completed turn that wrote a version or feedback; False when it wrote neither
        (the run stays in progress for the caller to fail)."""
        async with self._pool.connection() as conn, conn.transaction():
            row = await inbound.by_run(conn, run.id)
            conv = await conversations.get(conn, run.conversation_id)
            if row is None or conv is None or conv.state == "deleted":
                if row is None:
                    await runs.finish(conn, run.id, "failed", INBOUND_MISSING)
                else:
                    await self._close(conn, run, CONVERSATION_DELETED)
                return True
            version = await specs.last_of_run(conn, run.id)
            if version is None and await feedback.count_for_run(conn, run.id) == 0:
                return False
            if not await runs.finish(conn, run.id, "handled"):
                return True
            await inbound.finish(conn, run.id, "handled", None)
            extra = {}
            if version is not None:
                await feedback.set_spec_version(conn, run.id, version)
                extra["version"] = version
            type_, payload = status_event(row, "done", **extra)
            await events.append(conn, run.conversation_id, type_, payload)
            await conversations.touch(conn, run.conversation_id)
        log.info("run %s done (version %s) for %s", run.id, version, run.conversation_id)
        return True

    async def _fail_deleted(self, run: runs.RunRow) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await self._close(conn, run, CONVERSATION_DELETED)

    @staticmethod
    async def _close(conn, run: runs.RunRow, reason: str) -> None:
        """Fail the run and its row and release its binding, with no event."""
        if not await runs.finish(conn, run.id, "failed", reason):
            return
        await inbound.finish(conn, run.id, "failed", reason)
        binding = await bindings.active(conn, run.conversation_id)
        if binding is not None:
            await bindings.release(conn, binding.id, reason)
        log.info("run %s failed (%s)", run.id, reason)
