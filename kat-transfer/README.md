# kat-transfer

The **CSIT321 Transfer Client** from the Eddie System architecture diagram: it
watches the KAT SQLite database on a local machine, and moves new experimental
and environment data up to the archive on the Bored Owl development server.

```
  Desktop KAT ──0MQ PUB/SUB──► kat-transfer ──► NGINX ──► CSIT321 Gateway ──► Experimental Data DB
       │                            ▲                                          (PostgreSQL, 192.168.40.100)
       └──► KAT SQLite DB ──────────┘
            (we read this)
```

Right now the right-hand side is a PostgreSQL 16 container on your own machine
(`docker-compose.yml`), built from the same schema as the real server, so the
whole pipeline runs on one laptop with no VPN and no credentials. Switching to
the real server is a config change, not a code change.

## Quick start

```bash
python3 -m pip install -r requirements.txt
docker compose up -d                      # the remote archive (PostgreSQL 16)

python3 tools/make_fake_db.py --reset     # build a fake KAT database
python3 -m katxfer.service --status       # what is pending?
python3 -m katxfer.service --once         # transfer it
python3 tools/verify_transfer.py          # prove it arrived intact
```

Use `python3 -m pip install`, not plain `pip install`. On a machine with more
than one Python — which most Windows machines have — bare `pip` often belongs to
a different installation than the one running your scripts, and you get a
package that is installed but not importable. Running pip *through* the same
interpreter you launch the service with avoids that entirely.

On Windows use `python` wherever this README says `python3`, and keep it
consistent: `python -m pip install -r requirements.txt`, then
`python -m katxfer.service --once`.

That seeds 12,000 experimental rows plus 800 environment samples and ships them
in about a third of a second.

### If something goes wrong

Run this first — it prints which Python you are on, which dependencies *that*
interpreter can see, and the config paths it resolved:

```bash
python3 -m katxfer.service --doctor
```

**`ModuleNotFoundError: No module named 'zmq'`, but `pip install pyzmq` says
"Requirement already satisfied"** — the classic two-Pythons problem. pip
installed into one interpreter, your script is running under another. `--doctor`
prints the path of the interpreter actually running, and the exact command to
install into it:

```bash
python3 -m pip install pyzmq      # installs into the Python you just ran
```

A virtual environment prevents this permanently, and is worth doing if several
people are working on this:

```bash
python3 -m venv .venv
.venv\Scripts\activate            # Windows
source .venv/bin/activate         # macOS / Linux
python -m pip install -r requirements.txt
```

**`ModuleNotFoundError: No module named 'psycopg2'`** — only needed when
`remote.kind = "postgres"`. `python3 -m pip install psycopg2-binary`.

**`No config at ...`** — run the commands from the `kat-transfer` folder that
contains `config.toml`, or pass `-c path/to/config.toml`. Relative paths inside
the config resolve against the config file's own folder, not your shell's
working directory.

**`No KAT database at ...`** — run `python3 tools/make_fake_db.py --reset` first.

**`connection to server at "127.0.0.1", port 5432 failed`** — the Postgres
container is not running. Start Docker Desktop, then `docker compose up -d`
from the `kat-transfer` folder.

**`unable to open database file`** — usually a `local.path` pointing somewhere
that does not exist. `python3 -m katxfer.service --status` prints the paths it
resolved, which is the quickest way to see what it actually read.

### Running against the real KAT database

`config.toml` points at `~/katdata.db`, the file KAT 1.0.3 creates in your home
folder, and sends to the local Postgres container (`docker compose up -d`). 0MQ is switched off (`[zmq] enabled = false`) because KAT 1.0.3 has no
publisher yet (see [docs/zeromq.md](docs/zeromq.md)), so transfers are
triggered by hand:

```bash
python tools/show_state.py             # both databases, with the xfer column
python -m katxfer.service --once       # transfer everything pending, then exit
python tools/show_state.py             # xfer is now stamped; rows are remote
python tools/verify_transfer.py        # row-by-row check: no loss, no corruption
```

The service writes to `katdata.db` only to set `xfer` on rows once the remote
has them, which is what that column is for. To make every row look
untransferred again while testing:
`UPDATE ExperimentalData SET xfer = NULL; UPDATE Environment SET xfer = NULL;`
— the remote upserts, so re-sending never duplicates.

To empty the remote archive for a clean demo:
`docker compose down -v` then `docker compose up -d` (the schema reloads
automatically). To look at it directly:
`docker exec -it kat-remote-pg psql -U kat -d csit321`.

To go back to the generated fake database, set `local.path =
"data/kat.sqlite"`.

### The live version, with notifications

Three terminals:

```bash
python3 tools/kat_simulator.py --rate 20 --duration 60   # 1: pretends to be KAT
python3 -m katxfer.service -v                            # 2: the transfer service
python3 tools/zmq_listen.py                              # 3: watch the 0MQ traffic
```

Terminal 2 will show `trigger=zmq` as notifications arrive, and `trigger=sweep`
when the safety-net timer fires instead.

## How it works

The whole design rests on the `xfer` column that is already in your supervisor's
schema. A row with `xfer IS NULL` has not been transferred; that is the entire
state of the system, and it lives in the database rather than in the service's
memory or in a message queue.

```
wait for a 0MQ notification, or for the sweep timer
  └─► take a batch of rows WHERE xfer IS NULL
      └─► upsert them into the remote
          └─► only then, stamp xfer locally
```

Four properties follow, and `tests/test_transfer.py` asserts each one:

- **Nothing is missed.** The service asks the database what is pending, so it
  does not matter how many notifications were lost, or whether the service was
  even running when the data was written.
- **Nothing is duplicated.** Every remote write is an upsert on a natural key
  (`systemid, run, row` for experimental data). Re-sending is harmless.
- **Nothing is lost on a crash.** `xfer` is stamped *after* the remote commits.
  Crash in between and the rows get re-sent and upserted — a duplicate send
  rather than a silent gap. The opposite ordering would lose data permanently.
- **Parents arrive first.** `Experiment` has no `xfer` column, so there is no
  watermark to follow. Each batch fetches the `Experiment` rows for the runs it
  is about to ship and upserts those first, satisfying the remote foreign key.

### 0MQ is an optimisation, not the mechanism

ZeroMQ PUB/SUB drops messages — if no subscriber is connected yet, if a
subscriber falls behind, or during the few milliseconds a connection takes to
establish. This service therefore treats a notification as a hint that means
"there may be new rows", and never as data. The periodic sweep is what makes it
correct; the notifications are what make it fast.

Set `enabled = false` under `[zmq]` and everything still works, just with up to
`sweep_interval_s` of latency. **[docs/zeromq.md](docs/zeromq.md)** explains the
whole thing, including the questions still open for Jonathan.

## Configuration

Everything lives in `config.toml` (copy `config.example.toml`). `${VAR}` in any
string is read from the environment, which is how credentials stay out of git.

| Key | Meaning |
|---|---|
| `local.path` | KAT's SQLite file. `~/katdata.db` for real KAT, `data/kat.sqlite` for the generated one. |
| `local.batch_size` | Rows per transaction. 500 is comfortable. |
| `local.busy_timeout_s` | How long to wait for SQLite's write lock while KAT is inserting. |
| `remote.kind` | `postgres` (local container or dev server); `sqlite` for a no-Docker fallback. |
| `remote.path` | Mock remote file, when `kind = "sqlite"`. |
| `remote.host/port/database/user/password` | `127.0.0.1` / `kat` / `kat` for the container; the dev server's via `${KAT_PG_USER}` etc. |
| `zmq.enabled` | `false` runs on the sweep alone. |
| `zmq.endpoint` | Where KAT publishes. |
| `zmq.topics` | Prefix filters. `[""]` subscribes to everything. |
| `service.sweep_interval_s` | Safety-net re-check. |
| `service.notify_debounce_s` | Coalescing window after a notification, so a burst becomes one batch. |
| `service.origin` | Tags every row `fake` or `real` in the archive. |

## Going live on the dev server

Once the VPN and Postgres credentials exist:

```bash
psql -h 192.168.40.100 -U <you> -d csit321 -f schema/remote_postgres.sql

export KAT_PG_USER=... KAT_PG_PASSWORD=...
```

then in `config.toml`:

```toml
[remote]
host = "192.168.40.100"            # was "127.0.0.1"
user = "${KAT_PG_USER}"            # was "kat"
password = "${KAT_PG_PASSWORD}"    # was "kat"

[service]
origin = "real"            # was "fake", once this is real KAT data
```

Nothing else changes. This path is not theoretical — it has been run end to end
against a real PostgreSQL 16 instance: 12,800 rows transferred, re-sent in full
without duplicating, and the service recovered on its own after the database was
killed mid-run and restarted.

## Layout

```
katxfer/
  config.py      configuration loading
  localdb.py     reading KAT's SQLite, stamping xfer
  sinks.py       the two remotes: PostgresSink and SqliteMockSink (fallback)
  service.py     the drain loop, triggers, retries, CLI
  zmqbus.py      Publisher / Subscriber
  fakedata.py    fake data generation, shared by the seeder and the simulator
tools/
  make_fake_db.py    build a fake KAT database
  kat_simulator.py   stand in for KAT: write rows live and publish notifications
  show_state.py      print both databases and the xfer column, before/after
  zmq_listen.py      watch the notification stream
  verify_transfer.py compare local against remote, row by row
docker-compose.yml   local PostgreSQL 16 standing in for the dev server
schema/
  kat_sqlite.sql       KAT's schema, verbatim
  remote_postgres.sql  the archive schema, with design notes
docs/zeromq.md
tests/
```

## Tests

```bash
python3 -m pytest tests/ -q      # 17 tests
```

They cover the completeness, idempotence and ordering properties above, plus the
0MQ behaviour — including a test that asserts notifications sent before the
subscriber connects really are lost, and that the data transfers anyway.

## Open questions for Jonathan

1. **`Environment` has no unique constraint.** There is no natural key to upsert
   against, so the client derives `digest = sha256(systemid|timestamp|data)` and
   uses that. Two byte-identical samples for the same system at the same instant
   collapse into one remote row. That is almost certainly desirable, but it is a
   behaviour change — the alternative is adding `UNIQUE (systemid, timestamp)`
   on the KAT side.
2. **Writing `xfer` back to KAT's database.** The service writes to KAT's file,
   which assumes KAT tolerates another process holding the write lock briefly.
   Is WAL mode on? If KAT would rather we did not touch its file at all, the
   alternative is a sidecar database tracking what we have sent.
3. **Two indexes on `xfer`** (`schema/kat_sqlite.sql`). Without them every sweep
   is a full table scan. They change no semantics, but they are an addition to
   KAT's schema and he should approve them.
4. **The `data` column format.** Real KAT writes a JSON object built from a
   J4 Record (`{"temperature":22.5,"pressure":101.3}`). The service treats it
   as opaque text, so this only matters for the generated fake data, which is
   still CSV-ish.
5. **0MQ is not in KAT 1.0.3.** Which release will contain it, and its
   endpoint, topic names, and whether it publishes per row or per
   transaction. Until then transfers are triggered manually. See
   `docs/zeromq.md`.
