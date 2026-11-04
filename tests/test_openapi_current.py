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


TASK_PATHS = {
    "GET /app/v1/sketch/tasks",
    "POST /app/v1/sketch/tasks",
    "GET /app/v1/sketch/tasks/stream",
    "GET /app/v1/sketch/tasks/{key}",
    "PATCH /app/v1/sketch/tasks/{key}",
    "POST /app/v1/sketch/tasks/{key}/comments",
    "PUT /app/v1/sketch/tasks/{key}/todo",
    "PATCH /app/v1/sketch/tasks/{key}/todo/{item_id}",
    "POST /app/v1/sketch/tasks/{key}/decisions/{decision_id}/answer",
}


def test_task_relay_routes_documented_without_catch_alls():
    doc = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    found = {k for k in openapi.paths_of(doc) if "/tasks" in k}
    assert found == TASK_PATHS
    assert not [p for p in doc["paths"] if "rest" in p]


def test_task_relay_operations_declare_params_errors_and_security():
    doc = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    ops = {
        f"{m.upper()} {p}": op
        for p, methods in doc["paths"].items()
        if "/tasks" in p
        for m, op in methods.items()
    }
    for key, op in ops.items():
        assert op["security"] == [{"deviceToken": []}], key
        assert {"401", "429", "503"} <= set(op["responses"]), key
        assert op["operationId"].startswith("tasks_"), key
    answer = ops["POST /app/v1/sketch/tasks/{key}/decisions/{decision_id}/answer"]
    assert {"200", "404", "409", "413", "422"} <= set(answer["responses"])
    assert "412" not in answer["responses"]
    assert {p["name"] for p in answer["parameters"]} == {"key", "decision_id"}
    assert all(p["in"] == "path" and p["required"] for p in answer["parameters"])
    listing = ops["GET /app/v1/sketch/tasks"]
    assert {"status", "session", "label", "cursor", "limit"} <= {
        p["name"] for p in listing["parameters"] if p["in"] == "query"
    }
    assert "requestBody" in ops["POST /app/v1/sketch/tasks"]
    assert "requestBody" not in ops["GET /app/v1/sketch/tasks/{key}"]
    stream = ops["GET /app/v1/sketch/tasks/stream"]
    assert "text/event-stream" in stream["responses"]["200"]["content"]
    text = json.dumps(doc)
    for code in ("payload_too_large", "taskboard_unavailable", "taskboard_unconfigured"):
        assert code in text, code
    assert "TaskboardError" in doc["components"]["schemas"]
