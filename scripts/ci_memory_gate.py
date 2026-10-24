#!/usr/bin/env python3
"""Gate native child RSS growth, retaining every workload and judgment on disk.

Run on Linux CI with a Release native CLI, tests' fake_strategy plugin (and its
adjacent manifest.json), a two-bar CSV and an explicit artifact directory.
Exit 0: both trial slopes pass; 1: growth or unexpected gate error;
2: setup, incomplete workload or unmeasurable trial RSS.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

COUNTS = (10, 1_000, 100_000)
SLOPE_LIMITS = (256, 32)  # Bytes/trial; identical to streamed-trial acceptance.
BAR_COUNTS = (2, 1_000, 100_000)
GNU_TIME = "/usr/bin/time"
MAX_TIME_OUTPUT_BYTES = 4096
LAUNCHER_FLOOR_NOTE = (
    "ru_maxrss of a child launched directly from Python includes Python's "
    "high-water; GNU time reaps the native child with a launcher floor of about "
    "1 MiB instead."
)
MEASUREMENT_RESOLUTION = (
    "peak is the child's maximum resident set size from GNU time "
    "(wait4 ru_maxrss of the native child reaped by a ~1 MiB launcher): "
    "exact including the final milliseconds, floored at the launcher's own "
    "size (about 1 MiB)"
)
MAX_METADATA_BYTES = 1024 * 1024
CSV_HEADER = ["timestamp", "open", "high", "low", "close", "volume"]


def judge_trial_growth(samples: list[tuple[int, int]]) -> dict:
    """Use integer comparisons: exactly the bound passes; one extra byte fails."""
    if tuple(count for count, _ in samples) != COUNTS:
        raise ValueError(f"all three ordered trial counts are required: {samples!r}")
    if any(
        not isinstance(peak, int) or isinstance(peak, bool) or peak <= 0
        for _, peak in samples
    ):
        raise ValueError(f"missing or invalid child peak RSS: {samples!r}")
    edges = []
    for left, right, limit in zip(samples, samples[1:], SLOPE_LIMITS):
        delta = right[1] - left[1]
        trials = right[0] - left[0]
        edges.append(
            {
                "from_trials": left[0],
                "to_trials": right[0],
                "delta_rss_bytes": delta,
                "bytes_per_trial": delta / trials,
                "limit_bytes_per_trial": limit,
                "verdict": "PASS" if delta <= limit * trials else "FAIL",
            }
        )
    passed = all(edge["verdict"] == "PASS" for edge in edges)
    return {
        "type": "verdict",
        "verdict": "PASS" if passed else "FAIL",
        "exit_code": 0 if passed else 1,
        "measurements": len(samples),
        "trial_slopes": edges,
    }


def allowed_path(path: Path) -> Path:
    """Refuse protected names before filesystem resolution, and symlink targets."""

    def check(candidate: Path) -> None:
        parts = candidate.parts
        if any(
            part in {".ssh", ".aws", ".codex"} or part.endswith(".env")
            for part in parts
        ):
            raise ValueError(f"refused protected path by name: {candidate}")
        for index in range(len(parts)):
            if parts[index : index + 3] == (".config", "zsh", "keys") or parts[
                index : index + 2
            ] == (".config", "pineforge"):
                raise ValueError(f"refused protected path by name: {candidate}")

    check(path)
    absolute = Path(os.path.abspath(path))
    check(absolute)
    try:
        resolved = absolute.resolve()
    except RuntimeError as error:
        raise ValueError(f"cannot resolve input path: {absolute}") from error
    check(resolved)
    return resolved


def list_paths(label: str, paths: list[Path]) -> list[Path]:
    checked = [allowed_path(path) for path in paths]
    print(
        f"FILES {label}: {json.dumps([str(path) for path in checked])}",
        file=sys.stderr,
        flush=True,
    )
    return checked


def write_json(path: Path, value: dict) -> None:
    (path,) = list_paths("result write", [path])
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def parse_gnu_time_peak(output: str) -> int:
    """GNU time can prefix %M with a nonzero-exit or signal diagnostic."""
    try:
        peak_kib = int(output.splitlines()[-1])
    except (IndexError, ValueError) as error:
        raise ValueError(
            "unmeasurable GNU time peak RSS: last line must be an integer in KiB; "
            "install the GNU time package in the workflow"
        ) from error
    if peak_kib <= 0:
        raise ValueError("unmeasurable GNU time peak RSS: peak must be positive")
    return peak_kib * 1024


def run_child(
    directory: Path,
    label: str,
    command: list[str],
    inputs: list[Path],
) -> dict:
    """Let GNU time reap the native child without Python's inherited RSS floor."""
    list_paths("GNU time launcher", [Path(GNU_TIME)])
    if not os.path.exists(GNU_TIME):
        raise ValueError(
            "missing GNU /usr/bin/time: install the time package in the workflow"
        )
    stdout, stderr, exit_file, result_file, time_file = list_paths(
        label,
        [
            directory / f"{label}.stdout",
            directory / f"{label}.stderr",
            directory / f"{label}.exit",
            directory / f"{label}.result.json",
            directory / f"{label}.time",
        ],
    )
    list_paths(
        "child executable, inputs and anonymous spool directory",
        [Path(command[0]), *inputs, directory],
    )
    native_command = command
    command = native_command
    print("COMMAND " + json.dumps(command), file=sys.stderr, flush=True)
    environment = {**os.environ, "TMPDIR": str(directory)}
    with stdout.open("wb") as output, stderr.open("wb") as errors:
        child = subprocess.Popen(
            command, stdout=output, stderr=errors, cwd=directory, env=environment
        )
        while True:
            try:
                waited_pid, status, _usage = os.wait4(child.pid, 0)
            except InterruptedError:
                continue
            break
        child.returncode = os.waitstatus_to_exitcode(status)
        # GNU time propagates normal child exits and returns nonzero for a signal.
        exit_file.write_text(f"{child.returncode}\n", encoding="ascii")
    row = {
        "pid": child.pid,
        "waited_pid": waited_pid,
        "command": command,
        "native_command": native_command,
        "exit_code": child.returncode,
        "peak_rss_bytes": 0,
        "peak_source": "gnu_time_wait4",
        "gnu_time_file": str(time_file),
        "gnu_time_output": "",
        "launcher_floor_note": LAUNCHER_FLOOR_NOTE,
        "stdout": str(stdout),
        "stderr": str(stderr),
        "exit_file": str(exit_file),
        "result_file": str(result_file),
        "verdict": "ERROR",
    }
    try:
        if waited_pid != child.pid:
            raise ValueError(f"wait4 reaped {waited_pid}, expected {child.pid}")
        row["peak_rss_bytes"] = int(_usage.ru_maxrss) * 1024
    except ValueError as error:
        row["reason"] = str(error)
        raise
    finally:
        write_json(result_file, row)
    return row


def validate_output(path: Path, trials: int) -> int:
    """Observe every successful ordered trial without retaining the trial array."""
    (path,) = list_paths("native result read", [path])
    with path.open("rb") as source:
        header = bytearray()
        for line in source:
            if line == b'  "trials": [\n':
                break
            header.extend(line)
            if len(header) > MAX_METADATA_BYTES:
                raise ValueError("native result metadata is too large")
        else:
            raise ValueError("native result has no trials array")
        observed = 0
        for line in source:
            if line.startswith(b"  ]"):
                closing = line
                break
            if not line.startswith(b"    ") or not line.endswith(b"\n"):
                raise ValueError("invalid native trial row framing")
            payload = line[4:-1]
            if observed + 1 < trials:
                if not payload.endswith(b","):
                    raise ValueError("missing native trial separator")
                payload = payload[:-1]
            trial = json.loads(payload)
            objective = trial.get("objective")
            if (
                trial.get("trial_id") != observed
                or trial.get("status") != "ok"
                or trial.get("feasible") is not True
                or not isinstance(objective, (int, float))
                or isinstance(objective, bool)
                or not math.isfinite(objective)
            ):
                raise ValueError(
                    f"native trial {observed} did not complete successfully"
                )
            observed += 1
        else:
            raise ValueError("native result has an incomplete trials array")
        tail = source.read(MAX_METADATA_BYTES + 1)
        if len(tail) > MAX_METADATA_BYTES:
            raise ValueError("native result suffix is too large")
    document = json.loads(bytes(header) + b'  "trials": []' + closing[3:] + tail)
    expected = {
        "ok": True,
        "trials_out": "all",
        "sampler": "random",
        "trials_requested": trials,
        "trials_completed": trials,
        "stop_reason": "trial_budget_reached",
    }
    if (
        any(document.get(key) != value for key, value in expected.items())
        or "failure" in document
        or observed != trials
    ):
        raise ValueError(
            f"native result did not complete all {trials} requested trials"
        )
    return observed


def native_command(
    native: Path,
    plugin: Path,
    bars: Path,
    trials: int,
    *,
    bars_axis: bool = False,
) -> list[str]:
    command = [
        str(native),
        "run",
        "--strategy",
        str(plugin),
        "--ohlcv",
        str(bars),
        "--objective",
        "metrics.all.net_profit",
        "--sampler",
        "random",
        "--seed",
        "17",
        "--workers",
        "1",
        "--batch-size",
        "1",
        "--schedule",
        "batches",
        "--pruner",
        "none",
        "--max-trials",
        str(trials),
        "--trials-out",
        "all",
        "--input-tf",
        "1",
        "--script-tf",
        "5",
        "--bar-magnifier",
        "true",
        "--magnifier-samples",
        "6",
        "--magnifier-distribution",
        "triangle",
        "--real-dim",
        "Length",
        "1",
        "100",
        "continuous",
    ]
    if bars_axis:
        command += [
            "--fixed-input",
            "BatchPrefixTest",
            "1",
            "--fixed-input",
            "BatchPrefixJitter",
            "0",
        ]
    return command


def measure(
    directory: Path,
    native: Path,
    plugin: Path,
    bars: Path,
    trials: int,
    bar_count: int,
    *,
    bars_axis: bool = False,
) -> dict:
    axis = "bars" if bars_axis else "trials"
    row = run_child(
        directory,
        f"{axis}-{bar_count if bars_axis else trials}",
        native_command(native, plugin, bars, trials, bars_axis=bars_axis),
        [plugin, plugin.parent / "manifest.json", bars],
    )
    row.update(type="measurement", axis=axis, trials=trials, bars=bar_count)
    try:
        if row["exit_code"] != 0:
            (errors,) = list_paths(
                "failed child diagnostic read", [Path(row["stderr"])]
            )
            with errors.open("rb") as source:
                diagnostic = source.read(2_000).decode("utf-8", errors="replace")
            raise ValueError(f"native child exited {row['exit_code']}: {diagnostic}")
        if row["peak_rss_bytes"] <= 0:
            raise ValueError("unmeasurable GNU time peak RSS: peak must be positive")
        row["trial_rows"] = validate_output(Path(row["stdout"]), trials)
        row["verdict"] = "PASS"
    except (OSError, ValueError, TypeError, AttributeError, OverflowError) as error:
        row["reason"] = str(error)
        raise ValueError(str(error)) from error
    finally:
        write_json(Path(row["result_file"]), row)
        print(json.dumps(row, sort_keys=True), flush=True)
    return row


def write_bar_fixture(path: Path, count: int) -> None:
    (path,) = list_paths("bar fixture write", [path])
    with path.open("x", encoding="ascii") as output:
        output.write("timestamp,open,high,low,close,volume\n")
        for index in range(count):
            output.write(f"{1700000000000 + index * 60000},100,102,99,101,10\n")


def run_gate(args: argparse.Namespace, directory: Path) -> dict:
    if not sys.platform.startswith("linux") or not hasattr(os, "wait4"):
        raise ValueError("this gate requires Linux child os.wait4 and GNU time")
    native, plugin, bars, manifest = list_paths(
        "gate inputs",
        [args.native, args.plugin, args.ohlcv, args.plugin.parent / "manifest.json"],
    )
    for path in (native, plugin, bars, manifest):
        if not path.is_file():
            raise ValueError(f"missing regular input file: {path}")
    if not os.access(native, os.X_OK):
        raise ValueError(f"native CLI is not executable: {native}")
    with bars.open(newline="", encoding="utf-8") as source:
        reader = csv.reader(source)
        if next(reader, None) != CSV_HEADER:
            raise ValueError("expected the fixture CSV header")
        if sum(1 for _ in reader) != 2:
            raise ValueError("trial-axis fixture requires exactly two bars")

    # Every run proves that a crashed work process writes a nonzero child marker.
    crash = run_child(
        directory,
        "deliberate-crash",
        [
            sys.executable,
            "-I",
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
        ],
        [],
    )
    if (
        crash["exit_code"] == 0
        or f"Command terminated by signal {signal.SIGKILL}"
        not in crash["gnu_time_output"].splitlines()[:-1]
    ):
        raise ValueError("deliberate crash control did not record SIGKILL")
    (marker,) = list_paths("crash control marker read", [Path(crash["exit_file"])])
    if int(marker.read_text(encoding="ascii")) != crash["exit_code"]:
        raise ValueError("deliberate crash marker does not match the child's own exit")
    crash.update(
        verdict="PASS",
        reason="GNU time observed deliberate SIGKILL; wait4 and marker agree",
    )
    write_json(Path(crash["result_file"]), crash)

    samples = []
    peak_sources = []
    for trials in COUNTS:
        row = measure(directory, native, plugin, bars, trials, 2)
        samples.append((trials, row["peak_rss_bytes"]))
        peak_sources.append(row["peak_source"])
    verdict = judge_trial_growth(samples)
    verdict.update(
        peak_sources=peak_sources,
        measurement_resolution=MEASUREMENT_RESOLUTION,
    )
    if verdict["exit_code"] != 0:
        verdict["reason"] = (
            f"trial RSS growth exceeded a threshold; {MEASUREMENT_RESOLUTION}"
        )
    # A failed trial-growth control stops before the informational bar axis.
    if verdict["exit_code"] == 0 and args.bars_axis:
        bar_samples = []
        for count in BAR_COUNTS:
            fixture = bars
            if count != 2:
                fixture = directory / f"bars-{count}.csv"
                write_bar_fixture(fixture, count)
            row = measure(directory, native, plugin, fixture, 10, count, bars_axis=True)
            bar_samples.append((count, row["peak_rss_bytes"]))
        verdict["bar_slopes"] = [
            {
                "from_bars": left[0],
                "to_bars": right[0],
                "bytes_per_bar": (right[1] - left[1]) / (right[0] - left[0]),
                "gated": False,
            }
            for left, right in zip(bar_samples, bar_samples[1:])
        ]
    return verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--ohlcv", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument(
        "--bars-axis", action="store_true", help="report, never gate, bar slopes"
    )
    args = parser.parse_args(argv)
    directory = None
    verdict = {"type": "verdict", "verdict": "ERROR", "exit_code": 1}
    try:
        # Check every supplied name before creating artifacts or following inputs.
        args.native, args.plugin, args.ohlcv, args.artifacts = list_paths(
            "arguments", [args.native, args.plugin, args.ohlcv, args.artifacts]
        )
        args.artifacts.mkdir(parents=True, exist_ok=True)
        directory = allowed_path(
            Path(tempfile.mkdtemp(prefix="run-", dir=args.artifacts))
        )
        list_paths(
            "run results",
            [directory, directory / "RUN.COMPLETE", directory / "gate.exit"],
        )
        verdict = run_gate(args, directory)
    except (OSError, ValueError, TypeError, AttributeError, csv.Error) as error:
        verdict = {
            "type": "verdict",
            "verdict": "ERROR",
            "exit_code": 2,
            "reason": str(error),
        }
    finally:
        if directory is not None:
            verdict["artifact_directory"] = str(directory)
            try:
                write_json(directory / "RUN.COMPLETE", verdict)
            except BaseException:
                verdict["exit_code"] = 1
                verdict["verdict"] = "ERROR"
                raise
            finally:
                (marker,) = list_paths("gate exit write", [directory / "gate.exit"])
                marker.write_text(f"{verdict['exit_code']}\n", encoding="ascii")
        print(json.dumps(verdict, sort_keys=True), flush=True)
    return verdict["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
