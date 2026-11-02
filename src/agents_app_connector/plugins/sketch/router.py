"""Device routes of the sketch plugin."""

import asyncio
import hashlib
import uuid
from datetime import timedelta
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from agents_app_connector import clock
from agents_app_connector.api.auth import device_auth
from agents_app_connector.api.errors import ApiError
from agents_app_connector.api.schemas import MessageIn, SketchMeta
from agents_app_connector.db import attachments, conversations, events, inbound, specs
from agents_app_connector.db.devices import DeviceRow
from agents_app_connector.images import validate_png, validate_strokes
from agents_app_connector.plugins import PluginDeps

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

    return router
