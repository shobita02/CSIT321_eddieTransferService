-- Local KAT database (SQLite) -----------------------------------------------
-- Transcribed verbatim from the KAT source constants CREATE_EXPERIMENT,
-- CREATE_EXPERIMENTAL_DATA and CREATE_ENVIRONMENT.
--
-- Do not "improve" this file. It has to stay byte-for-byte compatible with the
-- database KAT creates on the student's machine, otherwise the transfer service
-- is testing against a schema that does not exist in the real world.
-- Note: `row` is a SQLite keyword in some contexts, hence the quoting in our
-- queries. It is also the reason PostgreSQL was chosen over MySQL (meeting
-- 11/09/26, item 4).

CREATE TABLE IF NOT EXISTS Experiment (
    systemid    CHAR(20),
    run         INTEGER,
    description TEXT,
    UNIQUE (systemid, run)
);

CREATE TABLE IF NOT EXISTS ExperimentalData (
    systemid  CHAR(20),
    run       INTEGER,
    row       INTEGER,
    timestamp TIMESTAMP,
    source    CHAR(20),
    data      TEXT,
    xfer      TIMESTAMP,
    UNIQUE (systemid, run, row),
    FOREIGN KEY (systemid, run) REFERENCES Experiment(systemid, run)
);

CREATE TABLE IF NOT EXISTS Environment (
    systemid  CHAR(20),
    timestamp TIMESTAMP,
    data      TEXT,
    xfer      TIMESTAMP
);

-- Indexes -------------------------------------------------------------------
-- NOT part of KAT's schema. These are ours, created separately by
-- tools/make_fake_db.py, because the transfer service's hot query is
-- "WHERE xfer IS NULL". Without them every sweep is a full table scan.
-- Flag these to Jonathan before go-live: they are safe to add to the real KAT
-- database (they change no semantics) but he should be the one to approve it.
CREATE INDEX IF NOT EXISTS idx_expdata_xfer ON ExperimentalData (xfer)
    WHERE xfer IS NULL;
CREATE INDEX IF NOT EXISTS idx_env_xfer ON Environment (xfer)
    WHERE xfer IS NULL;
