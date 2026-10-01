# How the 0MQ part works

Jonathan's note said: *"There is also 0MQ functionality in the latest KAT
release, so it will be available to notify your external process of new data in
the database."* This file explains what that means, what it does **not** mean,
and how this service uses it.

## The one-paragraph version

ZeroMQ is not a server. There is nothing to install and nothing to administer —
it is a library that makes a socket behave like a messaging system. KAT opens a
**PUB** (publish) socket and shouts short messages onto it whenever it writes
new rows. Our transfer service opens a **SUB** (subscribe) socket, connects to
KAT's, and listens. No broker sits in between, nothing is stored, and nobody
acknowledges anything.

```
   KAT (publisher)                     Transfer service (subscriber)
   ┌──────────────┐                    ┌──────────────────────────┐
   │ writes rows  │                    │                          │
   │ to SQLite    │                    │  SUB socket ─── connect ─┼──┐
   │              │                    │                          │  │
   │ PUB socket ──┼─── bind tcp://*:5556 ◄───────────────────────────┘
   └──────────────┘    "kat.experimentaldata {run:2, rows:10}"
```

## The three things that surprise people

**1. A publisher with no subscribers throws messages away.**
It does not queue them for later. If your service starts thirty seconds after
KAT does, the notifications from those thirty seconds never existed as far as
you are concerned.

**2. Connecting takes a few milliseconds, and messages sent during that window
are also lost.** This is the famous *slow joiner* problem. Even if you start the
subscriber first, the TCP connection and the subscription handshake take time,
and the publisher is free to send during it. This is why `Publisher.__init__`
sleeps 300ms before returning — it is a demo-grade workaround, not a fix. There
is no fix; the pattern does not offer one.

**3. A subscriber that falls behind gets messages dropped, not buffered.**
Once the high water mark is hit, the publisher discards. We set `RCVHWM` to
10,000 in `zmqbus.py`, which bounds our memory use and accepts the loss.

`tests/test_zmq.py::test_notifications_sent_before_subscribing_are_lost` asserts
point 1, so the behaviour is pinned down in the test suite rather than being
folklore.

You can watch it happen:

```bash
python3 tools/kat_simulator.py --rate 5 --duration 60   # terminal 1
python3 tools/zmq_listen.py                             # terminal 2
```

Stop terminal 2, wait ten seconds, start it again. The messages from those ten
seconds are gone for good.

## Why that is fine

Because the notification carries no data and no obligation. It says only
*"there may be new rows"*. The service answers by asking the database the
question it would have asked anyway:

```sql
SELECT ... FROM ExperimentalData WHERE xfer IS NULL
```

That query is true whether one notification arrived, fifty arrived, or none
did. So a lost message costs **latency, not data** — the row sits there with a
NULL `xfer` until the next notification or the next sweep picks it up.

This is the design decision worth defending in the report: 0MQ is an
**optimisation**, and the periodic sweep is the **correctness mechanism**. Set
`enabled = false` under `[zmq]` in `config.toml` and the service still transfers
everything, just with up to `sweep_interval_s` of delay. We test that path too.

If notifications were the only trigger, a single dropped message would mean a
row that never transfers and nobody notices — which for experimental data is
the worst possible failure.

## Message format

Two frames: a topic string and a UTF-8 JSON body.

```python
sock.send_multipart([b"kat.experimentaldata", b'{"systemid":"K4-RIG-01","run":2,"rows":10}'])
```

Subscribers filter on a **byte prefix** of the first frame, which is why topics
are dotted and hierarchical — subscribing to `kat.` gets everything, `kat.environment`
gets only environment notifications. Subscribing to `""` gets everything.

| Topic | Sent when |
|---|---|
| `kat.experimentaldata` | rows appended to `ExperimentalData` |
| `kat.environment` | a sample appended to `Environment` |
| `kat.run.started` | a new run begins |
| `kat.run.finished` | a run ends |

**These topic names are our invention.** KAT's real ones are not published yet.
The service defaults to subscribing to `""` (everything) precisely so it keeps
working whatever Jonathan's names turn out to be, and the body is ignored
entirely — only the fact that a message arrived matters. When the real names are
known, narrow the `topics` list in `config.toml`. No code changes.

## Questions for Jonathan

1. What endpoint does KAT's publisher bind to — is `tcp://127.0.0.1:5556` right,
   and is it configurable?
2. What are the real topic names?
3. Does KAT publish per row, per transaction, or per run? We debounce for
   `notify_debounce_s` (default 0.5s) to coalesce bursts, because per-row
   publishing at 20 rows/s would otherwise mean 20 separate transactions per
   second across the VPN.
4. Does the publisher bind and stay up for the life of the application, or come
   and go with each run? If it comes and goes, we should lengthen the sweep's
   role accordingly.
