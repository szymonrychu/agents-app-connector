# agents-app-connector

Part of the claude-code-tatara project (python).

## What this is

A Python microservice with its own Postgres and a PVC: a client-plugin connector for user
devices. The `sketch` plugin pairs Boox devices, takes sketch uploads, translates them into
versioned markdown specs through a sketch-translator agent session, streams progress back and
hands specs off to other sessions.

## Ports

| Process | Port setting | Default | Exposure |
|---------|--------------|---------|----------|
| `agents-app-connector admin` | `APPCONN_HTTP_PORT` | 8000 | admin API under `/app/admin`, behind SSO and a source CIDR allowlist |
| `agents-app-connector app` | `APPCONN_DEVICE_PORT` | 8001 | device API under `/app/v1/sketch`, the only public path |

Both serve `/healthz` and `/readyz` for probes. The OpenAPI document of both APIs is committed
as `openapi.json` (regenerate with `uv run python -m agents_app_connector.api.openapi > openapi.json`).

## Pairing

A device is paired through the admin API and then uses its bearer token on the device API.

1. An operator calls `POST /app/admin/v1/devices` with `{"name": "...", "plugin": "sketch"}`
   behind SSO. The response carries the device token once; only its sha256 is stored.
2. The device sends `Authorization: Bearer <token>` on every device request.
3. `DELETE /app/admin/v1/devices/{device_id}` revokes the device; its token then answers 401,
   and open event streams end.

A device token never authorises an admin route.

## API

Device API v1 (prefix `/app/v1/sketch`, bearer device token). Errors are JSON
`{"error": <code>, "detail": <text>}`. Every state-changing request carries a `client_id`
(uuid); repeating it with the same payload replays the stored answer (200), with another
payload it answers 422 `idempotency_mismatch`.

### `GET /app/v1/sketch/config`

Limits and version: `{max_long_edge_px, max_png_bytes, max_strokes_bytes, max_pages, max_upload_bytes, min_edge_px, api_version}`.

### `POST /app/v1/sketch/sketches`

Multipart upload of one or more pages: `meta` (JSON `{client_id, conversation:
"current"|"new", conversation_id?, caption?, pages: [{page, canvas: {w, h, dpi}}]}`), then per
position `i` in `pages` a required `sketch.<i>.png` and an optional `strokes.<i>.json`. Page
indices are unique, at most `max_pages` pages, all parts together at most `max_upload_bytes`.
`conversation_id` targets an existing open conversation of the device (404 `not_found` if
unknown or foreign) and cannot be combined with `conversation: "new"`. 202 `{sketch_id,
conversation_id, status: "queued"}`. 413 `too_large` or `quota_exceeded`, 422 `invalid_request`
(also a missing png, an unknown field name or a png without a `pages` entry).

### `GET /app/v1/sketch/conversations`

The device's conversations: `[{id, title, created_at, latest_spec_version}]`.

### `POST /app/v1/sketch/conversations`

Create an empty conversation. 201 `{id}`.

### `DELETE /app/v1/sketch/conversations/{conversation_id}`

Delete a conversation with its specs and files. 204; 404 `not_found`.

### `POST /app/v1/sketch/conversations/{conversation_id}/messages`

Follow-up text `{client_id, text}` that refines the spec. 202 `{message_id}`.

### `GET /app/v1/sketch/conversations/{conversation_id}/events`

Long poll: `?after=<cursor>&wait=<seconds>`. Returns `{events: [{cursor, type, created_at,
...}], cursor}`. Event types: `sketch_status`, `spec`, `message`, `handoff`.

### `GET /app/v1/sketch/conversations/{conversation_id}/stream`

The same events as server-sent events; the SSE id is the cursor and `Last-Event-ID` resumes.

### `GET /app/v1/sketch/conversations/{conversation_id}/spec`

Latest spec: `{version, markdown, created, sketch_ids}`. 404 `no_spec`.

### `GET /app/v1/sketch/conversations/{conversation_id}/spec.md`

Latest spec as `text/markdown`.

### `GET /app/v1/sketch/conversations/{conversation_id}/spec/{version}`

One spec version, same shape as the latest spec.

### `GET /app/v1/sketch/attachments/{attachment_id}`

A stored sketch file with an `ETag` (304 on `If-None-Match`). 410 `purged` after retention.

### `GET /app/v1/sketch/handoff/targets`

Additive route: `{sessions: [{id, slug, preset_name}], presets: [name]}`, the active sessions
and the allowed presets a spec can be handed to. 502 or 503 when the orchestrator fails.

### `POST /app/v1/sketch/conversations/{conversation_id}/handoff`

Hand the latest spec to a session: body `{client_id, session: "<slug>" | {"preset": "<name>"}}`.
202 `{session_slug}` plus the additive `handoff_id`; progress arrives as `handoff` events.
404 `unknown_session`, 409 `no_spec`, 422 `preset_not_allowed`.

### Task relay (Tasks tab)

A paired device acts as the user on the agents taskboard through these routes. The connector
forwards a fixed allowlist of taskboard paths to the taskboard delegate listener
(`/taskboard/api/v1`) with its own bearer (`APPCONN_TASKBOARD_TOKEN`, never the device token or
any inbound header; the query string passes through). It stores nothing and logs method, path
and status only. The upstream status and JSON body come back unchanged, so the taskboard errors
(404, 409 `already_answered`, 412 `stale_version`, 422) arrive in the taskboard shape
`{"error": {"code", "message"}}`; the connector's own errors keep `{"error", "detail"}`:
404 `not_found` for any path or method outside the allowlist, 413 `payload_too_large` for a
body over 256 KiB, 503 `taskboard_unavailable` (connect error, timeout or a non-JSON answer)
and 503 `taskboard_unconfigured` (`APPCONN_TASKBOARD_URL` unset). Asking and withdrawing
decisions are agent-only and are not relayed.

### `GET /app/v1/sketch/tasks`

List tasks. Query (all optional, passed through): `status` (`active` default, `done`,
`dropped`, `all`), `session`, `label` (repeatable), `cursor`, `limit`. 200 `{items, next_cursor}`.

### `POST /app/v1/sketch/tasks`

Create a task: `{kind, title, body?, labels?, client_key}`. 201, or 200 when `client_key` was
already used (a retry returns the same task).

### `GET /app/v1/sketch/tasks/stream`

Server-sent events `task_changed` with data `{"key": "T-5"}`, plus a keepalive comment every
15 s. All device streams share one upstream subscription that is opened with the first stream,
reconnects with backoff and closes with the last. A key is only a refresh hint. The stream ends
within one keepalive interval when the device token is revoked.

### `GET /app/v1/sketch/tasks/{key}`

One task with decisions, todo, comments and events. 404 for an unknown task.

### `PATCH /app/v1/sketch/tasks/{key}`

Edit a task: `{version, labels}` or `{version, body}`. 412 `stale_version` when the task changed
since it was read.

### `POST /app/v1/sketch/tasks/{key}/comments`

Add a comment: `{body}`. 201 with the updated task.

### `PUT /app/v1/sketch/tasks/{key}/todo`

Replace the todo list: `{items: [{text, status}]}` (text up to 200 characters on one line;
status `todo`, `inprogress` or `done`).

### `PATCH /app/v1/sketch/tasks/{key}/todo/{item_id}`

Set one item's status: `{status}`.

### `POST /app/v1/sketch/tasks/{key}/decisions/{decision_id}/answer`

Answer a decision with `{choice}` (option index) or `{text}`, plus an optional `note`. The
asking session is notified by the taskboard. A second answer is 409 `already_answered`.

Admin API v1 (prefix `/app/admin/v1`, behind SSO and a source CIDR allowlist; errors from the
allowlist are `{"error": "forbidden"}`).

### `POST /app/admin/v1/devices`

Pair a device: `{name, plugin}`. 201 `{device_id, name, plugin, token}`.

### `GET /app/admin/v1/devices`

List devices: `[{device_id, name, plugin, created_at, revoked_at}]`.

### `DELETE /app/admin/v1/devices/{device_id}`

Revoke a device. 204; 404 `not_found` for an unknown or already revoked device.

## Configuration

Environment variables use the `APPCONN_` prefix. Required: `APPCONN_DATABASE_URL`,
`APPCONN_ORCHESTRATOR_URL` and an orchestrator token (`APPCONN_ORCHESTRATOR_TOKEN` or
`APPCONN_ORCHESTRATOR_TOKEN_FILE`). A bad configuration exits with code 78.

| Variable | Default | Meaning |
|----------|---------|---------|
| `APPCONN_DATABASE_URL` | required | Postgres URL |
| `APPCONN_ORCHESTRATOR_URL` | required | base URL of agents-orchestrator |
| `APPCONN_ORCHESTRATOR_TOKEN` | empty | orchestrator bearer token |
| `APPCONN_ORCHESTRATOR_TOKEN_FILE` | empty | file holding the orchestrator token |
| `APPCONN_HTTP_HOST` | `0.0.0.0` | bind address |
| `APPCONN_HTTP_PORT` | 8000 | admin API port |
| `APPCONN_DEVICE_PORT` | 8001 | device API port, must differ from the admin port |
| `APPCONN_LOG_LEVEL` | `INFO` | log level |
| `APPCONN_SHUTDOWN_GRACE_SECONDS` | 20 | graceful shutdown window |
| `APPCONN_PRESET_NAME` | `sketch-translator` | preset used for translator sessions |
| `APPCONN_DATA_DIR` | `/data` | attachment storage root (a PVC) |
| `APPCONN_MAX_LONG_EDGE_PX` | 1568 | longest PNG edge accepted |
| `APPCONN_MIN_EDGE_PX` | 200 | shortest PNG edge accepted |
| `APPCONN_MAX_PNG_BYTES` | 6291456 | PNG size cap (at most 6 MiB) |
| `APPCONN_MAX_STROKES_BYTES` | 2097152 | strokes file size cap |
| `APPCONN_MAX_UPLOAD_BYTES` | 8388608 | all parts of one upload together (at most 8 MiB) |
| `APPCONN_MAX_PAGES` | 4 | pages per upload (two files per page must fit the orchestrator limit of 8 attachments) |
| `APPCONN_MAX_META_BYTES` | 16384 | `meta` field size cap |
| `APPCONN_RATE_PER_MINUTE` | 30 | per-device request rate |
| `APPCONN_DEVICE_QUOTA_BYTES` | 536870912 | stored bytes per device |
| `APPCONN_RETENTION_DAYS` | 30 | days before files and old rows are purged |
| `APPCONN_RUN_TIMEOUT_SECONDS` | 1800 | agent run timeout |
| `APPCONN_MAX_NEW_SESSIONS` | 2 | concurrent new translator sessions |
| `APPCONN_POLL_SECONDS` | 2 | worker and event poll interval |
| `APPCONN_EVENTS_WAIT_MAX_SECONDS` | 30 | cap of the events `wait` parameter |
| `APPCONN_ADMIN_ALLOWED_CIDRS` | `127.0.0.1/32` | comma-separated source CIDRs of the admin API |
| `APPCONN_HANDOFF_PRESETS` | empty | comma-separated presets a spec may be handed to |
| `APPCONN_MAX_TEXT_CHARS` | 8000 | follow-up message length cap |
| `APPCONN_TASKBOARD_URL` | unset | base URL of the taskboard delegate listener; set together with the token or not at all |
| `APPCONN_TASKBOARD_TOKEN` | unset | delegate bearer for the task relay (not the orchestrator relay token) |

## Subcommands

- `agents-app-connector migrate` applies database migrations.
- `agents-app-connector admin` runs the admin API.
- `agents-app-connector app` runs the device API, dispatch worker, reply tracker and retention.
- `agents-app-connector purge` deletes expired data once and exits.

## Development

Run `pre-commit install` once per clone to enable the gitleaks hook.

- `mise run test` runs the test suite.
- `mise run lint` runs ruff check and format check.
- DB tests need Docker (testcontainers).
