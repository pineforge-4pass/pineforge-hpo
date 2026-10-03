#!/usr/bin/env python3
"""Paired native replay on the separately pinned HPO-BENCH replica."""

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
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--replica", type=Path, required=True)
    parser.add_argument("--normalizers", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seeds", type=int, nargs="+", default=[17, 48, 79])
    parser.add_argument("--modes", nargs="+", default=["batch8"])
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    normalizers = json.loads(args.normalizers.read_text())
    problems = sorted({value["problem"] for value in normalizers.values()
                       if value["problem"] != "rastrigin20_fidelity"})
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def run(job):
        problem, mode, seed = job
        runs = []
        for label, binary in (("before", args.baseline), ("after", args.native)):
            command = [str(binary.resolve()), "--batch-study", "--problem", problem,
                       "--mode", mode, "--seed", str(seed), "--trials", "1000"]
            completed = subprocess.run(command, check=True, capture_output=True, text=True)
            result = json.loads(completed.stdout)
            (args.output / f"{label}-{problem}-{mode}-{seed}.json").write_text(completed.stdout)
            runs.append(result)
        scale = normalizers[f"{problem}:1"]["scale"]
        rows = []
        for budget in (100, 300, 1000):
            regrets = [max(0, (result["trace"][budget - 1][1] - result["optimum"]) / scale)
                       for result in runs]
            rows.append({"problem": problem, "mode": mode, "seed": seed,
                         "budget": budget, "before": regrets[0], "after": regrets[1],
                         "difference": regrets[1] - regrets[0]})
        return rows

    jobs = [(problem, mode, seed) for problem in problems for mode in args.modes
            for seed in args.seeds]
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        for index, batch in enumerate(executor.map(run, jobs), 1):
            rows.extend(batch)
            print(f"quality {index}/{len(jobs)}", flush=True)
    summary = {
        "problems": problems, "seeds": args.seeds, "modes": args.modes,
        "paired_studies": len(jobs), "elapsed_seconds": time.monotonic() - started,
        "budgets": {str(budget): {
            "median_before": statistics.median(
                row["before"] for row in rows if row["budget"] == budget),
            "median_after": statistics.median(
                row["after"] for row in rows if row["budget"] == budget),
            "max_abs_difference": max(
                abs(row["difference"]) for row in rows if row["budget"] == budget),
        } for budget in (100, 300, 1000)},
        "binary_sha256": {label: hashlib.sha256(binary.read_bytes()).hexdigest()
                          for label, binary in (("before", args.baseline), ("after", args.native))},
        "replica_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sorted(args.replica.glob("*")) if path.is_file()},
        "normalizers_sha256": hashlib.sha256(args.normalizers.read_bytes()).hexdigest(),
    }
    (args.output / "paired.json").write_text(json.dumps(rows, indent=2) + "\n")
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary["budgets"]), flush=True)


if __name__ == "__main__":
    main()
