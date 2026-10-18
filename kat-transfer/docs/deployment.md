# Deploy the CSIT321 gateway and API

Deploy the API behind the server's NGINX entry point with trusted HTTPS configured. Point
it at the existing PostgreSQL archive. Give the transfer laptop only the gateway
URL, an upload token and any private CA certificate required for TLS trust.

## Confirmed development access

The setup email supplies VPN access to `192.168.40.100`, PostgreSQL on port 5432,
and the shared development database login `csit`. The verified archive name is
`experimentaldata` with no space. `Skywards` is the
pgAdmin server label; it is not the database name. The database `boredowl` is the
maintenance database named in the setup email, not the archive this API targets.

Python 3.12 is installed and the team accounts have no sudo permission.
Use a Python virtual environment in the
account's own directory. Treat Docker on the server as an optional deployment
path only if the administrator has explicitly provided access to it. Docker on
the laptop remains the local demonstration environment.

Each teammate uses their own assigned VPN profile and server login. The .ovpn file contains private connection
material and belongs outside this repository. The setup email's passwords are
not stored in project files. Foreground API reads and a small synthetic upload
have been verified on the development server through an SSH tunnel. The HTTPS
hostname, NGINX route and persistent service remain to be configured.

Open `docs/pgadmin-checks.sql` in pgAdmin's Query Tool to confirm the archive's
tables, column types and keys before uploading data. The queries are read-only.

The archive must already contain the tables in `schema/remote_postgres.sql`.
Use its existing schema if it is installed. Apply
`schema/migrations/002_api_tokens.sql` as the archive owner to add per-user
authorization without reinitialising the live database. The live compose file
does not run initialization SQL. Do not drop or reset the live archive.

For example, from the project folder, apply the additive migration with
`psql -h <database-host> -U <archive-owner> -d experimentaldata -f schema/migrations/002_api_tokens.sql`
before deploying the new gateway.

Use a restricted database role for the API and a separate owner-managed login
for token/user administration. `deploy/grants.example.sql` shows the required
permissions: the API can read users/tokens and update only `last_used_at`; it
cannot create or delete tokens. The issuer login is used only by the CLI, not
the running gateway.

## Native Python setup for a team account

In Windows PowerShell, enter your assigned server username when prompted:

```powershell
$serverUser = Read-Host 'Your assigned server username'
ssh "${serverUser}@192.168.40.100"
```

If the server requires the initial account password to be changed, complete that interactively. Keep
the chosen password out of project files.

On the laptop, run `tools/package_server.py` to create the current server source
bundle at `output/kat-gateway-server.tar.gz`. The builder verifies its files and
excludes passwords, VPN profiles, databases and Python environments. The bundle
uses the current workspace source, including changes not yet pushed to GitHub.

Copy that archive to your assigned account using SCP in a separate local PowerShell
window, from the laptop's `kat-transfer` folder:

```powershell
$serverUser = Read-Host 'Your assigned server username'
scp output/kat-gateway-server.tar.gz "${serverUser}@192.168.40.100:kat-gateway-server.tar.gz"
```

Then, in the server SSH session:

```bash
mkdir -p ~/csit321-gateway
tar -xzf ~/kat-gateway-server.tar.gz -C ~/csit321-gateway
cd ~/csit321-gateway/kat-transfer
```

Recreate the Python environment on Linux. The administrator service template
uses `/home/TEAM_USER/csit321-gateway/kat-transfer`. Replace every `TEAM_USER`
with the selected service account's username, and use the actual absolute
checkout path reported by `pwd` if its home directory differs. Systemd does
not expand `~` in these paths. `SOURCE_MANIFEST.json` lists hashes of the packaged files.

On the server, from `~/csit321-gateway/kat-transfer`:

```bash
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-server.txt
```

If the installed Python cannot create a venv, ask the administrator to install
the needed system package. Do not attempt a sudo installation from the student
account. Dependencies installed with the venv's Python stay in that environment.
[Python's virtual environment documentation](https://docs.python.org/3.12/library/venv.html)
explains the environment's isolation and why it is recreated on each machine.

## Private native settings and foreground check

Run these commands separately in the server's SSH terminal:

```bash
.venv/bin/python -m katgateway --configure
```

Wait for the database password prompt, type the `csit` database password, then
press Enter. The launcher reads it directly through Python's password prompt.
It first checks archive counts, then creates `.env.live.json` with permissions
600 containing only the API database settings. Setup and startup do not print
passwords or tokens. Setup changes no database
records and refuses to replace an existing settings file. Each account creates
its own settings file; do not copy another account's private settings file.

The defaults match the verified server connection: PostgreSQL at 127.0.0.1:5432,
database `experimentaldata`, user `csit`, SSL mode `prefer`. Override `--pg-host`,
`--pg-port`, `--pg-database`, `--pg-user` or `--pg-sslmode` during configuration if
server settings change. A failed archive check saves no settings file.

Check saved settings, then start the process:

```bash
.venv/bin/python -m katgateway --check
.venv/bin/python -m katgateway --port 8001
```

The launcher binds the API to server loopback at 127.0.0.1:8001, with no path
prefix during direct testing. Keep this terminal open. Stop it with Ctrl+C.
Only one API instance can use port 8001 on this server at a time. Coordinate
which team account runs it before starting another instance.
The server's JSON settings file is private and excluded from Git; it must not
be copied into the repository, deployment package or a public message.

## Test through an SSH tunnel before the NGINX route is available

In a second Windows PowerShell window:

```powershell
$serverUser = Read-Host 'Your assigned server username'
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18080:127.0.0.1:8001 "${serverUser}@192.168.40.100"
```

Leave that connection open. It forwards laptop port 18080 to the API's loopback
port through encrypted SSH. Open `http://127.0.0.1:18080/health` and
`http://127.0.0.1:18080/docs`. This tests the API before NGINX is configured;
it is a temporary development connection, and the production route remains
an administrator task. A client configured for the tunnel uses
`http://127.0.0.1:18080`, which is allowed because its HTTP endpoint is loopback.

Create the allow-listed user and issue a token with the separate database-owner
login. The CLI prompts for its password; the resulting bearer token is printed
once and is never retrievable later:

```bash
.venv/bin/python -m katgateway --admin-user <issuer_login> user add alice --role write --granted-by <admin_name>
.venv/bin/python -m katgateway --admin-user <issuer_login> token create --user alice --role write --expires 90d --note "lab laptop"
.venv/bin/python -m katgateway --admin-user <issuer_login> token list
.venv/bin/python -m katgateway --admin-user <issuer_login> token revoke <id-or-prefix>
.venv/bin/python -m katgateway --admin-user <issuer_login> user set-role alice read --granted-by <admin_name>
.venv/bin/python -m katgateway --admin-user <issuer_login> user disable alice
```

Token lifetime is capped at 90 days. `user set-role` immediately caps existing
tokens; `user disable` invalidates them on the next request. Deliver the printed
token directly and securely to its owner; never put it in screenshots or shared
configuration.

## Persistent native service

Give the administrator `deploy/katgateway.service.example` and
`deploy/nginx.live.conf`. Replace every `TEAM_USER` in the service template
with the selected service account's username and adjust its absolute paths.
The service runs under that account, loads the
private `.env.live.json`, and invokes the venv's Python launcher with
`--port 8001 --root-path /csit321`. This matches the NGINX location that strips `/csit321/`
from forwarded request paths. Port 8000 was already occupied during setup; the verified native API uses 8001.
Stop the foreground test process before starting
the installed service on the same port.

The administrator adjusts the checkout path, installs and enables the service,
and validates/reloads NGINX. The template has not been installed on the server.
The user account cannot modify those system services with sudo.

PostgreSQL TLS must match its actual configuration. The verified native check
used `prefer`; select `require` when the server supports mandatory encrypted
connections, or `verify-full` with appropriate certificate trust for identity
verification. The setup email does not confirm database TLS settings. The
API's private settings belong only on the server and to the owning account.

## Optional container deployment with administrator-provided Docker access

If the administrator grants Docker access on the server, use the existing
`docker-compose.live.yml` with a separate private dotenv file. The native JSON
profile is not a Docker dotenv file. Prepare `.env.live` with these variable names and server-only API database credentials:

```dotenv
KAT_PG_HOST=192.168.40.100
KAT_PG_PORT=5432
KAT_PG_DATABASE=experimentaldata
KAT_PG_USER=<restricted gateway login>
KAT_PG_PASSWORD=<gateway database password held only on the server>
KAT_PG_SSLMODE=prefer
```

From the full checkout on the Linux server:

```bash
chmod 600 .env.live
docker compose --env-file .env.live -f docker-compose.live.yml up -d --build
```

The server-only native package does not include the Docker build files. This
optional route needs the full checkout. The compose file publishes container port 8000 on host loopback port 8001, creates no PostgreSQL container and does not initialise the
archive. The PostgreSQL host must be reachable from the container: a container's
127.0.0.1 refers to the container, rather than the host database. The compose
option handles restarts.

## Existing NGINX entry point

Add the location in `deploy/nginx.live.conf` inside the server's existing HTTPS
server block. It forwards `/csit321/` to the API while preserving other routes.
The trailing slash on `proxy_pass` strips `/csit321/`. The API uses the matching
`root-path` setting so its documentation resolves through the same prefix.
Validate the NGINX configuration before reloading it through the administrator's
normal process. Keep PostgreSQL firewalled from transfer laptops; only the API
host or its private network needs database access.

Use a trusted HTTPS certificate whose subject matches the selected hostname or
IP address. If BoredOwl uses a private CA, distribute its trust certificate and
set the client's `[remote] ca_file`. Do not bypass TLS verification. The local
demo's plain HTTP entry point is bound to loopback and is intended for local use.

The setup email confirms VPN access, but does not confirm an HTTPS API endpoint.
Keep `config.live.toml` as a template until the administrator supplies the route
and certificate details. Do not assume `/csit321/` already exists on the server.

## Laptop configuration and verification

Update `config.live.toml` with the confirmed HTTPS URL and the actual KAT SQLite
path. Choose `origin="real"` only when the selected data should be retained as
real experiment data. Store the upload token in the current terminal or the
chosen operating-system secret store:

```powershell
$env:KAT_API_WRITE_TOKEN = '<upload token issued by the administrator>'
python -m katxfer.service -c config.live.toml --doctor
python -m katxfer.service -c config.live.toml --status
python -m katxfer.service -c config.live.toml --once
python tools/verify_transfer.py -c config.live.toml
```

Confirm that pending counts reach zero, source JSON matches the archive, and a
second `--once` transfers zero rows. Give researchers the read token and the API
guide; read-token uploads must return 403. A request with no token must return
401. `/health` tests the process; authenticated `/v1/status` also tests the database.

If a connection fails, keep the SQLite database and its pending rows. The
long-running client retries with exponential backoff and reconnects. HTTP
validation or authentication failures also remain pending; correct the data or
settings before retrying. Reduce `batch_size` for unusually large JSON payloads
that exceed the 8 MiB request limit.

## Handover boundaries

The local demo archive and test database use a separate compose project and volume.
The persistent native service and live container templates have not been installed
on the development server. User accounts,
per-rig permissions, the future user sign-in/issuance app, database backups and
monitoring remain follow-up work. `src_host` is reported by the client, not a
trusted identity. Keep the issuer's PostgreSQL login off transfer laptops.
