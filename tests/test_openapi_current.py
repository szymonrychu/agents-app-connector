"""The committed openapi.json is the merged document of the device and admin apps."""

import json
from pathlib import Path

from agents_app_connector.api import openapi

ROOT = Path(__file__).resolve().parent.parent


def test_openapi_json_matches_apps():
    committed = (ROOT / "openapi.json").read_text(encoding="utf-8")
    assert committed == openapi.export(), (
        "run: uv run python -m agents_app_connector.api.openapi > openapi.json"
    )
    assert committed.endswith("\n")


def test_document_covers_both_apps():
    paths = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))["paths"]
    assert "/app/v1/sketch/sketches" in paths
    assert "/app/v1/sketch/handoff/targets" in paths
    assert "/app/admin/v1/devices" in paths
    assert set(openapi.paths_of(openapi.build_openapi())) == set(openapi.route_keys())


def test_error_shape_is_error_and_detail():
    doc = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    schemas = doc["components"]["schemas"]
    assert "HTTPValidationError" not in schemas
    assert set(schemas["ErrorResponse"]["properties"]) == {"error", "detail"}
