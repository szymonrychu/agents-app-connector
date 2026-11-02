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
    assert s.max_body_bytes == 6291456 + 2097152 + 16384 + 65536
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
