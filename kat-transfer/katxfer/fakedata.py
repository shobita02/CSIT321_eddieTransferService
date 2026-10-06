"""Generation of plausible fake KAT data.

Shared by the one-shot seeder (tools/make_fake_db.py) and the live simulator
(tools/kat_simulator.py) so that a database you seed and a database you grow
look the same.

`data` is a JSON object, as real KAT writes it (built from a J4 Record), and
the remote archive stores it in a `json` column, which rejects anything else.
The key names are still guesses; they are isolated in this one module so that
when Jonathan's k4 generator lands, matching them means editing
`experimental_payload` / `environment_payload` and nothing else.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# KAT stores timestamps as TIMESTAMP in SQLite, which is really just text.
# We write ISO-8601 with a space separator, which is what SQLite's own
# datetime() produces and what both SQLite and PostgreSQL parse without help.
TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)[:-3]


def parse_ts(text: str | int | float) -> datetime:
    """Tolerant reader for whatever KAT actually wrote.

    KAT 1.0.3 writes through sqlite-jdbc's `setTimestamp`, which stores
    milliseconds since the Unix epoch as an INTEGER (e.g. 1790898246165), not
    text. Our generated data uses ISO text. Accept both.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)) or text.strip().isdigit():
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
    text = text.strip().replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in (TS_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unrecognised timestamp: {text!r}")


SYSTEM_IDS = ["K4-RIG-01", "K4-RIG-02", "K4-BENCH-A"]
SOURCES = ["ADC-CH0", "ADC-CH1", "THERMO-01", "LOADCELL-02", "FLOW-01"]

CHANNELS = ("strain", "temp_c", "pressure_kpa", "flow_lpm", "volt_mv")


@dataclass
class Rig:
    """A little physical model, so the fake numbers drift like real ones.

    Pure noise is a bad test fixture: it hides bugs where rows are shipped out
    of order or silently duplicated, because every value looks like every
    other value. A slow sinusoid plus noise makes those bugs visible in a
    plot.
    """

    systemid: str
    seed: int = 0

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed or hash(self.systemid) & 0xFFFF)
        self._phase = self._rng.random() * math.tau

    def reading(self, row: int) -> dict[str, float]:
        t = row / 50.0
        r = self._rng
        return {
            "strain": round(120 + 18 * math.sin(t + self._phase) + r.gauss(0, 1.5), 3),
            "temp_c": round(22.5 + 4 * math.sin(t / 7 + self._phase) + r.gauss(0, 0.2), 3),
            "pressure_kpa": round(101.3 + 2.2 * math.sin(t / 3) + r.gauss(0, 0.15), 3),
            "flow_lpm": round(max(0.0, 12 + 3 * math.cos(t / 5) + r.gauss(0, 0.4)), 3),
            "volt_mv": round(2048 + 300 * math.sin(t * 2 + self._phase) + r.gauss(0, 12), 1),
        }

    def experimental_payload(self, row: int) -> str:
        """The TEXT that goes in ExperimentalData.data.

        A JSON object of channel -> value, in CHANNELS order. Adjust the keys
        when the real k4 payload format is known.
        """
        vals = self.reading(row)
        return json.dumps({c: vals[c] for c in CHANNELS})

    def environment_payload(self, tick: int) -> str:
        """The TEXT that goes in Environment.data.

        Ambient conditions as a JSON object.
        """
        r = self._rng
        t = tick / 20.0
        amb = 19.0 + 3.5 * math.sin(t / 11) + r.gauss(0, 0.15)
        rh = 48 + 9 * math.sin(t / 17 + 1.1) + r.gauss(0, 0.6)
        baro = 1013.2 + 4 * math.sin(t / 29) + r.gauss(0, 0.3)
        return json.dumps(
            {
                "ambient_c": round(amb, 2),
                "relative_humidity": round(rh, 1),
                "barometric_hpa": round(baro, 1),
                "mains_hz": round(50 + r.gauss(0, 0.02), 3),
            }
        )


def experiment_rows(
    rigs: list[Rig], runs_per_rig: int
) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for rig in rigs:
        for run in range(1, runs_per_rig + 1):
            out.append(
                (
                    rig.systemid,
                    run,
                    f"Synthetic run {run} on {rig.systemid} "
                    f"({len(CHANNELS)} channels: {', '.join(CHANNELS)})",
                )
            )
    return out


def experimental_data_rows(
    rig: Rig,
    run: int,
    count: int,
    start: datetime,
    interval: timedelta = timedelta(milliseconds=200),
) -> list[tuple[str, int, int, str, str, str, None]]:
    rng = random.Random((rig.seed, run, count).__hash__() & 0xFFFFFFFF)
    rows = []
    for i in range(1, count + 1):
        ts = start + interval * (i - 1)
        rows.append(
            (
                rig.systemid,
                run,
                i,
                fmt_ts(ts),
                rng.choice(SOURCES),
                rig.experimental_payload(i),
                None,  # xfer: NULL means "not yet transferred"
            )
        )
    return rows


def environment_rows(
    rig: Rig,
    count: int,
    start: datetime,
    interval: timedelta = timedelta(seconds=30),
) -> list[tuple[str, str, str, None]]:
    rows = []
    for i in range(count):
        ts = start + interval * i
        rows.append((rig.systemid, fmt_ts(ts), rig.environment_payload(i), None))
    return rows
