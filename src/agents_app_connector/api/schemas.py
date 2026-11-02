"""Request and response models of the admin API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


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
