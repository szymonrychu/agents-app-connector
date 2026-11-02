"""Client plugin interface. The registry arrives with the sketch plugin."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter
from psycopg_pool import AsyncConnectionPool

from agents_app_connector.api.limits import RateLimiter
from agents_app_connector.settings import Settings


@dataclass(frozen=True)
class PluginDeps:
    pool: AsyncConnectionPool
    settings: Settings
    store: Any
    limiter: RateLimiter


@dataclass(frozen=True)
class TurnFile:
    attachment_id: UUID
    kind: str
    name: str
    mime: str
    data: bytes


@dataclass(frozen=True)
class TurnInput:
    kind: Literal["sketch", "message"]
    first: bool
    conversation_id: UUID
    inbound_id: UUID
    caption: str | None
    text: str | None
    files: Sequence[TurnFile]
    previous_spec: tuple[int, str] | None


class ClientPlugin(Protocol):
    id: str
    prefix: str

    def router(self, deps: PluginDeps) -> APIRouter: ...
    def render_prompt(self, turn: TurnInput) -> str: ...
    def turn_attachments(self, turn: TurnInput) -> list[dict]: ...
    def outbound(self, final_text: str) -> tuple[str, bool]: ...
