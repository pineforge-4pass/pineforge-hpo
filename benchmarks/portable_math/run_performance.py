"""Eight independent builds; proposal-only timing with a build random intercept."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dlib", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    args.output.mkdir(parents=True, exist_ok=True)
    randomizer = random.Random(170905)
    order = [(arm, build) for arm in ("baseline", "portable") for build in range(4)]
    randomizer.shuffle(order)
    rows: list[dict] = []
    binaries = {}
    environment = {**os.environ, "CCACHE_DISABLE": "1"}
    cpu = min(os.sched_getaffinity(0))
    for arm, build in order:
        directory = root / "build" / f"perf-{arm}-{build}"
        subprocess.run(
            [
                "cmake",
                "-S",
                str(root / "benchmarks/portable_math"),
                "-B",
                str(directory),
                "-DCMAKE_BUILD_TYPE=Release",
                f"-DPINEFORGE_HPO_ROOT={args.baseline if arm == 'baseline' else root}",
                f"-DFETCHCONTENT_SOURCE_DIR_DLIB={args.dlib}",
            ],
            env=environment,
            check=True,
        )
        subprocess.run(
            [
                "cmake",
                "--build",
                str(directory),
                "--target",
                "proposal_benchmark",
                f"-j{args.jobs}",
            ],
            env=environment,
            check=True,
        )
        binary = directory / "proposal_benchmark"
        binaries[f"{arm}-{build}"] = hashlib.sha256(binary.read_bytes()).hexdigest()
        subprocess.run(
            ["taskset", "-c", str(cpu), str(binary), "170904"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        for repeat in range(6):
            output = subprocess.check_output(
                ["taskset", "-c", str(cpu), str(binary), str(170905 + repeat)],
                text=True,
            )
            total = 0
            for line in output.splitlines():
                space, seed, nanoseconds, proposals = line.split(",")
                total += int(nanoseconds)
                rows.append(
                    {
                        "arm": arm,
                        "build": build,
                        "repeat": repeat,
                        "space": space,
                        "seed": seed,
                        "nanoseconds": int(nanoseconds),
                        "proposals": int(proposals),
                    }
                )
            rows.append(
                {
                    "arm": arm,
                    "build": build,
                    "repeat": repeat,
                    "space": "combined",
                    "seed": 170905 + repeat,
                    "nanoseconds": total,
                    "proposals": 2048,
                }
            )
    csv_path = args.output / "performance.csv"
    with csv_path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    intervals = {}
    for space in sorted({row["space"] for row in rows}):
        estimates = {}
        for arm in ("baseline", "portable"):
            clusters = [
                [
                    math.log(row["nanoseconds"] / row["proposals"])
                    for row in rows
                    if row["space"] == space
                    and row["arm"] == arm
                    and row["build"] == build
                ]
                for build in range(4)
            ]
            means = [statistics.mean(cluster) for cluster in clusters]
            within = statistics.mean(
                statistics.variance(cluster) for cluster in clusters
            )
            build_variance = max(0.0, statistics.variance(means) - within / 6)
            estimates[arm] = {
                "mean": statistics.mean(means),
                "variance": build_variance / 4 + within / 24,
                "build_variance": build_variance,
                "within_variance": within,
            }
        difference = estimates["portable"]["mean"] - estimates["baseline"]["mean"]
        margin = 3.182446305 * math.sqrt(
            sum(value["variance"] for value in estimates.values())
        )
        intervals[space] = {
            "ratio": math.exp(difference),
            "lower95": math.exp(difference - margin),
            "upper95": math.exp(difference + margin),
            "arms": estimates,
        }
    metadata = {
        "model": "log(ns/proposal) = arm + build_random_intercept + repeat_error",
        "interval": "conservative Student t(df=3) 95%, method-of-moments variance",
        "builds_per_arm": 4,
        "repeats_per_build": 6,
        "timed_proposals_per_space": 256,
        "build_order": order,
        "cpu": cpu,
        "binary_sha256": binaries,
        "csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "intervals": intervals,
    }
    (args.output / "performance.csv.metadata.json").write_text(
        json.dumps(metadata, indent=2)
    )
    print(json.dumps(intervals, indent=2))
    if intervals["combined"]["upper95"] > 1.05:
        raise SystemExit("FAIL proposal regression upper95 exceeds 5%")
    print("PASS proposal regression upper95 <= 5%")


if __name__ == "__main__":
    main()
