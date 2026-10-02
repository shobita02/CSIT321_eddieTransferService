-- Remote archive database (PostgreSQL) --------------------------------------
-- Target schema on the Bored Owl development server (192.168.40.100).
-- Run once as the owner of the target database:
--     psql -h 192.168.40.100 -U <you> -d csit321 -f schema/remote_postgres.sql
--
-- Design notes (worth walking through with Jonathan):
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
--  3. `Environment` has no unique constraint in KAT, so there is no natural
--     key to upsert against. We derive `digest` = sha256(systemid|timestamp|
--     data) in the transfer client and make that the conflict target. Two
--     byte-identical environment samples for the same system at the same
--     instant are therefore collapsed into one remote row. That is almost
--     certainly the desired behaviour, but it IS a behaviour change and
--     Jonathan should sign off on it. The alternative is asking for a UNIQUE
--     (systemid, timestamp) on the KAT side.
--
--  4. Provenance columns (`origin`, `src_host`, `ingested_at`) implement the
--     meeting action "need to add transferred, real/fake data, source to
--     database as columns" (11/09/26, item 2).

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
    data         TEXT,
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
    digest       CHAR(64)     NOT NULL,                  -- sha256, see note 3
    systemid     VARCHAR(20)  NOT NULL,
    ts           TIMESTAMPTZ,
    data         TEXT,
    xfer         TIMESTAMPTZ,
    origin       VARCHAR(8)   NOT NULL DEFAULT 'real',
    src_host     VARCHAR(64),
    ingested_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (digest),
    CONSTRAINT environment_origin_ck CHECK (origin IN ('real', 'fake'))
);

-- Query patterns the API will use: "give me run N", "give me environment
-- between two instants". Both are range scans.
CREATE INDEX IF NOT EXISTS idx_expdata_run      ON experimental_data (systemid, run, row_no);
CREATE INDEX IF NOT EXISTS idx_expdata_ts       ON experimental_data (ts);
CREATE INDEX IF NOT EXISTS idx_env_system_ts    ON environment (systemid, ts);

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
