# kat-transfer

The **CSIT321 Transfer Client**. It reads new rows from the KAT SQLite database
on a laptop and sends them to the PostgreSQL archive on the Bored Owl dev
server (`192.168.40.100`).

```
KAT  ──► KAT SQLite DB (~/katdata.db) ──► kat-transfer ──► PostgreSQL archive
                                                           (192.168.40.100)
```

## How the transfer works

KAT's tables already have an `xfer` column. A row with `xfer IS NULL` has not
been sent yet. That column is the only state the service keeps.

```
1. read a batch of rows WHERE xfer IS NULL          (ExperimentalData, Environment)
2. send the parent Experiment rows for that batch   (so the foreign key is satisfied)
3. upsert the batch into PostgreSQL and commit
4. only then set xfer = now() on those rows in KAT's database
5. repeat until nothing is pending
```

What this gives you:

- **Nothing is missed.** The service asks the database what is pending, so it
  doesn't matter whether it was running when KAT wrote the data.
- **Nothing is duplicated.** Every remote write is an upsert on a primary key
  (see below), so sending a row twice just updates it.
- **Nothing is lost on a crash.** `xfer` is stamped only *after* the remote
  commits. A crash in between means the rows are sent again, never skipped.
- **A failed write is retried.** If the server is down or rejects a batch, the
  rows keep `xfer = NULL` and go in the next attempt.

The only thing the service writes to KAT's database is `xfer`. The KAT schema
remains exactly the same as provided (`schema/kat_sqlite.sql`).

## The remote tables and their primary keys

Defined in `schema/remote_postgres.sql`.

| Remote table | Comes from (KAT) | Primary key | Notes |
|---|---|---|---|
| `experiment` | `Experiment` | `(systemid, run)` | Same as KAT's `UNIQUE (systemid, run)` |
| `experimental_data` | `ExperimentalData` | `(systemid, run, row_no)` | KAT's `row` is renamed `row_no`. Foreign key to `experiment` |
| `environment` | `Environment` | `(systemid, ts)` | **Added by us**, see below |
| `transfer_log` | — | `id` | One row per batch sent: when, how many, success or failure |

- **`environment` key.** KAT's `Environment` table has no unique constraint, so
  the archive keys it on the timestamp plus `systemid` (several rigs send to
  one archive, and two rigs can sample at the same instant). Samples with the
  same data at different times are always kept as separate rows. If KAT ever
  writes two rows for the same rig at the same instant, the later one is kept.
- **`data` is `json`** in `experimental_data` and `environment`. Plain `json`
  (not `jsonb`) stores the text exactly as KAT wrote it. A payload that isn't
  valid JSON is rejected by PostgreSQL and its row stays unsent.
- **Extra columns** on every data table: `xfer` (when it was sent), `origin`
  (`real` or `fake`), `src_host` (which laptop sent it) and `ingested_at`.

## Install

Install the requirements into a virtual environment (venv). It keeps this
project's packages separate from everything else on your machine and and server avoids
the "installed but `No module named ...`" problem you get when a laptop has
more than one Python.

**First time only,** from the `kat-transfer` folder:

```powershell
python -m venv .venv                          # create the venv (macOS/Linux: python3)
.venv\Scripts\Activate.ps1                    # activate it (macOS/Linux: source .venv/bin/activate)
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

**Every new terminal:** activate it again before running anything:

```powershell
.venv\Scripts\Activate.ps1
```

You'll see `(.venv)` at the start of the prompt when it's active. Type
`deactivate` to leave it.

- **`running scripts is disabled on this system`** when activating in
  PowerShell: run
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then activate
  again. In Command Prompt use `.venv\Scripts\activate.bat` instead.
- **VS Code:** press Ctrl+Shift+P, choose *Python: Select Interpreter*, then
  pick `.venv`. New terminals then activate it automatically.
- `.venv` is in `.gitignore`, so it's never committed. Everyone creates their
  own.
- Always use `python -m pip`, not bare `pip`, so packages go into the Python
  that runs the service.


## 
## Run it locally (Docker, no VPN) (Database resets and cleans at the start of each container so easier to start from scratch)

`config.toml` sends to a PostgreSQL container on your own machine, built from
the same schema. Useful for testing and as a backup demo.

```powershell
docker compose up -d                          # start the local archive
python tools/make_fake_db.py --reset          # optional: build a fake KAT database or you can generate fake data through KAT
python -m katxfer.service --status
python -m katxfer.service --once
python tools/verify_transfer.py
python tools/show_state.py                    # print both databases side by side
```

`config.toml` reads the real `~/katdata.db`. To use the fake source database instead,
set `path = "data/kat.sqlite"` under `[local]`.

- **Start the archive again from empty:** `docker compose down -v`, then
  `docker compose up -d`. Do this after any change to `remote_postgres.sql`.
- **Look at the archive directly:** `docker exec -it kat-remote-pg psql -U kat -d csit321`
- **Make every KAT row unsent again:**
  `UPDATE ExperimentalData SET xfer = NULL; UPDATE Environment SET xfer = NULL;`
  It's safe, because the remote upserts. 

## Run it against the live PostgreSQL server (config.live.toml file)

You need the VPN connected.

**1. [DONE] [You can drop and rebuild everything if you want to, just set katdata.db back to original to reset if you want real type of data] Create the tables (once).** In pgAdmin, connect to the server, open the
Query Tool on the archive database, open `schema/remote_postgres.sql` and run
it. Or with `psql`:

```powershell
psql -h 192.168.40.100 -U <your_role> -d <database> -f schema/remote_postgres.sql
```

**2. Set your login for this terminal.** `config.live.toml` reads these from
the environment, so no password is ever saved in a file:

```powershell
$env:KAT_PG_DATABASE = "<database>"
$env:KAT_PG_USER     = "<your_role>"
$env:KAT_PG_PASSWORD = "<password>"
```

These last only for this PowerShell window. If one isn't set, the login fails
with what looks like a wrong password.

**3. Run the transfer:**

```powershell
python -m katxfer.service -c config.live.toml --doctor        # check setup and resolved config
python -m katxfer.service -c config.live.toml --status        # pending locally vs rows on the server
python -m katxfer.service -c config.live.toml --once          # send everything pending, then exit
python -m katxfer.service -c config.live.toml --status        # pending should now be 0
python tools/verify_transfer.py -c config.live.toml           # row-by-row check: nothing missing or changed
```

To keep it running and sending new rows as KAT writes them, leave out `--once`:

```powershell
python -m katxfer.service -c config.live.toml
```

Before sending real KAT data, set `origin = "real"` under `[service]` in
`config.live.toml`, so the archive doesn't label it `fake`.



## If something goes wrong

Run `python -m katxfer.service --doctor` first (add `-c config.live.toml` for
the live server). It shows which Python is running, which packages it can see,
and the config it loaded.


## Configuration

| File | Sends to |
|---|---|
| `config.toml` | Local Docker PostgreSQL (`127.0.0.1`, user `kat`) |
| `config.live.toml` | Live server `192.168.40.100`, login from `$env:KAT_PG_*` |

Any `${VAR}` in a config value is read from the environment. Main settings:
`[local] path` (KAT's database), `[local] batch_size` (rows per batch),
`[service] sweep_interval_s` (how often it checks for new rows when left
running), `[service] origin` (`real` or `fake`).


## Still open

1. **0mq notification subscription to be added**
2. **CSIT321 Gateway and API structure to be built**

## Tests

The tests send to a real PostgreSQL, so start the Docker container first:

```powershell
docker compose up -d
python -m pytest tests/ -q      # 23 tests
```

Each run creates its own `kat_test` database in the container and empties it before every test, so the demo database (`csit321`) is never touched. If PostgreSQL isn't running, the tests that need it are skipped and tell you to start it.

## Layout

```
katxfer/          the service: config, localdb (KAT side), sinks (remote side), service (loop + CLI), zmqbus
tools/            make_fake_db, kat_simulator, show_state, verify_transfer, zmq_listen
schema/           kat_sqlite.sql (KAT, as provided), remote_postgres.sql (archive)
config.toml       local Docker archive
config.live.toml  live server
tests/
```
