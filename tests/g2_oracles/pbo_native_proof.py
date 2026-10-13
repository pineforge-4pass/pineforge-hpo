#!/usr/bin/env python3
"""Prove block/PBO mechanics on committed synthetic input; not market behaviour."""

from __future__ import annotations

import argparse
import copy
from fractions import Fraction
import hashlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pareto_real_rows as producer
import pbo_native_controls as controls
import pbo_real_rows as checker

ROOT = Path(__file__).resolve().parents[2]
STUDY = ROOT / "examples" / "pbo_proof" / "study.json"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def admitted_grid(study: dict) -> tuple:
    """Derive the full roster and parameter vectors from the committed study, not its result."""
    require(study["sampler"]["kind"] == "grid", "the proof requires an exhaustive grid")
    require(type(study["sampler"]["seed"]) is int, "the sampler seed must be recorded")
    dimensions = study["strategies"][0]["search_space"]
    names = sorted(dimensions)
    values = []
    for name in names:
        dimension = dimensions[name]
        require(dimension["kind"] == "real", "the threshold grid uses stepped real inputs")
        low, high, step = (Fraction(dimension[key]) for key in ("low", "high", "step"))
        require(step > 0 and high >= low, f"{name}: invalid stepped interval")
        steps = (high - low) / step
        require(steps.denominator == 1, f"{name}: grid endpoint is not on the step")
        values.append([low + index * step for index in range(int(steps) + 1)])
    vectors = set(itertools.product(*values))
    require(len(vectors) >= 24 and study["sampler"]["trials"] == len(vectors),
            "the complete synthetic grid needs at least 24 trials")
    return names, vectors, list(range(len(vectors)))


def check_roster(result: dict, curves: dict, names: list, vectors: set, roster: list,
                 bars: int) -> None:
    rows = result.get("trials")
    require(isinstance(rows, list) and len(rows) == len(roster), "incomplete native grid")
    require(result.get("trials_out", "all") == "all" and not result.get("warm_start_trials"),
            "the proof requires fresh rows with complete retention")
    ids = [row.get("trial_id") for row in rows]
    require(all(type(item) is int for item in ids)
            and len(set(ids)) == len(roster) and set(ids) == set(roster),
            "native trial ids differ from the independently admitted grid roster")
    require(set(curves) == set(roster), "original curve ids differ from the admitted grid roster")
    actual = []
    for row in rows:
        parameters = row.get("parameters")
        require(isinstance(parameters, dict) and set(parameters) == set(names),
                "a native row has the wrong threshold parameter names")
        require(all(type(parameters[name]) in (int, float) for name in names),
                "native parameters must be numeric")
        actual.append(tuple(Fraction(parameters[name]) for name in names))
        require(row.get("backtest", {}).get("input_bars_processed") == bars,
                "a native row did not process the original full input window")
    require(len(set(actual)) == len(roster) and set(actual) == vectors,
            "native parameter vectors differ from the full admitted grid")
    require(len({tuple(curve) for curve in curves.values()}) == len(roster),
            "the synthetic grid did not produce distinct original trial curves")


def final_agreement(completed) -> str:
    """Exit 0 and one actual AGREE verdict are both necessary."""
    verdicts = [line for line in completed.stdout.splitlines() if line.startswith("RESULT:")]
    print(f"PBO exit code: {completed.returncode}")
    print("PBO verdict: " + (" | ".join(verdicts) if verdicts else "<missing>"))
    require(completed.returncode == 0, "PBO comparison exited without agreement")
    require(len(verdicts) == 1 and verdicts[0].startswith("RESULT: AGREE ("),
            "PBO comparison did not print exactly one RESULT: AGREE")
    return verdicts[0]


def curve_agreement(completed, rows: int, size: int) -> str:
    print(f"curve-check exit code: {completed.returncode}")
    expected = f"CURVES: AGREE rows={rows} blocks={rows * size}"
    require(rows > 0 and completed.returncode == 0
            and completed.stdout.splitlines().count(expected) == 1,
            "the exact curve check did not compare every native row and block")
    return expected


def control_agreement(report: dict) -> None:
    expected = [
        ("count_mismatch", "module", "pbo_block_mismatch"),
        ("sum_one_ulp", "curve", "curve_sum_bound"),
        ("self_consistent_count", "curve", "curve_count"),
        ("boundary_time", "curve", "curve_boundary"),
        ("non_positive_equity", "module", "pbo_non_positive_equity"),
        ("m2_one_ulp", "curve", "curve_m2_bits"),
        ("null_payload", "module", "pbo_rows_incomplete"),
    ]
    actual = [(item["name"], item["stage"], item["code"]) for item in report["controls"]]
    require(actual == expected, "the driver did not observe every intended control check")


def run_logged(command: list, directory: Path, name: str):
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    (directory / f"{name}.stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (directory / f"{name}.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    return completed


def canonical_curve(harness: Path, plugin: Path, csv_path: Path, study: dict,
                    result: dict, curves: dict, directory: Path) -> list:
    """Reconcile one candidate with the C-ABI probe; it never supplies expected moments."""
    row = min(result["trials"], key=lambda value: value["trial_id"])
    parameters = row["parameters"]
    dataset = study["datasets"][0]
    command = [str(harness), str(plugin), str(csv_path), dataset["input_tf"], dataset["script_tf"]]
    command += [f"{name}={parameters[name]}" for name in sorted(parameters)]
    completed = run_logged(command, directory, "canonical-curve")
    require(completed.returncode == 0, "the built C-ABI curve probe failed")
    reference = json.loads(completed.stdout)
    exact_curve = [(time, Fraction(float.fromhex(equity))) for time, equity in reference["curve"]]
    require(exact_curve == curves[row["trial_id"]], "original dump differs from the C-ABI curve")
    require(reference["points"] == len(exact_curve), "the C-ABI point count is inconsistent")
    for name in ("input_bars_processed", "script_bars_processed"):
        require(reference[name] == row["backtest"][name], f"C-ABI {name} differs")
    require(reference["total_trades"] == row["total_trades"], "C-ABI trade count differs")
    for name, found in (
        ("report_net_profit", row["net_profit"]),
        ("metric_net_profit", row["metrics"]["metrics.all.net_profit"]),
        ("max_equity_drawdown", row["metrics"]["metrics.equity.max_equity_drawdown"]),
    ):
        require(checker.wire_bits(float.fromhex(reference[name])) == checker.wire_bits(found),
                f"C-ABI {name} bits differ")
    return command


def prove(args) -> int:
    native, plugin, harness, engine_root = (
        Path(value).resolve()
        for value in (args.native, args.plugin, args.harness, args.engine_root)
    )
    for name, path in (("proof executable", native), ("strategy plugin", plugin),
                       ("C-ABI curve probe", harness)):
        require(path.is_file(), f"missing {name}: {path}")
    require(os.access(native, os.X_OK) and os.access(harness, os.X_OK),
            "the proof executable and C-ABI probe must be executable")
    require(engine_root.is_dir(), "the engine source/build directory is missing")
    study = json.loads(STUDY.read_text(encoding="utf-8"))
    names, vectors, roster = admitted_grid(study)
    require(args.S >= 2 and args.S % 2 == 0, "S must be even and at least two")
    csv_path = (STUDY.parent / study["datasets"][0]["ohlcv"]).resolve()
    source = (STUDY.parent / study["strategies"][0]["source"]).resolve()
    generator = STUDY.with_name("generate.py")
    bars = len(csv_path.read_text(encoding="ascii").splitlines()) - 1
    require(bars >= 2000, "the committed synthetic series has fewer than 2,000 bars")
    manifest_path = plugin.with_name("manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(manifest.get("request_identity", {}).get("source_sha256") == sha256(source),
            "the plugin manifest does not name the existing threshold source")
    require(manifest.get("plugin_sha256") == sha256(plugin), "the plugin manifest hash differs")
    require("-ffp-contract=off" in manifest["request_identity"]["compile"]["flags"],
            "the compiled strategy manifest does not retain -ffp-contract=off")
    base = Path(args.out).resolve()
    base.mkdir(parents=True, exist_ok=True)
    # A fresh directory prevents stale rows or curves from satisfying a failed execution.
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=base))
    print(f"proof artifacts: {directory}")
    runtime_study = copy.deepcopy(study)
    strategy = runtime_study["strategies"][0]
    del strategy["source"]
    strategy["artifact"] = str(plugin)
    runtime_study["datasets"][0]["ohlcv"] = str(csv_path)
    runtime_path = directory / "study.json"
    runtime_path.write_text(json.dumps(runtime_study, indent=2) + "\n", encoding="utf-8")
    result_path = directory / "rows.json"
    curves_path = directory / "curves.jsonl"
    dataset_identity = f"examples/pbo_proof/ohlcv.csv sha256 {sha256(csv_path)}"
    metadata = {
        "scope": "synthetic mechanics only; not market behaviour",
        "S": args.S, "seed": study["sampler"]["seed"], "roster": roster,
        "hashes": {name: sha256(path) for name, path in (
            ("study", STUDY), ("generator", generator), ("dataset", csv_path), ("source", source),
            ("native", native), ("plugin", plugin), ("manifest", manifest_path),
            ("harness", harness),
            ("runtime_study", runtime_path),
        )},
    }
    metadata_path = directory / "metadata.json"

    def retain_metadata():
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )

    retain_metadata()
    axes = [
        {"name": "metrics.all.net_profit", "direction": "maximize"},
        {"name": "metrics.equity.max_equity_drawdown", "direction": "minimize"},
    ]
    result = producer.run_native(argparse.Namespace(
        study=str(runtime_path), engine_root=str(engine_root), cache_dir=str(directory / "cache"),
        native=str(native), out=str(result_path), pbo_blocks=args.S, proof_curves=str(curves_path),
    ), axes)
    production = json.loads(result_path.with_suffix(".artifact.json").read_text(encoding="utf-8"))
    require(production["native_exit_code"] == 0, "native execution did not exit successfully")
    require(Path(production["artifact"]["plugin"]).resolve() == plugin,
            "the first stage did not use the supplied plugin")
    command = production["command"]
    for flag, expected in (("--pbo-blocks", str(args.S)), ("--proof-curves", str(curves_path))):
        require(command.count(flag) == 1 and command[command.index(flag) + 1] == expected,
                f"the first stage did not forward {flag}")
    metadata["native"] = production
    curves = checker.load_curves(curves_path)
    check_roster(result, curves, names, vectors, roster, bars)
    original_hashes = {"rows": sha256(result_path), "curves": sha256(curves_path)}
    metadata["hashes"].update(original_hashes)
    metadata["numeric_build_identity"] = result["return_blocks"]["numeric_build_identity"]
    retain_metadata()
    command = [
        sys.executable, "-B", str(ROOT / "tests" / "g2_oracles" / "pbo_real_rows.py"),
        "--rows", str(result_path), "--S", str(args.S), "--sampler", study["sampler"]["kind"],
        "--dataset-identity", dataset_identity, "--curves", str(curves_path),
    ]
    checked = run_logged(command + ["--check-curves-only"], directory, "curve-check")
    expected_line = curve_agreement(checked, len(roster), args.S)
    metadata["curve_check"] = {"command": command + ["--check-curves-only"],
                               "exit_code": checked.returncode, "verdict": expected_line}
    metadata["controls"] = controls.run_controls(
        result, curves, args.S, roster, dataset_identity, output=directory / "controls",
    )
    control_agreement(metadata["controls"])
    metadata["canonical_command"] = canonical_curve(
        harness, plugin, csv_path, study, result, curves, directory,
    )
    retain_metadata()
    completed = run_logged(command, directory, "pbo")
    agree = final_agreement(completed)
    require({"rows": sha256(result_path), "curves": sha256(curves_path)} == original_hashes,
            "the original rows or original curves changed during the checks")
    metadata["pbo"] = {"command": command, "exit_code": completed.returncode, "verdict": agree}
    metadata["status"] = "agree"
    retain_metadata()
    print("PROOF: AGREE (synthetic mechanics only; not market behaviour)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", required=True, help="built pineforge-hpo-native-proof")
    parser.add_argument(
        "--plugin", required=True, help="built threshold plugin with adjacent manifest",
    )
    parser.add_argument("--harness", required=True, help="built C-ABI real_strategy_curve probe")
    parser.add_argument("--engine-root", required=True, help="engine source/build directory")
    parser.add_argument("--out", required=True, help="directory retaining proof artifacts")
    parser.add_argument("--S", type=int, default=16)
    args = parser.parse_args(argv)
    try:
        return prove(args)
    except Exception as error:
        print(f"PROOF: FAIL: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
