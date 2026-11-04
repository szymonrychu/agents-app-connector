"""Request and response models of the admin API."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PairRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    plugin: str = "sketch"


class PairResponse(BaseModel):
    device_id: uuid.UUID
    name: str
    plugin: str
    token: str


class DeviceOut(BaseModel):
    device_id: uuid.UUID
    name: str
    plugin: str
    created_at: datetime
    revoked_at: datetime | None


class Canvas(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    w: int = Field(gt=0, le=100000)
    h: int = Field(gt=0, le=100000)
    dpi: float = Field(gt=0, le=10000, allow_inf_nan=False)


class PageMeta(BaseModel):
    """One page of a sketch upload: its index and its canvas."""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=0, strict=True)
    canvas: Canvas


class AnnotatedPage(BaseModel):
    """One uploaded page of an annotation: its upload index, document page and source lines."""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=0)
    doc_page: int = Field(ge=0)
    lines: tuple[int, int]

    @model_validator(mode="after")
    def _ordered(self):
        first, last = self.lines
        if first < 0 or last < first:
            raise ValueError("lines must be two ints with 0 <= a <= b")
        return self


class Annotates(BaseModel):
    """Marks an upload as handwritten notes over a stored spec version."""

    model_config = ConfigDict(extra="forbid")

    spec_version: int = Field(ge=1)
    pages: list[AnnotatedPage] = Field(min_length=1)


class SketchMeta(BaseModel):
    """The `meta` form field of a sketch upload."""

    model_config = ConfigDict(extra="forbid")

    client_id: uuid.UUID
    conversation: Literal["current", "new"] = "current"
    conversation_id: uuid.UUID | None = None
    caption: str | None = Field(default=None, max_length=500)
    pages: list[PageMeta] = Field(min_length=1)
    annotates: Annotates | None = None

    @model_validator(mode="after")
    def _consistent(self):
        if len({p.page for p in self.pages}) != len(self.pages):
            raise ValueError("page indices must be unique")
        if self.conversation_id is not None and self.conversation == "new":
            raise ValueError("conversation_id cannot be combined with conversation=new")
        if self.annotates is not None:
            if self.conversation_id is None:
                raise ValueError("annotates requires conversation_id")
            indices = {p.page for p in self.pages}
            annotated = [p.page for p in self.annotates.pages]
            if len(set(annotated)) != len(annotated) or not set(annotated) <= indices:
                raise ValueError("annotates.pages must be unique indices of meta.pages")
        return self


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: uuid.UUID
    text: str = Field(min_length=1)
