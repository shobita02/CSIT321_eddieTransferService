"""Run a blocking call (a gateway request) off the Tk thread and deliver its result back on it."""

from __future__ import annotations

import threading


def run_in_background(widget, func, on_done, poll_ms: int = 100) -> None:
    """Call func() in a worker thread, then on_done(result, error) on the Tk thread."""
    outcome: dict = {}

    def worker():
        try:
            outcome["result"] = func()
        except Exception as exc:  # noqa: BLE001 - handed to on_done
            outcome["error"] = exc

    thread = threading.Thread(target=worker, daemon=True, name="KatAppRequest")
    thread.start()

    def check():
        if thread.is_alive():
            widget.after(poll_ms, check)
            return
        try:
            exists = bool(widget.winfo_exists())
        except Exception:  # noqa: BLE001 - window already destroyed
            exists = False
        if exists:
            on_done(outcome.get("result"), outcome.get("error"))

    widget.after(poll_ms, check)
