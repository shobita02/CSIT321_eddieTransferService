# Deploy the CSIT321 gateway and API

Deploy the API behind the server's NGINX entry point with trusted HTTPS configured. Point
it at the existing PostgreSQL archive. Give the transfer laptop only the gateway
URL, an upload token and any private CA certificate required for TLS trust.

## Confirmed development access

Get VPN access to `192.168.40.100`, PostgreSQL on port 5432,
and the shared development database login `csit`. archive name is `experimentaldata` with no space. `Skywards` is the
pgAdmin server label; it is not the database name. The database `boredowl` is the
maintenance database named in the setup email, not the archive this API targets.

Python 3.12 is installed and
our accounts have no sudo permission. Use a Python virtual environment in the
account's own directory. Treat Docker on the server as an optional deployment
path only if the administrator has explicitly provided access to it. Docker on
the laptop remains the local demonstration environment.

Use own  VPN profile. The .ovpn file contains private connection
material and belongs outside this repository. The setup email's passwords are
not stored in project files. The API has not been deployed to or changed the
live server, and the HTTPS hostname and NGINX route are not yet confirmed.

Open `docs/pgadmin-checks.sql` in pgAdmin's Query Tool to confirm the archive's
tables, column types and keys before uploading data. The queries are read-only.

The archive must already contain the four tables in `schema/remote_postgres.sql`.
Use its existing schema if it is installed. The live compose file does not run
initialisation SQL. Do not drop or reset the live archive to install this API.

The API can initially use the provided `csit` login on the server for development.
The laptop transfer client uses only its API token. For a restricted API database
role, ask the administrator to create a dedicated login and grant it usage on the
archive schema, SELECT/INSERT/UPDATE on the three
data tables, SELECT/INSERT on `transfer_log`, and USAGE/SELECT on its sequence.
`deploy/grants.example.sql` provides those grants after changing its example role
name. The API needs no superuser, database-creation or table-deletion permission.

## Native Python setup for the chrisw account

Connect from the laptop using `ssh chrisw@192.168.40.100`. If the server requires
the initial account password to be changed, complete that interactively. Keep
the chosen password out of project files.

On the laptop, run `tools/package_server.py` to create the current server source
bundle at `output/kat-gateway-server.tar.gz`. The builder verifies its files and
excludes passwords, VPN profiles, databases and Python environments. The bundle
uses the current workspace source, including changes not yet pushed to GitHub.

Copy that archive to the `chrisw` account using SCP in a separate local PowerShell
window. Then, in the server SSH session:

```bash
mkdir -p ~/csit321-gateway
tar -xzf ~/kat-gateway-server.tar.gz -C ~/csit321-gateway
cd ~/csit321-gateway/kat-transfer
```

Recreate the Python environment on Linux. The administrator service template
uses `/home/chrisw/csit321-gateway/kat-transfer`; adjust its paths if another
directory is chosen. `SOURCE_MANIFEST.json` lists hashes of the packaged files.

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
600, containing the database settings and two separate random API tokens.
Setup and startup do not print passwords or tokens. Setup changes no database
records and refuses to replace an existing settings file.

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
The server's JSON settings file is private and excluded from Git; it must not
be copied into the repository, deployment package or a public message.

## Test through an SSH tunnel before the NGINX route is available

In a second Windows PowerShell window:

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18080:127.0.0.1:8001 chrisw@192.168.40.100
```

Leave that connection open. It forwards laptop port 18080 to the API's loopback
port through encrypted SSH. Open `http://127.0.0.1:18080/health` and
`http://127.0.0.1:18080/docs`. This tests the API before NGINX is configured;
it is a temporary development connection, and the production route remains
an administrator task. A client configured for the tunnel uses
`http://127.0.0.1:18080`, which is allowed because its HTTP endpoint is loopback.

The launcher can explicitly print one token for an authorised client with
`--print-token read` or `--print-token write`. For example, copy the read token
into the Windows clipboard without displaying it in the terminal:

```powershell
ssh chrisw@192.168.40.100 'cd /home/chrisw/csit321-gateway/kat-transfer && .venv/bin/python -m katgateway --print-token read' | Set-Clipboard
```

Paste that token into the docs page's Authorize dialog and use `/v1/status` to
check live counts. Give the upload token only to the intended transfer client.
Do not send either token in a chat screenshot. The tunnel does not establish
that the administrator's HTTPS route or NGINX integration is configured.

## Persistent native service

Give the administrator `deploy/katgateway.service.example` and
`deploy/nginx.live.conf`. The service template runs under `chrisw`, loads the
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
profile is not a Docker dotenv file. Prepare `.env.live` with these variable
names and actual server-only credentials/tokens:

```dotenv
KAT_PG_HOST=192.168.40.100
KAT_PG_PORT=5432
KAT_PG_DATABASE=experimentaldata
KAT_PG_USER=csit
KAT_PG_PASSWORD=<database password held only on the server>
KAT_PG_SSLMODE=prefer
KAT_API_WRITE_TOKEN=<upload token>
KAT_API_READ_TOKEN=<different read token>
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

The local demo and test database use a separate compose project and volume. The
live deployment template has not been executed against BoredOwl. User accounts,
per-rig permissions, managed token rotation, database backups, monitoring and
the final public-read policy need the team's deployment decisions. Shared tokens
are a prototype access mechanism; `src_host` is a reported source, not a trusted
identity. Credentials should be available only to the server and their intended
API clients. Keep the admin's PostgreSQL login off transfer laptops.
