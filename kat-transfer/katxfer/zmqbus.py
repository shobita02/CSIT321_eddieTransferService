"""The ZeroMQ notification channel.

What 0MQ is doing here, in one paragraph: KAT opens a PUB socket and, every
time it writes new rows, shouts a short message onto it. Our service opens a
SUB socket, connects to KAT, and listens. There is no broker, no queue server
and no acknowledgement — PUB/SUB is a loudspeaker, not a mailbox.

Three consequences shape this file and `service.py`:

* **Subscribers that are not connected yet miss everything.** This is the
  "slow joiner" problem: a PUB socket drops messages when it has no matching
  subscriber, and a TCP connect plus subscription handshake takes a few
  milliseconds. Start the service before the run, and still never trust it to
  have seen every message.

* **A slow subscriber gets dropped, not buffered.** Once the publisher's high
  water mark is reached, further messages for that subscriber are discarded.

* **Therefore the notification is a hint, never the data.** It says "there
  may be new rows", and the service answers by querying `WHERE xfer IS NULL`,
  which is true regardless of how many messages were lost. The periodic sweep
  in `service.py` is what makes the system correct; 0MQ is what makes it
  fast. If you delete the 0MQ code entirely the service still transfers
  everything, just with up to `sweep_interval_s` of latency.

Message format (ours, until KAT's is published): two frames, a topic string
and a UTF-8 JSON body. Subscribers filter on a byte-prefix of the first
frame, so topics are hierarchical with `.` separators.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Iterator

try:
    import zmq
except ImportError as exc:  # pragma: no cover - depends on the environment
    # Importing this module at all means something wants notifications, so a
    # raw ModuleNotFoundError here is unhelpful.
    #
    # The common cause is not a missing package but several Pythons on one
    # machine: `pip` installs into one, the script runs under another, and the
    # user is told "Requirement already satisfied" while the import still
    # fails. Naming the running interpreter turns that into a one-line fix.
    import sys as _sys

    raise ImportError(
        "pyzmq is needed for 0MQ notifications, but is not installed for the "
        "Python currently running:\n"
        f"    {_sys.executable}\n\n"
        "If `pip install pyzmq` already said \"Requirement already satisfied\", "
        "then pip belongs to a DIFFERENT Python than this one. Install it into "
        "this interpreter with:\n"
        f'    "{_sys.executable}" -m pip install pyzmq\n\n'
        "The transfer service itself does not need pyzmq: set enabled = false "
        "under [zmq] in config.toml and it runs on its periodic sweep instead."
    ) from exc

log = logging.getLogger(__name__)

TOPIC_EXPERIMENTAL = "kat.experimentaldata"
TOPIC_ENVIRONMENT = "kat.environment"
TOPIC_RUN_STARTED = "kat.run.started"
TOPIC_RUN_FINISHED = "kat.run.finished"


@dataclass(frozen=True)
class Notification:
    topic: str
    body: dict

    @property
    def table(self) -> str | None:
        if self.topic.startswith(TOPIC_EXPERIMENTAL):
            return "ExperimentalData"
        if self.topic.startswith(TOPIC_ENVIRONMENT):
            return "Environment"
        return None


class Publisher:
    """Stands in for KAT's publisher until the real one is wired up."""

    def __init__(self, endpoint: str = "tcp://*:5556", linger_ms: int = 500) -> None:
        self.endpoint = endpoint
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.PUB)
        self._sock.setsockopt(zmq.LINGER, linger_ms)
        self._sock.bind(endpoint)
        # Give subscribers already waiting a moment to complete their
        # handshake, otherwise the first few publishes go nowhere. This is the
        # standard mitigation for the slow-joiner problem in a demo; a real
        # publisher that runs for hours does not care.
        time.sleep(0.3)
        log.info("0MQ publisher bound to %s", endpoint)

    def send(self, topic: str, body: dict) -> None:
        self._sock.send_multipart(
            [topic.encode("utf-8"), json.dumps(body).encode("utf-8")]
        )

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> "Publisher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Subscriber:
    """Listens for KAT's notifications.

    `poll` returns notifications that arrived within a timeout, so the
    service's main loop stays single-threaded: it can wait on 0MQ and still
    wake up on schedule to run its sweep.
    """

    def __init__(
        self,
        endpoint: str = "tcp://127.0.0.1:5556",
        topics: list[str] | None = None,
        bind: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.SUB)
        self._sock.setsockopt(zmq.LINGER, 0)
        # Bounded inbox. If we fall this far behind, dropping is the right
        # answer: the sweep will find whatever we missed anyway.
        self._sock.setsockopt(zmq.RCVHWM, 10_000)
        for topic in topics or [""]:
            self._sock.setsockopt_string(zmq.SUBSCRIBE, topic)
        # SUB normally connects out to a bound PUB. `bind` is here only for
        # odd network layouts where KAT connects to us instead.
        if bind:
            self._sock.bind(endpoint)
        else:
            self._sock.connect(endpoint)
        self._poller = zmq.Poller()
        self._poller.register(self._sock, zmq.POLLIN)
        log.info(
            "0MQ subscriber %s %s (topics=%r)",
            "bound to" if bind else "connected to",
            endpoint,
            topics or [""],
        )

    def poll(self, timeout_s: float) -> Iterator[Notification]:
        """Yield every notification available within `timeout_s`.

        Blocks at most once; after the first message arrives the rest are
        drained without waiting, so a burst collapses into a single cycle.
        """
        deadline_ms = max(0, int(timeout_s * 1000))
        if not dict(self._poller.poll(deadline_ms)):
            return
        while True:
            try:
                frames = self._sock.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                return
            yield self._decode(frames)

    @staticmethod
    def _decode(frames: list[bytes]) -> Notification:
        topic = frames[0].decode("utf-8", "replace")
        body: dict = {}
        if len(frames) > 1 and frames[1]:
            try:
                decoded = json.loads(frames[1].decode("utf-8", "replace"))
                if isinstance(decoded, dict):
                    body = decoded
            except json.JSONDecodeError:
                # A malformed notification is not a reason to stop: the sweep
                # covers us, so log it and carry on.
                log.warning("undecodable notification body on topic %s", topic)
        return Notification(topic=topic, body=body)

    def close(self) -> None:
        self._poller.unregister(self._sock)
        self._sock.close()

    def __enter__(self) -> "Subscriber":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
