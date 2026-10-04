#!/usr/bin/env python3
"""Publish bounded, path-free CSV evidence with verified provenance sidecars."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import platform
import subprocess


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    args.output.mkdir(parents=True, exist_ok=True)
    sources = [
        "src/core/tpe_sampler.cpp",
        "src/core/pruner.cpp",
        "src/core/ordinal_set.hpp",
        "src/cli/main.cpp",
        "src/cli/batch_executor.hpp",
        "include/pineforge/hpo/sampler.hpp",
        "include/pineforge/hpo/pruner.hpp",
    ]
    sources.extend(
        str(path.relative_to(root))
        for path in sorted((root / "benchmarks/scaling").glob("*"))
        if path.is_file()
    )
    common = {
        "schema": "pineforge-hpo.scaling-evidence.v1",
        "baseline_revision": "6ccb6d4",
        "version": "0.4.0",
        "platform": platform.platform(),
        "cpu": "Intel Xeon Platinum 8375C, 8 vCPU, 16 GiB",
        "compiler": subprocess.check_output(
            ["g++", "--version"], text=True
        ).splitlines()[0],
        "build": "Release, C++17, -O3 -DNDEBUG",
        "source_sha256": {name: digest(root / name) for name in sources},
        "engine_revision": "5718c5dc05086fc5b66b4cb565617efe837131e3",
        "codegen_revision": "5bf595b5e826562c131710b3e93e96e6f8e0a0e4",
        "coco_repository": "numbbo/coco-experiment",
        "coco_revision": "dd4bd1f0cc7699a2b612448d85aafb94636fe947",
    }
    measured = args.evidence / "measured-binaries.json"
    if measured.exists():
        common["measured_binary_sha256"] = json.loads(measured.read_text())

    def publish(name, rows, metadata):
        path = args.output / f"2026-10-03-scale-{name}.csv"
        fields = sorted({key for row in rows for key in row})
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        sidecar = {**common, **metadata, "rows": len(rows), "csv_sha256": digest(path)}
        path.with_suffix(".csv.metadata.json").write_text(
            json.dumps(sidecar, indent=2) + "\n"
        )

    asks = []
    for phase, filename in (
        ("before", "profile-before.ndjson"),
        ("after", "ask-final.ndjson"),
    ):
        asks.extend(
            {"phase": phase, **row} for row in read_lines(args.evidence / filename)
        )
    publish(
        "ask",
        asks,
        {
            "protocol": "random prefill, then timed ask; after uses 256 asks with batch8 feedback",
            "limitations": "before is a snapshot probe, not a full million-trial adaptive run",
        },
    )
    profiles = []
    for directory in ("million-final", "real-final-v2", "real-slow", "output-final"):
        profiles.extend(
            {"suite": directory, **row}
            for row in read_lines(args.evidence / directory / "native-profiles.ndjson")
        )
    profiles = list(
        {
            (row["suite"], row["label"], row["trials_completed"]): row
            for row in profiles
        }.values()
    )
    publish(
        "native",
        profiles,
        {
            "protocol": "serial native runs; 8 workers; full fd billing strict-parsed when enabled",
            "limitations": "one timing sample per shape; slow shape uses 16, not one million trials",
        },
    )
    quality = []
    summaries = {}
    for directory in ("quality-final", "quality-32"):
        quality.extend(
            {"suite": directory, **row}
            for row in json.loads(
                (args.evidence / directory / "paired.json").read_text()
            )
        )
        summaries[directory] = json.loads(
            (args.evidence / directory / "summary.json").read_text()
        )
    publish(
        "quality",
        quality,
        {
            "protocol": "paired pinned HPO-BENCH replica, instance1, seeds17/48/79, batch8",
            "quality_summaries": summaries,
            "limitations": "100/300/1000 trials only; no long-budget quality equivalence claim",
        },
    )
    for path in sorted(args.output.glob("2026-10-03-scale-*.csv")):
        expected = json.loads(path.with_suffix(".csv.metadata.json").read_text())[
            "csv_sha256"
        ]
        if expected != digest(path):
            raise RuntimeError("evidence hash mismatch")
        print(f"{path.name}: SHA-256 {expected}", flush=True)


if __name__ == "__main__":
    main()
