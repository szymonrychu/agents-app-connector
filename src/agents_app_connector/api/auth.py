"""Device bearer auth: the token is hashed and looked up in the database on every request."""

from collections.abc import Awaitable, Callable

from fastapi import Request

from agents_app_connector.api.errors import ApiError
from agents_app_connector.db import devices
from agents_app_connector.db.devices import DeviceRow


def _unauthorized() -> ApiError:
    return ApiError(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})


def bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        return None
    return token


def device_auth(plugin_id: str) -> Callable[[Request], Awaitable[DeviceRow]]:
    """Dependency: the active device for this plugin, then the per-device rate limit.

    One 401 body for a missing, malformed, unknown, revoked or other-plugin token.
    """

    async def dependency(request: Request) -> DeviceRow:
        token = bearer_token(request)
        if token is None:
            raise _unauthorized()
        async with request.app.state.pool.connection() as conn:
            row = await devices.by_token(conn, token)
        if row is None or row.plugin != plugin_id:
            raise _unauthorized()
        limiter = request.app.state.limiter
        key = str(row.id)
        if not limiter.allow(key):
            raise ApiError(
                429,
                "rate_limited",
                "too many requests",
                headers={"Retry-After": str(limiter.retry_after(key))},
            )
        return row

    return dependency
