"""Device routes of the sketch plugin."""

import asyncio
import hashlib
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import ValidationError
from starlette.datastructures import FormData, UploadFile

from agents_app_connector import clock
from agents_app_connector.api.auth import active_device, device_auth
from agents_app_connector.api.errors import ApiError
from agents_app_connector.api.schemas import MessageIn, RevertIn, SketchMeta
from agents_app_connector.db import (
    attachments,
    conversations,
    events,
    feedback,
    inbound,
    spec_images,
    specs,
)
from agents_app_connector.db.devices import DeviceRow
from agents_app_connector.images import validate_png, validate_strokes
from agents_app_connector.plugins import PluginDeps
from agents_app_connector.plugins.sketch import handoff_routes, layout, pair_routes, task_routes
from agents_app_connector.plugins.sketch.layout import layout_field, region_field
from agents_app_connector.settings import Settings

PLUGIN_ID = "sketch"
API_VERSION = 1
JSON = "application/json"
REGION_FIELD = re.compile(r"^region\.([0-9]+)\.([0-9]+)\.png$")


def png_field(index: int) -> str:
    return f"sketch.{index}.png"


def strokes_field(index: int) -> str:
    return f"strokes.{index}.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _gate(
    conn: psycopg.AsyncConnection, device_id: uuid.UUID, client_id: uuid.UUID, fp: str
) -> inbound.InboundRow | None:
    """Idempotency: 409 while another request holds the key, the stored row on a replay."""
    if not await inbound.try_lock_client(conn, device_id, client_id):
        raise ApiError(409, "in_progress", "a request with this client_id is in progress")
    existing = await inbound.by_client_id(conn, device_id, client_id)
    if existing is None:
        return None
    if existing.fingerprint != fp:
        raise ApiError(422, "idempotency_mismatch", "client_id was used with another payload")
    return existing


def _parse_meta(raw: object, max_bytes: int) -> SketchMeta:
    if not isinstance(raw, str):
        raise ApiError(422, "invalid_request", "meta: form field is required")
    if len(raw.encode()) > max_bytes:
        raise ApiError(413, "too_large", f"meta exceeds {max_bytes} bytes")
    try:
        return SketchMeta.model_validate_json(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise ApiError(
            422, "invalid_request", f"meta.{where}: {first['msg']}".rstrip(".")
        ) from None


async def _read_part(part: object, name: str, cap: int) -> bytes:
    if not isinstance(part, UploadFile):
        raise ApiError(422, "invalid_request", f"{name}: file part is required")
    if part.size is not None and part.size > cap:
        raise ApiError(413, "too_large", f"{name} exceeds {cap} bytes")
    data = await part.read()
    if len(data) > cap:
        raise ApiError(413, "too_large", f"{name} exceeds {cap} bytes")
    return data


@dataclass
class Parts:
    """The file parts of one upload, by page index (and region index k)."""

    pngs: list[bytes]
    strokes: list[bytes | None]
    layouts: dict[int, bytes] = field(default_factory=dict)
    regions: dict[tuple[int, int], bytes] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(d) for d in self.in_field_order())

    def in_field_order(self) -> list[bytes]:
        """Per page png, strokes, layout and its regions by k; absent parts left out."""
        out: list[bytes] = []
        for i, png in enumerate(self.pngs):
            out += [d for d in (png, self.strokes[i], self.layouts.get(i)) if d is not None]
            out += [d for (p, _), d in sorted(self.regions.items()) if p == i]
        return out


async def _read_parts(form: FormData, meta: SketchMeta, settings: Settings) -> Parts:
    count = len(meta.pages)
    if count > settings.max_pages:
        raise ApiError(422, "invalid_request", f"meta.pages: at most {settings.max_pages} pages")
    regions: dict[tuple[int, int], str] = {}
    for key in form:
        match = REGION_FIELD.match(key)
        if match and int(match[1]) < count:
            regions[(int(match[1]), int(match[2]))] = key
    for i in {p for p, _ in regions}:
        if sum(1 for p, _ in regions if p == i) > settings.max_regions_per_page:
            raise ApiError(
                422,
                "invalid_request",
                f"page {i}: at most {settings.max_regions_per_page} region parts",
            )
    allowed = {"meta", *regions.values()}
    for i in range(count):
        allowed |= {png_field(i), strokes_field(i), layout_field(i)}
    if any(k not in allowed for k in form):
        raise ApiError(422, "invalid_request", "unknown form field")
    parts = Parts(pngs=[], strokes=[])
    for i in range(count):
        parts.pngs.append(
            await _read_part(form.get(png_field(i)), png_field(i), settings.max_png_bytes)
        )
        part = form.get(strokes_field(i))
        parts.strokes.append(
            None
            if part is None
            else await _read_part(part, strokes_field(i), settings.max_strokes_bytes)
        )
        part = form.get(layout_field(i))
        if part is not None:
            parts.layouts[i] = await _read_part(part, layout_field(i), settings.max_layout_bytes)
    for (i, k), name in regions.items():
        parts.regions[(i, k)] = await _read_part(form.get(name), name, settings.max_png_bytes)
    return parts


async def _validate_parts(
    parts: Parts, meta: SketchMeta, settings: Settings
) -> tuple[dict, dict[int, layout.PageLayout]]:
    """PNG sizes by page index and by region field name, and the checked page layouts. The
    CPU-bound validation runs in threads, off the event loop."""

    def png(data: bytes, min_edge: int) -> tuple[int, int]:
        return validate_png(
            data,
            max_long_edge=settings.max_long_edge_px,
            min_edge=min_edge,
            max_bytes=settings.max_png_bytes,
        )

    page_sizes = await asyncio.gather(
        *(asyncio.to_thread(png, d, settings.min_edge_px) for d in parts.pngs)
    )
    # A region is a crop of the page, so it may be smaller than a page's minimum edge.
    region_keys = sorted(parts.regions)
    region_sizes = await asyncio.gather(
        *(asyncio.to_thread(png, parts.regions[key], 1) for key in region_keys)
    )
    docs = await asyncio.gather(
        *(
            asyncio.to_thread(validate_strokes, d, max_bytes=settings.max_strokes_bytes)
            if d is not None
            else _none()
            for d in parts.strokes
        )
    )
    layouts = {
        i: layout.parse_layout(raw, index=i, max_bytes=settings.max_layout_bytes)
        for i, raw in sorted(parts.layouts.items())
    }
    for i in sorted({p for p, _ in parts.regions} - set(layouts)):
        raise ApiError(422, "invalid_request", f"{region_field(i, 0)}: no layout of page {i}")
    for i, lay in layouts.items():
        doc = docs[i]
        layout.check_layout(
            lay,
            index=i,
            strokes_count=None if doc is None else len(doc["strokes"]),
            canvas=meta.pages[i].canvas,
            region_parts={region_field(p, k) for p, k in parts.regions if p == i},
        )
    layout.check_unique_ops(layouts.values())
    if meta.base_version is None and any(lay.ops for lay in layouts.values()):
        raise ApiError(422, "invalid_request", "meta.base_version: required when a layout has ops")
    sizes: dict = dict(enumerate(page_sizes))
    sizes.update(
        {region_field(i, k): size for (i, k), size in zip(region_keys, region_sizes, strict=True)}
    )
    return sizes, layouts


async def _none() -> None:
    return None


async def _check_base(
    conn: psycopg.AsyncConnection,
    conversation_id: uuid.UUID,
    base_version: int,
    layouts: dict[int, layout.PageLayout],
) -> None:
    """409 `stale_base` unless base_version is the latest version, 409 `op_replayed` for an op
    id an earlier version already reported, then the line ids and quotes against the base. The
    conversation row is locked first so a racing revert cannot move the latest version."""
    await conversations.lock(conn, conversation_id)
    if await specs.latest_version(conn, conversation_id) != base_version:
        raise ApiError(409, "stale_base", f"spec version {base_version} is not the latest")
    reported = await specs.reported_op_ids(conn, conversation_id)
    for lay in layouts.values():
        for op in lay.ops:
            if op.id in reported:
                raise ApiError(409, "op_replayed", f"op {op.id} was already reported")
    base = await specs.get(conn, conversation_id, base_version)
    assert base is not None  # it is the latest version
    stored = None if base.layout is None else layout.SpecLayout.model_validate(base.layout)
    layout.check_base(layouts, base.markdown, stored)


@dataclass(frozen=True)
class _Stored:
    kind: str
    mime: str
    data: bytes
    id: uuid.UUID
    page: int
    width: int | None = None
    height: int | None = None
    region_id: uuid.UUID | None = None


def _stored_parts(
    parts: Parts,
    meta: SketchMeta,
    sizes: dict,
    layouts: dict[int, layout.PageLayout],
    sketch_id: uuid.UUID,
) -> list[_Stored]:
    """The attachment rows of an upload; the first page's png carries the sketch id."""
    region_ids = {
        op.region.part: op.region.id
        for lay in layouts.values()
        for op in lay.ops
        if op.region is not None
    }
    out: list[_Stored] = []
    for i, page_meta in enumerate(meta.pages):
        page = page_meta.page
        w, h = sizes[i]
        out.append(
            _Stored(
                "sketch_png",
                "image/png",
                parts.pngs[i],
                sketch_id if i == 0 else uuid.uuid4(),
                page,
                w,
                h,
            )
        )
        if parts.strokes[i] is not None:
            out.append(_Stored("sketch_strokes", JSON, parts.strokes[i], uuid.uuid4(), page))
        if i in parts.layouts:
            out.append(_Stored("sketch_layout", JSON, parts.layouts[i], uuid.uuid4(), page))
        for (p, k), data in sorted(parts.regions.items()):
            if p != i:
                continue
            name = region_field(p, k)
            w, h = sizes[name]
            out.append(
                _Stored("spec_image", "image/png", data, uuid.uuid4(), page, w, h, region_ids[name])
            )
    return out


def _event_json(row: events.EventRow) -> dict:
    return {
        "cursor": row.id,
        "type": row.type,
        **row.payload,
        "created_at": row.created_at.isoformat(),
    }


def build_router(deps: PluginDeps) -> APIRouter:
    router = APIRouter()
    settings = deps.settings
    pool = deps.pool
    store = deps.store
    auth = device_auth(PLUGIN_ID)
    Device = Annotated[DeviceRow, Depends(auth)]  # noqa: N806

    @router.get("/config", dependencies=[Depends(auth)])
    async def config() -> dict[str, int]:
        return {
            "max_long_edge_px": settings.max_long_edge_px,
            "max_png_bytes": settings.max_png_bytes,
            "max_strokes_bytes": settings.max_strokes_bytes,
            "max_pages": settings.max_pages,
            "max_upload_bytes": settings.max_upload_bytes,
            "min_edge_px": settings.min_edge_px,
            "api_version": API_VERSION,
        }

    @router.post("/sketches")
    async def upload(request: Request, device: Device) -> JSONResponse:
        max_files = settings.max_pages * (3 + settings.max_regions_per_page)
        async with request.form(
            max_files=max_files, max_fields=1, max_part_size=settings.max_meta_bytes
        ) as form:
            meta = _parse_meta(form.get("meta"), settings.max_meta_bytes)
            parts = await _read_parts(form, meta, settings)
        if parts.total > settings.max_upload_bytes:
            raise ApiError(413, "too_large", f"upload exceeds {settings.max_upload_bytes} bytes")
        sizes, layouts = await _validate_parts(parts, meta, settings)

        meta_dict = meta.model_dump(mode="json")
        if meta.base_version is None:
            meta_dict.pop("base_version")  # plain uploads keep their fingerprint
        fp = inbound.fingerprint(meta_dict, [_sha(d) for d in parts.in_field_order()])
        written: list[str] = []
        replaced: list[str] = []
        try:
            async with pool.connection() as conn, conn.transaction():
                existing = await _gate(conn, device.id, meta.client_id, fp)
                if existing is not None:
                    return JSONResponse(
                        {
                            "sketch_id": str(existing.id),
                            "conversation_id": str(existing.conversation_id),
                            "status": "queued",
                        },
                        status_code=200,
                    )
                used = await attachments.device_usage_bytes(conn, device.id)
                if used + parts.total > settings.device_quota_bytes:
                    raise ApiError(413, "quota_exceeded", "device storage quota exceeded")
                if meta.conversation_id is not None:
                    owned_row = await conversations.get_owned(conn, meta.conversation_id, device.id)
                    if owned_row is None:
                        raise ApiError(404, "not_found", "conversation not found")
                    conversation_id = meta.conversation_id
                    if meta.annotates is not None and (
                        await specs.get(conn, conversation_id, meta.annotates.spec_version) is None
                    ):
                        raise ApiError(404, "no_spec", "no such spec version")
                    if meta.base_version is not None:
                        await _check_base(conn, conversation_id, meta.base_version, layouts)
                elif meta.conversation == "new":
                    conversation_id = await conversations.create(conn, device.id, PLUGIN_ID)
                else:
                    conversation_id = await conversations.current_or_new(conn, device.id, PLUGIN_ID)
                sketch_id = uuid.uuid4()
                await inbound.insert(
                    conn,
                    id=sketch_id,
                    conversation_id=conversation_id,
                    device_id=device.id,
                    client_id=meta.client_id,
                    fingerprint=fp,
                    kind="sketch",
                    text=meta.caption,
                    meta=meta_dict,
                )
                expires_at = clock.utcnow() + timedelta(days=settings.retention_days)
                for part in _stored_parts(parts, meta, sizes, layouts, sketch_id):
                    stored = await asyncio.to_thread(store.put, device.id, part.id, part.data)
                    written.append(stored.path)
                    await attachments.insert(
                        conn,
                        attachments.NewAttachment(
                            id=part.id,
                            inbound_id=sketch_id,
                            conversation_id=conversation_id,
                            device_id=device.id,
                            kind=part.kind,
                            mime=part.mime,
                            size=stored.size,
                            sha256=stored.sha256,
                            path=stored.path,
                            expires_at=None if part.region_id is not None else expires_at,
                            width=part.width,
                            height=part.height,
                            page=part.page,
                        ),
                    )
                    if part.region_id is not None:
                        old_path = await spec_images.put(
                            conn, conversation_id, part.region_id, part.id
                        )
                        if old_path is not None:
                            replaced.append(old_path)
                await conversations.touch(conn, conversation_id)
                await events.append(
                    conn,
                    conversation_id,
                    "sketch_status",
                    {"sketch_id": str(sketch_id), "status": "queued"},
                )
        except BaseException:
            for path in written:
                store.delete(path)
            raise
        for path in replaced:
            store.delete(path)
        return JSONResponse(
            {
                "sketch_id": str(sketch_id),
                "conversation_id": str(conversation_id),
                "status": "queued",
            },
            status_code=202,
        )

    @router.get("/conversations")
    async def list_conversations(device: Device) -> list[dict]:
        async with pool.connection() as conn:
            rows = await conversations.list_for(conn, device.id)
        return [
            {
                "id": str(r.id),
                "title": r.title,
                "created_at": r.created_at.isoformat(),
                "latest_spec_version": r.latest_spec_version,
                "latest_feedback_id": r.latest_feedback_id,
            }
            for r in rows
        ]

    @router.post("/conversations", status_code=201)
    async def create_conversation(device: Device) -> dict[str, str]:
        async with pool.connection() as conn:
            conversation_id = await conversations.create(conn, device.id, PLUGIN_ID)
        return {"id": str(conversation_id)}

    @router.delete("/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(conversation_id: uuid.UUID, device: Device) -> Response:
        async with pool.connection() as conn, conn.transaction():
            if await conversations.get_owned(conn, conversation_id, device.id) is None:
                raise ApiError(404, "not_found", "conversation not found")
            await conversations.mark_deleted(conn, conversation_id)
            await inbound.fail_queued_for(conn, conversation_id, "conversation_deleted")
            await specs.delete_for(conn, conversation_id)
            await feedback.delete_for(conn, conversation_id)
            paths = await attachments.delete_for_conversation(conn, conversation_id)
        for path in paths:
            await asyncio.to_thread(store.delete, path)
        return Response(status_code=204)

    @router.post("/conversations/{conversation_id}/messages", status_code=202)
    async def post_message(
        conversation_id: uuid.UUID, body: MessageIn, device: Device
    ) -> JSONResponse:
        if len(body.text) > settings.max_text_chars:
            raise ApiError(
                422, "invalid_request", f"text: longer than {settings.max_text_chars} characters"
            )
        fp = inbound.fingerprint({"conversation_id": str(conversation_id), "text": body.text}, [])
        async with pool.connection() as conn, conn.transaction():
            existing = await _gate(conn, device.id, body.client_id, fp)
            if existing is not None:
                return JSONResponse({"message_id": str(existing.id)}, status_code=200)
            if await conversations.get_owned(conn, conversation_id, device.id) is None:
                raise ApiError(404, "not_found", "conversation not found")
            message_id = uuid.uuid4()
            await inbound.insert(
                conn,
                id=message_id,
                conversation_id=conversation_id,
                device_id=device.id,
                client_id=body.client_id,
                fingerprint=fp,
                kind="message",
                text=body.text,
                meta={},
            )
            await conversations.touch(conn, conversation_id)
            await events.append(
                conn,
                conversation_id,
                "message",
                {"message_id": str(message_id), "status": "queued"},
            )
        return JSONResponse({"message_id": str(message_id)}, status_code=202)

    async def owned(conversation_id: uuid.UUID, device: Device) -> uuid.UUID:
        async with pool.connection() as conn:
            if await conversations.get_owned(conn, conversation_id, device.id) is None:
                raise ApiError(404, "not_found", "conversation not found")
        return conversation_id

    Conversation = Annotated[uuid.UUID, Depends(owned)]  # noqa: N806

    async def events_after(conversation_id: uuid.UUID, cursor: int) -> list[events.EventRow]:
        async with pool.connection() as conn:
            return await events.after(conn, conversation_id, cursor)

    @router.get("/conversations/{conversation_id}/events")
    async def poll_events(
        request: Request,
        conversation_id: Conversation,
        after: Annotated[int, Query(ge=0)] = 0,
        wait: Annotated[int, Query(ge=0, le=settings.events_wait_max_seconds)] = 0,
    ) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            rows = await events_after(conversation_id, after)
            remaining = deadline - loop.time()
            if rows or remaining <= 0:
                break
            await deps.hub.wait_for(conversation_id, min(remaining, settings.poll_seconds))
            await active_device(request, PLUGIN_ID)  # revoked while waiting: 401
        await active_device(request, PLUGIN_ID)
        return {"events": [_event_json(r) for r in rows], "cursor": rows[-1].id if rows else after}

    @router.get("/conversations/{conversation_id}/stream", response_class=EventSourceResponse)
    async def stream_events(
        request: Request,
        conversation_id: Conversation,
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> AsyncIterator[ServerSentEvent]:
        cursor = after
        last_id = request.headers.get("last-event-id", "")
        if last_id.isdigit():
            cursor = int(last_id)
        while True:
            try:
                await active_device(request, PLUGIN_ID)
            except ApiError:
                return  # revoked: the client sees 401 on reconnect
            rows = await events_after(conversation_id, cursor)
            for row in rows:
                yield ServerSentEvent(data=_event_json(row), event=row.type, id=str(row.id))
            if rows:
                cursor = rows[-1].id
                continue
            await deps.hub.wait_for(conversation_id, settings.poll_seconds)

    def spec_json(row: specs.SpecRow) -> dict:
        return {
            "version": row.version,
            "markdown": row.markdown,
            "created": row.created_at.isoformat(),
            "sketch_ids": [str(i) for i in row.sketch_ids],
            "layout": row.layout,
        }

    async def spec_row(conversation_id: uuid.UUID, version: int | None) -> specs.SpecRow:
        async with pool.connection() as conn:
            row = (
                await specs.latest(conn, conversation_id)
                if version is None
                else await specs.get(conn, conversation_id, version)
            )
        if row is None:
            raise ApiError(404, "no_spec", "no such spec")
        return row

    @router.get("/conversations/{conversation_id}/spec")
    async def latest_spec(conversation_id: Conversation) -> dict:
        return spec_json(await spec_row(conversation_id, None))

    @router.get("/conversations/{conversation_id}/spec.md")
    async def latest_spec_markdown(conversation_id: Conversation) -> PlainTextResponse:
        row = await spec_row(conversation_id, None)
        return PlainTextResponse(row.markdown, media_type="text/markdown; charset=utf-8")

    @router.get("/conversations/{conversation_id}/spec/{version}")
    async def spec_version(conversation_id: Conversation, version: int) -> dict:
        return spec_json(await spec_row(conversation_id, version))

    @router.post("/conversations/{conversation_id}/spec/{version}/revert", status_code=201)
    async def revert_spec(conversation_id: Conversation, version: int, body: RevertIn) -> dict:
        """Copy version N (markdown and layout, line ids unchanged) as a new version. A replay
        after success is 409 stale_base: the base check is the idempotency."""
        async with pool.connection() as conn, conn.transaction():
            await conversations.lock(conn, conversation_id)
            target = await specs.get(conn, conversation_id, version)
            if target is None:
                raise ApiError(404, "no_spec", "no such spec version")
            latest = await specs.latest_version(conn, conversation_id)
            if body.base_version != latest:
                raise ApiError(409, "stale_base", f"spec version {latest} is the latest")
            if version == latest:
                raise ApiError(422, "invalid_request", "version is already the latest")
            if await inbound.has_open(conn, conversation_id):
                raise ApiError(409, "busy", "a translator turn of this conversation is open")
            new_layout = (
                None
                if target.layout is None
                else {**target.layout, "ops": [], "base_version": latest, "reverted_from": version}
            )
            new = await specs.add(
                conn,
                conversation_id,
                target.markdown,
                None,
                target.sketch_ids,
                None,
                layout=new_layout,
            )
            await events.append(conn, conversation_id, "spec", {"version": new})
            await conversations.touch(conn, conversation_id)
        return {"version": new, "reverted_from": version}

    @router.get("/conversations/{conversation_id}/feedback")
    async def list_feedback(
        conversation_id: Conversation, after: Annotated[int, Query(ge=0)] = 0
    ) -> dict:
        async with pool.connection() as conn:
            rows = await feedback.after(conn, conversation_id, after)
        return {
            "items": [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "text": r.text,
                    "where": r.where_text,
                    "spec_version": r.spec_version,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ],
            "cursor": rows[-1].id if rows else after,
        }

    @router.get("/conversations/{conversation_id}/images/{region_id}.png")
    async def spec_image(
        conversation_id: Conversation, region_id: uuid.UUID, request: Request
    ) -> Response:
        async with pool.connection() as conn:
            row = await spec_images.get(conn, conversation_id, region_id)
        if row is None:
            raise ApiError(404, "not_found", "image not found")
        etag = f'"{row.sha256}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        try:
            data = await asyncio.to_thread(store.read, row.path)
        except OSError:
            raise ApiError(404, "not_found", "image file is gone") from None
        return Response(data, media_type="image/png", headers={"ETag": etag})

    @router.get("/attachments/{attachment_id}")
    async def get_attachment(
        attachment_id: uuid.UUID, request: Request, device: Device
    ) -> Response:
        async with pool.connection() as conn:
            row = await attachments.get_owned(conn, attachment_id, device.id)
        if row is None:
            raise ApiError(404, "not_found", "attachment not found")
        if row.purged_at is not None:
            raise ApiError(410, "purged", "attachment was purged")
        etag = f'"{row.sha256}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        try:
            data = await asyncio.to_thread(store.read, row.path)
        except OSError:
            raise ApiError(410, "purged", "attachment file is gone") from None
        return Response(data, media_type=row.mime, headers={"ETag": etag})

    router.include_router(pair_routes.build_router(deps))
    router.include_router(handoff_routes.build_router(deps))
    router.include_router(task_routes.build_router(deps))
    return router
