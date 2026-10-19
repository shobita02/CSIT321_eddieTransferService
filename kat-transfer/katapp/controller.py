"""Run the transfer service in a background thread without blocking the GUI."""

from __future__ import annotations

import logging
import os
import queue
import threading
from pathlib import Path

from katxfer.config import load as load_config
from katxfer.service import TransferService
from katxfer.sinks import GatewayAuthError

from .auth import Session

TOKEN_ENV = "KAT_API_WRITE_TOKEN"


class _QueueHandler(logging.Handler):
    """Pass service warnings ("transfer failed ... retrying") to the window."""

    def __init__(self, events: queue.Queue):
        super().__init__(logging.WARNING)
        self.events = events

    def emit(self, record):
        try:
            self.events.put(("warning", record.getMessage()))
        except Exception:  # noqa: BLE001 - logging must never break the service
            self.handleError(record)


class TransferController:
    """
    Own one TransferService run. The GUI reads events and snapshot() from
    its own thread; widgets are never touched from the worker.

    Events: ("warning", text), ("stopped", None), ("auth_failed", text),
    ("crashed", text).
    """

    def __init__(self, config_path: str | Path, session: Session):
        self.config_path = Path(config_path)
        self.session = session
        self.events: queue.Queue = queue.Queue()
        self.service: TransferService | None = None
        self.thread: threading.Thread | None = None
        self.db_path: Path | None = None
        self._handler = _QueueHandler(self.events)

    def start(self) -> None:
        """Load the config with the signed-in token and start transferring."""
        if self.is_active():
            return
        # The token reaches the service in memory only. It is set for this
        # process just long enough for load() to read it.
        os.environ[TOKEN_ENV] = self.session.token
        try:
            cfg = load_config(self.config_path)
        finally:
            os.environ.pop(TOKEN_ENV, None)
        self.db_path = cfg.local.path
        self.service = TransferService(cfg)
        logging.getLogger("katxfer").addHandler(self._handler)
        self.thread = threading.Thread(target=self._run, daemon=False, name="KatTransferWorker")
        self.thread.start()

    def _run(self) -> None:
        try:
            self.service.run()
            self.events.put(("stopped", None))
        except GatewayAuthError:
            self.events.put(("auth_failed", "Your sign-in has expired or was revoked. Please sign in again."))
        except Exception as exc:  # noqa: BLE001 - shown to the user, never swallowed
            self.events.put(("crashed", f"{type(exc).__name__}: {exc}"))
        finally:
            logging.getLogger("katxfer").removeHandler(self._handler)

    def stop(self) -> None:
        """Ask the service to finish its current batch and stop."""
        if self.service is not None:
            self.service.stop()

    def is_active(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def join(self, timeout: float | None = None) -> bool:
        if self.thread is not None:
            self.thread.join(timeout)
        return not self.is_active()

    def get_events(self) -> list[tuple[str, object]]:
        events = []
        while True:
            try:
                events.append(self.events.get_nowait())
            except queue.Empty:
                return events

    def snapshot(self) -> dict:
        stats = self.service.stats if self.service is not None else None
        return {
            "experimental_sent": stats.experimental_sent if stats else 0,
            "environment_sent": stats.environment_sent if stats else 0,
            "experiments_sent": stats.experiments_sent if stats else 0,
            "failures": stats.failures if stats else 0,
            "cycles": stats.cycles if stats else 0,
            "db_path": str(self.db_path) if self.db_path else "",
        }
