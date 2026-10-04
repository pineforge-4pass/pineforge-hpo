"""Foreground 8-GiB continuation/fit measurements with explicit censored rows."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measure(executable, mode, inputs, history, switch, timeout, output, fixture=None):
    label = f"{executable.name}-{mode}-{inputs}-{history}-{switch}"
    stdout_path = output / (label + ".json")
    stderr_path = output / (label + ".stderr")
    command = [
        "sudo",
        "systemd-run",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        "-p",
        "MemoryMax=8G",
        "-p",
        "MemorySwapMax=0",
        "/usr/bin/timeout",
        "--signal=TERM",
        "--kill-after=5s",
        str(timeout),
        str(executable),
        mode,
        str(inputs),
        str(history),
        str(switch),
    ]
    if fixture is not None:
        command.append(str(fixture))
    started = time.monotonic()
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.run(command, stdout=stdout, stderr=stderr, check=False)
    result = {
        "implementation": executable.name,
        "mode": mode,
        "inputs": inputs,
        "history": history,
        "history_switch": switch,
        "timeout_s": timeout,
        "wall_s": time.monotonic() - started,
        "exit": process.returncode,
        "status": "ok"
        if process.returncode == 0
        else "timeout"
        if process.returncode == 124
        else "failed",
    }
    if process.returncode == 0:
        result.update(json.loads(stdout_path.read_text()))
    print(json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument(
        "--phase",
        choices=("resume", "batch", "replay", "sequence", "prepare"),
        required=True,
    )
    parser.add_argument("--replay-timeout", type=int, default=60)
    parser.add_argument("--only", choices=("baseline", "native"))
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    baseline, native = args.baseline.resolve(), args.native.resolve()
    rows = []
    if args.phase == "prepare":
        cases = [(baseline, "prepare", inputs, 8000, 0, 900) for inputs in (5, 32)]
    elif args.phase == "resume":
        cases = [
            (native, mode, inputs, history, 0, 600)
            for inputs in (5, 32)
            for history in (1000, 8000, 50000, 500000, 1000000)
            for mode in ("resume", "checkpoint")
        ]
    elif args.phase == "batch":
        cases = [
            (executable, "batch", inputs, history, switch, 900)
            for inputs in (5, 32)
            for history in (100000, 500000, 2000000)
            for executable, switch in ((baseline, 0), (native, 0), (native, 1000))
        ]
    elif args.phase == "replay":
        cases = [
            (
                baseline,
                "legacy",
                inputs,
                history,
                0,
                300 if history <= 8000 else args.replay_timeout,
            )
            for inputs in (5, 32)
            for history in (1000, 8000, 50000, 500000, 1000000)
        ]
    else:
        cases = [
            (executable, "sequence", inputs, history, switch, 600)
            for inputs in (5, 32)
            for history, switch in ((128, 0), (4104, 0), (256, 32))
            for executable in (baseline, native)
        ]
    if args.only:
        selected = baseline if args.only == "baseline" else native
        cases = [case for case in cases if case[0] == selected]
    for case in cases:
        if args.phase == "replay" and case[3] > 8000:
            prefix = next(
                row
                for row in rows
                if row["inputs"] == case[2] and row["history"] == 8000
            )
            prefix_seconds = prefix.get("import_s", prefix["timeout_s"])
            case = (*case[:-1], min(case[-1], max(1, int(prefix_seconds / 2))))
        fixture = (
            args.output / f"fixture-{case[2]}.bin"
            if args.phase in ("prepare", "replay", "resume")
            else None
        )
        rows.append(measure(*case, args.output, fixture=fixture))
    suffix = "-" + args.only if args.only else ""
    csv_path = args.output / (args.phase + suffix + ".csv")
    fields = sorted({key for row in rows for key in row})
    with csv_path.open("w") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    metadata = {
        "source_revision": args.source_revision,
        "baseline_revision": "6fc5b1fee4a2b76e5ee136f76805659460b9ad93",
        "seed": 73,
        "batch_size": 8,
        "memory_max_bytes": 8 * 1024**3,
        "memory_swap_max_bytes": 0,
        "platform": platform.platform(),
        "logical_cpus": subprocess.check_output(["nproc"], text=True).strip(),
        "compiler": subprocess.check_output(
            ["g++", "--version"], text=True
        ).splitlines()[0],
        "flags": "-std=c++17 -O3 -ffp-contract=off -pthread",
        "executable_sha256": {str(path): digest(path) for path in (baseline, native)},
        "current_source_sha256": {
            name: digest(Path(__file__).resolve().parents[2] / name)
            for name in (
                "benchmarks/noreplay/profile.cpp",
                "src/core/types.cpp",
                "src/core/search_space.cpp",
                "src/core/sampler.cpp",
                "src/core/tpe_sampler.cpp",
                "src/core/sha256.hpp",
                "src/core/mt19937_64.hpp",
                "src/core/numeric_build.hpp",
                "src/core/numeric_build_flags.hpp.in",
                "src/core/dimension_workers.hpp",
                "include/pineforge/hpo/sampler.hpp",
            )
        },
        "fixture_sha256": {
            path.name: digest(path) for path in args.output.glob("fixture-*.bin")
        },
        "csv_sha256": digest(csv_path),
        "boundary": "sampler import plus first ask/tell; batch=8 cold-cache asks plus tells; "
        "replay=actual legacy warm_start(source,8) plus first ask/tell. Histories <=8000 "
        "match uninterrupted proposals; larger fixtures use that matching prefix and "
        "synthetic suffix and are censored before the prefix finishes, not completed "
        "million-trial uninterrupted continuations",
        "fixture": "5 continuous; 32=4 int32 grids plus 28 continuous, exact permuted "
        "immutable columns and objective ties; continuation uses genuine seeded first "
        "8000 rows followed by synthetic rows; sequence uses genuine seeded proposals",
    }
    csv_path.with_suffix(".csv.metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )
    if args.phase == "prepare" and any(row["status"] != "ok" for row in rows):
        raise RuntimeError("genuine replay fixture preparation failed")
    if args.phase in {"sequence", "batch"}:
        grouped = {}
        for row in rows:
            if row["status"] == "ok" and row["history_switch"] in (0, 32):
                key = (row["inputs"], row["history"], row["history_switch"])
                grouped.setdefault(key, []).append(row["suggestion_sha256"])
        if any(len(set(values)) > 1 for values in grouped.values()):
            raise AssertionError("baseline/native suggestions differ bitwise")


if __name__ == "__main__":
    main()
