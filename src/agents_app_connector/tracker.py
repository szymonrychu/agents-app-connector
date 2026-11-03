"""The reply tracker: finished translator turns into stored spec versions.

Each tick polls the message of every in-progress `first` or `follow` run once. A completed turn
is closed in the connector's own transaction: the run, the spec version, the inbound row and the
`spec` and status events together, so a crash leaves the run in progress and the next tick
stores the spec once. Every other outcome (failed turn, failed or cancelled message, message not
found, timeout) goes to agents_common's `track` with the shared run store, whose finish settles
the row and emits the `failed` event; a follow run whose session is gone is requeued there. An
empty reply fails the run `empty_reply`. A run of a deleted conversation fails
`conversation_deleted` with no spec and no events. An unavailable orchestrator leaves the run in
progress for the next tick. A handoff run finishes when its message was sent
(`track_delivery`); a cancelled, failed or missing message fails it `session_gone`.
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
from agents_app_connector.db import bindings, conversations, events, inbound, runs, specs
from agents_app_connector.plugins import REGISTRY
from agents_app_connector.run_store import AppRunStore, status_event
from agents_app_connector.settings import Settings

log = logging.getLogger(__name__)

TRACKED = ("first", "follow")
EMPTY_REPLY = "empty_reply"
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
            if (turn.final_text or "").strip():
                await self._store_spec(run, conv.plugin, turn.final_text or "")
            else:
                await self._runs.finish(run.id, "failed", EMPTY_REPLY)
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

    async def _store_spec(self, run: runs.RunRow, plugin_id: str, final_text: str) -> None:
        markdown, has_front = REGISTRY[plugin_id].outbound(final_text)
        async with self._pool.connection() as conn, conn.transaction():
            row = await inbound.by_run(conn, run.id)
            conv = await conversations.get(conn, run.conversation_id)
            if row is None or conv is None or conv.state == "deleted":
                if row is None:
                    await runs.finish(conn, run.id, "failed", INBOUND_MISSING)
                else:
                    await self._close(conn, run, CONVERSATION_DELETED)
                return
            if not await runs.finish(conn, run.id, "handled"):
                return
            sketch_ids = await inbound.sketch_ids_upto(conn, row)
            version = await specs.add(
                conn, run.conversation_id, markdown, row.id, sketch_ids, has_front
            )
            await inbound.finish(conn, run.id, "handled", None)
            await events.append(
                conn, run.conversation_id, "spec", {"version": version, "front_matter": has_front}
            )
            type_, payload = status_event(row, "done", version=version)
            await events.append(conn, run.conversation_id, type_, payload)
            await conversations.touch(conn, run.conversation_id)
        log.info("run %s stored spec v%s of %s", run.id, version, run.conversation_id)

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
