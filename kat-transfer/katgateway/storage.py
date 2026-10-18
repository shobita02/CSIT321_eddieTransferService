"""PostgreSQL access owned exclusively by the API server."""

from contextlib import contextmanager
import hashlib
import secrets

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

from .config import DatabaseConfig
from .models import Receipt


class ArchiveStore:
    def __init__(self, cfg: DatabaseConfig):
        self.cfg = cfg

    def find_token(self, token_hash):
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """WITH matched AS MATERIALIZED (
                           SELECT t.id, t.username, t.role, u.max_role
                           FROM api_token t
                           JOIN api_user u ON u.username = t.username
                           WHERE t.token_hash = %s AND t.revoked_at IS NULL
                             AND (t.expires_at IS NULL OR t.expires_at > now())
                             AND u.active
                       ), touched AS (
                           UPDATE api_token t SET last_used_at = now()
                           FROM matched m
                           WHERE t.id = m.id
                             AND (t.last_used_at IS NULL OR t.last_used_at < now() - interval '1 minute')
                           RETURNING t.id
                       )
                       SELECT id, username, role, max_role FROM matched""",
                    (token_hash,),
                )
                return cur.fetchone()

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

    def write_batch(self, table, batch, token_id):
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
                cur.execute(
                    """INSERT INTO transfer_log
                    (started_at, src_host, table_name, rows_sent, rows_applied, trigger, ok, api_token_id)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s)""",
                    (batch.started_at, batch.src_host, table, len(batch.rows), applied, batch.trigger, token_id),
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
            "transfer_log": (
                "transfer_log.id, started_at, finished_at, src_host, table_name, rows_sent, rows_applied, "
                "trigger, ok, api_token_id, api_user.username AS uploaded_by",
                "transfer_log.id",
                "transfer_log LEFT JOIN api_token ON api_token.id = transfer_log.api_token_id "
                "LEFT JOIN api_user ON api_user.username = api_token.username",
            ),
        }
        query = queries[table]
        columns, order = query[:2]
        from_clause = query[2] if len(query) == 3 else table
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
                cur.execute(f"SELECT {columns} FROM {from_clause}{where} ORDER BY {order} LIMIT %s OFFSET %s",
                            (*params, limit, offset))
                return [dict(row) for row in cur.fetchall()]

    def add_user(self, username, role, granted_by, note=None):
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO api_user (username, max_role, granted_by, note)
                       VALUES (%s, %s, %s, %s)""",
                    (username, role, granted_by, note),
                )

    def set_user_role(self, username, role, granted_by):
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE api_user SET max_role = %s, granted_by = %s
                       WHERE username = %s AND active""",
                    (role, granted_by, username),
                )
                return cur.rowcount == 1

    def disable_user(self, username):
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE api_user SET active = FALSE, disabled_at = now()
                       WHERE username = %s AND active""",
                    (username,),
                )
                if cur.rowcount != 1:
                    return False
                cur.execute(
                    """UPDATE api_token SET revoked_at = now()
                       WHERE username = %s AND revoked_at IS NULL""",
                    (username,),
                )
                return True

    def create_token(self, username, role, expires_days=90, note=None):
        if role not in ("read", "write") or not 1 <= expires_days <= 90:
            raise ValueError("Token role must be read/write and expiry must be 1 to 90 days")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO api_token (username, role, token_hash, token_prefix, note, expires_at)
                       SELECT username, %s, %s, %s, %s, now() + (%s * interval '1 day')
                       FROM api_user
                       WHERE username = %s AND active
                         AND (max_role = 'write' OR %s = 'read')
                       RETURNING id""",
                    (role, token_hash, token[:8], note, expires_days, username, role),
                )
                row = cur.fetchone()
                if row is None:
                    raise ValueError("User is inactive, unknown, or not permitted to hold this role")
        return row[0], token

    def list_tokens(self):
        with self.connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT id, token_prefix, username, role, note, created_at, expires_at,
                              last_used_at, revoked_at
                       FROM api_token ORDER BY id"""
                )
                return [dict(row) for row in cur.fetchall()]

    def revoke_token(self, selector):
        with self.connection() as conn:
            with conn.cursor() as cur:
                if selector.isdecimal():
                    cur.execute("SELECT id FROM api_token WHERE id = %s AND revoked_at IS NULL",
                                (int(selector),))
                else:
                    if (not selector or len(selector) > 8
                            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                                   for char in selector)):
                        raise ValueError("Token prefix must contain 1 to 8 token characters")
                    cur.execute(
                        """SELECT id FROM api_token
                           WHERE left(token_prefix, %s) = %s AND revoked_at IS NULL
                           ORDER BY id LIMIT 2""",
                        (len(selector), selector),
                    )
                rows = cur.fetchall()
                if len(rows) > 1:
                    raise ValueError("Token prefix is ambiguous; use a longer prefix or id")
                if not rows:
                    return False
                cur.execute("UPDATE api_token SET revoked_at = now() WHERE id = %s", (rows[0][0],))
                return True
