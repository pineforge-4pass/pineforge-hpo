#!/usr/bin/env python3
"""Compare proposal/value bytes from the pinned quality identity executables."""

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--trials", type=int, default=3000)
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--history-switch", default="never")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    problems = ["rotated_rastrigin20", "ackley20", "bbob_f15_d20", "bbob_f21_d20",
                "bbob_f24_d20", "mixed_irrelevant15", "rosenbrock20", "rotated_ellipsoid20"]
    jobs = [(problem, 17 + 31 * index, args.trials, args.history_switch)
            for problem in problems for index in range(args.seeds)]

    def run(job):
        problem, seed, trials, switch = job
        outputs = []
        for label, binary in (("baseline", args.baseline), ("switch", args.native)):
            output = subprocess.run([str(binary.resolve()), problem, str(seed), str(trials),
                                     str(switch)], check=True, capture_output=True).stdout
            (args.output / f"{label}-{problem}-{seed}-{trials}.txt").write_bytes(output)
            outputs.append(output)
        before, after = [output.splitlines() for output in outputs]
        if len(before) != trials or len(after) != trials:
            raise RuntimeError(f"incomplete identity trace: {job}")
        return {"problem": problem, "seed": seed, "trials": trials,
                "history_switch": None if switch == "never" else int(switch),
                "diff_count": sum(previous != current for previous, current in zip(before, after)),
                "baseline_sha256": hashlib.sha256(outputs[0]).hexdigest(),
                "switch_sha256": hashlib.sha256(outputs[1]).hexdigest()}

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        rows = list(executor.map(run, jobs))
    summary = {"studies": len(rows), "proposal_value_records": sum(row["trials"] for row in rows),
               "diff_count": sum(row["diff_count"] for row in rows), "rows": rows,
               "binary_sha256": {label: hashlib.sha256(binary.read_bytes()).hexdigest()
                                 for label, binary in (("baseline", args.baseline),
                                                       ("switch", args.native))},
               "protocol": "pinned instance1, ordered ask/tell batches of eight"}
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({key: value for key, value in summary.items() if key != "rows"}), flush=True)
    if summary["diff_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
