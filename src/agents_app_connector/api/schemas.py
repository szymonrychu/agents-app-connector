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


class SketchMeta(BaseModel):
    """The `meta` form field of a sketch upload."""

    model_config = ConfigDict(extra="forbid")

    client_id: uuid.UUID
    conversation: Literal["current", "new"] = "current"
    conversation_id: uuid.UUID | None = None
    caption: str | None = Field(default=None, max_length=500)
    pages: list[PageMeta] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self):
        if len({p.page for p in self.pages}) != len(self.pages):
            raise ValueError("page indices must be unique")
        if self.conversation_id is not None and self.conversation == "new":
            raise ValueError("conversation_id cannot be combined with conversation=new")
        return self


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: uuid.UUID
    text: str = Field(min_length=1)
