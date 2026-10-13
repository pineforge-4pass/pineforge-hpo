#!/usr/bin/env python3
"""Generate the committed synthetic minute bars using integer arithmetic only.

Seed: 20260718. Advance the 32-bit LCG once per bar:
state = (1664525 * state + 1013904223) mod 2**32.
The first 256 closes are 10000 cents, providing flat equity blocks before any entry.
Thereafter the close is 9600 + 25 * triangle((i - 256) mod 96) + (state mod 25) - 12
cents, where triangle(p) = p for p <= 48, otherwise 96 - p. The open is the previous
close (10000 on the first bar). Wicks extend the open/close range by 25 cents.
Volume is 800 + state mod 401. Times start at 1704067200000 ms, one minute apart.
"""

from __future__ import annotations

import argparse
from pathlib import Path

SEED = 20260718
BARS = 2048
FLAT_BARS = 256
START_MS = 1704067200000
STEP_MS = 60000


def price(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def csv_bytes() -> bytes:
    state = SEED
    prior = 10000
    lines = ["timestamp,open,high,low,close,volume"]
    for index in range(BARS):
        state = (1664525 * state + 1013904223) % (1 << 32)
        phase = (index - FLAT_BARS) % 96
        triangle = phase if phase <= 48 else 96 - phase
        close = 10000 if index < FLAT_BARS else 9600 + 25 * triangle + state % 25 - 12
        high = max(prior, close) + 25
        low = min(prior, close) - 25
        lines.append(
            f"{START_MS + index * STEP_MS},{price(prior)},{price(high)},"
            f"{price(low)},{price(close)},{800 + state % 401}"
        )
        prior = close
    return ("\n".join(lines) + "\n").encode("ascii")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).with_name("ohlcv.csv")
    )
    args = parser.parse_args()
    args.out.write_bytes(csv_bytes())


if __name__ == "__main__":
    main()
