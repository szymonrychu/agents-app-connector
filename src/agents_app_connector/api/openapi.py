"""Deterministic OpenAPI export: `python -m agents_app_connector.api.openapi > openapi.json`.

The document merges the device app (public, bearer device token) and the admin app (behind SSO
and a source CIDR allowlist). Every error answers `{"error": <code>, "detail": <text>}` (see
`errors.py`), so the generated validation-error responses are replaced with that shape and the
statuses each route really returns. Handlers return plain dicts, so the success bodies are
declared here.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from agents_app_connector.api.admin import create_admin_app
from agents_app_connector.api.device import create_device_app
from agents_app_connector.plugins import REGISTRY
from agents_app_connector.plugins.sketch.layout import PageLayout, SpecLayout
from agents_app_connector.settings import Settings

DEVICE = "/app/v1/sketch"
ADMIN = "/app/admin/v1"
ERROR_REF = {"$ref": "#/components/schemas/ErrorResponse"}
UUID = {"type": "string", "format": "uuid"}
STR = {"type": "string"}
INT = {"type": "integer"}

ERROR_SCHEMAS: dict[str, Any] = {
    "ErrorResponse": {
        "type": "object",
        "title": "ErrorResponse",
        "required": ["error", "detail"],
        "properties": {"error": {"type": "string"}, "detail": {"type": "string"}},
    },
}

ERRORS = {
    400: "the pairing code is unknown, expired, used, superseded or locked (invalid_code)",
    401: "missing, unknown, revoked or other-plugin bearer token (unauthorized)",
    403: "source address is not allowed (forbidden)",
    404: "no such conversation, attachment or device (not_found, no_spec, unknown_session)",
    409: (
        "a request with this client_id is in progress (in_progress), no spec yet (no_spec), "
        "the device is already paired (already_paired), base_version is not the latest spec "
        "version (stale_base), an op id was already reported by an earlier version "
        "(op_replayed), or a translator turn of the conversation is open (busy)"
    ),
    410: "the attachment was purged (purged)",
    413: "body or part too large, or device quota exceeded (too_large, quota_exceeded)",
    422: (
        "invalid request (invalid_request, idempotency_mismatch, preset_not_allowed), or an op "
        "target no longer quotes the base version's source text (stale_target)"
    ),
    429: (
        "per-device or pairing rate limit, see Retry-After (rate_limited), or too many devices "
        "wait for a pairing code (too_many_pending)"
    ),
    502: "the orchestrator answered an error (orchestrator_error)",
    503: "the orchestrator or the database is unavailable (orchestrator_unavailable, unavailable)",
}

# Task relay routes: upstream taskboard statuses and bodies pass through unchanged, so those
# statuses may carry either error shape.
TASK_ERRORS = {
    401: ERRORS[401],
    404: (
        "no such task, todo item or decision (taskboard body), or a path outside the task "
        "allowlist (not_found)"
    ),
    409: "the decision was already answered (already_answered, taskboard body)",
    412: "the task changed since it was read (stale_version, taskboard body)",
    413: "request body over 256 KiB (payload_too_large)",
    422: "invalid request (taskboard body, or invalid_request)",
    429: ERRORS[429],
    503: (
        "the taskboard did not answer in JSON (taskboard_unavailable), no taskboard is "
        "configured (taskboard_unconfigured), or the database is unavailable (unavailable)"
    ),
}
TASK_PASS_THROUGH = {404, 409, 412, 422}
TASK_ERROR_REF = {
    "oneOf": [ERROR_REF, {"$ref": "#/components/schemas/TaskboardError"}],
}
TASK_PATH_PARAMS: dict[str, dict[str, Any]] = {
    "key": {"type": "string", "pattern": "^T-[0-9]+$"},
    "item_id": INT,
    "decision_id": INT,
}
TASK_QUERY = ("status", "session", "label", "cursor", "limit")
PASS_THROUGH = {
    "type": "object",
    "additionalProperties": True,
    "description": "the taskboard JSON body, unchanged",
}


def _task_body(description: str) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": True, "description": description}


# (status, description, content type, schema) of the success response; None keeps the generated one.
OK: dict[tuple[str, str], tuple[str, str, str, dict[str, Any]]] = {
    ("post", "/pair"): (
        "200",
        "the device token (shown once; Cache-Control: no-store)",
        "application/json",
        {
            "type": "object",
            "properties": {"device_id": UUID, "name": STR, "plugin": STR, "token": STR},
        },
    ),
    ("get", "/config"): (
        "200",
        "limits the device must respect",
        "application/json",
        {
            "type": "object",
            "properties": {
                "max_long_edge_px": INT,
                "max_png_bytes": INT,
                "max_strokes_bytes": INT,
                "max_pages": INT,
                "max_upload_bytes": INT,
                "min_edge_px": INT,
                "api_version": INT,
            },
        },
    ),
    ("post", "/sketches"): (
        "202",
        "queued; 200 with the stored ids on a replay of the same client_id",
        "application/json",
        {
            "type": "object",
            "properties": {
                "sketch_id": UUID,
                "conversation_id": UUID,
                "status": {"type": "string", "enum": ["queued"]},
            },
        },
    ),
    ("get", "/conversations"): (
        "200",
        "the device's conversations, newest first",
        "application/json",
        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": UUID,
                    "title": {"type": ["string", "null"]},
                    "created_at": {"type": "string", "format": "date-time"},
                    "latest_spec_version": {"type": ["integer", "null"]},
                    "latest_feedback_id": {"type": ["integer", "null"]},
                },
            },
        },
    ),
    ("post", "/conversations"): (
        "201",
        "a new empty conversation",
        "application/json",
        {"type": "object", "properties": {"id": UUID}},
    ),
    ("delete", "/conversations/{conversation_id}"): ("204", "deleted", "", {}),
    ("post", "/conversations/{conversation_id}/messages"): (
        "202",
        "queued; 200 on a replay of the same client_id",
        "application/json",
        {"type": "object", "properties": {"message_id": UUID}},
    ),
    ("get", "/conversations/{conversation_id}/events"): (
        "200",
        "events after the cursor (long poll with wait)",
        "application/json",
        {
            "type": "object",
            "properties": {
                "events": {"type": "array", "items": {"$ref": "#/components/schemas/Event"}},
                "cursor": INT,
            },
        },
    ),
    ("get", "/conversations/{conversation_id}/stream"): (
        "200",
        "server-sent events; the SSE id is the cursor, Last-Event-ID resumes",
        "text/event-stream",
        {"type": "string"},
    ),
    ("get", "/conversations/{conversation_id}/spec"): (
        "200",
        "the latest spec",
        "application/json",
        {"$ref": "#/components/schemas/Spec"},
    ),
    ("get", "/conversations/{conversation_id}/spec.md"): (
        "200",
        "the latest spec as markdown",
        "text/markdown",
        {"type": "string"},
    ),
    ("get", "/conversations/{conversation_id}/spec/{version}"): (
        "200",
        "one spec version",
        "application/json",
        {"$ref": "#/components/schemas/Spec"},
    ),
    ("post", "/conversations/{conversation_id}/spec/{version}/revert"): (
        "201",
        "the new version, a copy of version N (markdown and layout, line ids unchanged)",
        "application/json",
        {"type": "object", "properties": {"version": INT, "reverted_from": INT}},
    ),
    ("get", "/conversations/{conversation_id}/feedback"): (
        "200",
        "translator feedback items after the cursor, oldest first (at most 100)",
        "application/json",
        {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"$ref": "#/components/schemas/Feedback"}},
                "cursor": INT,
            },
        },
    ),
    ("get", "/conversations/{conversation_id}/images/{region_id}.png"): (
        "200",
        "a region image of the conversation with an ETag; 304 on a matching If-None-Match",
        "image/png",
        {"type": "string", "format": "binary"},
    ),
    ("get", "/attachments/{attachment_id}"): (
        "200",
        "the stored file with an ETag; 304 on a matching If-None-Match",
        "application/octet-stream",
        {"type": "string", "format": "binary"},
    ),
    ("get", "/handoff/targets"): (
        "200",
        "active sessions and allowed presets a spec can be handed to",
        "application/json",
        {
            "type": "object",
            "properties": {
                "sessions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": STR,
                            "slug": STR,
                            "preset_name": {"type": ["string", "null"]},
                        },
                    },
                },
                "presets": {"type": "array", "items": STR},
            },
        },
    ),
    ("post", "/conversations/{conversation_id}/handoff"): (
        "202",
        "queued; 200 on a replay of the same client_id",
        "application/json",
        {
            "type": "object",
            "properties": {"handoff_id": UUID, "session_slug": STR},
        },
    ),
    ("get", "/tasks"): (
        "200",
        "one page of tasks: items, next_cursor (taskboard body, unchanged)",
        "application/json",
        PASS_THROUGH,
    ),
    ("post", "/tasks"): (
        "201",
        "the new task; 200 on a replayed client_key (taskboard body, unchanged)",
        "application/json",
        PASS_THROUGH,
    ),
    ("get", "/tasks/stream"): (
        "200",
        "server-sent events: `task_changed` with data {key}, plus keepalive comments",
        "text/event-stream",
        {"type": "string"},
    ),
    ("get", "/tasks/{key}"): (
        "200",
        "the task with decisions, todo, comments and events (taskboard body, unchanged)",
        "application/json",
        PASS_THROUGH,
    ),
    ("patch", "/tasks/{key}"): (
        "200",
        "the updated task; send the version you read (taskboard body, unchanged)",
        "application/json",
        PASS_THROUGH,
    ),
    ("post", "/tasks/{key}/comments"): (
        "201",
        "the updated task (taskboard body, unchanged)",
        "application/json",
        PASS_THROUGH,
    ),
    ("put", "/tasks/{key}/todo"): (
        "200",
        "the updated task (taskboard body, unchanged)",
        "application/json",
        PASS_THROUGH,
    ),
    ("patch", "/tasks/{key}/todo/{item_id}"): (
        "200",
        "the updated task (taskboard body, unchanged)",
        "application/json",
        PASS_THROUGH,
    ),
    ("post", "/tasks/{key}/decisions/{decision_id}/answer"): (
        "200",
        "the updated task; the asking session is notified (taskboard body, unchanged)",
        "application/json",
        PASS_THROUGH,
    ),
}

# Request bodies of the task relay routes: JSON objects the taskboard validates.
TASK_BODIES = {
    ("post", "/tasks"): "{kind: task|idea, title (max 200), body?, labels?, client_key}",
    ("patch", "/tasks/{key}"): "{version, labels?} or {version, body?}",
    ("post", "/tasks/{key}/comments"): "{body}",
    ("put", "/tasks/{key}/todo"): "{items: [{text (max 200, one line), status}]}",
    ("patch", "/tasks/{key}/todo/{item_id}"): "{status: todo|inprogress|done}",
    ("post", "/tasks/{key}/decisions/{decision_id}/answer"): (
        "{choice: <option index>} or {text}, optional note"
    ),
}

EXTRA_SCHEMAS: dict[str, Any] = {
    "TaskboardError": {
        "type": "object",
        "title": "TaskboardError",
        "description": "error body of the taskboard, passed through by the task relay",
        "required": ["error"],
        "properties": {
            "error": {
                "type": "object",
                "required": ["code", "message"],
                "properties": {"code": STR, "message": STR},
            }
        },
    },
    "Event": {
        "type": "object",
        "title": "Event",
        "description": (
            "type is sketch_status, spec, message, handoff or feedback; the remaining fields "
            "depend on the type (sketch_id or message_id or handoff_id, status, reason, "
            "session_slug, version, feedback_id and kind)."
        ),
        "required": ["cursor", "type", "created_at"],
        "properties": {
            "cursor": INT,
            "type": {
                "type": "string",
                "enum": ["sketch_status", "spec", "message", "handoff", "feedback"],
            },
            "created_at": {"type": "string", "format": "date-time"},
        },
        "additionalProperties": True,
    },
    "Spec": {
        "type": "object",
        "title": "Spec",
        "description": (
            "layout is null for versions not written from notebook pages; otherwise it maps "
            "every markdown source line to its stable line id and lists the op statuses."
        ),
        "required": ["version", "markdown", "created", "sketch_ids", "layout"],
        "properties": {
            "version": INT,
            "markdown": STR,
            "created": {"type": "string", "format": "date-time"},
            "sketch_ids": {"type": "array", "items": UUID},
            "layout": {"anyOf": [{"$ref": "#/components/schemas/SpecLayout"}, {"type": "null"}]},
        },
    },
    "Feedback": {
        "type": "object",
        "title": "Feedback",
        "description": (
            "Something the translator tells the user about the sketch, kept apart from the "
            "document. spec_version is the version written by the same turn, null when the turn "
            "wrote none."
        ),
        "required": ["id", "kind", "text", "where", "spec_version", "created_at"],
        "properties": {
            "id": INT,
            "kind": {"type": "string", "enum": ["question", "uncertain", "illegible", "note"]},
            "text": STR,
            "where": {"type": ["string", "null"]},
            "spec_version": {"type": ["integer", "null"]},
            "created_at": {"type": "string", "format": "date-time"},
        },
    },
    "SketchUpload": {
        "type": "object",
        "title": "SketchUpload",
        "required": ["meta", "sketch.0.png"],
        "properties": {
            "meta": {
                "type": "string",
                "description": (
                    'JSON: {"client_id": uuid, "conversation": "current"|"new", '
                    '"conversation_id"?: uuid, "caption"?: string, "pages": [{"page": int, '
                    '"canvas": {"w", "h", "dpi"}}], "annotates"?: {"spec_version": int >= 1, '
                    '"pages": [{"page": int, "doc_page": int, "lines": [int, int]}]}}. '
                    "annotates needs conversation_id and answers 404 no_spec when that spec "
                    "version does not exist. Part i (sketch.<i>.png, strokes.<i>.json) "
                    "belongs to pages[i]; every page needs its png. "
                    '"base_version"?: int >= 1 is the spec version whose text the board '
                    "showed; it needs conversation_id, excludes annotates, is required when any "
                    "layout carries ops, and answers 409 stale_base when it is not the latest "
                    "version."
                ),
            },
            "sketch.0.png": {"type": "string", "format": "binary"},
            "strokes.0.json": {"type": "string", "format": "binary"},
            "layout.0.json": {
                "type": "string",
                "format": "binary",
                "description": (
                    "optional notebook layout.<i>.json of pages[i] (JSON PageLayout, at most "
                    "APPCONN_MAX_LAYOUT_BYTES). Stroke indices refer to strokes.<i>.json (no "
                    "strokes part means no indices), rects and segments lie inside the canvas, "
                    "op ids are unique uuids never reported before (409 op_replayed), line ids "
                    "exist in base_version and each target quote equals the source text at "
                    "[from, to) (422 stale_target)."
                ),
            },
            "region.0.0.png": {
                "type": "string",
                "format": "binary",
                "description": (
                    "region.<i>.<k>.png: the PNG an image op of pages[i] names in region.part "
                    "(k < APPCONN_MAX_REGIONS_PER_PAGE). Every region part must be referenced "
                    "and every referenced part present. Stored for the conversation's lifetime "
                    "and served by the images route; counts toward the upload size and the "
                    "device quota."
                ),
            },
        },
    },
}

ROUTE_ERRORS: dict[tuple[str, str], tuple[int, ...]] = {
    ("post", "/pair"): (400, 422, 429),
    ("get", "/config"): (401, 429),
    ("post", "/sketches"): (401, 404, 409, 413, 422, 429),
    ("get", "/conversations"): (401, 429),
    ("post", "/conversations"): (401, 429),
    ("delete", "/conversations/{conversation_id}"): (401, 404, 429),
    ("post", "/conversations/{conversation_id}/messages"): (401, 404, 409, 422, 429),
    ("get", "/conversations/{conversation_id}/events"): (401, 404, 422, 429),
    ("get", "/conversations/{conversation_id}/stream"): (401, 404, 422, 429),
    ("get", "/conversations/{conversation_id}/spec"): (401, 404, 429),
    ("get", "/conversations/{conversation_id}/spec.md"): (401, 404, 429),
    ("get", "/conversations/{conversation_id}/spec/{version}"): (401, 404, 422, 429),
    ("get", "/conversations/{conversation_id}/feedback"): (401, 404, 422, 429),
    ("post", "/conversations/{conversation_id}/spec/{version}/revert"): (401, 404, 409, 422, 429),
    ("get", "/attachments/{attachment_id}"): (401, 404, 410, 429),
    ("get", "/conversations/{conversation_id}/images/{region_id}.png"): (401, 404, 422, 429),
    ("get", "/handoff/targets"): (401, 429, 502, 503),
    ("get", "/tasks"): (401, 422, 429, 503),
    ("post", "/tasks"): (401, 413, 422, 429, 503),
    ("get", "/tasks/stream"): (401, 429, 503),
    ("get", "/tasks/{key}"): (401, 404, 429, 503),
    ("patch", "/tasks/{key}"): (401, 404, 412, 413, 422, 429, 503),
    ("post", "/tasks/{key}/comments"): (401, 404, 413, 422, 429, 503),
    ("put", "/tasks/{key}/todo"): (401, 404, 413, 422, 429, 503),
    ("patch", "/tasks/{key}/todo/{item_id}"): (401, 404, 413, 422, 429, 503),
    ("post", "/tasks/{key}/decisions/{decision_id}/answer"): (401, 404, 409, 413, 422, 429, 503),
    ("post", "/conversations/{conversation_id}/handoff"): (
        401,
        404,
        409,
        422,
        429,
        502,
        503,
    ),
    ("post", "/devices"): (403, 422, 429),
    ("post", "/devices/{device_id}/code"): (403, 404, 409, 422, 429),
    ("get", "/devices"): (403,),
    ("delete", "/devices/{device_id}"): (403, 404, 422),
    ("get", "/readyz"): (503,),
}

SUMMARIES = {
    "/pair": "Exchange a 6-digit pairing code for the device token (no bearer token)",
    "/config": "Limits and API version",
    "/sketches": "Upload a sketch (multipart: meta, sketch.<i>.png, strokes.<i>.json per page)",
    "/conversations": "List or create conversations",
    "/conversations/{conversation_id}": "Delete a conversation",
    "/conversations/{conversation_id}/messages": "Post a follow-up message",
    "/conversations/{conversation_id}/events": "Poll events after a cursor",
    "/conversations/{conversation_id}/stream": "Stream events over SSE",
    "/conversations/{conversation_id}/spec": "Latest spec as JSON",
    "/conversations/{conversation_id}/spec.md": "Latest spec as markdown",
    "/conversations/{conversation_id}/spec/{version}": "One spec version",
    "/conversations/{conversation_id}/feedback": "Translator feedback after a cursor",
    "/conversations/{conversation_id}/spec/{version}/revert": (
        "Revert to spec version N: store a copy of it as the next version"
    ),
    "/attachments/{attachment_id}": "Download a stored sketch file",
    "/conversations/{conversation_id}/images/{region_id}.png": (
        "A region image the spec references as images/<region_id>.png"
    ),
    "/handoff/targets": "Sessions and presets a spec can be handed to",
    "/conversations/{conversation_id}/handoff": "Hand the latest spec to a session",
    "/tasks": "List or create tasks (relayed to the taskboard)",
    "/tasks/stream": "Stream task changes over SSE (one shared upstream subscription)",
    "/tasks/{key}": "Read or edit a task (relayed)",
    "/tasks/{key}/comments": "Add a comment (relayed)",
    "/tasks/{key}/todo": "Replace the todo list (relayed)",
    "/tasks/{key}/todo/{item_id}": "Set a todo item status (relayed)",
    "/tasks/{key}/decisions/{decision_id}/answer": "Answer a decision (relayed)",
    "/devices": "Create a device with its first pairing code, or list devices",
    "/devices/{device_id}/code": "Issue a new pairing code for an unpaired device",
    "/devices/{device_id}": "Revoke a device",
}


def _model_schemas() -> dict[str, Any]:
    """Schemas of the notebook wire models, with their nested models as components."""
    out: dict[str, Any] = {}
    for model in (PageLayout, SpecLayout):
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}", by_alias=True)
        out.update(schema.pop("$defs", {}))
        out[model.__name__] = schema
    return out


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://openapi/export",
        orchestrator_url="http://orchestrator.invalid",
        orchestrator_token="openapi-export",
    )


def _apps() -> list[FastAPI]:
    settings = _settings()
    device = create_device_app(None, settings, list(REGISTRY.values()), None)  # type: ignore[arg-type]
    admin = create_admin_app(None, settings)  # type: ignore[arg-type]
    return [device, admin]


def _walk(router, prefix: str = ""):
    for route in router.routes:
        if isinstance(route, APIRoute):
            if not route.include_in_schema:
                continue  # the task allowlist catch-alls are not part of the contract
            yield prefix + route.path, route.methods
        elif hasattr(route, "original_router"):
            yield from _walk(route.original_router, prefix + route.include_context.prefix)


def route_keys() -> set[str]:
    """`METHOD path` of every documented route of both apps (HEAD left out)."""
    out: set[str] = set()
    for app in _apps():
        for path, methods in _walk(app):
            out.update(f"{m} {path}" for m in methods - {"HEAD"})
    return out


def paths_of(doc: dict[str, Any]) -> set[str]:
    return {f"{m.upper()} {p}" for p, ops in doc["paths"].items() for m in ops}


def _local(path: str) -> str:
    for prefix in (DEVICE, ADMIN):
        if path.startswith(prefix):
            return path.removeprefix(prefix) or "/"
    return path


def _task_operation(operation: dict[str, Any], method: str, local: str) -> None:
    """Path and query parameters and the JSON request body of a task relay route."""
    params: list[dict[str, Any]] = [
        {"name": name, "in": "path", "required": True, "schema": TASK_PATH_PARAMS[name]}
        for name in re.findall(r"{(\w+)}", local)
    ]
    if (method, local) == ("get", "/tasks"):
        for name in TASK_QUERY:
            schema = {"type": "array", "items": STR} if name == "label" else STR
            params.append({"name": name, "in": "query", "required": False, "schema": schema})
    if params:
        operation["parameters"] = params
    body = TASK_BODIES.get((method, local))
    if body is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": _task_body(body)}},
        }


def build_openapi() -> dict[str, Any]:
    device, admin = (app.openapi() for app in _apps())
    paths: dict[str, Any] = {**admin["paths"], **device["paths"]}
    schemas: dict[str, Any] = {**admin["components"]["schemas"], **device["components"]["schemas"]}
    for path, methods in paths.items():
        local = _local(path)
        for method, operation in methods.items():
            responses = operation["responses"]
            ok = OK.get((method, local))
            if ok is not None and path.startswith(DEVICE):
                status, description, ctype, schema = ok
                for key in [s for s in responses if s.startswith("2")]:
                    del responses[key]
                responses[status] = {"description": description}
                if ctype:
                    responses[status]["content"] = {ctype: {"schema": schema}}
            if method == "post" and local == "/sketches":
                operation["requestBody"] = {
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": {"$ref": "#/components/schemas/SketchUpload"}
                        }
                    },
                }
            if method == "post" and local == "/pair":
                operation["requestBody"] = {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["code"],
                                "additionalProperties": False,
                                "properties": {"code": {"type": "string", "pattern": "^[0-9]{6}$"}},
                            }
                        }
                    },
                }
            is_task = path.startswith(DEVICE) and local.startswith("/tasks")
            if is_task:
                _task_operation(operation, method, local)
            for status in [s for s in responses if not s.startswith("2")]:
                del responses[status]
            for status in ROUTE_ERRORS.get((method, local), ()):
                shape = TASK_ERROR_REF if is_task and status in TASK_PASS_THROUGH else ERROR_REF
                responses[str(status)] = {
                    "description": (TASK_ERRORS if is_task else ERRORS)[status],
                    "content": {"application/json": {"schema": shape}},
                }
            if path.startswith(DEVICE):
                operation["security"] = [] if local == "/pair" else [{"deviceToken": []}]
            if local in SUMMARIES:
                operation["summary"] = SUMMARIES[local]
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)
    schemas.update(ERROR_SCHEMAS)
    schemas.update(EXTRA_SCHEMAS)
    schemas.update(_model_schemas())
    doc = {
        "openapi": device["openapi"],
        "info": {
            "title": "agents-app-connector",
            "version": device["info"]["version"],
            "description": (
                f"Device API under {DEVICE} (public, bearer device token) and admin API under "
                f"{ADMIN} (behind SSO and a source CIDR allowlist)."
            ),
        },
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {"deviceToken": {"type": "http", "scheme": "bearer"}},
        },
    }
    return doc


def export() -> str:
    """The merged OpenAPI document as JSON with sorted keys and a trailing newline."""
    return json.dumps(build_openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(export(), end="")
