-- Translator feedback (questions, uncertain or illegible readings, notes) kept apart from the
-- document, and the run that wrote each spec version. Both are written by the app: the
-- translator's MCP tools run inside the app process.

CREATE TABLE feedback (
    id              bigserial PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    run_id          bigint NOT NULL,
    inbound_id      uuid NULL,
    spec_version    int NULL,
    kind            text NOT NULL CHECK (kind IN ('question', 'uncertain', 'illegible', 'note')),
    text            text NOT NULL CHECK (length(text) BETWEEN 1 AND 2000),
    where_text      text NULL CHECK (where_text IS NULL OR length(where_text) <= 200),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX feedback_conversation ON feedback (conversation_id, id);
CREATE INDEX feedback_run ON feedback (run_id);

ALTER TABLE specs ADD COLUMN run_id bigint NULL;

ALTER TABLE events DROP CONSTRAINT events_type_check;
ALTER TABLE events ADD CONSTRAINT events_type_check
    CHECK (type IN ('sketch_status', 'spec', 'message', 'handoff', 'feedback'));

COMMENT ON TABLE feedback IS 'writer: app';
