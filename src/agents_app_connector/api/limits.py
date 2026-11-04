"""In-process token bucket per key. Valid only with a single replica (the chart enforces it)."""

import ipaddress
import math
import time
from collections.abc import Callable, Sequence

from starlette.datastructures import Headers
from starlette.requests import Request

from agents_app_connector.api.errors import ApiError, error_response


class RateLimiter:
    def __init__(self, per_minute: int, now: Callable[[], float] = time.monotonic):
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self.now = now
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, stamp)

    def _tokens(self, key: str) -> tuple[float, float]:
        stamp = self.now()
        tokens, last = self._buckets.get(key, (self.capacity, stamp))
        return min(self.capacity, tokens + (stamp - last) * self.rate), stamp

    def allow(self, key: str) -> bool:
        tokens, stamp = self._tokens(key)
        if tokens >= 1:
            self._buckets[key] = (tokens - 1, stamp)
            return True
        self._buckets[key] = (tokens, stamp)
        return False

    def retry_after(self, key: str) -> int:
        """Whole seconds until one token is available (at least 1)."""
        tokens, _ = self._tokens(key)
        return max(1, math.ceil((1 - tokens) / self.rate))


def client_ip(
    request: Request, trusted: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> str:
    """The client address: `X-Real-IP` (else the rightmost `X-Forwarded-For`) only when the
    socket peer is inside a trusted proxy network, otherwise the socket peer itself."""
    peer = request.client.host if request.client else "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_ip in net for net in trusted):
        return peer
    forwarded = (
        request.headers.get("x-real-ip")
        or request.headers.get("x-forwarded-for", "").rsplit(",", 1)[-1]
    )
    forwarded = forwarded.strip()
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return peer


class BodyCap:
    """413 `{"error":"too_large"}` for a request body over `max_bytes`.

    Checks Content-Length up front and counts streamed bytes otherwise. (Starlette's own
    RequestBodyLimitMiddleware replaces every response with plain text, so it is not used.)
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._too_large(scope, receive, send)
            return
        total = 0

        async def counting() -> dict:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    raise ApiError(413, "too_large", "request body too large")
            return message

        await self.app(scope, counting, send)

    async def _too_large(self, scope, receive, send) -> None:
        response = error_response(413, "too_large", "request body too large")
        await response(scope, receive, send)
