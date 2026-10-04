#!/usr/bin/env python3
"""Run and compare seeded long-budget studies using the pinned native replica."""

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=3000)
    parser.add_argument(
        "--seeds", type=int, nargs="+", default=[17 + 31 * index for index in range(10)]
    )
    parser.add_argument(
        "--problems",
        nargs="+",
        default=[
            "rotated_rastrigin20",
            "ackley20",
            "bbob_f15_d20",
            "bbob_f21_d20",
            "bbob_f24_d20",
            "mixed_irrelevant15",
            "rosenbrock20",
            "rotated_ellipsoid20",
        ],
    )
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--reference")
    parser.add_argument("--normalizers", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    normalizers = json.loads(args.normalizers.read_text()) if args.normalizers else {}

    def result_path(label, problem, seed):
        return args.output / f"{label}-{problem}-{seed}-{args.trials}.json"

    def run(job):
        problem, seed = job
        path = result_path(args.label, problem, seed)
        if not path.exists():
            command = [
                str(args.native.resolve()),
                "--batch-study",
                "--mode",
                "batch8",
                "--problem",
                problem,
                "--seed",
                str(seed),
                "--trials",
                str(args.trials),
            ]
            completed = subprocess.run(
                command, check=True, capture_output=True, text=True
            )
            result = json.loads(completed.stdout)
            if len(result["trace"]) < args.trials:
                raise RuntimeError(f"incomplete study: {problem}/{seed}")
            path.write_text(completed.stdout)
        return problem, seed

    jobs = [(problem, seed) for problem in args.problems for seed in args.seeds]
    if args.native:
        binary_hash = hashlib.sha256(args.native.read_bytes()).hexdigest()
        metadata_path = args.output / f"{args.label}-{args.trials}.metadata.json"
        if metadata_path.exists():
            previous = json.loads(metadata_path.read_text())
            if previous["binary_sha256"] != binary_hash:
                raise RuntimeError(
                    "cached studies belong to a different binary; use a new label"
                )
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
            for index, job in enumerate(executor.map(run, jobs), 1):
                print(f"{args.label}: {index}/{len(jobs)} {job}", flush=True)
        metadata = {
            "label": args.label,
            "trials": args.trials,
            "seeds": args.seeds,
            "problems": args.problems,
            "mode": "batch8",
            "jobs": args.jobs,
            "binary_sha256": binary_hash,
            "elapsed_seconds": time.monotonic() - started,
        }
        (args.output / f"{args.label}-{args.trials}.metadata.json").write_text(
            json.dumps(metadata, indent=2) + "\n"
        )
    if args.reference:
        rows = []
        for problem in args.problems:
            before = []
            after = []
            paired = []
            for seed in args.seeds:
                reference = json.loads(
                    result_path(args.reference, problem, seed).read_text()
                )
                result = json.loads(result_path(args.label, problem, seed).read_text())
                reference_regret = max(
                    0.0, reference["trace"][args.trials - 1][1] - reference["optimum"]
                )
                result_regret = max(
                    0.0, result["trace"][args.trials - 1][1] - result["optimum"]
                )
                before.append(reference_regret)
                after.append(result_regret)
                paired.append(result_regret / max(reference_regret, 1e-300))
            ratio = statistics.median(after) / max(statistics.median(before), 1e-300)
            scale = normalizers.get(f"{problem}:1", {}).get("scale", 1.0)
            rows.append(
                {
                    "problem": problem,
                    "seeds": len(args.seeds),
                    "reference_regret": statistics.median(before) / scale,
                    "after_regret": statistics.median(after) / scale,
                    "ratio": ratio,
                    "median_paired_ratio": statistics.median(paired),
                }
            )
        summary = {
            "trials": args.trials,
            "reference": args.reference,
            "label": args.label,
            "rows": rows,
            "geomean_ratio": statistics.geometric_mean(
                max(row["ratio"], 1e-300) for row in rows
            ),
            "worst_problem_ratio": max(row["ratio"] for row in rows),
        }
        summary["regret_units"] = "normalized" if args.normalizers else "residual"
        (args.output / f"{args.label}-{args.trials}.summary.json").write_text(
            json.dumps(summary, indent=2) + "\n"
        )
        print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
