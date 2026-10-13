"""The transfer service itself.

Control flow is deliberately simple and single-threaded:

    wait for a 0MQ notification, or for the sweep timer to expire
      -> drain: repeatedly take a batch of rows with xfer IS NULL,
                upsert them remotely, then stamp xfer locally
      -> repeat

The drain loop is the whole correctness story. It is driven by the state of
the database, not by the contents of any message, so a lost notification
costs latency and nothing else.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from typing import TYPE_CHECKING

from .config import DEFAULT_CONFIG_PATH, ServiceConfig, load as load_config
from .localdb import LocalKatDb
from .sinks import RemoteSink, build_sink

if TYPE_CHECKING:  # import for type checkers only, never at runtime
    from .zmqbus import Subscriber

# NOTE: katxfer.zmqbus is deliberately NOT imported here. It imports pyzmq,
# which is an optional dependency -- the service is designed to run on its
# periodic sweep alone, and importing at module scope would make a package
# that is only needed for the notification shortcut mandatory just to run
# `--status`. It is imported inside `run()`, and only when 0MQ is enabled.

log = logging.getLogger("katxfer.service")


@dataclass
class Stats:
    cycles: int = 0
    experimental_sent: int = 0
    environment_sent: int = 0
    experiments_sent: int = 0
    failures: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def summary(self) -> str:
        secs = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        return (
            f"{self.cycles} cycles in {secs:.0f}s | "
            f"experiment={self.experiments_sent} "
            f"experimental_data={self.experimental_sent} "
            f"environment={self.environment_sent} "
            f"failures={self.failures}"
        )


class TransferService:
    def __init__(self, cfg: ServiceConfig) -> None:
        self.cfg = cfg
        self.local = LocalKatDb(cfg.local.path, cfg.local.busy_timeout_s)
        self.stats = Stats()
        self._stop = False
        self._sink: RemoteSink | None = None

    # -- lifecycle -----------------------------------------------------------

    def stop(self, *_: object) -> None:
        if not self._stop:
            log.info("shutdown requested, finishing current batch")
        self._stop = True

    def _sink_or_connect(self) -> RemoteSink:
        if self._sink is None:
            sink = build_sink(self.cfg.remote, self.cfg.origin, self.cfg.src_host)
            sink.connect()
            self._sink = sink
        return self._sink

    def _drop_sink(self) -> None:
        if self._sink is not None:
            try:
                self._sink.close()
            except Exception:  # noqa: BLE001 - closing a broken socket
                pass
            self._sink = None

    # -- the work ------------------------------------------------------------

    def drain(self, trigger: str = "sweep") -> int:
        """Ship everything currently pending. Returns rows transferred."""
        total = 0
        while not self._stop:
            moved = self._one_batch(trigger)
            if moved == 0:
                break
            total += moved
        if total:
            log.info("drained %d rows (trigger=%s)", total, trigger)
        return total

    def _one_batch(self, trigger: str) -> int:
        started = datetime.now(timezone.utc)
        limit = self.cfg.local.batch_size

        exp_rows = self.local.pending_experimental(limit)
        env_rows = self.local.pending_environment(limit)
        if not exp_rows and not env_rows:
            return 0

        sink = self._sink_or_connect()
        xfer_at = datetime.now(timezone.utc)
        moved = 0

        if exp_rows:
            # Parents first: the remote has a foreign key from
            # experimental_data to experiment, and the batch may be the first
            # time this run has been seen remotely.
            parents = self.local.experiments_for({(r.systemid, r.run) for r in exp_rows})
            if parents:
                res = sink.upsert_experiments(parents)
                self.stats.experiments_sent += res.sent
            res = sink.upsert_experimental(exp_rows, xfer_at)
            stamped = self.local.mark_experimental_transferred(exp_rows, xfer_at)
            sink.log_transfer(
                started, "experimental_data", res.sent, res.applied, trigger, True
            )
            self.stats.experimental_sent += res.sent
            moved += res.sent
            log.debug("experimental_data: sent=%d stamped=%d", res.sent, stamped)

        if env_rows:
            res = sink.upsert_environment(env_rows, xfer_at)
            stamped = self.local.mark_environment_transferred(env_rows, xfer_at)
            sink.log_transfer(
                started, "environment", res.sent, res.applied, trigger, True
            )
            self.stats.environment_sent += res.sent
            moved += res.sent
            log.debug("environment: sent=%d stamped=%d", res.sent, stamped)

        self.stats.cycles += 1
        return moved

    def _drain_with_retry(self, trigger: str) -> None:
        """Drain, surviving a remote that is down or a network that blinks.

        Nothing is lost by failing here: rows keep their NULL `xfer` and are
        picked up on the next attempt.
        """
        backoff = self.cfg.retry_base_s
        while not self._stop:
            try:
                self.drain(trigger)
                return
            except Exception as exc:  # noqa: BLE001 - any remote failure
                self.stats.failures += 1
                self._drop_sink()
                log.warning(
                    "transfer failed (%s: %s); retrying in %.0fs",
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                self._sleep(backoff)
                backoff = min(backoff * 2, self.cfg.retry_max_s)

    def _coalesce(self, sub: "Subscriber") -> list:
        """Absorb the rest of a burst before draining.

        KAT publishes one notification per write. Draining on the first one
        means a transaction -- and, against the real server, a network round
        trip -- per row. Since every notification says the same thing ("there
        may be new rows"), holding the door open briefly turns a burst into a
        single batch and costs only `notify_debounce_s` of latency.
        """
        window = self.cfg.notify_debounce_s
        if window <= 0:
            return []
        extra: list = []
        end = time.monotonic() + window
        while not self._stop:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            extra.extend(sub.poll(min(0.1, remaining)))
        return extra

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep so Ctrl-C does not wait out a long backoff."""
        end = time.monotonic() + seconds
        while not self._stop and time.monotonic() < end:
            time.sleep(min(0.25, end - time.monotonic()))

    # -- main loop -----------------------------------------------------------

    def run(self) -> None:
        log.info(
            "local=%s remote=%s origin=%s sweep=%.0fs",
            self.cfg.local.path,
            f"{self.cfg.remote.host}:{self.cfg.remote.port}/{self.cfg.remote.database}",
            self.cfg.origin,
            self.cfg.sweep_interval_s,
        )
        if not self.cfg.local.path.exists():
            raise FileNotFoundError(
                f"No KAT database at {self.cfg.local.path}. "
                "Run tools/make_fake_db.py first."
            )

        # Catch up on anything that accumulated while we were not running,
        # before we start listening. Notifications for these rows are long
        # gone; the watermark is what finds them.
        self._drain_with_retry("startup")

        sub = None
        if self.cfg.zmq.enabled:
            try:
                from .zmqbus import Subscriber

                sub = Subscriber(
                    self.cfg.zmq.endpoint, self.cfg.zmq.topics, self.cfg.zmq.bind
                )
            except ImportError:
                # pyzmq is not installed. Degrading to the sweep is safe -- it
                # is the mechanism that guarantees delivery anyway -- so say so
                # clearly and carry on rather than refusing to start.
                log.warning(
                    "pyzmq is not available to this Python (%s), so 0MQ "
                    "notifications are off and transfers will happen on the "
                    "%.0fs sweep instead. Install it into THIS interpreter "
                    'with:  "%s" -m pip install pyzmq   '
                    "(plain `pip` may belong to a different Python). Or set "
                    "enabled = false under [zmq] in your config to silence "
                    "this.",
                    sys.executable,
                    self.cfg.sweep_interval_s,
                    sys.executable,
                )
            except Exception as exc:  # noqa: BLE001
                # A missing publisher must not stop the service either; 0MQ is
                # an optimisation over the sweep, not a dependency.
                log.warning("0MQ unavailable (%s); falling back to sweep only", exc)

        next_sweep = time.monotonic() + self.cfg.sweep_interval_s
        try:
            while not self._stop:
                woke_for = None
                if sub is not None:
                    wait = max(0.0, min(1.0, next_sweep - time.monotonic()))
                    hits = list(sub.poll(wait))
                    if hits:
                        hits.extend(self._coalesce(sub))
                        tables = {n.table for n in hits if n.table}
                        log.debug(
                            "woken by %d notification(s) %s", len(hits), sorted(tables)
                        )
                        woke_for = "zmq"
                else:
                    self._sleep(max(0.0, min(1.0, next_sweep - time.monotonic())))

                if woke_for is None and time.monotonic() >= next_sweep:
                    woke_for = "sweep"

                if woke_for:
                    self._drain_with_retry(woke_for)
                    next_sweep = time.monotonic() + self.cfg.sweep_interval_s
        finally:
            if sub is not None:
                sub.close()
            self._drop_sink()
            log.info("stopped: %s", self.stats.summary())

    # -- one-shot ------------------------------------------------------------

    def run_once(self) -> int:
        moved = 0
        try:
            moved = self.drain("manual")
        finally:
            self._drop_sink()
        return moved

    def status(self) -> str:
        pending = self.local.pending_counts()
        lines = [
            f"local  : {self.cfg.local.path}",
            f"  pending ExperimentalData : {pending['ExperimentalData']}",
            f"  pending Environment      : {pending['Environment']}",
        ]
        try:
            sink = self._sink_or_connect()
            remote = sink.counts()
            r = self.cfg.remote
            target = f"{r.host}:{r.port}/{r.database}"
            lines.append(f"remote : {target}")
            for table, n in remote.items():
                lines.append(f"  {table:<24} : {n}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"remote : unreachable ({type(exc).__name__}: {exc})")
        finally:
            self._drop_sink()
        return "\n".join(lines)


def doctor(config_path: Path | None) -> str:
    """Report what this Python can actually see.

    Exists because the failure that wastes the most time is not a bug in the
    service but a machine with more than one Python on it: `pip install X`
    reports "Requirement already satisfied" while `import X` still fails,
    because pip and the interpreter are not the same installation. Printing
    the interpreter path next to each import result makes that obvious.
    """
    lines = [
        "python     : " + sys.version.split()[0],
        "executable : " + sys.executable,
        "",
        "dependencies (as seen by THIS interpreter):",
    ]

    def probe(module: str, package: str, needed_for: str) -> str:
        try:
            mod = __import__(module)
        except ImportError:
            return f"  [MISSING] {package:<16} needed for {needed_for}"
        # Modules disagree about where they keep their version: sqlite3 uses
        # `sqlite_version` for the engine, psycopg2 appends build flags to
        # `__version__`. Take the first thing that looks like a version.
        raw = next(
            (
                str(getattr(mod, attr))
                for attr in ("__version__", "sqlite_version", "version")
                if isinstance(getattr(mod, attr, None), str)
            ),
            "installed",
        )
        return f"  [ok]      {package:<16} {raw.split()[0]}"

    lines.append(probe("zmq", "pyzmq", "0MQ notifications (optional)"))
    lines.append(probe("psycopg2", "psycopg2-binary", "the PostgreSQL remote"))
    lines.append(probe("sqlite3", "sqlite3", "everything (standard library)"))

    if any("[MISSING]" in line for line in lines):
        lines += [
            "",
            "To install into this exact interpreter (plain `pip` may belong to",
            "a different Python):",
            f'    "{sys.executable}" -m pip install -r requirements.txt',
        ]

    lines.append("")
    try:
        cfg = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"config     : FAILED ({type(exc).__name__}: {exc})")
        return "\n".join(lines)

    lines += [
        f"config     : {config_path or DEFAULT_CONFIG_PATH}",
        f"  local KAT db   : {cfg.local.path}"
        + ("" if cfg.local.path.exists() else "   <-- DOES NOT EXIST"),
        f"  remote         : {cfg.remote.host}:{cfg.remote.port}/{cfg.remote.database}"
        f" as {cfg.remote.user or '(no user)'}",
        f"  0MQ enabled    : {cfg.zmq.enabled}",
        f"  sweep interval : {cfg.sweep_interval_s:.0f}s",
    ]
    if not cfg.local.path.exists():
        lines.append("")
        lines.append("Create the fake database with:")
        lines.append(f'    "{sys.executable}" tools/make_fake_db.py --reset')
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="kat-transfer",
        description="Move new KAT rows from the local SQLite database to the "
        "remote archive.",
    )
    ap.add_argument("-c", "--config", type=Path, default=None)
    ap.add_argument(
        "--doctor",
        action="store_true",
        help="report this Python, its visible dependencies and the resolved "
        "config, then exit; run this first when something will not start",
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help="transfer everything pending, then exit",
    )
    ap.add_argument(
        "--status",
        action="store_true",
        help="print pending and remote row counts, then exit",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    # Before load_config, which is itself one of the things that can fail.
    if args.doctor:
        print(doctor(args.config))
        return 0

    cfg = load_config(args.config)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else getattr(logging, cfg.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    svc = TransferService(cfg)

    if args.status:
        print(svc.status())
        return 0

    if args.once:
        moved = svc.run_once()
        print(f"transferred {moved} rows")
        return 0

    signal.signal(signal.SIGINT, svc.stop)
    signal.signal(signal.SIGTERM, svc.stop)
    svc.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
