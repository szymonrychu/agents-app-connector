"""Shared API errors: `{"error": <code>, "detail": <text>}`, used by the admin and device apps."""

import logging

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        error: str,
        detail: str = "",
        headers: dict[str, str] | None = None,
    ):
        super().__init__(f"{status} {error}: {detail}")
        self.status = status
        self.error = error
        self.detail = detail
        self.headers = headers


def error_response(
    status: int, error: str, detail: str = "", headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse({"error": error, "detail": detail}, status_code=status, headers=headers)


def add_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.error, exc.detail, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(
            str(part) for part in first.get("loc", ()) if part not in ("body", "query")
        )
        return error_response(
            422, "invalid_request", f"{where}: {first.get('msg', 'invalid request')}"
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        error = "not_found" if exc.status_code == 404 else "invalid_request"
        return error_response(exc.status_code, error, str(exc.detail))

    @app.exception_handler(psycopg.Error)
    async def _db(request: Request, exc: psycopg.Error) -> JSONResponse:
        log.error("api: database error (%s)", type(exc).__name__)
        return error_response(503, "unavailable", "database unavailable")
