"""Device routes of the sketch plugin."""

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from agents_app_connector import clock
from agents_app_connector.api.auth import active_device, device_auth
from agents_app_connector.api.errors import ApiError
from agents_app_connector.api.schemas import MessageIn, SketchMeta
from agents_app_connector.db import attachments, conversations, events, inbound, specs
from agents_app_connector.db.devices import DeviceRow
from agents_app_connector.images import validate_png, validate_strokes
from agents_app_connector.plugins import PluginDeps
from agents_app_connector.plugins.sketch import handoff_routes

PLUGIN_ID = "sketch"
API_VERSION = 1
PNG_FIELD = "sketch.png"
STROKES_FIELD = "strokes.json"


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
            "api_version": API_VERSION,
        }

    @router.post("/sketches")
    async def upload(request: Request, device: Device) -> JSONResponse:
        async with request.form(
            max_files=2, max_fields=1, max_part_size=settings.max_meta_bytes
        ) as form:
            meta = _parse_meta(form.get("meta"), settings.max_meta_bytes)
            if any(k not in ("meta", PNG_FIELD, STROKES_FIELD) for k in form):
                raise ApiError(422, "invalid_request", "unknown form field")
            png = await _read_part(form.get(PNG_FIELD), PNG_FIELD, settings.max_png_bytes)
            strokes = None
            if form.get(STROKES_FIELD) is not None:
                strokes = await _read_part(
                    form.get(STROKES_FIELD), STROKES_FIELD, settings.max_strokes_bytes
                )
        # The CPU-bound validation runs in threads, off the event loop.
        width, height = await asyncio.to_thread(
            validate_png,
            png,
            max_long_edge=settings.max_long_edge_px,
            min_edge=settings.min_edge_px,
            max_bytes=settings.max_png_bytes,
        )
        if strokes is not None:
            await asyncio.to_thread(validate_strokes, strokes, max_bytes=settings.max_strokes_bytes)

        meta_dict = meta.model_dump(mode="json")
        digests = [_sha(png)] + ([_sha(strokes)] if strokes is not None else [])
        fp = inbound.fingerprint(meta_dict, digests)
        written: list[str] = []
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
                new_bytes = len(png) + (len(strokes) if strokes is not None else 0)
                used = await attachments.device_usage_bytes(conn, device.id)
                if used + new_bytes > settings.device_quota_bytes:
                    raise ApiError(413, "quota_exceeded", "device storage quota exceeded")
                if meta.conversation == "new":
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
                parts = [("sketch_png", "image/png", png, sketch_id, width, height)]
                if strokes is not None:
                    parts.append(
                        ("sketch_strokes", "application/json", strokes, uuid.uuid4(), None, None)
                    )
                for kind, mime, data, att_id, w, h in parts:
                    stored = await asyncio.to_thread(store.put, device.id, att_id, data)
                    written.append(stored.path)
                    await attachments.insert(
                        conn,
                        attachments.NewAttachment(
                            id=att_id,
                            inbound_id=sketch_id,
                            conversation_id=conversation_id,
                            device_id=device.id,
                            kind=kind,
                            mime=mime,
                            size=stored.size,
                            sha256=stored.sha256,
                            path=stored.path,
                            expires_at=expires_at,
                            width=w,
                            height=h,
                        ),
                    )
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

    router.include_router(handoff_routes.build_router(deps))
    return router
