"""Request and response models of the admin API."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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


class SketchMeta(BaseModel):
    """The `meta` form field of a sketch upload."""

    model_config = ConfigDict(extra="forbid")

    client_id: uuid.UUID
    conversation: Literal["current", "new"] = "current"
    caption: str | None = Field(default=None, max_length=500)
    canvas: Canvas
    page: int = Field(default=0, ge=0, strict=True)


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: uuid.UUID
    text: str = Field(min_length=1)
