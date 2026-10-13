"""Retrieve a complete run using a read token, with optional CSV output."""
import argparse
import csv
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from katxfer.config import RemoteConfig
from katxfer.sinks import HttpSink


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--systemid", required=True)
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--ca-file")
    args = parser.parse_args()
    token = os.environ.get("KAT_API_READ_TOKEN", "")
    if not token:
        raise SystemExit("Set KAT_API_READ_TOKEN for this terminal")
    with HttpSink(RemoteConfig(url=args.url, token=token, ca_file=args.ca_file), "real", "researcher") as sink:
        rows = list(sink.records("experimental-data", systemid=args.systemid, run=args.run))
    print(f"Retrieved {len(rows)} rows")
    if args.csv and rows:
        with args.csv.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"Saved {args.csv}")
    elif rows:
        print(json.dumps(rows[0], indent=2))


if __name__ == "__main__":
    main()
