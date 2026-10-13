# CSIT321 gateway API contract

The ingestion gateway and researcher read API share one FastAPI application.
NGINX provides the entry point. PostgreSQL is accessed only by `katgateway/storage.py`.
The client implementation in `katxfer/sinks.py` contains no SQL or database driver.

## Authentication

Send `Authorization: Bearer <token>` over HTTPS. The server requires two distinct
tokens with at least 32 characters, loaded from `KAT_API_WRITE_TOKEN` and
`KAT_API_READ_TOKEN`. Generate random values; length alone does not provide security.

The read token can use archive GET endpoints. The upload token can read and use
the three ingestion POST endpoints. Invalid or absent tokens return 401; a read
token used for ingestion returns 403. `/health`, `/docs` and `/openapi.json` expose
health or documentation without archive data. There is no token-issuing endpoint
or user account system in this prototype. The administrator distributes tokens.

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

`data` is JSON **text inside a JSON string**, preserving the source's formatting.
Do not replace it with a nested object. Invalid JSON, including non-finite
numbers, is rejected. Nullable data means SQL null; the string `"null"` means
the JSON value null. PostgreSQL's `json` type preserves the source text.

HTTP 200 returns:

```json
{"committed": true, "sent": 1, "applied": 1}
```

The API sends this only after commit. `sent` counts source rows and `applied`
counts unique keys upserted. Repeated environment keys keep the last value, so
`applied` can be smaller than `sent`. First-ingestion timestamps are preserved
on retries. Only a complete receipt permits the client to stamp SQLite.

Measurement/environment data and the successful audit record commit together.
Parent upserts are separate, without child audit records, matching the existing
transfer-log convention. A rejection rolls back that entire table batch.
HTTP 409 means an archive constraint failed, such as a missing parent; 422
means validation failed; 413 means the request is too large; 503 means a database
request could not complete. Any unconfirmed batch remains eligible for replay.
SQL and submitted payloads are excluded from error responses. Failed attempts
are reported by the client and server logs, not recorded as successful transfers.

## Reading and Pandas

`GET /v1/experimental-data?systemid=CSIT321FAKE&run=9001&limit=100&offset=0`
returns `items`, `limit` and `offset`. List results use a stable primary-key order.
Use `limit` up to 1000 and keep requesting pages until one contains fewer rows.
Environment reads accept inclusive `from_ts` and `to_ts` timestamp filters.
The optional `origin=real` or `origin=fake` filter applies to all data lists.

This Python example retrieves one run into Pandas without a database login:

```python
import json
import os
import httpx
import pandas as pd

rows = []
offset = 0
with httpx.Client(
    base_url="https://192.168.40.100/csit321/",
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
`tools/read_run.py` provides pagination and CSV output without needing Pandas.
The verification tools use paged API reads and compare raw JSON text.

Offset pagination is not a database snapshot. Pause writes when collecting a
fixed integrity comparison. The prototype has role-based access via two shared
tokens; it does not restrict each token to a particular rig or user.

## Implementation references

- [FastAPI security tools](https://fastapi.tiangolo.com/reference/security/)
- [FastAPI error handling](https://fastapi.tiangolo.com/tutorial/handling-errors/)
- [Psycopg transaction control](https://www.psycopg.org/docs/usage.html#transactions-control)
- [NGINX proxy routing](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)
