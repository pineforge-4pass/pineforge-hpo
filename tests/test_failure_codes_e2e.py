"""Real-engine failure-channel and valid-study baseline equivalence receipts."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external/pineforge-codegen-oss"))

SOURCE = """//@version=6
strategy("Failure channel fixture", initial_capital=10000)
length = input.int(1, "Length", minval=1, maxval=3)
fixed_number = input.int(1, "FixedNumber")
mode = input.string("fast", "Mode", options=["fast", "slow"])
if bar_index % 2 == 0 and mode == "fast"
    strategy.entry("Long", strategy.long, qty=length * fixed_number)
else
    strategy.close_all()
"""


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    from pineforge_hpo import ArtifactBuilder

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--engine-root", type=Path, required=True)
    parser.add_argument("--engine-codes", choices=("present", "absent"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    csv = output / "bars.csv"
    csv.write_text(
        "timestamp,open,high,low,close,volume\n"
        + "".join(
            f"{1700000000000 + index * 60000},{100 + index},{102 + index},"
            f"{99 + index},{101 + index},100\n"
            for index in range(12)
        )
    )
    builder = ArtifactBuilder(engine_root=args.engine_root, cache_dir=output / "cache")
    valid = builder.build(SOURCE)
    silent = builder.build(SOURCE + "if bar_index >= 3\n    runtime.error()\n")
    engine_codes = hasattr(
        ctypes.CDLL(str(valid.plugin_path)), "strategy_get_last_error_code"
    )
    require(
        engine_codes == (args.engine_codes == "present"),
        "compiled plugin failure getters differ from the selected engine",
    )

    def run(label, executable, artifact, extra=(), exit_code=0):
        command = [
            str(executable.resolve()),
            "--strategy",
            str(artifact.plugin_path),
            "--ohlcv",
            str(csv),
            "--objective",
            "metrics.all.net_profit",
            "--input-tf",
            "1",
            "--script-tf",
            "1",
            "--bar-magnifier",
            "false",
            "--sampler",
            "grid",
            "--seed",
            "17",
            "--workers",
            "1",
            "--max-trials",
            "3",
            "--int-dim",
            "Length",
            "1",
            "3",
            "1",
            *extra,
        ]
        completed = subprocess.run(command, capture_output=True, timeout=180)
        (output / (label + ".stdout.json")).write_bytes(completed.stdout)
        (output / (label + ".stderr.txt")).write_bytes(completed.stderr)
        require(
            completed.returncode == exit_code,
            (label, completed.returncode, completed.stderr.decode()),
        )
        return json.loads(completed.stdout)

    before = run("valid-before", args.baseline, valid)
    after = run("valid-after", args.native, valid)
    observations = []
    for label, document in (("before", before), ("after", after)):
        require(all(trial["status"] == "ok" for trial in document["trials"]), label)
        fields = [
            {
                name: value
                for name, value in trial.items()
                if name not in {"failure_code", "failure_args", "failure_origin"}
            }
            for trial in document["trials"]
        ]
        raw = json.dumps(
            fields,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        (output / (label + "-observations.json")).write_bytes(raw)
        observations.append(raw)
    require(
        observations[0] == observations[1],
        "valid trial params/metrics/objective changed",
    )
    require(
        before["best_trial_id"] == after["best_trial_id"], "valid best trial changed"
    )
    stopped_before = run("silent-before", args.baseline, silent)
    stopped_after = run("silent-after", args.native, silent, exit_code=2)
    require(
        all(trial["status"] == "ok" for trial in stopped_before["trials"]),
        "baseline did not reproduce the silent-success bug",
    )
    require(
        stopped_before["best_trial_id"] is not None,
        "baseline did not score partial reports",
    )
    require(stopped_after["best_trial_id"] is None, "silent failure won the study")
    for trial in stopped_after["trials"]:
        require(trial["status"] == "engine_error" and trial["objective"] is None, trial)
        require(trial["error"] == "" and trial["failure_origin"] == "engine", trial)
        require(
            trial["failure_code"]
            == ("strategy_runtime_error" if engine_codes else None),
            trial,
        )
        require(trial["failure_args"] == ({} if engine_codes else None), trial)

    refusals = (
        (
            "unknown-title",
            ["--fixed-input", "Unknown title", "1"],
            "strategy_set_input",
        ),
        ("invalid-value", ["--fixed-input", "Mode", "forged"], "strategy_set_input"),
        (
            "invalid-number",
            ["--fixed-input", "FixedNumber", "not-a-number"],
            "strategy_set_input",
        ),
        (
            "unknown-override",
            ["--strategy-override", "unknown", "1"],
            "strategy_set_override",
        ),
        (
            "invalid-enum",
            ["--strategy-override", "commission_type", "forged"],
            "strategy_set_override",
        ),
    )
    for label, extra, entrypoint in refusals:
        document = run(label, args.native, valid, extra, exit_code=2)
        require(document["best_trial_id"] is None, label)
        for trial in document["trials"]:
            require(
                trial["status"] == "trial_error" and trial["objective"] is None, trial
            )
            require(
                trial["failure_origin"] == "engine"
                and trial["failure_code"] == "setting_rejected",
                trial,
            )
            require(trial["failure_args"]["entrypoint"] == entrypoint, trial)
            if "input" in trial["failure_args"]:
                require(
                    trial["failure_args"]["input"] in {"Mode", "FixedNumber"}, trial
                )

    receipt = {
        "schema": "pineforge-hpo-failure-e2e/v1",
        "engine_failure_getters": engine_codes,
        "valid_observations_equal": True,
        "valid_observations_sha256": hashlib.sha256(observations[0]).hexdigest(),
        "valid_candidates": len(after["trials"]),
        "baseline_silent_status": "ok",
        "candidate_silent_status": "engine_error",
        "silent_failure_code": "strategy_runtime_error" if engine_codes else None,
        "checked_refusals": [item[0] for item in refusals],
        "artifact_keys": {"valid": valid.artifact_key, "silent": silent.artifact_key},
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
