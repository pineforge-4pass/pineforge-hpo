"""Generate binary-loader fixtures independently of the C++ reader, in the build tree."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
from pathlib import Path
import struct


CSV = (
    "timestamp,open,high,low,close,volume\n"
    "-60000,100.25,103.5,98.75,101.125,10.5\n"
    "0,101.125,105.25,99.5,104.75,11.25\n"
    "1700000060123,-0.0,2.5,-1.25,0.0,-0.0\n"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    directory = parser.parse_args().directory
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "good.csv").write_text(CSV, encoding="utf-8")
    rows = [
        tuple(float(row[key]) for key in ("open", "high", "low", "close", "volume"))
        + (int(row["timestamp"]),)
        for row in csv.DictReader(io.StringIO(CSV))
    ]

    def pack(records: list[tuple]) -> bytes:
        return b"".join(struct.pack("<5dq", *row) for row in records)

    def write(name: str, records: list[tuple], **metadata: object) -> None:
        path = directory / name
        path.write_bytes(pack(records))
        Path(str(path) + ".json").write_text(
            json.dumps({"format": "pf_bar_t/v1", "count": len(records), **metadata}),
            encoding="utf-8",
        )

    write("good.bin", rows)
    write("extra-metadata.bin", rows, symbol="TEST:BIN", tf="1", sha256="unchecked")
    (directory / "directory.bin").mkdir(exist_ok=True)
    fifo = directory / "fifo.bin"
    if not fifo.exists():
        os.mkfifo(fifo)
    (directory / "truncated.bin").write_bytes(pack(rows)[:-1])
    (directory / "empty.bin").write_bytes(b"")
    # Sparse: exercise the real fstat/count refusal without allocating or reading 96 GiB.
    with (directory / "too-many.bin").open("wb") as stream:
        stream.truncate((2**31) * 48)
    (directory / "missing-sidecar.bin").write_bytes(pack(rows))
    write("wrong-count.bin", rows, count=len(rows) + 1)
    write("wrong-format.bin", rows, format="pf_bar_t/v2")
    for label, count in (("bool", True), ("float", 3.0), ("string", "3"), ("null", None)):
        write(f"count-{label}.bin", rows, count=count)
    for label, sidecar in (
        ("malformed", "{bad json"),
        ("nonobject", "[]"),
        ("missing-count", '{"format":"pf_bar_t/v1"}'),
        ("missing-format", '{"count":3}'),
        ("exponent-count", '{"format":"pf_bar_t/v1","count":3e0}'),
        ("negative-count", '{"format":"pf_bar_t/v1","count":-3}'),
        ("oversized-sidecar", " " * (1024 * 1024 + 1)),
    ):
        path = directory / f"{label}.bin"
        path.write_bytes(pack(rows))
        Path(str(path) + ".json").write_text(sidecar, encoding="utf-8")
    unreadable = directory / "unreadable-sidecar.bin"
    unreadable.write_bytes(pack(rows))
    Path(str(unreadable) + ".json").mkdir(exist_ok=True)
    for index, field in enumerate(("open", "high", "low", "close")):
        changed = list(rows)
        row = list(changed[1])
        row[index] = math.inf
        changed[1] = tuple(row)
        write(f"nonfinite-{field}.bin", changed)
    for label, volume in (("negative", -1.0), ("infinite", math.inf), ("nan", math.nan)):
        changed = list(rows)
        row = list(changed[1])
        row[4] = volume
        changed[1] = tuple(row)
        write(f"volume-{label}.bin", changed)
    for label, timestamp in (("duplicate", rows[0][5]), ("descending", rows[0][5] - 1)):
        changed = list(rows)
        changed[1] = changed[1][:5] + (timestamp,)
        write(f"timestamp-{label}.bin", changed)
    print(f"dataset binary fixtures generated in {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
