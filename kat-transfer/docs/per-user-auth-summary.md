# Per-user tokens and sign-in: summary

Branch: `feature-branch`. Replaces the shared read/write tokens with personal
tokens, and lets users sign in to get their own token (for the desktop app).

## Part 1: per-user API tokens (Araf, commit `6607a4a`)

- **Tables** `api_user` (who may hold a token, `max_role` read/write, `active`)
  and `api_token` (SHA-256 hash and 8-char prefix only, expiry, revoked,
  last used). `transfer_log.api_token_id` records who sent each batch.
  Migration: `schema/migrations/002_api_tokens.sql`.
- **Gateway auth** (`katgateway/app.py`, `storage.py` `find_token`): every
  request hashes the bearer token and looks it up. A revoked, expired, unknown
  or disabled-user token gets 401. Read tokens, or users lowered to `read`, get
  403 on `POST /v1/ingest/*`. Changes apply on the next request, with no restart.
- **Admin commands** (`python -m katgateway --admin-user <login> ...`):
  `token create|list|revoke` and `user add|set-role|disable`. Expiry is 1–90 days.
- **Shared tokens removed** from settings, compose files, `.env.example`, `--configure` and `--print-token`.
- **Grants** (`deploy/grants.example.sql`): the gateway role can't insert or delete tokens.
- **Not included:** user sign-in (added in Part 2) and the optional 30 s lookup cache.

## Part 2: sign-in endpoints (added after Araf's work)

| Route | Sends | Returns |
|---|---|---|
| `POST /v1/auth/login` | `username`, `password`, `role` (default `write`), `expires_days` (1–90, default 90) | `token`, `token_id`, `username`, `role`, `expires_at` |
| `GET /v1/auth/me` | Bearer token | `token_id`, `username`, `role`, `expires_at` |
| `POST /v1/auth/logout` | Bearer token | 204, token revoked |

- **Passwords:** salted scrypt hashes in `api_user.password_hash`
  (migration `003_api_user_password.sql`). Set with `user set-password <name>`.
- **Errors:** wrong password, unknown user and inactive user all get the same
  401. A `read`-only user asking for `write` gets 403 "Your account can view
  but not send data".
- **Rate limits:** 5 failed attempts per username in 5 minutes, then 429. NGINX
  limits attempts per client address (`deploy/nginx.live.conf`).
- **HTTPS only:** sign-in is refused over plain HTTP, except on loopback or
  when `KAT_AUTH_ALLOW_HTTP=1` (local demo only).
- **Separate issuer login:** tokens are created through a separate database
  login (`KAT_ISSUER_PG_USER` / `KAT_ISSUER_PG_PASSWORD`, or
  `--configure --issuer-user`). The ingest role still can't create tokens or
  read password hashes. With no issuer configured, login and logout return 503.
- **Never logged:** passwords and tokens.
- **Tests:** `tests/test_gateway.py` (sign-in cases and a PostgreSQL end-to-end
  test), `tests/test_passwords.py` and `tests/test_server_launcher.py`.

## Part 3: desktop app sign-in (`katapp`)

- **Sign-in screen** calls `POST /v1/auth/login` for a `write` token. Wrong
  details, read-only accounts ("Your account can view but not send data"),
  rate limits and an unreachable gateway each show a plain message.
- **Token handling:** the token goes into the service in memory and is
  never shown or written to a file. It is saved in Windows Credential Manager
  (service `kat-transfer`) with `keyring`. On start-up an unexpired saved token
  is checked with `GET /v1/auth/me` and skips sign-in.
- **Status screen:** transfers start automatically and run in a background
  thread until **Quit**. The screen shows the user and token expiry (with a
  warning 7 days before), whether the gateway is connected or retrying (with the
  last warning), the KAT database path, rows sent this session and the last
  transfer time. Service warnings appear in the activity log.
- **Quit / X** finish the current batch, then exit. **Sign out** does the same,
  then forgets the saved token and revokes it (`POST /v1/auth/logout`).
- **Refused token:** if the gateway answers 401/403 during transfers (expired,
  revoked, user disabled or lowered to read), the service stops instead of
  retrying (`GatewayAuthError` in `katxfer/sinks.py` and `service.py`). The
  saved token is deleted and the app returns to sign-in with the reason. Other
  errors show a **Restart** button.
- **Live gateway:** the app now accepts HTTPS gateways such as `config.live.toml`.
  The previous local-only restriction is removed; the client's usual rule still
  applies (HTTP only on loopback).
- **Tests:** `tests/test_app.py` (no window needed).

## Part 4: the desktop exe

- **One-file exe:** `python tools/build_app.py` builds `dist/EddieTransfer.exe`
  (PyInstaller, no console window). The client config is built in
  (`config.tunnel.toml` by default; `-c config.live.toml` once the HTTPS route
  exists). A `config.toml` placed beside the exe overrides it. The token is still
  kept only in Windows Credential Manager.

## How to run (local, from `kat-transfer/`)

Needs Python 3.12+ and Docker Desktop running.

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
```

**Tests:**

```powershell
$env:KAT_PG_PASSWORD = "kat-local-demo-only"
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --wait remote-db
python -m pytest tests -q
```

**Demo gateway, with a sign-in user created for you:**

```powershell
python tools/demo.py    # needs unsent rows in ~/katdata.db; writes KAT_DEMO_USER / KAT_DEMO_PASSWORD to .env
```

**Or create a user by hand** (with the stack already up via `docker compose up -d --build --wait`):

```powershell
$admin = "docker compose exec -T -e PW=choose-a-long-password gateway python -m katgateway --admin-user kat --admin-password-env KAT_PG_PASSWORD"
Invoke-Expression "$admin user add alice --role write --granted-by me"
Invoke-Expression "$admin user set-password alice --password-env PW"
```

**Try sign-in:**

```powershell
$r = Invoke-RestMethod -Method Post http://127.0.0.1:8080/v1/auth/login `
     -ContentType application/json -Body '{"username":"alice","password":"choose-a-long-password"}'
$h = @{ Authorization = "Bearer $($r.token)" }
Invoke-RestMethod http://127.0.0.1:8080/v1/auth/me -Headers $h
Invoke-RestMethod -Method Post http://127.0.0.1:8080/v1/auth/logout -Headers $h
```

Or open <http://127.0.0.1:8080/docs> and use the **auth** section.

**Transfer with the signed-in token:**

```powershell
$env:KAT_API_WRITE_TOKEN = $r.token   # take it from a new login; logout revokes it
python -m katxfer.service --once
```

**Desktop app** (needs a user with a password, see above):

```powershell
python -m katapp -c config.toml        # local Docker gateway
python -m katapp -c config.live.toml   # office HTTPS gateway
python run_eddie.py -c config.toml     # same, through the EXE entry point
python tools/build_app.py              # dist/EddieTransfer.exe (needs pyinstaller==6.16.0)
```

The app transfers from the `[local] path` in that config (`~/katdata.db` in
`config.toml`). To forget a saved sign-in, use **Sign out**, or delete
`kat-transfer` in Windows Credential Manager.

**Stop:**

```powershell
docker compose down
```

## Live server checklist

1. The database owner applies migrations `002` and `003` to `experimentaldata`.
2. Apply `deploy/grants.example.sql` (API role and issuer role).
3. Configure the issuer login for the gateway and restart it.
4. Add users (`user add`) and set their passwords (`user set-password`).
5. Add the NGINX `limit_req` lines from `deploy/nginx.live.conf`.
