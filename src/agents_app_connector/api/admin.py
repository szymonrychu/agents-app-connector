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
from agents_app_connector.api.schemas import CodeResponse, DeviceOut, PairRequest, PairResponse
from agents_app_connector.db import devices, pairing
from agents_app_connector.plugins import PLUGIN_IDS
from agents_app_connector.settings import Settings

log = logging.getLogger(__name__)

PREFIX = "/app/admin/v1"
EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})


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


def build_router(pool: AsyncConnectionPool, settings: Settings) -> APIRouter:
    router = APIRouter()
    ttl = settings.pairing_code_ttl_seconds

    async def issue(conn: psycopg.AsyncConnection, device_id: uuid.UUID) -> pairing.IssuedCode:
        try:
            return await pairing.issue(conn, device_id, ttl, settings.pairing_max_pending)
        except pairing.TooManyPending:
            raise ApiError(
                429, "too_many_pending", "too many devices are waiting for a pairing code"
            ) from None

    @router.post("/devices", status_code=201, response_model=PairResponse)
    async def pair(body: PairRequest) -> PairResponse:
        if body.plugin not in PLUGIN_IDS:
            raise ApiError(422, "invalid_request", f"unknown plugin: {body.plugin!r}")
        async with pool.connection() as conn, conn.transaction():
            row = await devices.create(conn, body.plugin, body.name)
            issued = await issue(conn, row.id)
        log.info("device %s created, pairing code issued", row.id)
        return PairResponse(
            device_id=row.id,
            name=row.name,
            plugin=row.plugin,
            code=issued.code,
            expires_at=issued.expires_at,
            expires_in=ttl,
        )

    @router.post("/devices/{device_id}/code", status_code=201, response_model=CodeResponse)
    async def new_code(device_id: uuid.UUID) -> CodeResponse:
        """A fresh code for a device that has no token yet; it replaces the older code."""
        async with pool.connection() as conn, conn.transaction():
            info = await devices.get_info(conn, device_id)
            if info is None or info.state == "revoked":
                raise ApiError(404, "not_found", "no active device with this id")
            if info.state == "active":
                raise ApiError(409, "already_paired", "the device is already paired")
            issued = await issue(conn, device_id)
        log.info("device %s: new pairing code issued", device_id)
        return CodeResponse(code=issued.code, expires_at=issued.expires_at, expires_in=ttl)

    @router.get("/devices", response_model=list[DeviceOut])
    async def list_devices() -> list[DeviceOut]:
        async with pool.connection() as conn:
            rows = await devices.list_all(conn)
        return [
            DeviceOut(
                device_id=r.id,
                name=r.name,
                plugin=r.plugin,
                state=r.state,
                created_at=r.created_at,
                revoked_at=r.revoked_at,
                paired_at=r.paired_at,
                paired_ip=r.paired_ip,
                code_status=r.code_status,
                code_expires_at=r.code_expires_at,
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
    app.include_router(build_router(pool, settings), prefix=PREFIX)
    app.add_middleware(SourceAllowlist, networks=settings.admin_networks())
    add_error_handlers(app)
    add_probes(app, pool)
    return app
