"""Admin API (device pairing), behind SSO at the ingress and a source CIDR allowlist."""

import ipaddress
import logging
import uuid

import psycopg
from fastapi import APIRouter, FastAPI, Response
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool
from starlette.types import ASGIApp, Receive, Scope, Send

from agents_app_connector.api.errors import ApiError, add_error_handlers, error_response
from agents_app_connector.api.schemas import DeviceOut, PairRequest, PairResponse
from agents_app_connector.db import devices
from agents_app_connector.settings import Settings

log = logging.getLogger(__name__)

PREFIX = "/app/admin/v1"
EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})
# Stub until the plugin registry lands (Task 8); then this becomes the registry keys.
PLUGIN_IDS = frozenset({"sketch"})


class SourceAllowlist:
    """403 `{"error":"forbidden"}` for a socket peer outside the allowlist; probes stay open.

    The SSO sidecar is the only intended caller. X-Forwarded-For is never read, and no bearer
    token (a device token included) authorises an admin route.
    """

    def __init__(self, app: ASGIApp, networks: list):
        self.app = app
        self.networks = list(networks)

    def allowed(self, scope: Scope) -> bool:
        client = scope.get("client")
        if not client:
            return False
        try:
            addr = ipaddress.ip_address(client[0])
        except ValueError:
            return False
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        return any(addr.version == n.version and addr in n for n in self.networks)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in EXEMPT_PATHS or self.allowed(scope):
            await self.app(scope, receive, send)
            return
        await JSONResponse({"error": "forbidden"}, status_code=403)(scope, receive, send)


def add_probes(app: FastAPI, pool: AsyncConnectionPool) -> None:
    """`/healthz` says the process is up; `/readyz` says the database answers."""

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        try:
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
        except psycopg.Error as exc:
            log.warning("readyz: database unreachable (%s)", type(exc).__name__)
            return error_response(503, "unavailable", "database unavailable")
        return JSONResponse({"status": "ok"})


def build_router(pool: AsyncConnectionPool) -> APIRouter:
    router = APIRouter()

    @router.post("/devices", status_code=201, response_model=PairResponse)
    async def pair(body: PairRequest) -> PairResponse:
        if body.plugin not in PLUGIN_IDS:
            raise ApiError(422, "invalid_request", f"unknown plugin: {body.plugin!r}")
        async with pool.connection() as conn:
            row, token = await devices.create(conn, body.plugin, body.name)
        return PairResponse(device_id=row.id, name=row.name, plugin=row.plugin, token=token)

    @router.get("/devices", response_model=list[DeviceOut])
    async def list_devices() -> list[DeviceOut]:
        async with pool.connection() as conn:
            rows = await devices.list_all(conn)
        return [
            DeviceOut(
                device_id=r.id,
                name=r.name,
                plugin=r.plugin,
                created_at=r.created_at,
                revoked_at=r.revoked_at,
            )
            for r in rows
        ]

    @router.delete("/devices/{device_id}", status_code=204)
    async def revoke(device_id: uuid.UUID) -> Response:
        async with pool.connection() as conn:
            if not await devices.revoke(conn, device_id):
                raise ApiError(404, "not_found", "no active device with this id")
        return Response(status_code=204)

    return router


def create_admin_app(pool: AsyncConnectionPool, settings: Settings) -> FastAPI:
    app = FastAPI(
        title="agents-app-connector admin API",
        openapi_url=f"{PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.include_router(build_router(pool), prefix=PREFIX)
    app.add_middleware(SourceAllowlist, networks=settings.admin_networks())
    add_error_handlers(app)
    add_probes(app, pool)
    return app
