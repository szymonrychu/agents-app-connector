"""Notebook pages: the wire models of `layout.<i>.json` and the stored spec layout.

A notebook page upload carries, per page, the rendered runs the board showed (line ids of the
base spec version), the new handwriting split into row segments, and the edit ops the app
classified (replace, delete, insert, image). The translator reports, through `document_write`,
the provenance of every source line and the status of every op; the connector stores that as
the version's `SpecLayout` with connector-assigned line ids (`line_ids.assign`).
"""

import uuid
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agents_app_connector.api.errors import ApiError
from agents_app_connector.api.schemas import Canvas

LINE_ID = r"^L[0-9]+$"
SEGMENT_ID = r"^p[0-9]+-r[0-9]+-[0-9]+$"
REGION_PART = r"^region\.[0-9]+\.[0-9]+\.png$"

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
}


class EditOp(_Strict):
    id: uuid.UUID
    kind: Literal["replace", "delete", "insert", "image"]
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
    region_parts: set[str],
) -> None:
    """Stroke indices within that page's strokes part (none without one), segments and rects
    inside the canvas, and region parts of this page exactly the ones its image ops name."""
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
    named = {op.region.part for op in layout.ops if op.region is not None}
    for part in sorted(named - region_parts):
        raise _invalid(index, f"{part} is named by an image op but missing")
    for part in sorted(region_parts - named):
        raise ApiError(422, "invalid_request", f"{part}: no image op names it")


def check_unique_ops(layouts: Iterable[PageLayout]) -> None:
    """Op ids and region ids are unique across the upload."""
    seen: set[uuid.UUID] = set()
    for lay in layouts:
        for op in lay.ops:
            ids = [op.id] + ([op.region.id] if op.region is not None else [])
            for item in ids:
                if item in seen:
                    raise ApiError(422, "invalid_request", f"duplicate op or region id {item}")
                seen.add(item)


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
