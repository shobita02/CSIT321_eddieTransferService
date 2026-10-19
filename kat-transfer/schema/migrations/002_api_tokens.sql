-- Apply once as the owner of the existing archive database.
-- This migration preserves all existing archive and transfer-log rows.
CREATE TABLE IF NOT EXISTS api_user (
    username    VARCHAR(64) PRIMARY KEY,
    max_role    VARCHAR(8) NOT NULL CHECK (max_role IN ('read', 'write')),
    active      BOOLEAN NOT NULL DEFAULT true,
    granted_by  VARCHAR(64) NOT NULL,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS api_token (
    id           BIGSERIAL PRIMARY KEY,
    username     VARCHAR(64) NOT NULL REFERENCES api_user (username),
    role         VARCHAR(8) NOT NULL CHECK (role IN ('read', 'write')),
    token_hash   CHAR(64) NOT NULL UNIQUE,
    token_prefix VARCHAR(8) NOT NULL,
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ
);

ALTER TABLE transfer_log
    ADD COLUMN IF NOT EXISTS api_token_id BIGINT REFERENCES api_token (id);
