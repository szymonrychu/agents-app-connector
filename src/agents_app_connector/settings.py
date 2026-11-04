"""APPCONN_* settings of the connector."""

import ipaddress
from urllib.parse import urlsplit

from agents_common.settings import BaseServiceSettings, CsvList
from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

# Orchestrator attachment limits (agents-orchestrator settings attachments_max_count and
# attachments_max_total_bytes defaults); a turn carries every file of one upload.
ORCHESTRATOR_ATTACHMENTS_MAX_COUNT = 8
ORCHESTRATOR_ATTACHMENTS_MAX_TOTAL_BYTES = 8388608


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
    max_upload_bytes: int = 8388608
    max_pages: int = Field(4, ge=1, le=32)
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
    taskboard_url: str | None = None
    taskboard_token: SecretStr | None = Field(None, repr=False)
    pairing_code_ttl_seconds: int = Field(600, ge=60, le=1800)
    pairing_max_failures: int = Field(5, ge=1, le=10)
    pairing_max_pending: int = Field(3, ge=1, le=20)
    pairing_ip_per_minute: int = Field(5, ge=1)
    pairing_global_per_minute: int = Field(30, ge=1)
    trusted_proxy_cidrs: CsvList = []
    orchestrator_agent_url: str | None = None
    mcp_port: int = 8002
    max_document_bytes: int = Field(262144, ge=1024, le=1048576)
    max_feedback_per_run: int = Field(30, ge=1, le=200)

    @property
    def max_body_bytes(self) -> int:
        return self.max_upload_bytes + self.max_meta_bytes + 65536

    @field_validator("admin_allowed_cidrs", "trusted_proxy_cidrs")
    @classmethod
    def _cidrs(cls, value: list[str], info: ValidationInfo) -> list[str]:
        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError(f"{_var(info)}::not a CIDR: {item!r}") from None
        return value

    @field_validator("taskboard_url", "orchestrator_agent_url")
    @classmethod
    def _http_url(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"{_var(info)}::must be an absolute http(s) URL")
        return value

    @model_validator(mode="after")
    def _taskboard_pair(self):
        has_token = self.taskboard_token is not None and bool(
            self.taskboard_token.get_secret_value()
        )
        if self.taskboard_url and not has_token:
            raise ValueError("APPCONN_TASKBOARD_TOKEN::required when APPCONN_TASKBOARD_URL is set")
        if has_token and not self.taskboard_url:
            raise ValueError("APPCONN_TASKBOARD_URL::required when APPCONN_TASKBOARD_TOKEN is set")
        return self

    @model_validator(mode="after")
    def _attachment_caps(self):
        # The orchestrator caps one attachment at 6 MiB, all attachments of a message at 8 MiB
        # and their count at ORCHESTRATOR_ATTACHMENTS_MAX_COUNT (a png and a strokes file per page).
        if self.max_png_bytes > 6291456:
            raise ValueError("APPCONN_MAX_PNG_BYTES::must be at most 6291456")
        if self.max_upload_bytes > ORCHESTRATOR_ATTACHMENTS_MAX_TOTAL_BYTES:
            raise ValueError("APPCONN_MAX_UPLOAD_BYTES::must be at most 8388608")
        if self.max_png_bytes > self.max_upload_bytes:
            raise ValueError("APPCONN_MAX_PNG_BYTES::must be at most APPCONN_MAX_UPLOAD_BYTES")
        if 2 * self.max_pages > ORCHESTRATOR_ATTACHMENTS_MAX_COUNT:
            raise ValueError(
                f"APPCONN_MAX_PAGES::two files per page must fit "
                f"{ORCHESTRATOR_ATTACHMENTS_MAX_COUNT} attachments per message"
            )
        return self

    @model_validator(mode="after")
    def _device_port_differs(self):
        if self.device_port == self.http_port:
            raise ValueError("APPCONN_DEVICE_PORT::must differ from APPCONN_HTTP_PORT")
        if self.mcp_port in (self.http_port, self.device_port):
            raise ValueError("APPCONN_MCP_PORT::must differ from the admin and device ports")
        return self

    def trusted_proxy_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(c, strict=False) for c in self.trusted_proxy_cidrs]

    def admin_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(c, strict=False) for c in self.admin_allowed_cidrs]
