#!/usr/bin/env python3
"""Native-process contracts; the test plugin controls only the external engine ABI."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time

from test_native_cli import invoke, require


def check_progress(result: dict, lines: list[dict]) -> None:
    expected = {trial["trial_id"]: trial for trial in result["trials"]}
    require(len(lines) == len(expected), "progress missing or duplicated terminal trials")
    require(len({trial["trial_id"] for trial in lines}) == len(lines), "duplicate progress IDs")
    require(all(expected.get(trial["trial_id"]) == trial for trial in lines),
            "progress objects differ from final trial objects")


def process(native: Path, plugin: Path, csv: Path, progress: int, *extra: str,
            sampler: str = "grid", workers: int = 4) -> subprocess.Popen:
    command = [str(native), "run", "--strategy", str(plugin), "--ohlcv", str(csv),
               "--objective", "metrics.all.net_profit", "--sampler", sampler,
               "--seed", "37", "--max-trials", "120", "--workers", str(workers),
               "--input-tf", "1", "--script-tf", "5", "--chart-timezone", "Asia/Taipei",
               "--bar-magnifier", "true", "--magnifier-samples", "6",
               "--magnifier-distribution", "triangle", "--int-dim", "Length", "14", "133",
               "1", "--progress-fd", str(progress), *extra]
    return subprocess.Popen(command, pass_fds=(progress,), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def interrupted(native: Path, plugin: Path, csv: Path, directory: Path,
                sampler: str, signum: int | None) -> None:
    read_fd, write_fd = os.pipe()
    final = directory / "stopped.json"
    extra = ["--fixed-input", "DelayMs", "80", "--output", str(final)]
    if signum is None:
        extra.extend(("--max-wall-seconds", "0.25"))
    child = process(native, plugin, csv, write_fd, *extra, sampler=sampler)
    os.close(write_fd)
    try:
        with os.fdopen(read_fd) as progress:
            require(bool(select.select([progress], [], [], 8)[0]), "no live progress line")
            first = json.loads(progress.readline())
            require(child.poll() is None, "progress was buffered until study exit")
            if signum is not None:
                child.send_signal(signum)
            stdout, stderr = child.communicate(timeout=8)
            lines = [first, *(json.loads(line) for line in progress)]
        require(child.returncode == 0, f"stop failed: {stderr}")
        result = json.loads(stdout)
        require(result == json.loads(final.read_text()), "stopped output file differs")
        require(result["stop_reason"] == ("deadline" if signum is None else "cancelled"),
                "wrong cooperative stop reason")
        require(0 < result["trials_completed"] < 120, "batch did not stop mid-run")
        require(all(trial["status"] != "pending" for trial in result["trials"]),
                "unstarted candidates were reported")
        check_progress(result, lines)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def main() -> int:
    native, plugin = (Path(value).resolve() for value in sys.argv[1:3])
    case = sys.argv[3]
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        csv = directory / "bars.csv"
        csv.write_text("timestamp,open,high,low,close,volume\n"
                       "1700000000000,100,102,99,101,10\n"
                       "1700000060000,101,103,100,102,11\n")
        if case == "syminfo":
            harness = ast.parse((root / "external/pineforge-engine/docker/run_json.py").read_text())
            function = next(node for node in harness.body if isinstance(node, ast.FunctionDef)
                            and node.name == "apply_syminfo")
            setters = [node.func.attr for node in ast.walk(function)
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and node.func.attr.startswith("strategy_set_syminfo_")
                       and node.func.attr != "strategy_set_syminfo_metadata"]
            require(setters == ["strategy_set_syminfo_mintick", "strategy_set_syminfo_pointvalue",
                                "strategy_set_syminfo_timezone", "strategy_set_syminfo_session"],
                    "pinned harness changed the four-setter order")
            baseline = json.loads(invoke(native, plugin, csv).stdout)["trials"][0]
            # The fake adds 1000 * qty_step + 10 * mincontract when it sees the lot-grid metadata.
            lots = (("absent", {}, 0.0), ("null", {"mincontract": None}, 0.0),
                    ("grid", {"mincontract": 0.25}, 252.5))
            for wrapped in (False, True):
                for label, extra, bonus in lots:
                    syminfo = {"mintick": 0.00001, "pointvalue": 2, "timezone": "UTC",
                               "session": "24x7", "extra": [None, {"ignored": True}], **extra}
                    symbol_file = directory / "syminfo.json"
                    symbol_file.write_text(json.dumps({"syminfo": syminfo} if wrapped else syminfo))
                    completed = invoke(native, plugin, csv, "--syminfo", str(symbol_file),
                                       "--strategy-override", "initial_capital", "100000")
                    require(completed.returncode == 0, f"syminfo failed: {completed.stderr}")
                    trial = json.loads(completed.stdout)["trials"][0]
                    expected = baseline["net_profit"] + 32.001 + bonus
                    require(abs(trial["net_profit"] - expected) < 1e-9,
                            f"symbol ABI values or order were not forwarded (mincontract {label})")
            symbol_file.write_text('{"mincontract": 0.25}')
            completed = invoke(native, plugin, csv, "--syminfo", str(symbol_file))
            require(completed.returncode == 0, f"grid-only syminfo failed: {completed.stderr}")
            require(abs(json.loads(completed.stdout)["trials"][0]["net_profit"]
                        - baseline["net_profit"] - 252.5) < 1e-9,
                    "mincontract alone did not reach the engine as qty_step and mincontract")
            for content in ('{"mintick": true}', '{"mintick": -1}', '{"timezone": 5}',
                            '{"session": "bad\\u0000value"}', '{"mintick": 1,}', '[]'):
                symbol_file.write_text(content)
                require(invoke(native, plugin, csv, "--syminfo", str(symbol_file)).returncode == 1,
                        f"invalid syminfo accepted: {content}")
            for content in ('{"mincontract": 0}', '{"mincontract": -0.25}',
                            '{"mincontract": "0.25"}', '{"mincontract": true}',
                            '{"mincontract": false}', '{"mincontract": [0.25]}',
                            '{"mincontract": {}}', '{"mincontract": 1e999}',
                            '{"mincontract": -1e999}', '{"mincontract": 1e-400}',
                            '{"syminfo": {"mincontract": 0}}'):
                symbol_file.write_text(content)
                completed = invoke(native, plugin, csv, "--syminfo", str(symbol_file))
                require(completed.returncode == 1 and completed.stdout == "" and
                        "syminfo.mincontract must be a positive finite number" in completed.stderr,
                        f"invalid mincontract was not rejected visibly: {content}: "
                        f"{completed.stderr}")
        elif case == "progress":
            with (directory / "progress.jsonl").open("w+") as progress:
                child = process(native, plugin, csv, progress.fileno(),
                                "--categorical-choice", "long_metadata", "x" * 8192, workers=12)
                stdout, stderr = child.communicate(timeout=12)
                require(child.returncode == 0, f"progress run failed: {stderr}")
                progress.seek(0)
                check_progress(json.loads(stdout), [json.loads(line) for line in progress])
        elif case == "final_write_signal":
            progress_path = directory / "progress.jsonl"
            final = directory / "final.json"
            with progress_path.open("w+") as progress:
                child = process(native, plugin, csv, progress.fileno(),
                                "--categorical-choice", "long_metadata", "x" * 8192,
                                "--output", str(final), workers=12)
                try:
                    deadline = time.monotonic() + 8
                    while progress_path.read_text().count("\n") < 120:
                        require(time.monotonic() < deadline, "terminal progress did not finish")
                        time.sleep(0.01)
                    require(bool(select.select([child.stdout], [], [], 8)[0]),
                            "final stdout write did not start")
                    time.sleep(0.05)
                    require(child.poll() is None, "large final output did not block on stdout")
                    child.send_signal(signal.SIGTERM)
                    time.sleep(0.05)
                    if child.poll() is None:
                        child.send_signal(signal.SIGTERM)
                    stdout, stderr = child.communicate(timeout=8)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
                require(child.returncode == 0, f"signal interrupted final output: {stderr}")
                require(len(stdout) > 65536, "final result did not exercise pipe backpressure")
                result = json.loads(stdout)
                require(result == json.loads(final.read_text()), "interrupted output file differs")
                progress.seek(0)
                check_progress(result, [json.loads(line) for line in progress])
        elif case == "progress_nonblocking":
            read_fd, write_fd = os.pipe()
            atomic_limit = os.fpathconf(write_fd, "PC_PIPE_BUF")
            os.set_blocking(write_fd, False)
            child = process(native, plugin, csv, write_fd,
                            "--categorical-choice", "metadata", "x" * (atomic_limit + 1),
                            workers=12)
            os.close(write_fd)
            chunks = []

            def drain_progress() -> None:
                with os.fdopen(read_fd) as progress:
                    chunks.append(progress.read())

            time.sleep(0.1)
            reader = threading.Thread(target=drain_progress)
            reader.start()
            try:
                stdout, stderr = child.communicate(timeout=8)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                reader.join(timeout=8)
            require(not reader.is_alive(), "non-blocking progress did not close")
            require(child.returncode == 0, f"non-blocking progress cancelled the run: {stderr}")
            result = json.loads(stdout)
            require(result["trials_completed"] == 120, "non-blocking progress stopped candidates")
            content = "".join(chunks)
            require(content.endswith("\n"), "non-blocking progress ended with a partial record")
            lines = content.splitlines()
            require(all(len(line.encode()) + 1 > atomic_limit for line in lines),
                    "non-blocking progress did not exercise the actual pipe atomic limit")
            check_progress(result, [json.loads(line) for line in lines])
        elif case == "progress_error":
            read_fd, write_fd = os.pipe()
            os.close(read_fd)
            final = directory / "progress-error.json"
            child = process(native, plugin, csv, write_fd, "--fixed-input", "DelayMs", "20",
                            "--output", str(final), workers=2)
            os.close(write_fd)
            try:
                stdout, stderr = child.communicate(timeout=8)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            require(child.returncode == 1, f"progress I/O failure has wrong exit: {stderr}")
            require("--progress-fd" in stderr, "progress I/O diagnostic missing")
            result = json.loads(stdout)
            require(result["stop_reason"] == "cancelled", "progress I/O final JSON missing")
            require(result["trials_completed"] > 0, "progress I/O discarded completed trials")
            require(result == json.loads(final.read_text()), "progress I/O output file differs")
        elif case == "cancel":
            for sampler in ("grid", "random", "tpe", "dlib_global"):
                for signum in (signal.SIGTERM, signal.SIGINT):
                    interrupted(native, plugin, csv, directory, sampler, signum)
        elif case == "deadline":
            for sampler in ("grid", "random", "tpe", "dlib_global"):
                interrupted(native, plugin, csv, directory, sampler, None)
            completed = invoke(native, plugin, csv, "--max-wall-seconds", "0.000000001")
            require(completed.returncode == 2, "zero-completion deadline has wrong exit")
            require(json.loads(completed.stdout)["stop_reason"] == "deadline",
                    "zero-completion deadline did not produce final JSON")
        elif case == "metrics":
            completed = invoke(native, plugin, csv, "--record-metric", "metrics.all.profit_factor",
                               "--record-metric", "report.magnifier_sample_ticks_total")
            require(completed.returncode == 0, f"recording metrics failed: {completed.stderr}")
            metrics = json.loads(completed.stdout)["trials"][0]["metrics"]
            require(metrics["metrics.all.profit_factor"] == 2.5 and
                    metrics["report.magnifier_sample_ticks_total"] == 72, "wrong recorded values")
            require(invoke(native, plugin, csv, "--record-metric", "report.no_such_metric").returncode
                    == 1, "unknown recorded metric did not fail initialization")
        elif case == "magnifier":
            trial = json.loads(invoke(native, plugin, csv).stdout)["trials"][0]
            require(trial["backtest"].get("magnifier_sample_ticks_total") == 72,
                    "magnifier tick metering missing from native trial")
        elif case == "timeout":
            final = directory / "timeout.json"
            with (directory / "progress.jsonl").open("w+") as progress:
                started = time.monotonic()
                child = process(native, plugin, csv, progress.fileno(),
                                "--fixed-input", "HangAtLength", "15", "--trial-timeout-seconds",
                                "0.2", "--output", str(final), workers=2)
                try:
                    stdout, stderr = child.communicate(timeout=5)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
                require(child.returncode == 3, f"hung trial did not exit 3: {stderr}")
                require(time.monotonic() - started < 3, "watchdog joined the hung worker")
                result = json.loads(stdout)
                require(result == json.loads(final.read_text()), "timeout final file differs")
                require(result["stop_reason"] == "trial_timeout", "wrong timeout stop reason")
                require(sum(trial["status"] == "trial_timeout" for trial in result["trials"]) == 1,
                        "watchdog did not record exactly one timed-out trial")
                require(any(trial["status"] == "ok" for trial in result["trials"]),
                        "watchdog discarded completed trials")
                progress.seek(0)
                check_progress(result, [json.loads(line) for line in progress])
        elif case == "timeout_progress_error":
            read_fd, write_fd = os.pipe()
            os.close(read_fd)
            child = process(native, plugin, csv, write_fd, "--fixed-input", "HangAtLength", "15",
                            "--fixed-input", "DelayMs", "50", "--trial-timeout-seconds", "0.2",
                            workers=2)
            os.close(write_fd)
            try:
                stdout, stderr = child.communicate(timeout=3)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            require(child.returncode == 3, f"progress I/O disabled the watchdog: {stderr}")
            result = json.loads(stdout)
            require(result["stop_reason"] == "trial_timeout", "timeout final JSON missing")
            require(sum(trial["status"] == "trial_timeout" for trial in result["trials"]) == 1,
                    "progress I/O changed the timeout terminal table")
        else:
            raise RuntimeError(f"unknown test case: {case}")
    print(f"PASS native runner {case}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
