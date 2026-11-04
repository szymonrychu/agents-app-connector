"""Public pairing exchange of the sketch plugin: a 6-digit code for the device token.

No bearer auth: the code is the secret, so every attempt is limited per client IP and globally,
and failed attempts burn the budget of the pending codes (durable lockout in the database).
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from agents_app_connector.api.errors import ApiError
from agents_app_connector.api.limits import RateLimiter, client_ip
from agents_app_connector.api.schemas import ExchangeRequest, ExchangeResponse
from agents_app_connector.db import device_tokens, pairing
from agents_app_connector.plugins import PluginDeps

log = logging.getLogger(__name__)

PLUGIN_ID = "sketch"
MAX_BODY_BYTES = 256


def _limited(limiter: RateLimiter, key: str) -> ApiError | None:
    if limiter.allow(key):
        return None
    return ApiError(
        429,
        "rate_limited",
        "too many pairing attempts",
        headers={"Retry-After": str(limiter.retry_after(key))},
    )


def build_router(deps: PluginDeps) -> APIRouter:
    router = APIRouter()
    settings = deps.settings
    trusted = settings.trusted_proxy_networks()
    per_ip = RateLimiter(settings.pairing_ip_per_minute)
    overall = RateLimiter(settings.pairing_global_per_minute)

    @router.post("/pair", response_model=ExchangeResponse)
    async def exchange(request: Request) -> JSONResponse:
        ip = client_ip(request, trusted)
        for limiter, key in ((per_ip, ip), (overall, "all")):
            if (error := _limited(limiter, key)) is not None:
                log.info("pairing from %s: rate limited", ip)
                raise error
        raw = await request.body()
        try:
            if len(raw) > MAX_BODY_BYTES:
                raise ValueError("body too large")
            body = ExchangeRequest.model_validate_json(raw)
        except (ValidationError, ValueError):
            async with deps.pool.connection() as conn:
                await pairing.record_failure(conn, settings.pairing_max_failures)
            log.info("pairing from %s: malformed request", ip)
            raise ApiError(422, "invalid_request", "code: six digits are required") from None
        async with deps.pool.connection() as conn, conn.transaction():
            device_id = await pairing.redeem(conn, body.code, PLUGIN_ID, ip)
            if device_id is None:
                locked = await pairing.record_failure(conn, settings.pairing_max_failures)
                token = None
            else:
                token = await device_tokens.issue(conn, device_id)
                cur = await conn.execute(
                    "SELECT name, plugin FROM devices WHERE id = %s", (device_id,)
                )
                name, plugin = await cur.fetchone()
        if token is None:
            log.info("pairing from %s: invalid code (%d locked)", ip, locked)
            raise ApiError(400, "invalid_code", "the code is not valid or has expired")
        log.info("device %s paired from %s", device_id, ip)
        return JSONResponse(
            {"device_id": str(device_id), "name": name, "plugin": plugin, "token": token},
            headers={"Cache-Control": "no-store"},
        )

    return router
