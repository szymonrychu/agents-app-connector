-- 6-digit pairing codes. The admin creates devices and codes, the public app redeems a code and
-- writes the device token hash, so the hash moves out of devices (writer: admin) into
-- device_tokens (writer: app). Existing tokens keep working.

CREATE TABLE device_tokens (
    device_id    uuid PRIMARY KEY REFERENCES devices (id),
    token_sha256 text NOT NULL UNIQUE CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    issued_at    timestamptz NOT NULL DEFAULT now()
);

INSERT INTO device_tokens (device_id, token_sha256, issued_at)
SELECT id, token_sha256, created_at FROM devices;

ALTER TABLE devices DROP COLUMN token_sha256;

CREATE TABLE pairing_codes (
    id              uuid PRIMARY KEY,
    device_id       uuid NOT NULL REFERENCES devices (id),
    code_sha256     text NOT NULL CHECK (code_sha256 ~ '^[0-9a-f]{64}$'),
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'redeemed', 'expired', 'superseded', 'locked')),
    failed_attempts int NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    redeemed_at     timestamptz NULL,
    redeemed_ip     text NULL
);
CREATE INDEX pairing_codes_pending ON pairing_codes (code_sha256) WHERE status = 'pending';
CREATE INDEX pairing_codes_device ON pairing_codes (device_id, created_at DESC);

COMMENT ON TABLE device_tokens IS 'writer: app';
COMMENT ON TABLE pairing_codes IS 'queue: insert by admin, transitions by app';
