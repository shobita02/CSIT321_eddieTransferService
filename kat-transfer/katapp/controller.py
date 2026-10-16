
"""Control the transfer service without blocking the GUI."""

from __future__ import annotations

import json
import os
import queue
import threading

from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from katxfer.config import load as load_config
from katxfer.service import TransferService


class TransferController:
    """
    Manage the transfer worker and communicate with the GUI
    through a thread-safe event queue.
    """

    def __init__(
        self,
        config_path: str | Path | None = None,
        simulation: bool = True,
    ):
        self.config_path = (
            Path(config_path) if config_path is not None else None
        )
        self.simulation = simulation

        self.events = queue.Queue()

        self.thread = None
        self.service = None

        self.status = "Stopped"

        self.lock = threading.Lock()
        self.stop_requested = threading.Event()

    # ==================================
    # Event Notification
    # ==================================

    def _notify(self, event_type, message):
        """
        Send an event to the GUI without updating widgets
        directly from a background thread.
        """
        self.events.put((event_type, message))

    def _set_status(self, status):
        with self.lock:
            self.status = status

        self._notify("status", status)

    def get_events(self):
        """
        Retrieve all pending events for the GUI.
        """
        messages = []

        while True:
            try:
                messages.append(self.events.get_nowait())
            except queue.Empty:
                break

        return messages

    # ==================================
    # Start Transfer
    # ==================================

    def start(self):
        """
        Start the transfer service in a background thread.
        """

        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return False

            self.stop_requested.clear()
            self.status = "Starting"

            self.thread = threading.Thread(
                target=self._run,
                daemon=False,
                name="EddieTransferWorker",
            )

            self.thread.start()

        self._notify("status", "Starting")
        return True

    # ==================================
    # Transfer Worker
    # ==================================

    def _run(self):
        try:
            if self.simulation:
                self._run_simulation()
            else:
                self._run_real_service()

        except Exception as exc:
            self._notify(
                "error",
                f"Transfer service failed: "
                f"{type(exc).__name__}: {exc}",
            )

        finally:
            with self.lock:
                self.service = None

            self._set_status("Stopped")
            self._notify("log", "Transfer service stopped.")

    # ==================================
    # Simulation Mode
    # ==================================

    def _run_simulation(self):
        self._set_status("Running (Simulation)")

        self._notify(
            "log",
            "Simulation started. No data will be transferred.",
        )

        self.stop_requested.wait()

    # ==================================
    # Real Transfer Mode
    # ==================================

    def _run_real_service(self):
        if self.config_path is None:
            raise ValueError(
                "A configuration file is required."
            )

        cfg = load_config(self.config_path)

        # Safety restriction for local testing.
        self._validate_local_gateway(cfg.remote.url)

        if not cfg.local.path.is_file():
            raise FileNotFoundError(
                "Local test SQLite database is missing."
            )

        service = TransferService(cfg)

        with self.lock:
            self.service = service

            if self.stop_requested.is_set():
                service.stop()
                return

        self._set_status("Running")

        self._notify(
            "log",
            "Local transfer service started.",
        )

        # Runs until Stop Transfer is requested.
        service.run()

        self._notify(
            "log",
            service.stats.summary(),
        )

    # ==================================
    # Stop Transfer
    # ==================================

    def stop(self):
        """
        Request a graceful shutdown.
        """

        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                return False

            self.stop_requested.set()
            self.status = "Stopping"
            service = self.service

        self._notify("status", "Stopping")
        self._notify("log", "Shutdown requested.")

        if service is not None:
            service.stop()

        return True

    # ==================================
    # Worker Status
    # ==================================

    def is_active(self):
        with self.lock:
            return (
                self.thread is not None
                and self.thread.is_alive()
            )

    # ==================================
    # Gateway Validation
    # ==================================

    @staticmethod
    def _validate_local_gateway(url):
        """
        Allow only the local test gateway.
        """

        target = urlsplit(url)

        if (
            target.scheme != "http"
            or target.hostname not in (
                "127.0.0.1",
                "localhost",
                "::1",
            )
            or target.port not in (8000, 8080)
        ):
            raise ValueError(
                "Only a local HTTP test gateway "
                "on port 8000 or 8080 is permitted."
            )

    # ==================================
    # Gateway HTTP Request
    # ==================================

    @staticmethod
    def _request_json(url, token=None):
        """
        Perform a GET request and decode a JSON response.
        """

        headers = {
            "Accept": "application/json",
        }

        if token:
            headers["Authorization"] = f"Bearer {token}"

        request = Request(
            url,
            headers=headers,
            method="GET",
        )

        with urlopen(request, timeout=3) as response:
            return json.load(response)

    # ==================================
    # Gateway Monitoring
    # ==================================

    def check_gateway(self):
        """
        Check Gateway connectivity and retrieve the
        remote archive counts.

        This method is called from a background thread
        by dashboard.py.
        """

        try:
            if self.config_path is None:
                raise ValueError(
                    "A configuration file is required."
                )

            cfg = load_config(self.config_path)

            base_url = cfg.remote.url.rstrip("/")

            self._validate_local_gateway(base_url)

            # Step 1: Check basic connectivity.
            self._request_json(
                f"{base_url}/health"
            )

            self._notify(
                "gateway",
                "Connected",
            )

            # Step 2: Retrieve remote statistics.
            token = os.getenv("KAT_API_WRITE_TOKEN")

            if not token:
                self._notify(
                    "log",
                    "Archive statistics unavailable: "
                    "API token is missing.",
                )
                return

            data = self._request_json(
                f"{base_url}/v1/status",
                token=token,
            )

            # The response structure may differ
            # depending on the Gateway implementation.
            # Extract only the known archive counters.
            counts = self._extract_counts(data)

            if counts is not None:
                self._notify(
                    "statistics",
                    counts,
                )
            else:
                self._notify(
                    "log",
                    "Gateway connected, but archive "
                    "statistics response format "
                    "is not recognised.",
                )

        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            self._notify(
                "gateway",
                "Disconnected",
            )

            self._notify(
                "log",
                f"Gateway check failed: "
                f"{type(exc).__name__}",
            )

        except Exception as exc:
            self._notify(
                "gateway",
                "Disconnected",
            )

            self._notify(
                "log",
                f"Gateway check failed: "
                f"{type(exc).__name__}: {exc}",
            )

    # ==================================
    # Archive Count Extraction
    # ==================================

    @staticmethod
    def _extract_counts(data):
        """
        Extract archive counters from supported
        JSON response structures.

        Returns None if no known structure is found.
        """

        if not isinstance(data, dict):
            return None

        # Some APIs return counts directly.
        candidates = [data]

        # Other APIs wrap them in an object.
        for key in (
            "counts",
            "archive",
            "remote",
            "statistics",
        ):
            nested = data.get(key)

            if isinstance(nested, dict):
                candidates.append(nested)

        for candidate in candidates:
            if any(
                key in candidate
                for key in (
                    "experiment",
                    "experimental_data",
                    "environment",
                )
            ):
                try:
                    return {
                        "experiment": int(
                            candidate.get("experiment", 0)
                        ),
                        "experimental_data": int(
                            candidate.get(
                                "experimental_data", 0
                            )
                        ),
                        "environment": int(
                            candidate.get("environment", 0)
                        ),
                    }
                except (TypeError, ValueError):
                    return None

        return None