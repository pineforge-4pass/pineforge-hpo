"""Independent wire fixtures, using only the documented descriptor and column rules."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys


def canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def descriptor(tapes: dict[str, Path] | None = None) -> dict:
    datasets = {}
    # The fixture has one bar per tape: all five values are the price, timestamp is zero.
    for dataset_id, symbol, price in (
        ("second", "asset-b", 2.0),
        ("first", "asset-a", 1.0),
    ):
        bars = [([price] * 5, 0)]
        if tapes is not None:
            with tapes[dataset_id].open(newline="", encoding="utf-8") as source:
                bars = [
                    (
                        [
                            float(row[name])
                            for name in ("open", "high", "low", "close", "volume")
                        ],
                        int(row["timestamp"]),
                    )
                    for row in csv.DictReader(source)
                ]
        values = b"pineforge:hpo:chart-bars:le:v1\0" + b"".join(
            struct.pack("<5dq", *fields, timestamp) for fields, timestamp in bars
        )
        datasets[dataset_id] = {
            "chart_timezone": "UTC",
            "count": {"int": str(len(bars))},
            "facts": {
                "currency": "units",
                "mincontract": {"binary64": "3ff0000000000000"},
                "mintick": {"binary64": "3ff0000000000000"},
                "pointvalue": {"binary64": "3ff0000000000000"},
                "session": "24x7",
                "tickerid": symbol,
                "timezone": "UTC",
                "type": "synthetic",
            },
            "first_ts": {"int": str(bars[0][1])},
            "identity_revision": {"int": "1"},
            "input_tf": "60",
            "last_ts": {"int": str(bars[-1][1])},
            "parser": "chart-csv/v1",
            "script_tf": "60",
            "symbol": symbol,
            "values_sha256": hashlib.sha256(values).hexdigest(),
            "verification": "parsed",
        }
    return {
        "artifact_key": "artifact",
        "bindings": {
            "Market": {
                "choices": ["asset-a", "asset-b"],
                "datasets": {"asset-b": "second", "asset-a": "first"},
                "role": "chart",
            }
        },
        "comparison": {
            "alignment": "exact_timestamps",
            "pruner": "none",
            "warmup": "full_input",
        },
        "datasets": datasets,
        "fixed_inputs": {
            "Count": {"int": "2"},
            "Enabled": True,
            "Label": "α",
            "Threshold": {"binary64": "3fe0000000000000"},
        },
        "overrides": {"slippage": {"int": "0"}},
        "plugin_sha256": "a" * 64,
        "schema": "pineforge-hpo-data-context/v1",
        "symbol_feeds": {},
    }


def digest(value: dict) -> str:
    payload = b"pineforge:hpo:data-context:v1\0" + canonical(value).encode()
    return hashlib.sha256(payload).hexdigest()


def space() -> dict:
    return {
        "parameters": {
            "Length": {"kind": "integer", "low": 1, "high": 2, "step": 1, "log": False},
            "Market": {"kind": "categorical", "choices": ["asset-a", "asset-b"]},
        },
        "objective": {
            "expression": "metrics.all.net_profit",
            "direction": "maximize",
            "constraints": [],
        },
    }


def space_digest() -> bytes:
    # Independently apply the search-space v1 tags, decimal integer strings and choice tags.
    value = space()
    value["parameters"]["Length"].update(low="1", high="2", step="1")
    value["parameters"]["Market"]["choices"] = [
        ["string", "asset-a"],
        ["string", "asset-b"],
    ]
    value["space_hash_version"] = 1
    return hashlib.sha256(canonical(value).encode()).digest()


def rows() -> list[dict]:
    return [
        {
            "trial_id": identifier,
            "status": "ok",
            "feasible": True,
            "objective": score,
            "parameters": {"Length": length, "Market": "asset-a"},
            "constraint_values": [],
            "symbol": "asset-a",
            "dataset_id": "first",
            "data_context_sha256": digest(descriptor()),
        }
        for identifier, length, score in ((7, 1, 3.0), (19, 2, 4.0))
    ]


def result() -> dict:
    return {
        "space": space(),
        "dataset_id": None,
        "dataset_ids": ["first", "second"],
        "data_context_sha256": digest(descriptor()),
        "applied_runtime": {"data_context": descriptor()},
        "trials_out": "all",
        "trials_completed": 2,
        "trials": [{**row, "space": space()} for row in rows()],
    }


def binary(*, bound: bool = True, feeds: dict | None = None) -> bytes:
    # Fixed header 80, two four-byte descriptors, optional feed block, context 4 + 32.
    extension = b""
    if feeds is not None:
        encoded = canonical(feeds).encode()
        extension += struct.pack("<I", len(encoded)) + encoded
    if bound:
        extension += struct.pack("<I", 1) + bytes.fromhex(digest(descriptor()))
    header_size = 88 + len(extension)
    # Two IDs, two states, Length indices, Market indices, then objective binary64s.
    payload = (
        struct.pack("<QQ", 7, 19)
        + b"\0\0"
        + struct.pack("<ii", 0, 1)
        + struct.pack("<ii", 0, 0)
        + struct.pack("<dd", 3.0, 4.0)
    )
    header = struct.pack(
        "<8sHHIQQIIII32s",
        b"PFHWARM\0",
        2,
        (2 if bound else 0) | (1 if feeds is not None else 0),
        header_size,
        header_size + len(payload),
        2,
        2,
        1,
        0,
        0,
        space_digest(),
    )
    return header + b"\x01\x01\0\0\x04\x01\0\0" + extension + payload


def main() -> None:
    executable, plugin, destination = sys.argv[1:]
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    (root / "descriptor.json").write_text(canonical(descriptor()), encoding="utf-8")
    (root / "context.sha256").write_text(digest(descriptor()), encoding="ascii")
    (root / "parent.json").write_text(canonical(result()), encoding="utf-8")
    (root / "decoded.json").write_text(canonical(rows()), encoding="utf-8")
    (root / "bound.bin").write_bytes(binary())
    (root / "legacy.bin").write_bytes(binary(bound=False))
    feeds = {"symbols": {"reference": {"facts": {"currency": "units"}, "feeds": {}}}}
    (root / "feeds.json").write_text(canonical(feeds), encoding="utf-8")
    (root / "dual.bin").write_bytes(binary(feeds=feeds))
    (root / "legacy-feeds.bin").write_bytes(binary(bound=False, feeds=feeds))
    subprocess.run([executable, plugin, str(root)], check=True)


if __name__ == "__main__":
    main()
