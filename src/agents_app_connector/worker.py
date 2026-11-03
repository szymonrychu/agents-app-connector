"""The dispatch worker: queued sketches and messages into translator sessions.

One conversation maps to one session (`bindings`). A tick first retries every in-progress
run without a message (orchestrator unavailable or restart mid-dispatch), then handles each
conversation with a queued row, no row in progress and no hold, oldest first. One inbound row
is one run. The conversation's active binding decides:

1. bound to a running session: a `follow` run delivered into it;
2. bound to a session that is gone or deleting: the binding is released (`session_gone`) and the
   conversation is handled as unbound in the same tick;
3. reserved by a first run in progress: nothing to do;
4. unbound: the preset `settings.preset_name` must exist (else the rows fail `no_preset`); below
   `max_new_sessions` first runs in progress, a `first` run creates session `app-<run id>`.

The run, the claim of the row, the reservation and the "translating" event happen in one
transaction before any orchestrator call; the claim is a status-guarded queued -> in-progress
transition, so a row can never get a second run. agents-common's `dispatch` and `deliver` skip
every stored step and adopt a message carrying the run's marker, so a retry posts no second
prompt. A usage-quota refusal fails the run, requeues the row and holds the conversation until
the refusal's retry time. A first prompt of a conversation with specs carries the latest spec
and, for a message row, the files of the latest sketch.
"""

import dataclasses
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import psycopg
from agents_common import runs as agent_runs
from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
    Preset,
    Session,
    UsageQuotaRefused,
)
from psycopg_pool import AsyncConnectionPool

from agents_app_connector.clock import utcnow
from agents_app_connector.db import (
    attachments,
    bindings,
    conversations,
    events,
    holds,
    inbound,
    runs,
    specs,
)
from agents_app_connector.db.inbound import InboundRow
from agents_app_connector.handoff import Handoffs
from agents_app_connector.plugins import REGISTRY, TurnFile, TurnInput
from agents_app_connector.plugins.sketch import MISSING_FILE
from agents_app_connector.run_store import AppRunStore, status_event
from agents_app_connector.settings import Settings
from agents_app_connector.storage import AttachmentStore

log = logging.getLogger(__name__)

RUN_KIND = "app"
SESSION_GONE = "session_gone"
USAGE_QUOTA = "usage_quota"
NO_PRESET = "no_preset"
RESUMABLE = ("first", "follow")


@dataclass(frozen=True)
class _Batch:
    kind: str
    ref: agent_runs.RunRef
    row: InboundRow


class Worker:
    def __init__(
        self,
        orchestrator: OrchestratorClient,
        pool: AsyncConnectionPool,
        settings: Settings,
        store: AttachmentStore,
        *,
        now: Callable[[], datetime] = utcnow,
    ):
        self._orchestrator = orchestrator
        self._pool = pool
        self._settings = settings
        self._files = store
        self._now = now
        self._runs = AppRunStore(pool)
        self._handoffs = Handoffs(orchestrator, pool)

    async def tick(self) -> None:
        try:
            await self._handoffs.tick()
        except Exception:
            log.exception("hand-off tick failed, sketch dispatch continues")
        async with self._pool.connection() as conn:
            unposted = [
                r
                for r in await runs.in_progress(conn)
                if r.message_id is None and r.kind in RESUMABLE
            ]
        for run in unposted:
            await self._guard(self._resume(run))
        async with self._pool.connection() as conn:
            ready = await inbound.next_queued(conn, self._now())
        presets: list[Preset] | None = None
        for conversation_id in ready:
            try:
                presets = await self._conversation(conversation_id, presets)
            except OrchestratorUnavailable as exc:
                log.warning("conversation %s: orchestrator unavailable: %s", conversation_id, exc)

    async def _guard(self, coro) -> None:
        try:
            await coro
        except OrchestratorUnavailable as exc:
            log.warning("orchestrator unavailable, retrying next tick: %s", exc)

    async def _conversation(
        self, conversation_id: uuid.UUID, presets: list[Preset] | None
    ) -> list[Preset] | None:
        async with self._pool.connection() as conn:
            binding = await bindings.active(conn, conversation_id)
        if binding is not None and binding.session_id is None:
            return presets
        if binding is not None:
            try:
                session = await agent_runs.running_session(
                    self._orchestrator, str(binding.session_id)
                )
            except OrchestratorError as exc:
                log.warning(
                    "session check of %s refused: %s %s", conversation_id, exc.status, exc.code
                )
                return presets
            if session is not None:
                await self._follow(conversation_id, session)
                return presets
            async with self._pool.connection() as conn:
                await bindings.release(conn, binding.id, SESSION_GONE)
            log.info("conversation %s: session %s is gone", conversation_id, binding.session_id)
        if presets is None:
            presets = await self._orchestrator.list_presets()
        preset = next((p for p in presets if p.name == self._settings.preset_name), None)
        if preset is None:
            await self._no_preset(conversation_id)
            return presets
        batch = await self._claim(conversation_id, preset.id, "first", None)
        if batch is not None:
            await self._dispatch(batch)
        return presets

    async def _no_preset(self, conversation_id: uuid.UUID) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            for row in await inbound.fail_queued_rows(conn, conversation_id, NO_PRESET):
                type_, payload = status_event(row, "failed", reason=NO_PRESET)
                await events.append(conn, conversation_id, type_, payload)
        log.warning(
            "preset %s not found: rows of %s failed", self._settings.preset_name, conversation_id
        )

    async def _follow(self, conversation_id: uuid.UUID, session: Session) -> None:
        batch = await self._claim(conversation_id, session.preset_id, "follow", session.id)
        if batch is not None:
            await self._deliver(batch)

    async def _claim(
        self,
        conversation_id: uuid.UUID,
        preset_id: uuid.UUID | None,
        kind: str,
        session_id: uuid.UUID | None,
    ) -> _Batch | None:
        """Run, claimed row, session or reservation and event in one transaction; None when a
        first run would pass the session cap, no row is left or the conversation got bound."""
        async with self._pool.connection() as conn, conn.transaction():
            if kind == "first" and (
                await runs.count_first_in_progress(conn) >= self._settings.max_new_sessions
            ):
                return None
            ref = await runs.create(
                conn, conversation_id=conversation_id, kind=kind, preset_id=preset_id
            )
            row = await inbound.claim_oldest(conn, conversation_id, int(ref.id))
            if row is None:
                raise psycopg.Rollback()
            await runs.attach_inbound(conn, int(ref.id), row.id)
            if session_id is not None:
                await runs.set_session(conn, int(ref.id), session_id)
                ref = dataclasses.replace(ref, session_id=str(session_id))
            elif not await bindings.reserve(conn, conversation_id, int(ref.id)):
                raise psycopg.Rollback()
            type_, payload = status_event(row, "translating")
            await events.append(conn, conversation_id, type_, payload)
            return _Batch(kind, ref, row)
        return None

    async def _resume(self, run: runs.RunRow) -> None:
        async with self._pool.connection() as conn:
            row = await inbound.by_run(conn, run.id)
        if row is None:
            await self._runs.finish(run.id, "failed", "inbound_missing")
            return
        batch = _Batch(run.kind, runs.to_ref(run), row)
        if run.kind == "follow":
            await self._deliver(batch)
        else:
            await self._dispatch(batch)

    async def _dispatch(self, batch: _Batch) -> None:
        text, files = await self._render(batch, first=True)
        try:
            await agent_runs.dispatch(
                self._orchestrator,
                self._runs,
                batch.ref,
                text,
                agent_runs.marker(RUN_KIND, batch.ref.id),
                attachments=files or None,
            )
        except UsageQuotaRefused as exc:
            await self._usage_quota(batch, exc)
        except NotFound as exc:  # dispatch finished the run
            log.warning("run %s: %s %s", batch.ref.id, exc.status, exc.code)
        except OrchestratorError as exc:
            log.warning("run %s refused: %s %s", batch.ref.id, exc.status, exc.code)
            await self._runs.finish(batch.ref.id, "failed", exc.code)

    async def _deliver(self, batch: _Batch) -> None:
        session_id = batch.ref.session_id
        if session_id is None:
            await self._session_gone(batch)
            return
        async with self._pool.connection() as conn:
            seen = await runs.has_other_run(
                conn, session_id, batch.row.conversation_id, int(batch.ref.id)
            )
        text, files = await self._render(batch, first=not seen)
        try:
            delivered = await agent_runs.deliver(
                self._orchestrator,
                self._runs,
                batch.ref,
                session_id,
                text,
                agent_runs.marker(RUN_KIND, batch.ref.id),
                attachments=files or None,
            )
        except OrchestratorError as exc:
            log.warning("run %s refused: %s %s", batch.ref.id, exc.status, exc.code)
            await self._runs.finish(batch.ref.id, "failed", exc.code)
            return
        if delivered is None:
            await self._session_gone(batch)

    async def _session_gone(self, batch: _Batch) -> None:
        """Fail the run, return the row to the queue and release the binding."""
        async with self._pool.connection() as conn, conn.transaction():
            if not await runs.finish(conn, int(batch.ref.id), "failed", SESSION_GONE):
                return
            await inbound.requeue(conn, int(batch.ref.id))
            binding = await bindings.active(conn, batch.row.conversation_id)
            if binding is not None:
                await bindings.release(conn, binding.id, SESSION_GONE)
            type_, payload = status_event(batch.row, "queued", reason=SESSION_GONE)
            await events.append(conn, batch.row.conversation_id, type_, payload)

    async def _usage_quota(self, batch: _Batch, exc: UsageQuotaRefused) -> None:
        retry_at = exc.retry_at(self._now())
        async with self._pool.connection() as conn, conn.transaction():
            run_id = int(batch.ref.id)
            if not await runs.finish(conn, run_id, "failed", USAGE_QUOTA):
                return
            await inbound.requeue(conn, run_id)
            await bindings.release_reservation(conn, run_id, USAGE_QUOTA)
            await holds.set(conn, batch.row.conversation_id, retry_at, USAGE_QUOTA)
            type_, payload = status_event(
                batch.row, "queued", reason=USAGE_QUOTA, retry_at=retry_at.isoformat()
            )
            await events.append(conn, batch.row.conversation_id, type_, payload)
        log.info("run %s: usage quota, conversation held until %s", batch.ref.id, retry_at)

    async def _render(self, batch: _Batch, *, first: bool) -> tuple[str, list]:
        row = batch.row
        async with self._pool.connection() as conn:
            conv = await conversations.get(conn, row.conversation_id)
            spec = await specs.latest(conn, row.conversation_id) if first else None
            previous = None if spec is None else (spec.version, spec.markdown)
            source = row
            if first and row.kind == "message":
                source = await inbound.latest_sketch(conn, row.conversation_id) or row
            rows = await attachments.for_inbound(conn, source.id) if source.kind == "sketch" else []
        rows.sort(key=lambda a: (a.page or 0, a.kind))  # per page: image first, strokes second
        files: list[TurnFile] = []
        missing = False
        for att in rows:
            data = None if att.purged_at is not None else self._read(att.path)
            if data is None:
                missing = True
                continue
            files.append(TurnFile(att.id, att.kind, att.path, att.mime, data, att.page))
        plugin = REGISTRY[conv.plugin]
        turn = TurnInput(
            kind=row.kind,  # type: ignore[arg-type]
            first=first,
            conversation_id=row.conversation_id,
            inbound_id=row.id,
            caption=row.text if row.kind == "sketch" else None,
            text=row.text if row.kind == "message" else None,
            files=files,
            previous_spec=previous,
        )
        text = plugin.render_prompt(turn)
        if missing and MISSING_FILE not in text:
            text = f"{MISSING_FILE}\n\n{text}"
        return text, plugin.turn_attachments(turn)

    def _read(self, path: str) -> bytes | None:
        try:
            return self._files.read(path)
        except OSError:
            return None
