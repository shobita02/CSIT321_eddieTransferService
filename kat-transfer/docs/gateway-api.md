# CSIT321 gateway API contract

The ingestion gateway and researcher read API share one FastAPI application.
NGINX provides the entry point. PostgreSQL is accessed only by `katgateway/storage.py`.
The client in `katxfer/sinks.py` contains no SQL or database driver.

## Authentication and token administration

Send `Authorization: Bearer <personal-token>` over HTTPS. Each token belongs to
a named `api_user` and has a `read` or `write` role. The laptop transfer client
uses the user's write token in `KAT_API_WRITE_TOKEN`; researchers may use their
read token in `KAT_API_READ_TOKEN`. Write tokens can read and upload; read tokens
can only use GET endpoints.

The archive stores a SHA-256 hash and an eight-character recognition prefix,
never the bearer token. The gateway resolves the hash against PostgreSQL on
every request. Revocation, expiration, user deactivation and role changes
therefore take effect without restarting the gateway. Inactive, expired,
revoked or unknown tokens return 401. Read tokens, and write tokens whose user
has been lowered to `read`, receive 403 on every ingestion route.

Only administrators should run the token/user CLI, using a separate database
owner/issuer login that is not available to the gateway process:

```bash
python -m katgateway --admin-user <issuer_login> user add alice --role write --granted-by <admin>
python -m katgateway --admin-user <issuer_login> token create --user alice --role write --expires 90d
python -m katgateway --admin-user <issuer_login> token list
python -m katgateway --admin-user <issuer_login> token revoke <id-or-prefix>
python -m katgateway --admin-user <issuer_login> user set-role alice read --granted-by <admin>
python -m katgateway --admin-user <issuer_login> user disable alice
```

`token create` prints the bearer token once; it cannot be recovered from the
database. Expiry is 1-90 days and defaults to 90 days. There is not yet a
user-facing sign-in/token-issuing flow; the future transfer-service app will
authenticate users before issuance. See [deployment](deployment.md) for the
migration and database grants.

`/health`, `/docs` and `/openapi.json` expose process health or documentation
without archive data.

## Ingestion

Every request is limited to 8 MiB and 1 to 1000 rows. Fields not in the contract
are rejected. A batch cannot mix `origin` or `src_host` values. All timestamp
strings require a timezone. The client converts KAT epoch milliseconds to UTC.
Database INTEGER bounds are enforced; rig IDs are 1 to 20 characters, source
names at most 20, and source host names 1 to 64. Environment timestamps cannot
be null because the archive uses them as primary keys. Measurement timestamps
and payload data may be null, matching the existing schema.

First POST parent experiments to `/v1/ingest/experiments`:

```json
{
  "origin": "fake",
  "src_host": "lab-laptop",
  "started_at": "2026-10-13T00:00:00Z",
  "trigger": "manual",
  "rows": [{"systemid": "CSIT321FAKE", "run": 9001, "description": "Gateway demo"}]
}
```

Then POST measurements to `/v1/ingest/experimental-data`:

```json
{
  "origin": "fake",
  "src_host": "lab-laptop",
  "started_at": "2026-10-13T00:00:00Z",
  "trigger": "manual",
  "xfer": "2026-10-13T00:00:01Z",
  "rows": [{
    "systemid": "CSIT321FAKE",
    "run": 9001,
    "row_no": 0,
    "ts": "2026-10-13T00:00:00.165Z",
    "source": "simulated",
    "data": "{\"temperature\":22.5,\"pressure\":101.3}"
  }]
}
```

For `/v1/ingest/environment`, each row contains `systemid`, non-null `ts` and
`data`; the batch also contains `xfer`, origin, host, started_at and trigger.
Allowed triggers are `manual`, `startup`, `sweep` and `zmq`.

`data` is JSON **text inside a JSON string**, preserving the source formatting.
Invalid JSON, including non-finite numbers, is rejected. Do not replace it with
a nested object. Nullable data means SQL null; the string `"null"` means JSON
null. PostgreSQL's `json` type preserves the source text.

HTTP 200 returns:

```json
{"committed": true, "sent": 1, "applied": 1}
```

The API sends this only after commit. `sent` counts source rows and `applied`
counts unique keys upserted. Repeated environment keys keep the last value, so
`applied` can be smaller than `sent`. Only a complete receipt permits the client
to stamp SQLite.

Each ingestion batch and its `transfer_log` row (including `api_token_id`) commit
in one transaction. Transfer reads include the associated username as
`uploaded_by`. A rejection rolls back that entire batch. HTTP 409 means an
archive constraint failed, such as a missing parent; 422 means validation
failed; 413 means the request is too large; 503 means a database request could
not complete. SQL, submitted payloads and bearer tokens are excluded from error
responses. Failed attempts are not recorded as successful transfers.

## Reading and Pandas

`GET /v1/experimental-data?systemid=CSIT321FAKE&run=9001&limit=100&offset=0`
returns `items`, `limit` and `offset`. List results use a stable primary-key
order. Use `limit` up to 1000 and keep requesting pages until one contains fewer
rows. Environment reads accept inclusive `from_ts` and `to_ts` timestamp filters.
The optional `origin=real` or `origin=fake` filter applies to data lists.

```python
import json
import os
import httpx
import pandas as pd

rows = []
offset = 0
with httpx.Client(
    base_url="https://example.edu/csit321/",
    headers={"Authorization": "Bearer " + os.environ["KAT_API_READ_TOKEN"]},
    timeout=30,
) as client:
    while True:
        response = client.get("v1/experimental-data", params={
            "systemid": "CSIT321FAKE", "run": 9001, "limit": 1000, "offset": offset,
        })
        response.raise_for_status()
        page = response.json()["items"]
        rows.extend(page)
        if len(page) < 1000:
            break
        offset += len(page)

frame = pd.DataFrame(rows)
if not frame.empty:
    frame["payload"] = frame["data"].map(lambda text: json.loads(text) if text is not None else None)
print(frame)
```

Install `httpx` and `pandas` in the researcher's environment. Confirm the live URL
with the administrator. With a private certificate authority, provide its trust
certificate to the HTTP client. Do not disable certificate verification.
`tools/read_run.py` provides pagination and CSV output without a database login.
Offset pagination is not a database snapshot. Pause writes when collecting a
fixed integrity comparison. Access is per user and token, but is not restricted
to a particular rig.
