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
| `agents-app-connector app` | `APPCONN_MCP_PORT` | 8002 | translator tools (MCP) at `/mcp`, cluster-internal, never on an ingress |

Both serve `/healthz` and `/readyz` for probes. The OpenAPI document of both APIs is committed
as `openapi.json` (regenerate with `uv run python -m agents_app_connector.api.openapi > openapi.json`).

## Pairing

A device is paired with a short-lived 6-digit code that the operator reads on the web page and
types on the device.

1. An operator calls `POST /app/admin/v1/devices` with `{"name": "...", "plugin": "sketch"}`
   behind SSO. The response carries a 6-digit `code` once, valid for 10 minutes
   (`APPCONN_PAIRING_CODE_TTL_SECONDS`). Only its sha256 is stored. `POST
   /app/admin/v1/devices/{device_id}/code` issues a fresh code (the older one stops working)
   for a device that is not paired yet; a paired device never gets a code (no re-pairing).
2. The device sends `Authorization: Bearer <token>` on every other device request. Only the
   sha256 of the token is stored.
3. `DELETE /app/admin/v1/devices/{device_id}` revokes the device; its token then answers 401,
   and open event streams end.

A device token never authorises an admin route. The code and the token are never logged.

## API

Device API v1 (prefix `/app/v1/sketch`, bearer device token). Errors are JSON
`{"error": <code>, "detail": <text>}`. Every state-changing request carries a `client_id`
(uuid); repeating it with the same payload replays the stored answer (200), with another
payload it answers 422 `idempotency_mismatch`.

### `POST /app/v1/sketch/pair`

Public, no bearer token: exchange the 6-digit code from the Devices page for the device
token. Body `{"code": "123456"}` (exactly six digits, nothing else). 200
`{device_id, name, plugin, token}` with `Cache-Control: no-store`; the code works once and only
while it is the newest code of its device. 400 `invalid_code` for every other cause (unknown,
expired, used, superseded, revoked device, locked): one body, no oracle. 422 `invalid_request`
for a malformed body. Every failed exchange (also a 422) counts against all pending codes;
at `APPCONN_PAIRING_MAX_FAILURES` failures a code is locked for good. 429 `rate_limited` with
`Retry-After` when a client IP (`APPCONN_PAIRING_IP_PER_MINUTE`) or all clients together
(`APPCONN_PAIRING_GLOBAL_PER_MINUTE`) exceed their budget. The client IP is the socket peer, or
`X-Real-IP` when the peer is inside `APPCONN_TRUSTED_PROXY_CIDRS`.

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

`meta.annotates` (optional, needs `conversation_id`) marks the upload as handwritten notes over a
stored spec version: `{spec_version, pages: [{page, doc_page, lines: [a, b]}]}`, where `spec_version`
is at least 1, each `page` is a unique index of `meta.pages`, `doc_page` is the page of the rendered
document and `lines` the source line range (0 <= a <= b) of that page. 422 `invalid_request` when
it is malformed or has no `conversation_id`; 404 `no_spec` when that spec version does not exist
in the conversation. The field is part of the idempotency fingerprint. The agent always gets spec
version `spec_version` in full with the page images ("Annotated spec" prompt rule), on first and
follow runs alike, and returns the next version; any existing version may be annotated.

Notebook pages add, per page index `i`, an optional `layout.<i>.json` (at most
`APPCONN_MAX_LAYOUT_BYTES`) and the `region.<i>.<k>.png` parts its image ops name (k below
`APPCONN_MAX_REGIONS_PER_PAGE`). A layout is `{version: 1, geometry: {cell, row_pitch, top, rows,
text_left, margin_left}, segments: [{id: "p<page>-r<row>-<n>", row, x0, x1, strokes}], runs:
[{line_id, row, x0}], ops: [{id, kind: replace|delete|insert|image|note, target?: {line_id, from, to,
quote}, at?: {line_id, offset, mode: inline|after_line}, strokes, rect?: [x, y, w, h], region?:
{id, part, after_line}}]}` (unknown keys are 422). Stroke indices refer to `strokes.<i>.json` (no
strokes part allows none), segments, runs and rects lie inside the page canvas, op and region ids
are unique, every region part is named by exactly one image op of its page and every named part is
present (422 `invalid_request`). A `note` op needs only `rect` and a non-empty `strokes` (no target): margin ink that is not part of another op, applied by the translator to the line its arrow points at or the nearest lines, and reported applied or rejected like any op. `meta.base_version` (needs `conversation_id`, excludes
`annotates`) is the spec version whose text the board showed and is required when any layout has
ops: 409 `stale_base` when it is not the latest version, 409 `op_replayed` when an op id was
already reported by a stored version, 422 `invalid_request` for a line id that is not in that
version and 422 `stale_target` when a target's `quote` no longer equals the source text at
`[from, to)` of its line. Layouts are kept like sketch files; region images are stored for the
conversation's lifetime (never purged, deleted with the conversation), count toward the upload
size and the device quota, and are served by the images route. Layout and region parts are part
of the idempotency fingerprint; neither is attached to the translator message.

### `GET /app/v1/sketch/conversations`

The device's conversations: `[{id, title, created_at, latest_spec_version,
latest_feedback_id}]`; `latest_feedback_id` is null until the translator sent feedback.

### `POST /app/v1/sketch/conversations`

Create an empty conversation. 201 `{id}`.

### `DELETE /app/v1/sketch/conversations/{conversation_id}`

Delete a conversation with its specs, feedback and files. 204; 404 `not_found`.

### `POST /app/v1/sketch/conversations/{conversation_id}/messages`

Follow-up text `{client_id, text}` that refines the spec. 202 `{message_id}`.

### `GET /app/v1/sketch/conversations/{conversation_id}/events`

Long poll: `?after=<cursor>&wait=<seconds>`. Returns `{events: [{cursor, type, created_at,
...}], cursor}`. Event types: `sketch_status`, `spec`, `message`, `handoff`, `feedback`
(`{feedback_id, kind}`, one per item).

### `GET /app/v1/sketch/conversations/{conversation_id}/stream`

The same events as server-sent events; the SSE id is the cursor and `Last-Event-ID` resumes.

### `GET /app/v1/sketch/conversations/{conversation_id}/spec`

Latest spec: `{version, markdown, created, sketch_ids, layout}`. 404 `no_spec`. `layout` is
null unless the version was written from notebook pages (or reverted to one): `{lines:
[{line_id, source_line, origin}], ops: [{id, status: applied|rejected, note}], base_version,
reverted_from}`. Every markdown source line (1-based) has a line id `L<n>`; a kept line keeps
its id, every other line gets the next number of the conversation (never reused, also after a
revert). `origin` is `{segments}`, `{kept}`, `{op}` (several may combine) or null.

### `GET /app/v1/sketch/conversations/{conversation_id}/spec.md`

Latest spec as `text/markdown`.

### `GET /app/v1/sketch/conversations/{conversation_id}/spec/{version}`

One spec version, same shape as the latest spec.

### `POST /app/v1/sketch/conversations/{conversation_id}/spec/{version}/revert`

Revert to version N: body `{base_version}` (the latest version the app shows). Stores a copy of
version N (markdown, sketch ids and layout with its line ids unchanged; ops cleared,
`base_version` and `reverted_from` set) as the next version and appends a `spec` event. 201
`{version, reverted_from}`. 404 `no_spec` when N does not exist, 409 `stale_base` when
`base_version` is not the latest version (also a replay after success: the base check is the
idempotency), 409 `busy` while a sketch or message of the conversation is queued or being
translated, 422 when N is already the latest.

### `GET /app/v1/sketch/conversations/{conversation_id}/images/{region_id}.png`

A region image a spec references as `images/<region_id>.png`, with an `ETag` (304 on
`If-None-Match`). 404 `not_found` for an unknown region or another device's conversation.

### `GET /app/v1/sketch/conversations/{conversation_id}/feedback`

Translator feedback, kept apart from the document: `?after=<id>` returns `{items: [{id, kind,
text, where, spec_version, created_at}], cursor}`, oldest first, at most 100 per call. `kind` is
`question`, `uncertain`, `illegible` or `note`; `where` is optional free text (for example
"page 2, box Login"); `spec_version` is the version the same turn wrote, null when it wrote none.
404 `not_found`.

### `GET /app/v1/sketch/attachments/{attachment_id}`

A stored sketch file with an `ETag` (304 on `If-None-Match`). 410 `purged` after retention.

### `GET /app/v1/sketch/handoff/targets`

Additive route: `{sessions: [{id, slug, preset_name}], presets: [name]}`, the active sessions
and the allowed presets a spec can be handed to. 502 or 503 when the orchestrator fails.

### `POST /app/v1/sketch/conversations/{conversation_id}/handoff`

Hand the latest spec to a session: body `{client_id, session: "<slug>" | {"preset": "<name>"}}`.
202 `{session_slug}` plus the additive `handoff_id`; progress arrives as `handoff` events.
404 `unknown_session`, 409 `no_spec`, 422 `preset_not_allowed`. The region images the spec
references as `images/<id>.png` are attached to the message as `<id>.png`, in document order, at
most 8 and 8 MiB per message (the orchestrator's caps); the rest are listed by name in the text.

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

Create a device and its first pairing code: `{name, plugin}`. 201
`{device_id, name, plugin, code, expires_at, expires_in}` (`code` is 6 digits, shown once,
`expires_in` in seconds). 429 `too_many_pending` when `APPCONN_PAIRING_MAX_PENDING` other
devices already wait for a code (nothing is created).

### `POST /app/admin/v1/devices/{device_id}/code`

A new code for a device that has no token yet; the older code stops working. 201
`{code, expires_at, expires_in}`; 404 `not_found` for an unknown or revoked device; 409
`already_paired` when the device already has a token; 429 `too_many_pending`.

### `GET /app/admin/v1/devices`

List devices: `[{device_id, name, plugin, state, created_at, revoked_at, paired_at, paired_ip,
code_status, code_expires_at}]`. `state` is `pending` (no token yet), `active` or `revoked`;
`code_status` is the newest code's status (`pending`, `redeemed`, `expired`, `superseded`,
`locked`) or null. The code itself is never listed.

### `DELETE /app/admin/v1/devices/{device_id}`

Revoke a device. 204; 404 `not_found` for an unknown or already revoked device.

## Translator tools

The app process also serves an MCP server (streamable HTTP, stateless, `POST /mcp` on
`APPCONN_MCP_PORT`) for the sketch-translator sessions. The preset points the translator at it
through `wrapper.mcp_servers` with the session's own orchestrator agent token as bearer
(`Authorization: Bearer ${ORCH_AGENT_TOKEN}`) and its session id in `X-Orch-Session:
${ORCH_SESSION_ID}`. The connector verifies the bearer against that one session with `POST
/api/v1/sessions/{id}/agent-token/verify` on the orchestrator service port (its own service token,
the token in the body; 404 means the session does not own the token; cached 60 s by the token's
sha256 and the session id; the token is never logged or stored), then maps the session to the
in-progress turn of the conversation bound to it. A missing `X-Orch-Session` header (no fallback),
a malformed one or a refused bearer answers 401, an unreachable orchestrator 503.

- `document_write(markdown, lines?, ops?)` stores the next spec version of the conversation: the
  document is a faithful transcription of the page and nothing else. Each call is a new version.
  Errors: `no_active_turn`, `empty_document`, `document_too_large` (`APPCONN_MAX_DOCUMENT_BYTES`),
  `unknown_image` (an `images/<id>.png` reference that is not a region image of the
  conversation). For a turn whose upload has layouts, `lines` (`[{n, segments?, kept?, op?}]`,
  the provenance of source line n) and `ops` (`[{id, status: applied|rejected, note?}]`) must
  cover every segment id and op id of the upload (`layout_incomplete` naming the missing ids, so
  the model retries in the same turn; `layout_invalid` for malformed entries, ids not in the
  upload or a kept id not in the base version); the version stores the layout with
  connector-assigned line ids. On other turns non-empty `lines` or `ops` are
  `layout_not_expected`.
- `feedback_add(kind, text, where?)` stores one feedback item (`question`, `uncertain`,
  `illegible` or `note`; text up to 2000 characters, where up to 200) and appends a `feedback`
  event. Errors: `no_active_turn`, `empty_text`, `text_too_long`, `where_too_long`,
  `feedback_limit` (`APPCONN_MAX_FEEDBACK_PER_RUN` per turn).

The server checks structure only (sizes, kinds, caps, own conversation); it cannot tell
transcription from invention, so the prompt and the eval hold the content contract.

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
| `APPCONN_PAIRING_CODE_TTL_SECONDS` | 600 | life of a pairing code (60 to 1800) |
| `APPCONN_PAIRING_MAX_FAILURES` | 5 | failed exchanges that lock a pending code (1 to 10) |
| `APPCONN_PAIRING_MAX_PENDING` | 3 | devices that may wait for a code at once (1 to 20) |
| `APPCONN_PAIRING_IP_PER_MINUTE` | 5 | pairing exchanges per client IP per minute |
| `APPCONN_PAIRING_GLOBAL_PER_MINUTE` | 30 | pairing exchanges per minute for all clients |
| `APPCONN_TRUSTED_PROXY_CIDRS` | empty | source CIDRs of the ingress whose `X-Real-IP` names the client IP |
| `APPCONN_TASKBOARD_URL` | unset | base URL of the taskboard delegate listener; set together with the token or not at all |
| `APPCONN_TASKBOARD_TOKEN` | unset | delegate bearer for the task relay (not the orchestrator relay token) |
| `APPCONN_MCP_PORT` | 8002 | translator tools port, must differ from the admin and device ports |
| `APPCONN_MAX_DOCUMENT_BYTES` | 262144 | `document_write` size cap (1024 to 1048576) |
| `APPCONN_MAX_FEEDBACK_PER_RUN` | 30 | `feedback_add` items per turn (1 to 200) |
| `APPCONN_MAX_REGIONS_PER_PAGE` | 4 | `region.<i>.<k>.png` parts per notebook page (0 to 16) |
| `APPCONN_MAX_LAYOUT_BYTES` | 262144 | size cap of one `layout.<i>.json` part (1024 to 1048576) |

## Subcommands

- `agents-app-connector migrate` applies database migrations.
- `agents-app-connector admin` runs the admin API.
- `agents-app-connector app` runs the device API, translator tools, dispatch worker, reply
  tracker and retention.
- `agents-app-connector purge` deletes expired data once and exits.

## Development

Run `pre-commit install` once per clone to enable the gitleaks hook.

- `mise run test` runs the test suite.
- `mise run lint` runs ruff check and format check.
- DB tests need Docker (testcontainers).
