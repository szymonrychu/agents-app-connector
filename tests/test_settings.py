"""APPCONN_* settings of the connector."""

import os

import pytest
from agents_common.settings import SettingsError, load_settings

from agents_app_connector.settings import Settings

BASE_ENV = {
    "APPCONN_DATABASE_URL": "postgresql://u:p@db/x",
    "APPCONN_ORCHESTRATOR_URL": "http://orch:8000",
    "APPCONN_ORCHESTRATOR_TOKEN": "orch-token",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("APPCONN_"):
            monkeypatch.delenv(key)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)


def problems() -> dict[str, str]:
    with pytest.raises(SettingsError) as exc:
        load_settings(Settings)
    return exc.value.problems


def test_defaults():
    s = load_settings(Settings)
    assert (s.max_long_edge_px, s.max_png_bytes, s.max_strokes_bytes) == (1568, 6291456, 2097152)
    assert s.preset_name == "sketch-translator" and s.device_port == 8001
    assert s.max_body_bytes == 8388608 + 16384 + 65536
    assert (s.max_pages, s.max_upload_bytes) == (4, 8388608)
    assert s.admin_allowed_cidrs == ["127.0.0.1/32"]
    assert s.handoff_presets == []


def test_ports_must_differ(monkeypatch):
    monkeypatch.setenv("APPCONN_DEVICE_PORT", "8000")
    assert "must differ" in problems()["APPCONN_DEVICE_PORT"]


def test_bad_cidr_named(monkeypatch):
    monkeypatch.setenv("APPCONN_ADMIN_ALLOWED_CIDRS", "nope")
    assert "APPCONN_ADMIN_ALLOWED_CIDRS" in problems()


def test_csv_cidrs_parsed(monkeypatch):
    monkeypatch.setenv("APPCONN_ADMIN_ALLOWED_CIDRS", "10.0.0.0/8, 192.168.1.0/24")
    assert load_settings(Settings).admin_allowed_cidrs == ["10.0.0.0/8", "192.168.1.0/24"]


def test_long_edge_bounds(monkeypatch):
    monkeypatch.setenv("APPCONN_MAX_LONG_EDGE_PX", "100")
    assert "APPCONN_MAX_LONG_EDGE_PX" in problems()


def test_png_cap_bounded(monkeypatch):
    monkeypatch.setenv("APPCONN_MAX_PNG_BYTES", "6291457")
    assert "APPCONN_MAX_PNG_BYTES" in problems()


def test_max_upload_bytes_cap(monkeypatch):
    monkeypatch.setenv("APPCONN_MAX_UPLOAD_BYTES", "8388609")
    assert "APPCONN_MAX_UPLOAD_BYTES" in problems()


def test_png_cap_within_upload_cap(monkeypatch):
    monkeypatch.setenv("APPCONN_MAX_UPLOAD_BYTES", "1000000")
    assert "APPCONN_MAX_PNG_BYTES" in problems()


def test_max_pages_fits_orchestrator_attachment_limit(monkeypatch):
    monkeypatch.setenv("APPCONN_MAX_PAGES", "5")
    assert "APPCONN_MAX_PAGES" in problems()
    monkeypatch.setenv("APPCONN_MAX_PAGES", "4")
    assert load_settings(Settings).max_pages == 4


def test_taskboard_unset_is_allowed():
    s = load_settings(Settings)
    assert s.taskboard_url is None and s.taskboard_token is None


def test_taskboard_pair_accepted(monkeypatch):
    monkeypatch.setenv("APPCONN_TASKBOARD_URL", "http://taskboard-delegate:8003")
    monkeypatch.setenv("APPCONN_TASKBOARD_TOKEN", "test-token")
    s = load_settings(Settings)
    assert s.taskboard_url == "http://taskboard-delegate:8003"
    assert s.taskboard_token.get_secret_value() == "test-token"


def error_text() -> str:
    with pytest.raises(SettingsError) as exc:
        load_settings(Settings)
    return str(exc.value)


def test_taskboard_url_without_token_is_rejected(monkeypatch):
    monkeypatch.setenv("APPCONN_TASKBOARD_URL", "http://taskboard-delegate:8003")
    text = error_text()
    assert "APPCONN_TASKBOARD_URL" in text and "APPCONN_TASKBOARD_TOKEN" in text


def test_taskboard_token_without_url_is_rejected(monkeypatch):
    monkeypatch.setenv("APPCONN_TASKBOARD_TOKEN", "test-token")
    text = error_text()
    assert "APPCONN_TASKBOARD_URL" in text and "APPCONN_TASKBOARD_TOKEN" in text


@pytest.mark.parametrize("bad", ["taskboard:8003", "ftp://taskboard", "http://", "/path"])
def test_taskboard_url_must_be_absolute_http(monkeypatch, bad):
    monkeypatch.setenv("APPCONN_TASKBOARD_URL", bad)
    monkeypatch.setenv("APPCONN_TASKBOARD_TOKEN", "test-token")
    assert "absolute http" in problems()["APPCONN_TASKBOARD_URL"]


def test_taskboard_token_not_in_repr(monkeypatch):
    monkeypatch.setenv("APPCONN_TASKBOARD_URL", "http://taskboard-delegate:8003")
    monkeypatch.setenv("APPCONN_TASKBOARD_TOKEN", "secret-value-xyz")
    s = load_settings(Settings)
    assert "secret-value-xyz" not in repr(s) and "secret-value-xyz" not in str(s)


def test_pairing_defaults(monkeypatch):
    s = load_settings(Settings)
    assert s.pairing_code_ttl_seconds == 600 and s.pairing_max_failures == 5
    assert s.pairing_max_pending == 3 and s.trusted_proxy_networks() == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PAIRING_CODE_TTL_SECONDS", "59"),
        ("PAIRING_CODE_TTL_SECONDS", "1801"),
        ("PAIRING_MAX_FAILURES", "0"),
        ("PAIRING_MAX_FAILURES", "11"),
        ("PAIRING_MAX_PENDING", "0"),
        ("PAIRING_IP_PER_MINUTE", "0"),
        ("PAIRING_GLOBAL_PER_MINUTE", "0"),
        ("TRUSTED_PROXY_CIDRS", "not-a-cidr"),
    ],
)
def test_pairing_settings_are_bounded(monkeypatch, name, value):
    monkeypatch.setenv(f"APPCONN_{name}", value)
    assert f"APPCONN_{name}" in problems()


def test_translator_tool_defaults():
    s = load_settings(Settings)
    assert s.orchestrator_agent_url is None and s.mcp_port == 8002
    assert (s.max_document_bytes, s.max_feedback_per_run) == (262144, 30)


@pytest.mark.parametrize("port", ["8000", "8001"])
def test_mcp_port_must_differ(monkeypatch, port):
    monkeypatch.setenv("APPCONN_MCP_PORT", port)
    assert "must differ" in problems()["APPCONN_MCP_PORT"]


@pytest.mark.parametrize("bad", ["orch:8001", "ftp://orch", "/agent"])
def test_orchestrator_agent_url_must_be_absolute_http(monkeypatch, bad):
    monkeypatch.setenv("APPCONN_ORCHESTRATOR_AGENT_URL", bad)
    assert "absolute http" in problems()["APPCONN_ORCHESTRATOR_AGENT_URL"]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_DOCUMENT_BYTES", "1023"),
        ("MAX_DOCUMENT_BYTES", "1048577"),
        ("MAX_FEEDBACK_PER_RUN", "0"),
        ("MAX_FEEDBACK_PER_RUN", "201"),
    ],
)
def test_translator_tool_limits_are_bounded(monkeypatch, name, value):
    monkeypatch.setenv(f"APPCONN_{name}", value)
    assert f"APPCONN_{name}" in problems()
