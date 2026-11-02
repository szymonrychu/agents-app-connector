-- agents-app-connector schema. Every table has exactly one writer component (see COMMENT ON
-- TABLE). inbound and handoffs are queue tables: rows are inserted by the app api and change
-- status only through status-guarded UPDATEs by the app worker.

CREATE TABLE devices (
    id           uuid PRIMARY KEY,
    plugin       text NOT NULL,
    name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    token_sha256 text NOT NULL UNIQUE CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    created_at   timestamptz NOT NULL DEFAULT now(),
    revoked_at   timestamptz NULL
);

CREATE TABLE conversations (
    id         uuid PRIMARY KEY,
    device_id  uuid NOT NULL REFERENCES devices (id),
    plugin     text NOT NULL,
    title      text NULL,
    state      text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'deleted')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_device_open ON conversations (device_id, created_at DESC)
    WHERE state = 'open';

CREATE TABLE inbound (
    id              uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    device_id       uuid NOT NULL REFERENCES devices (id),
    client_id       uuid NOT NULL,
    fingerprint     text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('sketch', 'message')),
    text            text NULL,
    meta            jsonb NOT NULL DEFAULT '{}',
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'in-progress', 'handled', 'failed')),
    reason          text NULL,
    run_id          bigint NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (device_id, client_id)
);
CREATE INDEX inbound_queued ON inbound (conversation_id, created_at) WHERE status = 'queued';

CREATE TABLE attachments (
    id              uuid PRIMARY KEY,
    inbound_id      uuid NOT NULL REFERENCES inbound (id) ON DELETE CASCADE,
    conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    device_id       uuid NOT NULL REFERENCES devices (id),
    kind            text NOT NULL CHECK (kind IN ('sketch_png', 'sketch_strokes')),
    mime            text NOT NULL,
    size            bigint NOT NULL,
    sha256          text NOT NULL,
    path            text NOT NULL,
    width           int NULL,
    height          int NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    purged_at       timestamptz NULL
);
CREATE INDEX attachments_inbound ON attachments (inbound_id);
CREATE INDEX attachments_expiry ON attachments (expires_at) WHERE purged_at IS NULL;

CREATE TABLE handoffs (
    id               uuid PRIMARY KEY,
    conversation_id  uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    device_id        uuid NOT NULL REFERENCES devices (id),
    client_id        uuid NOT NULL,
    fingerprint      text NOT NULL,
    spec_version     int NOT NULL,
    target_session_id uuid NULL,
    target_preset    text NULL,
    status           text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'in-progress', 'handled', 'failed')),
    reason           text NULL,
    run_id           bigint NULL,
    session_slug     text NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (device_id, client_id),
    CHECK ((target_session_id IS NULL) <> (target_preset IS NULL))
);
CREATE INDEX handoffs_queued ON handoffs (created_at) WHERE status = 'queued';

CREATE TABLE runs (
    id              bigserial PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    inbound_id      uuid NULL,
    handoff_id      uuid NULL,
    kind            text NOT NULL CHECK (kind IN ('first', 'follow', 'handoff')),
    preset_id       uuid NULL,
    slug            text NULL,
    session_id      uuid NULL,
    message_id      text NULL,
    status          text NOT NULL DEFAULT 'in-progress'
                    CHECK (status IN ('in-progress', 'handled', 'failed')),
    reason          text NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz NULL
);
CREATE INDEX runs_in_progress ON runs (id) WHERE status = 'in-progress';
CREATE INDEX runs_conversation ON runs (conversation_id, id);

CREATE TABLE bindings (
    id              bigserial PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    run_id          bigint NULL,
    session_id      uuid NULL,
    slug            text NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    released_at     timestamptz NULL,
    reason          text NULL
);
CREATE UNIQUE INDEX bindings_one_active ON bindings (conversation_id)
    WHERE released_at IS NULL;

CREATE TABLE holds (
    conversation_id uuid PRIMARY KEY REFERENCES conversations (id) ON DELETE CASCADE,
    until           timestamptz NOT NULL,
    reason          text NULL
);

CREATE TABLE specs (
    conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    version         int NOT NULL,
    markdown        text NOT NULL,
    inbound_id      uuid NULL,
    sketch_ids      uuid[] NOT NULL DEFAULT '{}',
    front_matter    boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id, version)
);

CREATE TABLE events (
    id              bigserial PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    type            text NOT NULL CHECK (type IN ('sketch_status', 'spec', 'message', 'handoff')),
    payload         jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX events_conversation ON events (conversation_id, id);

COMMENT ON TABLE devices IS 'writer: admin';
COMMENT ON TABLE conversations IS 'writer: app';
COMMENT ON TABLE inbound IS 'queue: insert by app api, transitions by app worker';
COMMENT ON TABLE attachments IS 'writer: app';
COMMENT ON TABLE handoffs IS 'queue: insert by app api, transitions by app worker';
COMMENT ON TABLE runs IS 'writer: app';
COMMENT ON TABLE bindings IS 'writer: app';
COMMENT ON TABLE holds IS 'writer: app';
COMMENT ON TABLE specs IS 'writer: app';
COMMENT ON TABLE events IS 'writer: app';
