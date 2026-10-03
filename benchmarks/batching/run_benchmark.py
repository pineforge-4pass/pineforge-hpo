#!/usr/bin/env python3
"""Paired study timing, metering, optimizer quality, and replay evidence."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import platform
import re
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def invoke(command: list[str]):
    started = time.perf_counter()
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    elapsed = time.perf_counter() - started
    if completed.returncode not in (0, 2):
        raise RuntimeError(completed.stderr)
    return json.loads(completed.stdout), elapsed, completed.stdout


def trial_bytes(raw: str) -> bytes:
    start = raw.index('  "trials": [') + len('  "trials": ')
    return raw[start : raw.rfind("\n}")].encode()


def legacy_trial_bytes(raw: str) -> bytes:
    return re.sub(rb', "magnifier_sample_ticks_total": [0-9]+', b"", trial_bytes(raw))


def canonical_parity(study, artifact, candidate, probe: dict) -> dict:
    command = [
        sys.executable,
        str(ROOT / "external/pineforge-engine/docker/run_json.py"),
        "--so",
        str(artifact.plugin_path),
        "--ohlcv",
        str(study.datasets[0].ohlcv),
        "--inputs",
        json.dumps(candidate["parameters"]),
        "--input-tf",
        study.datasets[0].input_tf,
        "--script-tf",
        study.datasets[0].script_tf,
        "--chart-tz",
        study.datasets[0].chart_timezone,
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=True)
    canonical = json.loads(completed.stdout)
    for path, score in candidate["metrics"].items():
        value = canonical
        for component in path.split("."):
            value = value[component]
        if value != score:
            raise RuntimeError(f"canonical metric mismatch: {path}")
    actual = []
    integer_fields = {0, 1, 6, 11, 12, 13}
    for row in probe["full_trades"].split(";"):
        if row:
            actual.append(
                tuple(
                    int(value) if index in integer_fields else float.fromhex(value)
                    for index, value in enumerate(row.split(":"))
                )
            )
    expected = [
        (
            trade["entry_time"],
            trade["exit_time"],
            trade["entry_price"],
            trade["exit_price"],
            trade["pnl"],
            trade["pnl_pct"],
            int(trade["side"] == "long"),
            trade["max_runup"],
            trade["max_drawdown"],
            trade["qty"],
            trade["commission"],
            trade["entry_bar_index"],
            trade["exit_bar_index"],
            int(trade["open_at_end"]),
        )
        for trade in canonical["trades"]
    ]
    if actual != expected or len(actual) != candidate["total_trades"]:
        raise RuntimeError("canonical trade mismatch")
    return {"metrics_equal": True, "trades_equal": True, "closed_trades": len(actual)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--trials", type=int, default=64)
    args = parser.parse_args()
    sys.path[:0] = [str(ROOT / "python"), str(ROOT / "external/pineforge-codegen-oss")]
    from pineforge_hpo.artifact import ArtifactBuilder
    from pineforge_hpo.cli import _native_command
    from pineforge_hpo.study_spec import load_study_spec

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    builder = ArtifactBuilder(
        engine_root=ROOT / "external/pineforge-engine",
        cache_dir=output / "artifacts",
        compiler="g++",
    )
    example = json.loads((ROOT / "examples/single_strategy/study.json").read_text())
    example["strategies"][0]["source"] = str(
        ROOT / "examples/single_strategy/threshold.pine"
    )
    example["datasets"][0]["ohlcv"] = str(ROOT / "examples/single_strategy/ohlcv.csv")
    example["execution"]["workers"] = 4
    studies = {"example": example}
    assets = ROOT / "external/pineforge-engine/benchmarks/assets"
    for name, strategy, length, multiplier in (
        ("bb_rsi", "068-ta-bb-rsi-mean-reversion-01", "BB Length", "BB Multiplier"),
        (
            "volatility",
            "085-ta-stdev-sma-expansion-break-01",
            "Baseline / stdev length",
            "Expansion multiple",
        ),
    ):
        document = copy.deepcopy(example)
        document["strategies"][0]["source"] = str(
            assets / "strategies" / strategy / "strategy.pine"
        )
        document["strategies"][0]["search_space"] = {
            length: {"kind": "integer", "low": 5, "high": 60, "step": 1},
            multiplier: {"kind": "real", "low": 0.5, "high": 3.0},
        }
        document["datasets"][0].update(
            ohlcv=str(assets / "data/ETHUSDT_15.csv"), input_tf="15", script_tf="15"
        )
        document["objective"]["expression"] = (
            "metrics.all.net_profit - 0.25 * metrics.equity.max_equity_drawdown"
        )
        document["sampler"].update(kind="tpe", trials=args.trials)
        studies[name] = document

    modes = {
        "0.1.x": [],
        "default": ["--batch-size", "4"],
        "batch8": ["--batch-size", "8"],
        "lag1": ["--batch-size", "4", "--batch-lag", "1"],
        "median": ["--batch-size", "4", "--pruner", "median"],
        "halving3": ["--batch-size", "4", "--pruner", "halving", "--pruner-eta", "3"],
        "lag1_halving3": [
            "--batch-size",
            "4",
            "--batch-lag",
            "1",
            "--pruner",
            "halving",
            "--pruner-eta",
            "3",
        ],
    }
    rows = []
    replay = []
    probes = {}
    harness_checks = {}
    inputs = {}
    for study_name, document in studies.items():
        path = output / f"{study_name}.study.json"
        path.write_text(json.dumps(document, indent=2))
        study = load_study_spec(path, require_files=True)
        source = study.strategy.source
        print(f"compile {study_name}", flush=True)
        artifact = builder.build(source.read_text(), filename=str(source))
        inputs[study_name] = {
            "strategy_sha256": digest(source),
            "data_sha256": digest(study.datasets[0].ohlcv),
            "artifact_key": artifact.artifact_key,
            "bars": sum(1 for _ in study.datasets[0].ohlcv.open()) - 1,
            "objective": document["objective"]["expression"],
            "seed": study.sampler.seed,
            "trials": study.sampler.trials,
        }
        baseline_result = None
        baseline_bytes = None
        baseline_best = None
        full_cache = {}
        for mode, flags in modes.items():
            binary = args.baseline if mode == "0.1.x" else args.native
            command = _native_command(
                study,
                native=binary.resolve(),
                plugin=artifact.plugin_path,
                artifact_key=artifact.artifact_key,
            )
            command.extend(flags)
            times = []
            idle = []
            reference = None
            for repeat in range(args.repeats):
                trial_command = list(command)
                if mode != "0.1.x":
                    stats = output / f"{study_name}.{mode}.{repeat}.stats.json"
                    trial_command.extend(("--scheduler-stats", str(stats)))
                result, wall, raw = invoke(trial_command)
                (output / f"{study_name}.{mode}.{repeat}.json").write_text(raw)
                times.append(wall)
                if mode != "0.1.x":
                    utilization = json.loads(stats.read_text())
                    idle.append(
                        utilization["idle_seconds"] / utilization["worker_seconds"]
                    )
                encoded = trial_bytes(raw)
                if reference is not None and encoded != reference:
                    raise RuntimeError(f"repeat mismatch: {study_name} {mode}")
                reference = encoded
            if mode == "0.1.x":
                baseline_result = result
                baseline_bytes = legacy_trial_bytes(raw)
                baseline_best = next(
                    trial
                    for trial in result["trials"]
                    if trial["trial_id"] == result["best_trial_id"]
                )
            if mode == "default" and legacy_trial_bytes(raw) != baseline_bytes:
                raise RuntimeError(f"default compatibility mismatch: {study_name}")
            for trial in result["trials"]:
                key = json.dumps(trial["parameters"], sort_keys=True)
                if trial["status"] != "pruned":
                    full_cache[key] = trial
            full_scores = []
            for trial in result["trials"]:
                key = json.dumps(trial["parameters"], sort_keys=True)
                if key not in full_cache:
                    shadow = copy.deepcopy(document)
                    shadow["sampler"].update(kind="random", trials=1)
                    for parameter, value in trial["parameters"].items():
                        shadow["strategies"][0]["search_space"][parameter].update(
                            low=value, high=value
                        )
                    shadow_path = output / "shadow.study.json"
                    shadow_path.write_text(json.dumps(shadow))
                    shadow_study = load_study_spec(shadow_path, require_files=True)
                    full, _, _ = invoke(
                        _native_command(
                            shadow_study,
                            native=args.native.resolve(),
                            plugin=artifact.plugin_path,
                            artifact_key=artifact.artifact_key,
                        )
                    )
                    full_cache[key] = full["trials"][0]
                full_trial = full_cache[key]
                if full_trial["feasible"]:
                    full_scores.append(full_trial["objective"])
            baseline_status = next(
                (
                    trial["status"]
                    for trial in result["trials"]
                    if trial["parameters"] == baseline_best["parameters"]
                ),
                "absent",
            )
            row = {
                "study": study_name,
                "mode": mode,
                "wall_seconds": statistics.median(times),
                "bars_processed": sum(
                    trial.get("pruning", {}).get(
                        "bars_processed_total",
                        trial["backtest"]["input_bars_processed"],
                    )
                    for trial in result["trials"]
                ),
                "best_objective": result["best_value"],
                "baseline_best_rank": 1
                + sum(score > baseline_best["objective"] for score in full_scores),
                "baseline_best_status": baseline_status,
                "pruned": sum(
                    trial["status"] == "pruned" for trial in result["trials"]
                ),
                "lost_proposed_best": bool(
                    full_scores and max(full_scores) > result["best_value"]
                ),
                "idle_fraction": statistics.median(idle) if idle else None,
            }
            rows.append(row)
            print(json.dumps(row), flush=True)
            if mode != "0.1.x":
                hashes = []
                for workers in (1, 2, 4, 8):
                    replay_command = list(command) + ["--workers", str(workers)]
                    _, _, raw = invoke(replay_command)
                    hashes.append(hashlib.sha256(trial_bytes(raw)).hexdigest())
                    (output / f"{study_name}.{mode}.workers{workers}.json").write_text(
                        raw
                    )
                if len(set(hashes)) != 1:
                    raise RuntimeError(f"worker mismatch: {study_name} {mode}")
                replay.append(
                    {
                        "study": study_name,
                        "mode": mode,
                        "workers": [1, 2, 4, 8],
                        "trials_sha256": hashes[0],
                    }
                )
        probes[study_name] = []
        for candidate in (baseline_result["trials"][0], baseline_best):
            probe_command = [
                str(args.probe.resolve()),
                str(artifact.plugin_path),
                str(study.datasets[0].ohlcv),
                study.datasets[0].input_tf,
                study.datasets[0].script_tf,
                "0.5" if study_name == "example" else "0.25",
                *(f"{name}={value}" for name, value in candidate["parameters"].items()),
            ]
            completed = subprocess.run(probe_command, text=True, capture_output=True)
            probes[study_name].append(
                json.loads(completed.stdout)
                if completed.returncode == 0
                else {"error": completed.stderr.strip()}
            )
        harness_checks[study_name] = canonical_parity(
            study, artifact, baseline_best, probes[study_name][-1]
        )
        for probe in probes[study_name]:
            probe.pop("full_trades", None)
    with (output / "measurements.csv").open("w") as destination:
        writer = csv.DictWriter(destination, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    metadata = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "logical_cpus": os.cpu_count(),
        "compiler": subprocess.check_output(
            ["g++", "--version"], text=True
        ).splitlines()[0],
        "source_revision": subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip(),
        "driver_sha256": digest(Path(__file__)),
        "probe_sha256": digest(args.probe),
        "baseline_sha256": digest(args.baseline),
        "native_sha256": digest(args.native),
        "engine_revision": subprocess.check_output(
            ["git", "-C", str(ROOT / "external/pineforge-engine"), "rev-parse", "HEAD"],
            text=True,
        ).strip(),
        "repeats": args.repeats,
        "inputs": inputs,
        "replay": replay,
        "stream_probes": probes,
        "canonical_harness_checks": harness_checks,
        "shadow_full_runs_excluded_from_timing_and_metering": True,
        "measurements_sha256": digest(output / "measurements.csv"),
    }
    (output / "measurements.metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
