"""Generate binary-loader fixtures independently of the C++ reader, in the build tree."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import struct
import shutil


CSV = (
    "timestamp,open,high,low,close,volume\n"
    "-60000,100.25,103.5,98.75,101.125,10.5\n"
    "0,101.125,105.25,99.5,104.75,11.25\n"
    "1700000060123,-0.0,2.5,-1.25,0.0,-0.0\n"
)

# Each row differs from converter metadata in one field. None means remove the key.
SIDECAR_MUTATIONS = (
    ("record-size-wrong", "record_size", 47),
    ("record-size-missing", "record_size", None),
    ("record-size-bool", "record_size", True),
    ("record-size-float", "record_size", 48.0),
    ("record-size-string", "record_size", "48"),
    ("symbol-missing", "symbol", None),
    ("symbol-number", "symbol", 42),
    ("symbol-bool", "symbol", True),
    ("tf-missing", "tf", None),
    ("tf-number", "tf", 1),
    *((f"tf-bad-{index}", "tf", tf) for index, tf in enumerate(
        ("", "0", "01", "D", "1d", " 1", "1 ", "1\n", "100000", "10000D", "1DD", "x1")
    )),
    ("first-ts-mismatch", "first_ts", -59999),
    ("last-ts-mismatch", "last_ts", 1700000060124),
    ("first-ts-missing", "first_ts", None),
    ("last-ts-missing", "last_ts", None),
    ("first-ts-bool", "first_ts", False),
    ("last-ts-float", "last_ts", 1700000060123.0),
    ("first-ts-string", "first_ts", "-60000"),
    ("last-ts-overflow", "last_ts", 2**63),
    ("sha256-missing", "sha256", None),
    ("sha256-number", "sha256", 42),
    ("sha256-short", "sha256", "abc"),
    ("sha256-uppercase", "sha256", "A" * 64),
    ("sha256-nonhex", "sha256", "g" * 64),
    ("sha256-unchecked", "sha256", "unchecked"),
)
SIDECAR_REFUSALS = tuple(name + ".bin" for name, _, _ in SIDECAR_MUTATIONS) + (
    "digest-mismatch.bin",
)


def converter_metadata(data: bytes, *, symbol: str = "TEST:BIN", tf: str = "1") -> dict:
    """The chart converter's actual metadata, independently computed from packed bytes."""
    return {
        "format": "pf_bar_t/v1", "record_size": 48, "symbol": symbol, "tf": tf,
        "count": len(data) // 48, "first_ts": struct.unpack_from("<q", data, 40)[0],
        "last_ts": struct.unpack_from("<q", data, len(data) - 8)[0],
        "sha256": hashlib.sha256(data).hexdigest(),
    }


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
        data = pack(records)
        path.write_bytes(data)
        Path(str(path) + ".json").write_text(
            json.dumps({**converter_metadata(data), **metadata}, sort_keys=True,
                       separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    write("good.bin", rows)
    write("extra-metadata.bin", rows, extra="allowed", symbol="", tf="9999M")
    write("single.bin", rows[:1])
    write("int64-timestamps.bin", [rows[0][:5] + (-(2**63),), rows[1][:5] + (2**63 - 1,)])
    for name, field, value in SIDECAR_MUTATIONS:
        write(name + ".bin", rows)
        path = directory / (name + ".bin.json")
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if value is None:
            metadata.pop(field)
        else:
            metadata[field] = value
        path.write_text(json.dumps(metadata), encoding="utf-8")
    # Corrupt the BAR BYTES of a copy, preserving the complete original sidecar byte-for-byte.
    # Byte zero is a finite price's low mantissa byte: metadata endpoints and all values stay valid.
    corrupt = directory / "digest-mismatch.bin"
    shutil.copyfile(directory / "good.bin", corrupt)
    shutil.copyfile(directory / "good.bin.json", Path(str(corrupt) + ".json"))
    with corrupt.open("r+b") as stream:
        first = stream.read(1)
        stream.seek(0)
        stream.write(bytes([first[0] ^ 1]))
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
    (directory / "fifo-sidecar.bin").write_bytes(pack(rows))
    sidecar_fifo = directory / "fifo-sidecar.bin.json"
    if not sidecar_fifo.exists():
        os.mkfifo(sidecar_fifo)
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
