#!/usr/bin/env python3
"""Black-box regression tests for native CLI initialization and result semantics."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def invoke(
    native: Path,
    plugin: Path,
    csv: Path,
    *extra: str,
    sampler: str = "grid",
    max_trials: int = 1,
    workers: int = 1,
    length_low: int = 14,
    length_high: int = 14,
) -> subprocess.CompletedProcess[str]:
    command = [
        str(native),
        "run",
        "--strategy",
        str(plugin),
        "--ohlcv",
        str(csv),
        "--objective",
        "metrics.all.net_profit",
        "--sampler",
        sampler,
        "--max-trials",
        str(max_trials),
        "--workers",
        str(workers),
        "--input-tf",
        "1",
        "--script-tf",
        "5",
        "--chart-timezone",
        "Asia/Taipei",
        "--bar-magnifier",
        "true",
        "--magnifier-samples",
        "6",
        "--magnifier-distribution",
        "triangle",
        "--int-dim",
        "Length",
        str(length_low),
        str(length_high),
        "1",
        *extra,
    ]
    return subprocess.run(command, text=True, capture_output=True, check=False)


def main() -> int:
    native = Path(sys.argv[1]).resolve()
    plugin = Path(sys.argv[2]).resolve()
    with tempfile.TemporaryDirectory() as directory:
        csv = Path(directory) / "bars.csv"
        csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            "1700000000000,100,102,99,101,10\n"
            "1700000060000,101,103,100,102,11\n",
            encoding="utf-8",
        )

        unknown = invoke(
            native,
            plugin,
            csv,
            "--objective",
            "metrics.all.not_a_metric",
        )
        require(unknown.returncode == 1, "unknown metric did not fail initialization")
        require(
            "unknown report metric" in unknown.stderr,
            "unknown metric diagnostic was not preserved",
        )

        renamed = invoke(
            native,
            plugin,
            csv,
            "--objective",
            "metrics.equity.sharpe_monthly",
            "--constraint",
            "metrics.equity.sharpe_tv == metrics.equity.sharpe_monthly",
            "--constraint",
            "metrics.equity.sortino_tv == metrics.equity.sortino_monthly",
            "--constraint",
            "metrics.equity.sortino_monthly != metrics.equity.sharpe_monthly",
        )
        require(
            renamed.returncode == 0,
            f"engine 1.0 metric names were rejected: {renamed.stderr}",
        )
        renamed_trial = json.loads(renamed.stdout)["trials"][0]
        require(
            renamed_trial["status"] == "ok" and renamed_trial["feasible"],
            "pre-1.0 metric aliases disagreed with the engine 1.0 names",
        )

        legacy = invoke(
            native,
            plugin,
            csv,
            "--objective",
            "metrics.equity.sharpe_tv",
        )
        require(
            legacy.returncode == 0,
            f"pre-1.0 metric alias was rejected: {legacy.stderr}",
        )
        legacy_trial = json.loads(legacy.stdout)["trials"][0]
        require(
            legacy_trial["objective"] == renamed_trial["objective"],
            "pre-1.0 metric alias changed the objective value",
        )
        require(
            list(legacy_trial["metrics"]) == ["metrics.equity.sharpe_tv"],
            "result did not keep the metric name the objective used",
        )

        constraint = invoke(
            native,
            plugin,
            csv,
            "--constraint",
            "1 / 0",
            "--division-by-zero",
            "ieee",
            "--non-finite",
            "allow",
        )
        require(
            constraint.returncode == 2, "non-finite constraint selected a best trial"
        )
        constraint_json = json.loads(constraint.stdout)
        require(
            not constraint_json["ok"], "result without a feasible trial reported ok"
        )
        constraint_trial = constraint_json["trials"][0]
        require(not constraint_trial["feasible"], "non-finite constraint was feasible")
        require(
            constraint_trial["status"] == "constraint_error",
            "non-finite constraint had the wrong status",
        )

        compared_constraint = invoke(
            native,
            plugin,
            csv,
            "--constraint",
            "1 / 0 > 0",
            "--division-by-zero",
            "ieee",
            "--non-finite",
            "allow",
        )
        require(
            compared_constraint.returncode == 2,
            "comparison hid a non-finite constraint operand",
        )
        compared_trial = json.loads(compared_constraint.stdout)["trials"][0]
        require(
            compared_trial["status"] == "constraint_error"
            and not compared_trial["feasible"],
            "non-finite constraint operand became feasible",
        )

        objective = invoke(
            native,
            plugin,
            csv,
            "--objective",
            "1 / 0",
            "--division-by-zero",
            "ieee",
            "--non-finite",
            "allow",
        )
        require(objective.returncode == 2, "non-finite objective selected a best trial")
        objective_trial = json.loads(objective.stdout)["trials"][0]
        require(not objective_trial["feasible"], "non-finite objective was feasible")
        require(
            objective_trial["status"] == "objective_error",
            "non-finite objective had the wrong status",
        )

        typed = invoke(
            native,
            plugin,
            csv,
            "--categorical-int-choice",
            "Category",
            "7",
        )
        require(typed.returncode == 0, "typed categorical trial failed")
        typed_trial = json.loads(typed.stdout)["trials"][0]
        require(
            typed_trial["parameters"]["Category"] == 7,
            "integer categorical choice lost its type",
        )
        require(
            typed_trial["backtest"]
            == {
                "input_bars_processed": 2,
                "script_bars_processed": 1,
                "input_tf_seconds": 60,
                "script_tf_seconds": 300,
                "script_tf_ratio": 5,
                "needs_aggregation": True,
            },
            "native result omitted the engine timeframe aggregation evidence",
        )

        adaptive = invoke(
            native,
            plugin,
            csv,
            sampler="dlib_global",
            max_trials=8,
            workers=2,
            length_low=1,
            length_high=20,
        )
        require(adaptive.returncode == 0, "native dlib_global study failed")
        adaptive_json = json.loads(adaptive.stdout)
        require(adaptive_json["sampler"] == "dlib_global", "wrong sampler in output")
        require(adaptive_json["trials_completed"] == 8, "adaptive trial budget drifted")
        adaptive_trials = adaptive_json["trials"]
        require(
            adaptive_json["best_value"]
            == max(trial["objective"] for trial in adaptive_trials),
            "adaptive runner selected the wrong best trial",
        )
        repeat = invoke(
            native,
            plugin,
            csv,
            sampler="dlib_global",
            max_trials=8,
            workers=2,
            length_low=1,
            length_high=20,
        )
        require(repeat.returncode == 0, "repeated native dlib_global study failed")
        repeat_trials = json.loads(repeat.stdout)["trials"]
        require(
            [trial["parameters"] for trial in adaptive_trials]
            == [trial["parameters"] for trial in repeat_trials],
            "fixed seed and batch size did not reproduce dlib proposals",
        )

        tpe_args = (
            "--tpe-startup-trials",
            "3",
            "--tpe-ei-candidates",
            "16",
            "--tpe-gamma-fraction",
            "0.25",
            "--tpe-gamma-cap",
            "4",
            "--tpe-prior-weight",
            "1.5",
            "--tpe-constant-liar",
            "true",
        )
        tpe = invoke(
            native,
            plugin,
            csv,
            *tpe_args,
            sampler="tpe",
            max_trials=12,
            workers=3,
            length_low=1,
            length_high=20,
        )
        require(tpe.returncode == 0, f"native TPE study failed: {tpe.stderr}")
        tpe_json = json.loads(tpe.stdout)
        require(tpe_json["schema_version"] == 1, "wrong native result schema")
        require(
            tpe_json["pineforge_hpo_version"] != "unknown",
            "native result omitted the HPO version",
        )
        require(
            tpe_json["sampler_implementation"] == "pineforge_product_tpe_v2",
            "native result omitted the TPE implementation version",
        )
        require(tpe_json["sampler"] == "tpe", "wrong TPE sampler in output")
        require(tpe_json["trials_completed"] == 12, "TPE trial budget drifted")
        require(
            tpe_json["candidate_policy"] == "sampler_default"
            and tpe_json["candidate_policy_implementation"] == "sampler_default",
            "default TPE candidate policy provenance drifted",
        )
        require(
            tpe_json["search_space_finite"]
            and tpe_json["search_space_cardinality"] == 20,
            "default TPE omitted finite search-space provenance",
        )
        require(
            tpe_json["trials_requested"] == 12
            and tpe_json["duplicate_proposals_skipped"] == 0,
            "default TPE trial or duplicate provenance drifted",
        )
        require(
            tpe_json["sampler_config"]
            == {
                "startup_trials": 3,
                "ei_candidates": 16,
                "gamma_fraction": 0.25,
                "gamma_cap": 4,
                "prior_weight": 1.5,
                "constant_liar": True,
            },
            "TPE configuration was not preserved in result provenance",
        )
        tpe_trials = tpe_json["trials"]
        unique_tpe_parameters = {
            tuple(sorted(trial["parameters"].items())) for trial in tpe_trials
        }
        require(
            tpe_json["unique_candidates_attempted"] == len(unique_tpe_parameters),
            "default TPE unique-candidate count disagreed with its trial table",
        )
        require(
            tpe_json["remaining_candidates"] == 20 - len(unique_tpe_parameters),
            "default TPE remaining-candidate count drifted",
        )
        require(
            not tpe_json["search_space_exhausted"]
            and tpe_json["stop_reason"] == "trial_budget_reached"
            and not tpe_json["full_parameter_coverage"]
            and not tpe_json["exhaustive_equivalent"],
            "default TPE overstated finite-space coverage",
        )
        require(
            tpe_json["best_value"] == max(trial["objective"] for trial in tpe_trials),
            "TPE runner selected the wrong best trial",
        )
        tpe_repeat = invoke(
            native,
            plugin,
            csv,
            *tpe_args,
            sampler="tpe",
            max_trials=12,
            workers=3,
            length_low=1,
            length_high=20,
        )
        require(tpe_repeat.returncode == 0, "repeated native TPE study failed")
        tpe_repeat_trials = json.loads(tpe_repeat.stdout)["trials"]
        require(
            [trial["parameters"] for trial in tpe_trials]
            == [trial["parameters"] for trial in tpe_repeat_trials],
            "fixed seed and batch size did not reproduce TPE proposals",
        )

        exhaustive_dimensions = (
            "--candidate-policy",
            "exhaustive",
            "--bool-dim",
            "Enabled",
            "--bool-dim",
            "Confirmed",
        )
        exhaustive_tpe = invoke(
            native,
            plugin,
            csv,
            *exhaustive_dimensions,
            sampler="tpe",
            max_trials=4,
            workers=4,
        )
        require(
            exhaustive_tpe.returncode == 0,
            f"exhaustive 2x2 TPE study failed: {exhaustive_tpe.stderr}",
        )
        exhaustive_tpe_json = json.loads(exhaustive_tpe.stdout)
        exhaustive_tpe_parameters = {
            tuple(sorted(trial["parameters"].items()))
            for trial in exhaustive_tpe_json["trials"]
        }
        require(
            len(exhaustive_tpe_json["trials"]) == 4
            and len(exhaustive_tpe_parameters) == 4,
            "exhaustive 2x2 TPE did not attempt four unique parameter vectors",
        )
        require(
            exhaustive_tpe_json["sampler_implementation"]
            == "pineforge_product_tpe_v2_finite"
            and exhaustive_tpe_json["candidate_policy"] == "exhaustive"
            and exhaustive_tpe_json["candidate_policy_implementation"]
            == "pineforge_finite_space_v1",
            "exhaustive TPE implementation provenance drifted",
        )
        require(
            exhaustive_tpe_json["search_space_finite"]
            and exhaustive_tpe_json["search_space_cardinality"] == 4
            and exhaustive_tpe_json["trials_requested"] == 4
            and exhaustive_tpe_json["trials_completed"] == 4
            and exhaustive_tpe_json["unique_candidates_attempted"] == 4
            and exhaustive_tpe_json["remaining_candidates"] == 0,
            "exhaustive TPE finite-space counters drifted",
        )
        require(
            exhaustive_tpe_json["search_space_exhausted"]
            and exhaustive_tpe_json["stop_reason"] == "search_space_exhausted"
            and exhaustive_tpe_json["full_parameter_coverage"]
            and exhaustive_tpe_json["exhaustive_equivalent"],
            "exhaustive TPE did not certify complete successful coverage",
        )

        exhaustive_grid = invoke(
            native,
            plugin,
            csv,
            *exhaustive_dimensions,
            sampler="grid",
            max_trials=4,
            workers=4,
        )
        require(
            exhaustive_grid.returncode == 0,
            f"exhaustive 2x2 grid study failed: {exhaustive_grid.stderr}",
        )
        exhaustive_grid_json = json.loads(exhaustive_grid.stdout)
        exhaustive_grid_parameters = {
            tuple(sorted(trial["parameters"].items()))
            for trial in exhaustive_grid_json["trials"]
        }
        require(
            exhaustive_grid_json["sampler_implementation"] == "pineforge_grid_v2_finite"
            and exhaustive_grid_json["candidate_policy"] == "exhaustive"
            and exhaustive_grid_json["exhaustive_equivalent"],
            "exhaustive grid provenance drifted",
        )
        require(
            exhaustive_tpe_parameters == exhaustive_grid_parameters,
            "exhaustive TPE and grid covered different parameter sets",
        )

        missing_plugin = Path(directory) / "does-not-exist.dylib"
        missing_csv = Path(directory) / "does-not-exist.csv"
        continuous_exhaustive = invoke(
            native,
            missing_plugin,
            missing_csv,
            "--candidate-policy",
            "exhaustive",
            "--real-dim",
            "Continuous",
            "0",
            "1",
            "continuous",
            sampler="tpe",
            max_trials=2,
        )
        require(
            continuous_exhaustive.returncode == 1,
            "continuous exhaustive space reached nonexistent plugin/data paths",
        )
        require(
            "finite candidate policy requires a step on every varying real dimension"
            in continuous_exhaustive.stderr,
            "continuous exhaustive space did not fail at policy validation",
        )

        mismatched_exhaustive = invoke(
            native,
            missing_plugin,
            missing_csv,
            "--candidate-policy",
            "exhaustive",
            "--bool-dim",
            "Enabled",
            "--bool-dim",
            "Confirmed",
            sampler="tpe",
            max_trials=3,
        )
        require(
            mismatched_exhaustive.returncode == 1,
            "exhaustive cardinality mismatch reached nonexistent plugin/data paths",
        )
        require(
            "exhaustive candidate budget must equal search-space cardinality"
            in mismatched_exhaustive.stderr,
            "exhaustive cardinality mismatch did not fail before artifact loading",
        )

        invalid_tpe = invoke(
            native,
            plugin,
            csv,
            "--tpe-startup-trials",
            "0",
            sampler="tpe",
            max_trials=4,
        )
        require(invalid_tpe.returncode == 1, "invalid TPE config was accepted")
        require(
            "--tpe-startup-trials must be greater than zero" in invalid_tpe.stderr,
            "invalid TPE config diagnostic was not preserved",
        )

        oversized_tpe = invoke(
            native,
            plugin,
            csv,
            "--tpe-ei-candidates",
            "1000001",
            sampler="tpe",
            max_trials=4,
        )
        require(oversized_tpe.returncode == 1, "oversized TPE EI pool was accepted")
        require(
            "--tpe-ei-candidates must be between 1 and 1000000" in oversized_tpe.stderr,
            "oversized TPE EI diagnostic was not preserved",
        )

        log_tpe = invoke(
            native,
            plugin,
            csv,
            *tpe_args,
            "--log-int-dim",
            "Scale",
            "1",
            "1000000",
            "--log-real-dim",
            "Learning Rate",
            "0.000001",
            "1000",
            "--seed",
            "123",
            sampler="tpe",
            max_trials=64,
        )
        require(
            log_tpe.returncode == 0, f"native log TPE study failed: {log_tpe.stderr}"
        )
        log_trials = json.loads(log_tpe.stdout)["trials"]
        require(len(log_trials) == 64, "native log study trial budget drifted")
        require(
            all(
                1 <= trial["parameters"]["Scale"] <= 1_000_000
                and 1e-6 <= trial["parameters"]["Learning Rate"] <= 1e3
                for trial in log_trials
            ),
            "native log flags produced an out-of-bounds value",
        )

        invalid_log = invoke(
            native,
            plugin,
            csv,
            "--log-real-dim",
            "Learning Rate",
            "0",
            "1",
            sampler="random",
            max_trials=2,
        )
        require(invalid_log.returncode == 1, "non-positive log bound was accepted")
        require(
            "log real dimension bounds must be positive" in invalid_log.stderr,
            "invalid log-bound diagnostic was not preserved",
        )

    print("native CLI tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
