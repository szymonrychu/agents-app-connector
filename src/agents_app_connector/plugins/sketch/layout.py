"""Notebook pages: the wire models of `layout.<i>.json` and the stored spec layout.

A notebook page upload carries, per page, the rendered runs the board showed (line ids of the
base spec version), the new handwriting split into row segments, and the edit ops the app
classified (replace, delete, insert, image). The translator reports, through `document_write`,
the provenance of every source line and the status of every op; the connector stores that as
the version's `SpecLayout` with connector-assigned line ids (`line_ids.assign`).
"""

import asyncio
import re
import uuid
from collections.abc import Iterable
from typing import Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agents_app_connector.api.errors import ApiError
from agents_app_connector.api.schemas import REGION_PART, Canvas, PictureRegion, SketchMeta
from agents_app_connector.db import attachments
from agents_app_connector.storage import AttachmentStore

LINE_ID = r"^L[0-9]+$"
SEGMENT_ID = r"^p[0-9]+-r[0-9]+-[0-9]+$"

Rect = tuple[float, float, float, float]


def layout_field(index: int) -> str:
    return f"layout.{index}.json"


def region_field(index: int, k: int) -> str:
    return f"region.{index}.{k}.png"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Geometry(_Strict):
    """Notebook grid of the page in page-canvas px."""

    cell: int = Field(gt=0)
    row_pitch: int = Field(gt=0)
    top: int = Field(ge=0)
    rows: int = Field(gt=0)
    text_left: int = Field(ge=0)
    margin_left: int = Field(ge=0)


class Segment(_Strict):
    """New handwriting of one row, split at gaps wider than two cells."""

    id: str = Field(pattern=SEGMENT_ID)
    row: int = Field(ge=0)
    x0: float
    x1: float
    strokes: list[int]


class VisibleRun(_Strict):
    """A rendered spec line as the board showed it."""

    line_id: str = Field(pattern=LINE_ID)
    row: int = Field(ge=0)
    x0: float


class OpTarget(_Strict):
    """Char offsets `[from, to)` in the markdown source line of `line_id` and the text there."""

    line_id: str = Field(pattern=LINE_ID)
    from_: int = Field(alias="from", ge=0)
    to: int = Field(ge=0)
    quote: str

    @model_validator(mode="after")
    def _ordered(self):
        if self.to < self.from_:
            raise ValueError("to must not be less than from")
        return self


class OpAt(_Strict):
    line_id: str = Field(pattern=LINE_ID)
    offset: int = Field(ge=0)
    mode: Literal["inline", "after_line"]


class Region(_Strict):
    id: uuid.UUID
    part: str = Field(pattern=REGION_PART)
    after_line: str | None = Field(default=None, pattern=LINE_ID)


REQUIRED: dict[str, tuple[str, ...]] = {
    "replace": ("target",),
    "delete": ("target",),
    "insert": ("at", "rect"),
    "image": ("region", "rect"),
    "note": ("rect",),
}


class EditOp(_Strict):
    id: uuid.UUID
    kind: Literal["replace", "delete", "insert", "image", "note"]
    target: OpTarget | None = None
    at: OpAt | None = None
    strokes: list[int] = []
    rect: Rect | None = None
    region: Region | None = None

    @model_validator(mode="after")
    def _fields_of_kind(self):
        missing = [name for name in REQUIRED[self.kind] if getattr(self, name) is None]
        if missing:
            raise ValueError(f"a {self.kind} op needs {' and '.join(missing)}")
        if self.kind == "note" and not self.strokes:
            raise ValueError("a note op needs strokes")
        return self


class PageLayout(_Strict):
    version: Literal[1]
    geometry: Geometry
    segments: list[Segment] = []
    runs: list[VisibleRun] = []
    ops: list[EditOp] = []


class SpecLine(_Strict):
    """One markdown source line (1-based) of a stored version and its stable id."""

    line_id: str = Field(pattern=LINE_ID)
    source_line: int = Field(ge=1)
    origin: dict | None = None


class OpStatus(_Strict):
    id: uuid.UUID
    status: Literal["applied", "rejected"]
    note: str | None = None


class SpecLayout(_Strict):
    lines: list[SpecLine]
    ops: list[OpStatus] = []
    base_version: int | None = None
    reverted_from: int | None = None


def _invalid(index: int, message: str) -> ApiError:
    return ApiError(422, "invalid_request", f"{layout_field(index)}: {message}")


def parse_layout(raw: bytes, *, index: int, max_bytes: int) -> PageLayout:
    """The page layout of part `layout.<index>.json`, or 422 `invalid_request`."""
    if len(raw) > max_bytes:
        raise ApiError(413, "too_large", f"{layout_field(index)} exceeds {max_bytes} bytes")
    try:
        return PageLayout.model_validate_json(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        message = first["msg"].rstrip(".")
        raise _invalid(index, f"{where}: {message}" if where else message) from None


def _inside(rect: Rect, canvas: Canvas) -> bool:
    x, y, w, h = rect
    return x >= 0 and y >= 0 and w >= 0 and h >= 0 and x + w <= canvas.w and y + h <= canvas.h


def check_layout(
    layout: PageLayout,
    *,
    index: int,
    strokes_count: int | None,
    canvas: Canvas,
) -> None:
    """Stroke indices within that page's strokes part (none without one), segments and rects
    inside the canvas."""
    limit = strokes_count or 0
    for name, strokes in [(s.id, s.strokes) for s in layout.segments] + [
        (f"op {o.id}", o.strokes) for o in layout.ops
    ]:
        bad = [i for i in strokes if not 0 <= i < limit]
        if bad:
            raise _invalid(index, f"{name}: stroke index {bad[0]} outside strokes.{index}.json")
    for seg in layout.segments:
        if not 0 <= seg.x0 <= seg.x1 <= canvas.w or seg.row >= layout.geometry.rows:
            raise _invalid(index, f"{seg.id}: outside the page")
    for run in layout.runs:
        if run.row >= layout.geometry.rows or not 0 <= run.x0 <= canvas.w:
            raise _invalid(index, f"run {run.line_id}: outside the page")
    for op in layout.ops:
        if op.rect is not None and not _inside(op.rect, canvas):
            raise _invalid(index, f"op {op.id}: rect outside the canvas")


def check_region_parts(
    meta: SketchMeta, layouts: dict[int, PageLayout], region_parts: set[tuple[int, int]]
) -> None:
    """Per page position i, the region parts present are exactly the ones the image ops of its
    layout and the pictures of its meta name, each named once (422 `invalid_request`)."""
    for i, page in enumerate(meta.pages):
        lay = layouts.get(i)
        named = [op.region.part for op in lay.ops if op.region is not None] if lay else []
        for pic in page.regions:
            if not re.fullmatch(rf"region\.{i}\.[0-9]+\.png", pic.part):
                raise ApiError(
                    422,
                    "invalid_request",
                    f"meta.pages[{i}].regions: {pic.part} is not a part of page {i}",
                )
            if not _inside(pic.rect, page.canvas):
                raise ApiError(
                    422,
                    "invalid_request",
                    f"meta.pages[{i}].regions: {pic.id}: rect outside the canvas",
                )
            named.append(pic.part)
        present = {region_field(p, k) for p, k in region_parts if p == i}
        for part in sorted(set(named) - present):
            raise ApiError(422, "invalid_request", f"{part} is named but missing")
        for part in sorted(present - set(named)):
            raise ApiError(422, "invalid_request", f"{part}: no image op or picture names it")
        for part in sorted({n for n in named if named.count(n) > 1}):
            raise ApiError(422, "invalid_request", f"{part} is named twice")


def check_unique_ops(layouts: Iterable[PageLayout], pictures: Iterable[PictureRegion] = ()) -> None:
    """Op ids, op region ids and picture ids are unique across the upload."""
    seen: set[uuid.UUID] = set()

    def add(item: uuid.UUID) -> None:
        if item in seen:
            raise ApiError(422, "invalid_request", f"duplicate op or region id {item}")
        seen.add(item)

    for lay in layouts:
        for op in lay.ops:
            add(op.id)
            if op.region is not None:
                add(op.region.id)
    for pic in pictures:
        add(pic.id)


def check_base(layouts: dict[int, PageLayout], base_markdown: str, base: SpecLayout | None) -> None:
    """Every line id of the runs and ops exists in the base version (422 `invalid_request`), and
    every op target still quotes the base source text (422 `stale_target`)."""
    lines = {} if base is None else {line.line_id: line.source_line for line in base.lines}
    source = base_markdown.splitlines()
    for index, lay in sorted(layouts.items()):
        named = [r.line_id for r in lay.runs]
        for op in lay.ops:
            named += [x.line_id for x in (op.target, op.at) if x is not None]
            if op.region is not None and op.region.after_line is not None:
                named.append(op.region.after_line)
        for line_id in named:
            if line_id not in lines:
                raise _invalid(index, f"line id {line_id} is not in the base version")
        for op in lay.ops:
            if op.target is None:
                continue
            n = lines[op.target.line_id]
            text = source[n - 1] if n <= len(source) else ""
            t = op.target
            if t.to > len(text) or text[t.from_ : t.to] != t.quote:
                raise ApiError(
                    422,
                    "stale_target",
                    f"op {op.id}: {t.line_id}[{t.from_}:{t.to}] no longer reads {t.quote!r}",
                )


IMAGE_REF = re.compile(r"!\[[^\]]*\]\(images/([0-9a-f-]{36})\.png\)")


def image_ids(markdown: str) -> list[uuid.UUID]:
    """Region ids of the `![..](images/<id>.png)` references, first-seen order, unique."""
    out: list[uuid.UUID] = []
    for raw in IMAGE_REF.findall(markdown):
        try:
            item = uuid.UUID(raw)
        except ValueError:
            continue
        if item not in out:
            out.append(item)
    return out


async def load_layouts(
    conn: psycopg.AsyncConnection, store: AttachmentStore, inbound_id: uuid.UUID
) -> dict[int, PageLayout]:
    """The page layouts of an upload by page (validated at upload); unreadable ones left out."""
    out: dict[int, PageLayout] = {}
    for att in await attachments.for_inbound(conn, inbound_id):
        if att.kind != "sketch_layout" or att.purged_at is not None:
            continue
        try:
            raw = await asyncio.to_thread(store.read, att.path)
        except OSError:
            continue
        out[att.page or 0] = PageLayout.model_validate_json(raw)
    return out
