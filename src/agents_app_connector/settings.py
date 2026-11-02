"""APPCONN_* settings of the connector."""

import ipaddress

from agents_common.settings import BaseServiceSettings, CsvList
from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import SettingsConfigDict


def _var(info: ValidationInfo) -> str:
    return f"APPCONN_{str(info.field_name).upper()}"


class Settings(BaseServiceSettings):
    """Environment-driven settings shared by every subcommand."""

    model_config = SettingsConfigDict(env_prefix="APPCONN_", extra="ignore")

    preset_name: str = "sketch-translator"
    data_dir: str = "/data"
    max_long_edge_px: int = Field(1568, ge=256, le=8000)
    min_edge_px: int = Field(200, ge=1)
    max_png_bytes: int = 6291456
    max_strokes_bytes: int = 2097152
    max_meta_bytes: int = 16384
    rate_per_minute: int = Field(30, ge=1)
    device_quota_bytes: int = 536870912
    retention_days: int = Field(30, ge=1)
    run_timeout_seconds: int = Field(1800, ge=60)
    max_new_sessions: int = Field(2, ge=1)
    poll_seconds: float = 2
    events_wait_max_seconds: int = Field(30, ge=1, le=60)
    admin_allowed_cidrs: CsvList = ["127.0.0.1/32"]
    device_port: int = 8001
    handoff_presets: CsvList = []
    max_text_chars: int = 8000

    @property
    def max_body_bytes(self) -> int:
        return self.max_png_bytes + self.max_strokes_bytes + self.max_meta_bytes + 65536

    @field_validator("admin_allowed_cidrs")
    @classmethod
    def _cidrs(cls, value: list[str], info: ValidationInfo) -> list[str]:
        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError(f"{_var(info)}::not a CIDR: {item!r}") from None
        return value

    @model_validator(mode="after")
    def _device_port_differs(self):
        if self.device_port == self.http_port:
            raise ValueError("APPCONN_DEVICE_PORT::must differ from APPCONN_HTTP_PORT")
        return self

    def admin_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(c, strict=False) for c in self.admin_allowed_cidrs]
