-- Remote archive database (PostgreSQL) --------------------------------------
-- Target schema on the Bored Owl development server (192.168.40.100).
-- Run once as the owner of the target database:
--     psql -h 192.168.40.100 -U <you> -d csit321 -f schema/remote_postgres.sql
--
-- Design notes
--
--  1. Unquoted identifiers are folded to lower case by PostgreSQL, so KAT's
--     `ExperimentalData` becomes `experimentaldata`. We use explicit
--     snake_case names here so nobody has to guess.
--
--  2. `row` is a reserved word in PostgreSQL too, but unlike MySQL it is
--     usable as a column name when double-quoted. We instead name it
--     `row_no` remotely and map it in the transfer service, so no query in
--     the API layer ever has to remember to quote an identifier.
--
--  3. `environment` is keyed on the timestamp, together with systemid: the
--     archive collects from several rigs, and two rigs sampling at the same
--     instant must not overwrite each other. Samples with identical data at
--     different instants are separate rows and are never collapsed. KAT's own
--     Environment table has no unique constraint, so IF it ever holds two
--     rows for the same system and instant, the later one wins here.
--
--  4. Provenance columns (`origin`, `src_host`, `ingested_at`) implement the
--     meeting action "need to add transferred, real/fake data, source to
--     database as columns" (11/09/26, item 2).
--
--  5. `data` is `json`, not `jsonb`. `json` validates the payload but stores
--     the text exactly as KAT wrote it, so tools/verify_transfer.py can compare
--     local and remote payloads byte for byte. `jsonb` would reorder keys.

CREATE TABLE IF NOT EXISTS experiment (
    systemid     VARCHAR(20)  NOT NULL,
    run          INTEGER      NOT NULL,
    description  TEXT,
    origin       VARCHAR(8)   NOT NULL DEFAULT 'real',   -- 'real' | 'fake'
    src_host     VARCHAR(64),
    ingested_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (systemid, run),
    CONSTRAINT experiment_origin_ck CHECK (origin IN ('real', 'fake'))
);

CREATE TABLE IF NOT EXISTS experimental_data (
    systemid     VARCHAR(20)  NOT NULL,
    run          INTEGER      NOT NULL,
    row_no       INTEGER      NOT NULL,
    ts           TIMESTAMPTZ,
    source       VARCHAR(20),
    data         JSON,
    xfer         TIMESTAMPTZ,                            -- as stamped locally
    origin       VARCHAR(8)   NOT NULL DEFAULT 'real',
    src_host     VARCHAR(64),
    ingested_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (systemid, run, row_no),
    CONSTRAINT experimental_data_origin_ck CHECK (origin IN ('real', 'fake')),
    CONSTRAINT experimental_data_run_fk
        FOREIGN KEY (systemid, run) REFERENCES experiment (systemid, run)
);

CREATE TABLE IF NOT EXISTS environment (
    systemid     VARCHAR(20)  NOT NULL,
    ts           TIMESTAMPTZ  NOT NULL,
    data         JSON,
    xfer         TIMESTAMPTZ,
    origin       VARCHAR(8)   NOT NULL DEFAULT 'real',
    src_host     VARCHAR(64),
    ingested_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (systemid, ts),                          -- see note 3
    CONSTRAINT environment_origin_ck CHECK (origin IN ('real', 'fake'))
);

-- Query patterns the API will use: "give me run N", "give me environment
-- between two instants". Both are range scans; served by their primary keys.
CREATE INDEX IF NOT EXISTS idx_expdata_ts       ON experimental_data (ts);

-- Transfer audit log. One row per batch the service commits remotely; this is
-- what you point at when someone asks "did last Tuesday's run make it across?"
CREATE TABLE IF NOT EXISTS transfer_log (
    id           BIGSERIAL    PRIMARY KEY,
    started_at   TIMESTAMPTZ  NOT NULL,
    finished_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    src_host     VARCHAR(64),
    table_name   VARCHAR(32)  NOT NULL,
    rows_sent    INTEGER      NOT NULL,
    rows_applied INTEGER      NOT NULL,
    trigger      VARCHAR(16)  NOT NULL,                  -- 'zmq' | 'sweep' | 'manual'
    ok           BOOLEAN      NOT NULL,
    detail       TEXT
);
