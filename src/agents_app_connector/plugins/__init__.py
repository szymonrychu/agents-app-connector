"""Client plugin interface and registry."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

import httpx
from agents_common.orchestrator import Attachment
from fastapi import APIRouter
from psycopg_pool import AsyncConnectionPool

from agents_app_connector.api.events import EventHub
from agents_app_connector.api.limits import RateLimiter
from agents_app_connector.plugins.sketch.layout import PageLayout, SpecLayout
from agents_app_connector.settings import Settings
from agents_app_connector.taskboard_stream import TaskChangeRelay


@dataclass(frozen=True)
class PluginDeps:
    pool: AsyncConnectionPool
    settings: Settings
    store: Any
    limiter: RateLimiter
    hub: EventHub
    orchestrator: Any = None
    taskboard: httpx.AsyncClient | None = None
    task_changes: TaskChangeRelay | None = None


@dataclass(frozen=True)
class TurnFile:
    attachment_id: UUID
    kind: str
    name: str
    mime: str
    data: bytes
    page: int | None = None


@dataclass(frozen=True)
class AnnotatedPage:
    """One uploaded page of an annotation turn: its upload index, document page, source lines."""

    page: int
    doc_page: int
    lines: tuple[int, int]


@dataclass(frozen=True)
class NotebookPage:
    """The layout of one uploaded notebook page and its canvas width in page px."""

    page: int
    layout: PageLayout
    width: int


@dataclass(frozen=True)
class Picture:
    """A freehand picture of an uploaded page, embedded from its stored image."""

    page: int
    id: UUID
    rect: tuple[float, float, float, float]


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
    annotated_spec: tuple[int, str] | None = None
    annotated_pages: Sequence[AnnotatedPage] = ()
    layouts: Sequence[NotebookPage] = ()
    base: tuple[int, str, SpecLayout] | None = None
    pictures: Sequence[Picture] = ()


class ClientPlugin(Protocol):
    id: str
    prefix: str

    def router(self, deps: PluginDeps) -> APIRouter: ...
    def render_prompt(self, turn: TurnInput) -> str: ...
    def turn_attachments(self, turn: TurnInput) -> list[Attachment]: ...


from agents_app_connector.plugins.sketch import PLUGIN as _SKETCH  # noqa: E402

REGISTRY: dict[str, ClientPlugin] = {_SKETCH.id: _SKETCH}
PLUGIN_IDS = frozenset(REGISTRY)
