#!/usr/bin/env python3
"""Native million-trial, billing-line and real-strategy scaling profiles."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time


def write_bars(path, count, minutes):
    with path.open("w") as stream:
        stream.write("timestamp,open,high,low,close,volume\n")
        previous = 100.0
        for index in range(count):
            close = 100 + 7 * math.sin(index * 0.07) + 3 * math.sin(index * 0.013)
            stream.write(f"{1700000000000 + index * minutes * 60000},{previous:.8f},"
                         f"{max(previous, close) + 0.5:.8f},"
                         f"{min(previous, close) - 0.5:.8f},{close:.8f},100\n")
            previous = close


def profile(binary, command, label, output, billing=False):
    stats = output / f"{label}.stats.json"
    results = output / f"{label}.results.json"
    timing = output / f"{label}.time.json"
    receipt = ["/usr/bin/time", "-f", '{"peak_rss_kib":%M,"wall_seconds":%e}',
               "-o", str(timing), str(binary.resolve()), "run", *command,
               "--scheduler-stats", str(stats), "--output", str(results)]
    read_fd = write_fd = None
    if billing:
        read_fd, write_fd = os.pipe()
        receipt.extend(["--progress-fd", str(write_fd)])
    started = time.monotonic()
    process = subprocess.Popen(receipt, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               pass_fds=(write_fd,) if billing else (), text=True)
    lines = 0
    byte_count = 0
    line_hash = hashlib.sha256()
    if billing:
        os.close(write_fd)
        with os.fdopen(read_fd, "rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    raise RuntimeError("incomplete billing line")
                record = json.loads(line)
                if record["trial_id"] != lines:
                    raise RuntimeError("billing IDs are not unique and monotonic")
                for key in ("status", "objective", "feasible", "parameters"):
                    if key not in record:
                        raise RuntimeError(f"missing billing field {key}")
                for key in ("input_bars_processed", "script_bars_processed",
                            "magnifier_sample_ticks_total"):
                    if key not in record["backtest"]:
                        raise RuntimeError(f"missing billing counter {key}")
                lines += 1
                byte_count += len(line)
                line_hash.update(line)
    _, errors = process.communicate()
    if process.returncode:
        raise RuntimeError(f"{label}: {process.returncode}: {errors}")
    result = json.loads(results.read_text())
    row = {"label": label, "elapsed_seconds": time.monotonic() - started,
           "native_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
           **json.loads(timing.read_text()), **json.loads(stats.read_text()),
           "trials_completed": result["trials_completed"],
           "final_json_bytes": results.stat().st_size,
           "billing_lines": lines, "billing_bytes": byte_count,
           "billing_sha256": line_hash.hexdigest() if billing else None,
           "best_objective": result.get("best_value")}
    with (output / "native-profiles.ndjson").open("a") as stream:
        stream.write(json.dumps(row) + "\n")
    print(json.dumps(row), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--engine-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=1000000)
    parser.add_argument("--dims", type=int, default=64)
    parser.add_argument("--delay-ms", type=int, default=3)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--slow", action="store_true")
    parser.add_argument("--lag", type=int, default=1)
    parser.add_argument("--modes", nargs="+", default=["none"])
    parser.add_argument("--sampler", choices=("random", "tpe"), default="tpe")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not args.real:
        if args.fixture is None:
            parser.error("cheap-objective profiles require --fixture")
        bars = output / "two-bars.csv"
        write_bars(bars, 2, 1)
        command = ["--strategy", str(args.fixture.resolve()), "--ohlcv", str(bars),
                   "--objective", "metrics.all.net_profit", "--sampler", args.sampler,
                   "--seed", "17",
                   "--workers", "8", "--batch-size", "8", "--batch-lag", str(args.lag),
                   "--max-trials", str(args.trials), "--input-tf", "1", "--script-tf", "5",
                   "--bar-magnifier", "true", "--magnifier-samples", "6",
                   "--magnifier-distribution", "triangle", "--fixed-input", "Length", "37",
                   "--fixed-input", "DelayMs", str(args.delay_ms)]
        command.extend(value for index in range(args.dims)
                       for value in ("--real-dim", f"x{index}", "-5", "5", "continuous"))
        if args.baseline:
            profile(args.baseline, command, f"output-n{args.trials}-d{args.dims}-before", output)
        for mode in args.modes:
            profile(args.native, command + ["--trials-out", mode, "--best-k", "10"],
                    f"million-d{args.dims}-{mode}-delay{args.delay_ms}-lag{args.lag}", output,
                    billing=mode != "all")
        return
    from pineforge_hpo.artifact import ArtifactBuilder
    source = Path(__file__).with_name("strategy.pine")
    artifact = ArtifactBuilder(engine_root=args.engine_root, cache_dir=output / "artifacts",
                               compiler="g++").build(source.read_text(), filename=source.name)
    numeric = [("Lower RSI", 10, 40), ("Upper RSI", 60, 90), ("Entry Threshold", 0, 1),
               ("Exit Threshold", 0, 1), ("Stop Multiple", 1, 4), ("Take Multiple", 1, 6),
               ("Quantity", 0.5, 2)]
    integers = [("Fast Length", 2, 32), ("Slow Length", 16, 96), ("Signal Length", 2, 20),
                ("ATR Length", 5, 30), ("RSI Length", 5, 30)]
    shapes = ((12, 2103840, 1),) if args.slow else ((4, 744, 60), (12, 35064, 60))
    for dimensions, count, minutes in shapes:
        bars = output / f"bars-{count}-{minutes}m.csv"
        write_bars(bars, count, minutes)
        command = ["--strategy", str(artifact.library_path), "--ohlcv", str(bars),
                   "--objective", "metrics.all.net_profit", "--sampler", "tpe", "--seed", "17",
                   "--workers", "8", "--batch-size", "8", "--max-trials",
                   "16" if args.slow else "1000", "--input-tf", str(minutes),
                   "--script-tf", str(minutes)]
        if args.slow:
            command.extend(("--bar-magnifier", "true", "--magnifier-samples", "6",
                            "--magnifier-distribution", "triangle"))
        for name, low, high in integers[:4] if dimensions == 4 else integers:
            command.extend(("--int-dim", name, str(low), str(high), "1"))
        if dimensions == 12:
            for name, low, high in numeric:
                command.extend(("--real-dim", name, str(low), str(high), "continuous"))
        if args.baseline:
            profile(args.baseline, command, f"real-d{dimensions}-before", output)
        for lag in (0, 1):
            profile(args.native, command + ["--batch-lag", str(lag), "--trials-out", "none"],
                    f"real-d{dimensions}-after-lag{lag}", output, billing=True)


if __name__ == "__main__":
    main()
