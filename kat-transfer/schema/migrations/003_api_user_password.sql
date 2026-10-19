-- Apply once as the owner of the archive database, after 002_api_tokens.sql.
-- Adds gateway sign-in passwords; users without one can only use admin-issued tokens.
ALTER TABLE api_user
    ADD COLUMN IF NOT EXISTS password_hash TEXT;
