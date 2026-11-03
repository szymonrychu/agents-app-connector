"""Hand-off routes of the sketch plugin: list targets, send a spec to a session or a new one."""

import uuid
from typing import Annotated

from agents_common.orchestrator import OrchestratorError, OrchestratorUnavailable
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agents_app_connector.api.auth import device_auth
from agents_app_connector.api.errors import ApiError
from agents_app_connector.db import conversations, handoffs, inbound, specs
from agents_app_connector.db.devices import DeviceRow
from agents_app_connector.plugins import PluginDeps

PLUGIN_ID = "sketch"
TRANSLATOR_PREFIX = "app-"


class PresetTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: str = Field(min_length=1)


class HandoffIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: uuid.UUID
    session: str | PresetTarget


def build_router(deps: PluginDeps) -> APIRouter:
    router = APIRouter()
    pool = deps.pool
    settings = deps.settings
    auth = device_auth(PLUGIN_ID)
    Device = Annotated[DeviceRow, Depends(auth)]  # noqa: N806

    def orchestrator():
        if deps.orchestrator is None:
            raise ApiError(503, "orchestrator_unavailable", "no orchestrator is configured")
        return deps.orchestrator

    async def listed(call):
        try:
            return await call()
        except OrchestratorUnavailable as exc:
            raise ApiError(
                503, "orchestrator_unavailable", "the orchestrator is unavailable"
            ) from exc
        except OrchestratorError as exc:
            raise ApiError(
                502, "orchestrator_error", f"orchestrator answered {exc.status}"
            ) from exc

    @router.get("/handoff/targets")
    async def targets(device: Device) -> dict:
        client = orchestrator()
        sessions = await listed(client.list_sessions)
        presets = await listed(client.list_presets)
        names = {p.id: p.name for p in presets}
        return {
            "sessions": [
                {
                    "id": str(s.id),
                    "slug": s.slug,
                    "preset_name": names.get(s.preset_id) if s.preset_id else None,
                }
                for s in sessions
                if s.desired == "active" and not s.slug.startswith(TRANSLATOR_PREFIX)
            ],
            "presets": [n for n in settings.handoff_presets if n in set(names.values())],
        }

    @router.post("/conversations/{conversation_id}/handoff", status_code=202)
    async def handoff(conversation_id: uuid.UUID, body: HandoffIn, device: Device) -> JSONResponse:
        target = body.session
        fp = inbound.fingerprint(
            {
                "conversation_id": str(conversation_id),
                "session": target if isinstance(target, str) else {"preset": target.preset},
            },
            [],
        )
        async with pool.connection() as conn, conn.transaction():
            if not await inbound.try_lock_client(conn, device.id, body.client_id):
                raise ApiError(409, "in_progress", "a request with this client_id is in progress")
            existing = await handoffs.by_client_id(conn, device.id, body.client_id)
            if existing is not None:
                if existing.fingerprint != fp:
                    raise ApiError(
                        422, "idempotency_mismatch", "client_id was used with another payload"
                    )
                return JSONResponse(
                    {"handoff_id": str(existing.id), "session_slug": existing.session_slug},
                    status_code=200,
                )
            if await conversations.get_owned(conn, conversation_id, device.id) is None:
                raise ApiError(404, "not_found", "conversation not found")
            version = await specs.latest_version(conn, conversation_id)
            if version is None:
                raise ApiError(409, "no_spec", "the conversation has no spec yet")
            handoff_id = uuid.uuid4()
            session_id = None
            preset = None
            if isinstance(target, str):
                session = next(
                    (
                        s
                        for s in await listed(orchestrator().list_sessions)
                        if s.slug == target and s.desired == "active"
                    ),
                    None,
                )
                if session is None:
                    raise ApiError(404, "unknown_session", "no active session with this slug")
                session_id, slug = session.id, session.slug
            else:
                if target.preset not in settings.handoff_presets:
                    raise ApiError(422, "preset_not_allowed", "preset is not allowed for hand-off")
                preset, slug = target.preset, handoffs.planned_slug(handoff_id)
            await handoffs.insert(
                conn,
                id=handoff_id,
                conversation_id=conversation_id,
                device_id=device.id,
                client_id=body.client_id,
                fingerprint=fp,
                spec_version=version,
                target_session_id=session_id,
                target_preset=preset,
                session_slug=slug,
            )
        return JSONResponse({"handoff_id": str(handoff_id), "session_slug": slug}, status_code=202)

    return router
