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


def test_upload_documents_annotates_and_no_spec():
    doc = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    post = doc["paths"]["/app/v1/sketch/sketches"]["post"]
    assert "no_spec" in post["responses"]["404"]["description"]
    meta = doc["components"]["schemas"]["SketchUpload"]["properties"]["meta"]["description"]
    assert "annotates" in meta and "spec_version" in meta


def test_feedback_route_and_schema_documented():
    doc = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    op = doc["paths"]["/app/v1/sketch/conversations/{conversation_id}/feedback"]["get"]
    assert {"200", "401", "404"} <= set(op["responses"])
    schemas = doc["components"]["schemas"]
    assert schemas["Feedback"]["properties"]["kind"]["enum"] == [
        "question",
        "uncertain",
        "illegible",
        "note",
    ]
    assert "feedback" in schemas["Event"]["properties"]["type"]["enum"]
    listing = doc["paths"]["/app/v1/sketch/conversations"]["get"]["responses"]["200"]
    item = listing["content"]["application/json"]["schema"]["items"]["properties"]
    assert "latest_feedback_id" in item


def test_upload_documents_layout_and_region_parts():
    doc = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    post = doc["paths"]["/app/v1/sketch/sketches"]["post"]
    upload = doc["components"]["schemas"]["SketchUpload"]
    props = upload["properties"]
    assert "layout.0.json" in props and "region.0.0.png" in props
    text = json.dumps(upload)
    for word in ("layout.<i>.json", "region.<i>.<k>.png", "base_version"):
        assert word in text, word
    assert "stale_base" in post["responses"]["409"]["description"]
    assert "op_replayed" in post["responses"]["409"]["description"]
    assert "stale_target" in post["responses"]["422"]["description"]
    schemas = doc["components"]["schemas"]
    for name in ("PageLayout", "EditOp", "Segment", "VisibleRun", "Geometry", "Region"):
        assert name in schemas, name
    assert schemas["EditOp"]["properties"]["kind"]["enum"] == [
        "replace",
        "delete",
        "insert",
        "image",
        "note",
    ]


def test_spec_schema_has_layout():
    doc = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    schemas = doc["components"]["schemas"]
    spec = schemas["Spec"]
    assert "layout" in spec["required"]
    assert spec["properties"]["layout"] == {
        "anyOf": [{"$ref": "#/components/schemas/SpecLayout"}, {"type": "null"}]
    }
    for name in ("SpecLayout", "SpecLine", "OpStatus"):
        assert name in schemas, name
    assert "reverted_from" in schemas["SpecLayout"]["properties"]


def test_notebook_routes_documented():
    doc = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    paths = doc["paths"]
    image = paths["/app/v1/sketch/conversations/{conversation_id}/images/{region_id}.png"]["get"]
    assert "image/png" in image["responses"]["200"]["content"]
    assert {"401", "404"} <= set(image["responses"])
    revert = paths["/app/v1/sketch/conversations/{conversation_id}/spec/{version}/revert"]["post"]
    assert {"201", "401", "404", "409", "422"} <= set(revert["responses"])
    assert "busy" in revert["responses"]["409"]["description"]
    assert "stale_base" in revert["responses"]["409"]["description"]
    body = revert["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/RevertIn"}


def test_upload_documents_picture_regions():
    doc = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    schemas = doc["components"]["schemas"]
    assert set(schemas["PictureRegion"]["properties"]) == {"id", "rect", "part"}
    upload = schemas["SketchUpload"]
    assert '"regions"?' in upload["properties"]["meta"]["description"]
    assert "pages[i].regions" in upload["properties"]["region.0.0.png"]["description"]
