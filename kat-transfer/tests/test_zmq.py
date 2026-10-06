"""Tests for the 0MQ notification channel.

The interesting assertions here are the negative ones. PUB/SUB is allowed to
lose messages, and the service is built so that losing them costs latency and
nothing else. `test_notifications_lost_while_away_are_still_transferred` is the
one to point at if anyone asks why we did not use a message queue.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from conftest import local_pending, remote_count

from katxfer.service import TransferService
from katxfer.zmqbus import (
    TOPIC_ENVIRONMENT,
    TOPIC_EXPERIMENTAL,
    Notification,
    Publisher,
    Subscriber,
)

ENDPOINT = "tcp://127.0.0.1:55571"


@pytest.fixture()
def pubsub():
    pub = Publisher(ENDPOINT.replace("127.0.0.1", "*"))
    sub = Subscriber(ENDPOINT, topics=[""])
    time.sleep(0.3)  # let the subscription handshake finish (slow joiner)
    yield pub, sub
    sub.close()
    pub.close()


def drain(sub, timeout_s=1.0):
    """Collect notifications until the stream goes quiet."""
    out = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        got = list(sub.poll(0.1))
        if got:
            out.extend(got)
            deadline = time.monotonic() + 0.2
    return out


def test_notification_round_trip(pubsub):
    pub, sub = pubsub
    pub.send(TOPIC_EXPERIMENTAL, {"systemid": "K4-RIG-01", "run": 1, "rows": 10})

    got = drain(sub)

    assert len(got) == 1
    assert got[0].topic == TOPIC_EXPERIMENTAL
    assert got[0].table == "ExperimentalData"
    assert got[0].body["run"] == 1


def test_topic_filtering_is_a_prefix_match():
    """SUB filters on a byte prefix, which is why topics are dotted strings."""
    pub = Publisher("tcp://*:55572")
    sub = Subscriber("tcp://127.0.0.1:55572", topics=[TOPIC_ENVIRONMENT])
    time.sleep(0.3)
    try:
        pub.send(TOPIC_EXPERIMENTAL, {"ignored": True})
        pub.send(TOPIC_ENVIRONMENT, {"kept": True})

        got = drain(sub)

        assert [n.topic for n in got] == [TOPIC_ENVIRONMENT]
    finally:
        sub.close()
        pub.close()


def test_malformed_body_does_not_raise(pubsub):
    """A bad message must not take the service down; the sweep still covers us."""
    pub, sub = pubsub
    pub._sock.send_multipart([TOPIC_EXPERIMENTAL.encode(), b"{not json"])

    got = drain(sub)

    assert len(got) == 1
    assert got[0].body == {}


def test_notifications_sent_before_subscribing_are_lost():
    """The slow-joiner problem, asserted rather than described.

    This is why the service cannot treat a notification as a delivery
    guarantee, and why `sweep_interval_s` exists.
    """
    pub = Publisher("tcp://*:55573")
    pub.send(TOPIC_EXPERIMENTAL, {"sent": "before anyone was listening"})

    sub = Subscriber("tcp://127.0.0.1:55573", topics=[""])
    time.sleep(0.3)
    try:
        assert drain(sub, timeout_s=0.5) == []
    finally:
        sub.close()
        pub.close()


def test_notifications_lost_while_away_are_still_transferred(cfg):
    """The property that makes losing notifications survivable.

    Rows are written with no subscriber connected, so every notification about
    them is gone. The service still finds them, because it asks the database
    what is pending rather than trusting the message stream.
    """
    pub = Publisher("tcp://*:55574")
    conn = sqlite3.connect(cfg.local.path)
    conn.execute(
        'INSERT INTO ExperimentalData (systemid, run, "row", timestamp, source, '
        'data, xfer) VALUES (?,?,?,?,?,?,NULL)',
        ("K4-RIG-01", 1, 500, "2026-10-01 11:00:00.000", "ADC-CH0", "unannounced"),
    )
    conn.commit()
    conn.close()
    pub.send(TOPIC_EXPERIMENTAL, {"rows": 1})  # nobody hears this
    pub.close()

    moved = TransferService(cfg).run_once()

    assert moved == 26
    assert local_pending(cfg.local.path) == (0, 0)
    assert remote_count(cfg.remote.path, "experimental_data") == 21


def _run_with_fake_subscriber(cfg, monkeypatch, *, notify):
    """Keep real transfers, replacing only notification delivery and time."""
    from katxfer import service, zmqbus

    cfg.zmq.enabled = True
    svc = TransferService(cfg)

    class FakeClock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            raise AssertionError("the run-loop tests must not sleep or retry")

    clock = FakeClock()

    class FakeSubscriber:
        polls = 0
        closed = False

        def poll(self, timeout_s):
            self.polls += 1
            if self.polls > 1:
                svc.stop()
                return []

            # run() has completed its real startup drain before polling.
            assert local_pending(cfg.local.path) == (0, 0)
            assert remote_count(cfg.remote.path, "experimental_data") == 20
            assert remote_count(cfg.remote.path, "environment") == 5
            conn = sqlite3.connect(cfg.local.path)
            try:
                conn.execute(
                    'INSERT INTO ExperimentalData (systemid, run, "row", '
                    'timestamp, source, data, xfer) VALUES (?,?,?,?,?,?,NULL)',
                    ("K4-RIG-01", 1, 999, "2026-10-01 11:00:00.000",
                     "ADC-CH0", "new experimental"),
                )
                conn.execute(
                    "INSERT INTO Environment (systemid, timestamp, data, xfer) "
                    "VALUES (?,?,?,NULL)",
                    ("K4-RIG-01", "2026-10-01 11:00:00.000", "new environment"),
                )
                conn.commit()
            finally:
                conn.close()
            assert local_pending(cfg.local.path) == (1, 1)

            if notify:
                # Time stays before the deadline: only the message can wake us.
                return [Notification(TOPIC_EXPERIMENTAL, {})]
            # An empty poll reaches the deadline without a real wait.
            assert timeout_s > 0
            clock.now += timeout_s
            return []

        def close(self):
            self.closed = True

    sub = FakeSubscriber()
    monkeypatch.setattr(zmqbus, "Subscriber", lambda *args: sub)
    monkeypatch.setattr(service, "time", clock)
    svc.run()

    assert sub.polls == 2
    assert sub.closed
    assert local_pending(cfg.local.path) == (0, 0)
    assert remote_count(cfg.remote.path, "experimental_data") == 21
    assert remote_count(cfg.remote.path, "environment") == 6
    assert remote_count(cfg.remote.path, "experiment") == 2

    conn = sqlite3.connect(cfg.remote.path)
    try:
        return conn.execute(
            "SELECT table_name, rows_sent, trigger, ok FROM transfer_log "
            "WHERE trigger != 'startup'"
        ).fetchall()
    finally:
        conn.close()


def test_run_transfers_on_zmq_notification(cfg, monkeypatch):
    """A notification transfers post-startup rows through the running service."""
    rows = _run_with_fake_subscriber(cfg, monkeypatch, notify=True)

    assert sorted(rows) == [
        ("environment", 1, "zmq", 1),
        ("experimental_data", 1, "zmq", 1),
    ]


def test_run_sweep_recovers_without_notification(cfg, monkeypatch):
    """The periodic sweep recovers post-startup rows with a silent subscriber."""
    rows = _run_with_fake_subscriber(cfg, monkeypatch, notify=False)

    assert sorted(rows) == [
        ("environment", 1, "sweep", 1),
        ("experimental_data", 1, "sweep", 1),
    ]
