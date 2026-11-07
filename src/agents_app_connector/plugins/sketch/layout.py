"""Notebook pages: the wire models of `layout.<i>.json` and the stored spec layout.

A notebook page upload carries, per page, the rendered runs the board showed (line ids of the
base spec version), the new handwriting split into row segments, and the edit ops the app
classified (replace, delete, insert, image). The translator reports, through `document_write`,
the provenance of every source line and the status of every op; the connector stores that as
the version's `SpecLayout` with connector-assigned line ids (`line_ids.assign`).
"""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
