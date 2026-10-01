#!/usr/bin/env python3
"""Watch the 0MQ notification stream.

Not part of the service. It exists because the quickest way to understand
PUB/SUB is to see it: start the simulator, start this, and watch messages
appear. Then stop this, let the simulator run for ten seconds, start it
again, and notice that the messages sent while it was away are gone forever.
That is the behaviour the sweep in the transfer service compensates for.

    python3 tools/zmq_listen.py
    python3 tools/zmq_listen.py --topics kat.environment
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from katxfer.zmqbus import Subscriber  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", default="tcp://127.0.0.1:5556")
    ap.add_argument(
        "--topics",
        nargs="*",
        default=[""],
        help='prefix filters; default is everything. e.g. --topics kat.environment',
    )
    args = ap.parse_args()

    print(f"listening on {args.endpoint}, topics={args.topics or ['(all)']}")
    print("Ctrl-C to stop\n")
    seen = 0
    with Subscriber(args.endpoint, args.topics) as sub:
        try:
            while True:
                for note in sub.poll(1.0):
                    seen += 1
                    print(f"{note.topic:<24} {json.dumps(note.body)}")
        except KeyboardInterrupt:
            print(f"\n{seen} notifications seen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
