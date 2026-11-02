"""Hand-off of a finished spec to a chosen agent session or a new one from a preset.

A tick first retries every in-progress handoff run without a message (orchestrator unavailable
or restart mid-dispatch), then claims each queued hand-off, oldest first. A claim is one
transaction: the run (kind `handoff`), the status-guarded queued -> in-progress transition, the
session of an existing target and the `handoff` event. An existing session gets the spec through
agents-common's `deliver`; a preset creates session `app-handoff-<id>` through `dispatch`. Both
skip every stored step and adopt a message carrying the run's marker, so a retry posts the spec
once. A target that is gone (or a preset that no longer exists) fails the hand-off, with no
fallback: the user chose it. The tracker finishes the run when the message was sent.
"""

import logging

import psycopg
from agents_common import runs as agent_runs
from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
    UsageQuotaRefused,
)
from psycopg_pool import AsyncConnectionPool

from agents_app_connector.db import conversations, events, handoffs, runs, specs
from agents_app_connector.run_store import AppRunStore, handoff_event

log = logging.getLogger(__name__)

RUN_KIND = "app"
SESSION_GONE = "session_gone"
NO_PRESET = "no_preset"
NO_SPEC = "no_spec"
USAGE_QUOTA = "usage_quota"


class Handoffs:
    def __init__(self, orchestrator: OrchestratorClient, pool: AsyncConnectionPool) -> None:
        self._orchestrator = orchestrator
        self._pool = pool
        self._runs = AppRunStore(pool)

    async def tick(self) -> None:
        async with self._pool.connection() as conn:
            unposted = [
                r
                for r in await runs.in_progress(conn)
                if r.kind == "handoff" and r.message_id is None
            ]
            queued = await handoffs.queued(conn)
        for run in unposted:
            await self._guard(self._resume(run))
        for row in queued:
            await self._guard(self._claim(row))

    @staticmethod
    async def _guard(coro) -> None:
        try:
            await coro
        except OrchestratorUnavailable as exc:
            log.warning("orchestrator unavailable, retrying next tick: %s", exc)

    async def _claim(self, row: handoffs.HandoffRow) -> None:
        reason = await self._precheck(row)
        if reason is not None:
            await self._fail_queued(row, reason)
            return
        preset_id = None
        if row.target_preset is not None:
            preset_id = await self._preset_id(row.target_preset)
            if preset_id is None:
                await self._fail_queued(row, NO_PRESET)
                return
        claimed = False
        async with self._pool.connection() as conn, conn.transaction():
            ref = await runs.create(
                conn,
                conversation_id=row.conversation_id,
                kind="handoff",
                preset_id=preset_id,
                handoff_id=row.id,
                slug=row.session_slug if row.target_preset is not None else None,
            )
            if not await handoffs.claim(conn, row.id, int(ref.id)):
                raise psycopg.Rollback()
            if row.target_session_id is not None:
                await runs.set_session(conn, int(ref.id), row.target_session_id)
            await events.append(
                conn, row.conversation_id, "handoff", handoff_event(row, "in-progress")
            )
            claimed = True
        if not claimed:
            return
        async with self._pool.connection() as conn:
            run = await runs.get(conn, int(ref.id))
        await self._post(row, run)

    async def _precheck(self, row: handoffs.HandoffRow) -> str | None:
        """The reason a queued hand-off cannot go out, None when it can."""
        async with self._pool.connection() as conn:
            conv = await conversations.get(conn, row.conversation_id)
            spec = await specs.get(conn, row.conversation_id, row.spec_version)
        if conv is None or conv.state == "deleted" or spec is None:
            return NO_SPEC
        if row.target_session_id is not None:
            session = await agent_runs.running_session(
                self._orchestrator, str(row.target_session_id)
            )
            if session is None:
                return SESSION_GONE
        return None

    async def _preset_id(self, name: str) -> str | None:
        presets = await self._orchestrator.list_presets()
        return next((str(p.id) for p in presets if p.name == name), None)

    async def _fail_queued(self, row: handoffs.HandoffRow, reason: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            if await handoffs.fail_queued(conn, row.id, reason):
                await events.append(
                    conn,
                    row.conversation_id,
                    "handoff",
                    handoff_event(row, "failed", reason=reason),
                )
        log.info("handoff %s failed before dispatch: %s", row.id, reason)

    async def _resume(self, run: runs.RunRow) -> None:
        async with self._pool.connection() as conn:
            row = await handoffs.by_run(conn, run.id)
        if row is None:
            await self._runs.finish(run.id, "failed", "handoff_missing")
            return
        await self._post(row, run)

    async def _post(self, row: handoffs.HandoffRow, run: runs.RunRow) -> None:
        async with self._pool.connection() as conn:
            spec = await specs.get(conn, row.conversation_id, row.spec_version)
        if spec is None:
            await self._runs.finish(run.id, "failed", NO_SPEC)
            return
        text = (
            f"Spec from the Boox sketch translator (conversation {row.conversation_id}, "
            f"version {row.spec_version}):\n\n{spec.markdown}"
        )
        ref = runs.to_ref(run)
        marker = agent_runs.marker(RUN_KIND, ref.id)
        try:
            if row.target_session_id is not None:
                delivered = await agent_runs.deliver(
                    self._orchestrator,
                    self._runs,
                    ref,
                    str(row.target_session_id),
                    text,
                    marker,
                )
                if delivered is None:
                    await self._runs.finish(run.id, "failed", SESSION_GONE)
            else:
                await agent_runs.dispatch(self._orchestrator, self._runs, ref, text, marker)
        except UsageQuotaRefused:
            await self._runs.finish(run.id, "failed", USAGE_QUOTA)
        except NotFound as exc:  # dispatch finished the run
            log.warning("handoff run %s: %s %s", run.id, exc.status, exc.code)
        except OrchestratorUnavailable:
            raise
        except OrchestratorError as exc:
            log.warning("handoff run %s refused: %s %s", run.id, exc.status, exc.code)
            await self._runs.finish(run.id, "failed", exc.code)
