"""Taskboard relay routes of the sketch plugin.

A paired device acts as the user on the taskboard: an allowlist of taskboard paths is forwarded
to the taskboard delegate listener with the connector's own bearer. Nothing is stored; the log
carries method, path and status only. Upstream status and JSON body come back unchanged.
"""

import logging
import re
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from agents_app_connector.api.auth import device_auth
from agents_app_connector.api.errors import ApiError
from agents_app_connector.db.devices import DeviceRow
from agents_app_connector.plugins import PluginDeps

log = logging.getLogger(__name__)

PLUGIN_ID = "sketch"
UPSTREAM_PREFIX = "/taskboard/api/v1"
BODY_CAP_BYTES = 256 * 1024
KEY = re.compile(r"T-\d+")
ID = re.compile(r"\d+")
WITH_BODY = frozenset({"POST", "PUT", "PATCH"})
ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

# (method, route path, path params checked against a pattern); the route path is also the
# upstream path.
ROUTES: list[tuple[str, str, dict[str, re.Pattern[str]]]] = [
    ("GET", "/tasks", {}),
    ("POST", "/tasks", {}),
    ("GET", "/tasks/{key}", {"key": KEY}),
    ("PATCH", "/tasks/{key}", {"key": KEY}),
    ("POST", "/tasks/{key}/comments", {"key": KEY}),
    ("PUT", "/tasks/{key}/todo", {"key": KEY}),
    ("PATCH", "/tasks/{key}/todo/{item_id}", {"key": KEY, "item_id": ID}),
    (
        "POST",
        "/tasks/{key}/decisions/{decision_id}/answer",
        {"key": KEY, "decision_id": ID},
    ),
]


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "no such route")


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > BODY_CAP_BYTES:
        raise ApiError(413, "payload_too_large", "request body too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > BODY_CAP_BYTES:
            raise ApiError(413, "payload_too_large", "request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def build_router(deps: PluginDeps) -> APIRouter:
    router = APIRouter()
    auth = device_auth(PLUGIN_ID)
    Device = Annotated[DeviceRow, Depends(auth)]  # noqa: N806

    def configured() -> tuple[httpx.AsyncClient, str]:
        token = deps.settings.taskboard_token
        if deps.taskboard is None or token is None:
            raise ApiError(503, "taskboard_unconfigured", "no taskboard is configured")
        return deps.taskboard, token.get_secret_value()

    async def forward(request: Request, method: str, upstream_path: str) -> JSONResponse:
        client, token = configured()
        headers = {"Authorization": f"Bearer {token}"}
        body = b""
        if method in WITH_BODY:
            body = await _read_body(request)
            if body:
                headers["Content-Type"] = "application/json"
        url = UPSTREAM_PREFIX + upstream_path
        if request.url.query:
            url += "?" + request.url.query
        try:
            resp = await client.request(method, url, headers=headers, content=body or None)
            payload = resp.json()
        except (httpx.HTTPError, ValueError):
            log.warning("task relay %s %s -> unavailable", method, request.url.path)
            raise ApiError(503, "taskboard_unavailable", "the taskboard is unavailable") from None
        log.info("task relay %s %s -> %s", method, request.url.path, resp.status_code)
        return JSONResponse(payload, status_code=resp.status_code)

    def add(method: str, path: str, checks: dict[str, re.Pattern[str]]) -> None:
        async def handler(request: Request, device: Device) -> JSONResponse:
            params = request.path_params
            if not all(p.fullmatch(params[name]) for name, p in checks.items()):
                raise _not_found()
            return await forward(request, method, path.format(**params))

        name = f"task_relay_{method.lower()}_{path.strip('/').replace('/', '_')}"
        handler.__name__ = name.replace("{", "").replace("}", "")
        router.add_api_route(path, handler, methods=[method], name=handler.__name__)

    for method, path, checks in ROUTES:
        add(method, path, checks)

    @router.api_route("/tasks", methods=ALL_METHODS, include_in_schema=False)
    @router.api_route("/tasks/{rest:path}", methods=ALL_METHODS, include_in_schema=False)
    async def outside_allowlist(device: Device, rest: str = "") -> JSONResponse:
        raise _not_found()

    return router
