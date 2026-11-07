-- Notebook pages: the layout of each spec version (stable line ids, op statuses), the page
-- layouts and region images of uploads, and the regions a spec may reference. Region images
-- never expire (expires_at NULL); they are deleted with their conversation.

ALTER TABLE specs ADD COLUMN layout jsonb NULL;

ALTER TABLE attachments DROP CONSTRAINT attachments_kind_check;
ALTER TABLE attachments ADD CONSTRAINT attachments_kind_check
    CHECK (kind IN ('sketch_png', 'sketch_strokes', 'sketch_layout', 'spec_image'));
ALTER TABLE attachments ALTER COLUMN expires_at DROP NOT NULL;

CREATE TABLE spec_images (
    conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    region_id       uuid NOT NULL,
    attachment_id   uuid NOT NULL REFERENCES attachments (id) ON DELETE CASCADE,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id, region_id)
);
CREATE INDEX spec_images_attachment ON spec_images (attachment_id);

COMMENT ON TABLE spec_images IS 'writer: app';
