"""Client plugin interface and registry."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from agents_common.orchestrator import Attachment
from fastapi import APIRouter
from psycopg_pool import AsyncConnectionPool

from agents_app_connector.api.events import EventHub
from agents_app_connector.api.limits import RateLimiter
from agents_app_connector.settings import Settings


@dataclass(frozen=True)
class PluginDeps:
    pool: AsyncConnectionPool
    settings: Settings
    store: Any
    limiter: RateLimiter
    hub: EventHub
    orchestrator: Any = None


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
    def turn_attachments(self, turn: TurnInput) -> list[Attachment]: ...
    def outbound(self, final_text: str) -> tuple[str, bool]: ...


from agents_app_connector.plugins.sketch import PLUGIN as _SKETCH  # noqa: E402

REGISTRY: dict[str, ClientPlugin] = {_SKETCH.id: _SKETCH}
PLUGIN_IDS = frozenset(REGISTRY)
