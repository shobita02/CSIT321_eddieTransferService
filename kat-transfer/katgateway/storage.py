"""PostgreSQL access owned exclusively by the API server."""

from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

from .config import DatabaseConfig
from .models import Receipt


class ArchiveStore:
    def __init__(self, cfg: DatabaseConfig):
        self.cfg = cfg

    @contextmanager
    def connection(self):
        conn = psycopg2.connect(
            host=self.cfg.host, port=self.cfg.port, dbname=self.cfg.database,
            user=self.cfg.user, password=self.cfg.password, sslmode=self.cfg.sslmode,
            connect_timeout=self.cfg.connect_timeout_s, application_name="kat-gateway",
        )
        try:
            # Commit on success, rollback on any exception. Closing is separate:
            # psycopg2's transaction context does not close its connection.
            with conn:
                yield conn
        finally:
            conn.close()

    def write_batch(self, table, batch):
        if table == "experiment":
            sql = """
                INSERT INTO experiment (systemid, run, description, origin, src_host)
                VALUES %s ON CONFLICT (systemid, run) DO UPDATE SET
                description=EXCLUDED.description, origin=EXCLUDED.origin, src_host=EXCLUDED.src_host
            """
            keyed = {(r.systemid, r.run): (r.systemid, r.run, r.description, batch.origin, batch.src_host)
                     for r in batch.rows}
        elif table == "experimental_data":
            sql = """
                INSERT INTO experimental_data
                    (systemid, run, row_no, ts, source, data, xfer, origin, src_host)
                VALUES %s ON CONFLICT (systemid, run, row_no) DO UPDATE SET
                ts=EXCLUDED.ts, source=EXCLUDED.source, data=EXCLUDED.data, xfer=EXCLUDED.xfer,
                origin=EXCLUDED.origin, src_host=EXCLUDED.src_host
            """
            keyed = {(r.systemid, r.run, r.row_no):
                     (r.systemid, r.run, r.row_no, r.ts, r.source, r.data, batch.xfer, batch.origin, batch.src_host)
                     for r in batch.rows}
        elif table == "environment":
            sql = """
                INSERT INTO environment (systemid, ts, data, xfer, origin, src_host)
                VALUES %s ON CONFLICT (systemid, ts) DO UPDATE SET
                data=EXCLUDED.data, xfer=EXCLUDED.xfer, origin=EXCLUDED.origin, src_host=EXCLUDED.src_host
            """
            # Preserve the established archive rule: repeated rig/timestamp
            # samples in one batch keep the last row, and every source row is acknowledged.
            keyed = {(r.systemid, r.ts): (r.systemid, r.ts, r.data, batch.xfer, batch.origin, batch.src_host)
                     for r in batch.rows}
        else:
            raise ValueError("Unknown archive table")

        values = list(keyed.values())
        with self.connection() as conn:
            with conn.cursor() as cur:
                # One page avoids psycopg2's rowcount only describing its last page.
                execute_values(cur, sql, values, page_size=1000)
                applied = cur.rowcount
                if table != "experiment":
                    cur.execute(
                        """INSERT INTO transfer_log
                        (started_at, src_host, table_name, rows_sent, rows_applied, trigger, ok)
                        VALUES (%s, %s, %s, %s, %s, %s, TRUE)""",
                        (batch.started_at, batch.src_host, table, len(batch.rows), applied, batch.trigger),
                    )
        # The transaction has committed before this receipt is constructed.
        return Receipt(sent=len(batch.rows), applied=applied)

    def counts(self):
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT
                    (SELECT COUNT(*) FROM experiment),
                    (SELECT COUNT(*) FROM experimental_data),
                    (SELECT COUNT(*) FROM environment)""")
                return dict(zip(("experiment", "experimental_data", "environment"), cur.fetchone()))

    def records(self, table, *, limit=100, offset=0, systemid=None, run=None,
                origin=None, from_ts=None, to_ts=None):
        # Table names, columns and ordering come from this whitelist; all user
        # values are SQL parameters, including pagination and filters.
        queries = {
            "experiment": ("systemid, run, description, origin, src_host, ingested_at", "systemid, run"),
            "experimental_data": ("systemid, run, row_no, ts, source, data::text AS data, xfer, origin, src_host, ingested_at",
                                  "systemid, run, row_no"),
            "environment": ("systemid, ts, data::text AS data, xfer, origin, src_host, ingested_at", "systemid, ts"),
            "transfer_log": ("id, started_at, finished_at, src_host, table_name, rows_sent, rows_applied, trigger, ok", "id"),
        }
        columns, order = queries[table]
        clauses, params = [], []
        for name, value in (("systemid", systemid), ("run", run), ("origin", origin)):
            if value is not None:
                clauses.append(f"{name} = %s")
                params.append(value)
        for operator, value in ((">=", from_ts), ("<=", to_ts)):
            if value is not None:
                clauses.append(f"ts {operator} %s")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"SELECT {columns} FROM {table}{where} ORDER BY {order} LIMIT %s OFFSET %s",
                            (*params, limit, offset))
                return [dict(row) for row in cur.fetchall()]
