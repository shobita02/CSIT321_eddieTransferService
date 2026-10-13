"""HTTP archive sink. No PostgreSQL driver or SQL is used by the client."""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
import httpx
import ssl
from .config import RemoteConfig
from .fakedata import parse_ts
from .localdb import EnvironmentRow, ExperimentalRow, ExperimentRow


@dataclass
class WriteResult:
    sent: int
    applied: int


class RemoteSink(ABC):
    def __init__(self, origin: str, src_host: str) -> None:
        self.origin = origin
        self.src_host = src_host

    def begin_batch(self, started_at: datetime, trigger: str) -> None:
        pass

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def upsert_experiments(self, rows: list[ExperimentRow]) -> WriteResult: ...

    @abstractmethod
    def upsert_experimental(self, rows: list[ExperimentalRow], xfer: datetime) -> WriteResult: ...

    @abstractmethod
    def upsert_environment(self, rows: list[EnvironmentRow], xfer: datetime) -> WriteResult: ...

    @abstractmethod
    def log_transfer(self, started_at: datetime, table_name: str, rows_sent: int,
                     rows_applied: int, trigger: str, ok: bool, detail: str | None = None) -> None: ...

    @abstractmethod
    def counts(self) -> dict[str, int]: ...

    def __enter__(self) -> "RemoteSink":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _ts(value: str | int | None) -> datetime | None:
    return parse_ts(value) if value is not None and value != "" else None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _wire_ts(value: str | int | None) -> str | None:
    dt = _ts(value)
    return dt.isoformat() if dt is not None else None


class GatewayError(RuntimeError):
    """The gateway did not confirm a committed write."""


class HttpSink(RemoteSink):
    def __init__(self, cfg: RemoteConfig, origin: str, src_host: str) -> None:
        super().__init__(origin, src_host)
        self.cfg = cfg
        self._client: httpx.Client | None = None
        self.begin_batch(datetime.now(timezone.utc), "manual")

    def begin_batch(self, started_at: datetime, trigger: str) -> None:
        self.started_at = started_at
        self.trigger = trigger

    def connect(self) -> None:
        # TLS verification is enabled. Redirects cannot forward the token.
        self._client = httpx.Client(
            base_url=self.cfg.url + "/", timeout=self.cfg.timeout_s,
            headers={"Authorization": "Bearer " + self.cfg.token},
            follow_redirects=False, trust_env=False,
            verify=ssl.create_default_context(cafile=self.cfg.ca_file) if self.cfg.ca_file else True,
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def request(self, method: str, path: str, **kwargs) -> dict:
        if self._client is None:
            raise GatewayError("Gateway connection is not open")
        try:
            response = self._client.request(method, path.lstrip("/"), **kwargs)
        except httpx.HTTPError as exc:
            raise GatewayError("Gateway connection failed; pending rows will be retried") from exc
        if response.status_code != 200:
            # Do not echo arbitrary server bodies, local data or credentials.
            raise GatewayError(f"Gateway returned HTTP {response.status_code}; write not confirmed")
        try:
            result = response.json()
        except ValueError as exc:
            raise GatewayError("Gateway returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise GatewayError("Gateway returned an invalid response")
        return result

    def _write(self, table: str, rows: list[dict], xfer: datetime | None = None) -> WriteResult:
        if not rows:
            return WriteResult(0, 0)
        body = {
            "origin": self.origin, "src_host": self.src_host,
            "started_at": self.started_at.isoformat(), "trigger": self.trigger, "rows": rows,
        }
        if xfer is not None:
            body["xfer"] = xfer.isoformat()
        result = self.request("POST", f"v1/ingest/{table}", json=body)
        sent, applied = result.get("sent"), result.get("applied")
        if (result.get("committed") is not True or type(sent) is not int
                or sent != len(rows) or type(applied) is not int or not 0 < applied <= sent):
            raise GatewayError("Gateway did not acknowledge the complete committed batch")
        return WriteResult(sent, applied)

    def upsert_experiments(self, rows: list[ExperimentRow]) -> WriteResult:
        return self._write("experiments", [
            {"systemid": r.systemid, "run": r.run, "description": r.description} for r in rows
        ])

    def upsert_experimental(self, rows: list[ExperimentalRow], xfer: datetime) -> WriteResult:
        return self._write("experimental-data", [
            {"systemid": r.systemid, "run": r.run, "row_no": r.row_no,
             "ts": _wire_ts(r.ts), "source": r.source, "data": r.data} for r in rows
        ], xfer)

    def upsert_environment(self, rows: list[EnvironmentRow], xfer: datetime) -> WriteResult:
        return self._write("environment", [
            {"systemid": r.systemid, "ts": _wire_ts(r.ts), "data": r.data} for r in rows
        ], xfer)

    def log_transfer(self, started_at: datetime, table_name: str, rows_sent: int,
                     rows_applied: int, trigger: str, ok: bool, detail: str | None = None) -> None:
        # The gateway writes the audit record in the data transaction. A second
        # call here could fail after SQLite has already been stamped.
        pass

    def counts(self) -> dict[str, int]:
        counts = self.request("GET", "v1/status").get("counts")
        tables = {"experiment", "experimental_data", "environment"}
        if (not isinstance(counts, dict) or set(counts) != tables
                or any(type(n) is not int or n < 0 for n in counts.values())):
            raise GatewayError("Gateway returned invalid archive counts")
        return counts

    def records(self, resource: str, **filters):
        """Iterate bounded read pages, for verification and diagnostic tools."""
        offset = 0
        while True:
            page = self.request("GET", f"v1/{resource}", params={**filters, "limit": 1000, "offset": offset})
            rows = page.get("items")
            if not isinstance(rows, list):
                raise GatewayError("Gateway returned an invalid read page")
            yield from rows
            if len(rows) < 1000:
                return
            offset += len(rows)


def build_sink(cfg: RemoteConfig, origin: str, src_host: str) -> RemoteSink:
    return HttpSink(cfg, origin, src_host)
