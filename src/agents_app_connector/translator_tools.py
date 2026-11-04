"""The translator's MCP server: `document_write` and `feedback_add`, served over streamable HTTP.

The sketch-translator session reaches it through the preset's `wrapper.mcp_servers` entry with
its own orchestrator agent token as bearer and its session id in `X-Orch-Session`. The connector
verifies the bearer against that one session with `OrchestratorClient.verify_agent_token`
(`POST /api/v1/sessions/{id}/agent-token/verify` on the orchestrator service port; cached by the
token's sha256 and the session id, never logged or stored); a request without the header is a
401. The verified session maps to the in-progress first or follow run of the conversation bound
to it. A session with no such run gets the tool error `no_active_turn`, so a translator can
write only into its own conversation, and only while one of its turns is running.

`document_write` stores the next spec version through `specs.add` (the app is the single writer
of `specs`); `feedback_add` stores one feedback item. Both append their event in the same
transaction. Content is checked for structure only (non-empty, size caps, kind enum, per-run
cap): the server cannot tell transcription from invention.
"""

import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from agents_common.orchestrator import (
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
)
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from psycopg_pool import AsyncConnectionPool
from pydantic import Field, ValidationError
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

from agents_app_connector.db import conversations, events, feedback, inbound, runs, specs
from agents_app_connector.settings import Settings

log = logging.getLogger(__name__)

SERVER_NAME = "appconn"
PATH = "/mcp"
SESSION_STATE = "translator_session_id"
SESSION_HEADER = "X-Orch-Session"
IDENTITY_TTL_SECONDS = 60.0
IDENTITY_CACHE_MAX = 1024
MAX_FEEDBACK_CHARS = 2000
MAX_WHERE_CHARS = 200

NO_ACTIVE_TURN = "no_active_turn"
EMPTY_DOCUMENT = "empty_document"
DOCUMENT_TOO_LARGE = "document_too_large"
EMPTY_TEXT = "empty_text"
TEXT_TOO_LONG = "text_too_long"
WHERE_TOO_LONG = "where_too_long"
FEEDBACK_LIMIT = "feedback_limit"

DOCUMENT_WRITE = (
    "Store the document: a faithful markdown transcription of what the user wrote and drew on "
    "the page, and nothing else. Recreate their words verbatim and their diagrams as markdown. "
    "Add nothing that is not on the page: no title, no front matter, no Requirements, no Open "
    "questions, no summary, no explanations, no commentary, no notes to the user. The only "
    "additions allowed are the reading markers defined in your instructions. Each call stores a "
    "new version of the document, so call it once per turn with the complete document. "
    "Anything you want to tell the user goes to feedback_add, never here."
)
FEEDBACK_ADD = (
    "Tell the user something about the sketch: a question, an uncertain or illegible reading, "
    "or a note. This is shown to the user apart from the document; never put it in the "
    "document. One item per call. kind is question, uncertain, illegible or note; where is "
    "optional and says where on the page it applies (for example 'page 2, box Login')."
)


class IdentityUnavailable(Exception):
    """The orchestrator could not answer whether the caller owns the session."""


class OrchestratorIdentity:
    """Check that an agent bearer token belongs to the session the caller names.

    One `OrchestratorClient.verify_agent_token` call (service-token client) against that single
    session. Accepted answers are cached for `ttl` seconds by the token's sha256 plus the session
    id; a refused token is not cached. The token is only ever sent in the request body, never
    logged or stored.
    """

    def __init__(
        self,
        orchestrator: OrchestratorClient,
        *,
        ttl: float = IDENTITY_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._orchestrator = orchestrator
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[tuple[str, uuid.UUID], float] = {}

    async def verify(self, token: str, session_id: uuid.UUID) -> bool:
        key = (hashlib.sha256(token.encode()).hexdigest(), session_id)
        now = self._clock()
        if self._cache.get(key, 0.0) > now:
            return True
        try:
            confirmed = await self._orchestrator.verify_agent_token(session_id, token)
        except (OrchestratorUnavailable, OrchestratorError, ValidationError) as exc:
            raise IdentityUnavailable(type(exc).__name__) from None
        if confirmed is None:
            return False
        if confirmed != session_id:
            raise IdentityUnavailable("verify answered for another session")
        if len(self._cache) >= IDENTITY_CACHE_MAX:
            self._cache = {k: v for k, v in self._cache.items() if v > now}
        if len(self._cache) < IDENTITY_CACHE_MAX:
            self._cache[key] = now + self._ttl
        return True


Verifier = Callable[[str, uuid.UUID], Awaitable[bool]]


class BearerSession:
    """ASGI guard: a bearer plus an `X-Orch-Session` header naming the session it belongs to, or
    401 (503 when the orchestrator cannot tell). A missing or malformed header is a 401: there
    is no fallback. The verified session id goes into the request state and the Authorization
    header is dropped before the MCP app sees the request."""

    def __init__(self, app: Any, *, verify: Verifier) -> None:
        self.app = app
        self.verify = verify

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, token = auth.partition(" ")
        token = token.strip()
        session_id = _session_header(headers.get(SESSION_HEADER.lower().encode(), b""))
        verified = False
        if scheme.lower() == "bearer" and token and session_id is not None:
            try:
                verified = await self.verify(token, session_id)
            except IdentityUnavailable as exc:
                log.warning("translator tools: identity unavailable: %s", exc)
                body = {"error": "unavailable", "detail": "cannot verify the caller"}
                await JSONResponse(body, status_code=503)(scope, receive, send)
                return
        if not verified:
            body = {
                "error": "unauthorized",
                "detail": f"a valid agent token and the {SESSION_HEADER} header are required",
            }
            response = JSONResponse(body, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        state = dict(scope.get("state") or {})
        state[SESSION_STATE] = session_id
        kept = [(k, v) for k, v in scope["headers"] if k != b"authorization"]
        await self.app({**scope, "headers": kept, "state": state}, receive, send)


def _session_header(raw: bytes) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw.decode("latin-1").strip())
    except ValueError:
        return None


class TranslatorTools:
    """The two tools' logic, bound to one session id per call."""

    def __init__(self, pool: AsyncConnectionPool, settings: Settings) -> None:
        self._pool = pool
        self._settings = settings

    async def write_document(self, session_id: uuid.UUID, markdown: str) -> dict:
        if not markdown.strip():
            raise ToolError(f"{EMPTY_DOCUMENT}: the document is empty")
        size = len(markdown.encode())
        if size > self._settings.max_document_bytes:
            raise ToolError(
                f"{DOCUMENT_TOO_LARGE}: at most {self._settings.max_document_bytes} bytes"
            )
        async with self._pool.connection() as conn, conn.transaction():
            run = await runs.active_translator_run(conn, session_id)
            row = None if run is None else await inbound.by_run(conn, run.id)
            if run is None or row is None:
                raise ToolError(f"{NO_ACTIVE_TURN}: no turn of yours is running")
            sketch_ids = await inbound.sketch_ids_upto(conn, row)
            version = await specs.add(
                conn, run.conversation_id, markdown, row.id, sketch_ids, run.id
            )
            await events.append(conn, run.conversation_id, "spec", {"version": version})
            await conversations.touch(conn, run.conversation_id)
        log.info("run %s: document v%s stored (%d bytes)", run.id, version, size)
        return {
            "stored": True,
            "version": version,
            "chars": len(markdown),
            "illegible": markdown.count("[illegible]"),
        }

    async def add_feedback(
        self, session_id: uuid.UUID, kind: str, text: str, where: str | None
    ) -> dict:
        text = text.strip()
        where = (where or "").strip() or None
        if not text:
            raise ToolError(f"{EMPTY_TEXT}: text is empty")
        if len(text) > MAX_FEEDBACK_CHARS:
            raise ToolError(f"{TEXT_TOO_LONG}: at most {MAX_FEEDBACK_CHARS} characters")
        if where is not None and len(where) > MAX_WHERE_CHARS:
            raise ToolError(f"{WHERE_TOO_LONG}: at most {MAX_WHERE_CHARS} characters")
        cap = self._settings.max_feedback_per_run
        async with self._pool.connection() as conn, conn.transaction():
            run = await runs.active_translator_run(conn, session_id)
            if run is None:
                raise ToolError(f"{NO_ACTIVE_TURN}: no turn of yours is running")
            if await feedback.count_for_run(conn, run.id) >= cap:
                raise ToolError(f"{FEEDBACK_LIMIT}: at most {cap} items per turn")
            item_id = await feedback.add(
                conn, run.conversation_id, run.id, run.inbound_id, kind, text, where=where
            )
            await events.append(
                conn, run.conversation_id, "feedback", {"feedback_id": item_id, "kind": kind}
            )
            await conversations.touch(conn, run.conversation_id)
        log.info("run %s: feedback %s (%s) stored", run.id, item_id, kind)
        return {"stored": True, "id": item_id}


def _session_id() -> uuid.UUID:
    return get_http_request().state.translator_session_id


def build_server(tools: TranslatorTools) -> FastMCP:
    server = FastMCP(SERVER_NAME, mask_error_details=True)

    @server.tool(name="document_write", description=DOCUMENT_WRITE)
    async def document_write(
        markdown: Annotated[
            str, Field(description="The complete document, markdown, transcription only.")
        ],
    ) -> dict:
        return await tools.write_document(_session_id(), markdown)

    @server.tool(name="feedback_add", description=FEEDBACK_ADD)
    async def feedback_add(
        kind: Annotated[
            Literal["question", "uncertain", "illegible", "note"],
            Field(description="question, uncertain, illegible or note"),
        ],
        text: Annotated[str, Field(description="What to tell the user, at most 2000 characters.")],
        where: Annotated[
            str | None, Field(description="Optional place on the page, at most 200 characters.")
        ] = None,
    ) -> dict:
        return await tools.add_feedback(_session_id(), kind, text, where)

    return server


def build_mcp_app(pool: AsyncConnectionPool, settings: Settings, verify: Verifier):
    """The ASGI app serving both tools as stateless streamable HTTP at `/mcp`."""
    server = build_server(TranslatorTools(pool, settings))
    return server.http_app(
        path=PATH,
        stateless_http=True,
        json_response=True,
        middleware=[Middleware(BearerSession, verify=verify)],
    )
