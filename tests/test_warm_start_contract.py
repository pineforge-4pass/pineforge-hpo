"""Native continuation contract and 200 + 200 versus 400 trial equivalence."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run(
    native,
    plugin,
    csv,
    directory,
    sampler,
    batch,
    trials=200,
    warm=None,
    policy="sampler_default",
    extra=(),
    expected=0,
    label="run",
    dimensions=("--int-dim", "Length", "0", "399", "1"),
):
    progress_path = directory / f"{label}.fd3.jsonl"
    trials_path = directory / f"{label}.trials.jsonl"
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
        "--candidate-policy",
        policy,
        "--seed",
        "73",
        "--max-trials",
        str(trials),
        "--workers",
        "8",
        "--batch-size",
        str(batch),
        *dimensions,
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
        "--trials-file",
        str(trials_path),
    ]
    if sampler == "tpe":
        command += ["--tpe-history-switch", "32"]
    if warm is not None:
        command += ["--warm-start", str(warm)]
    command += list(extra)
    with progress_path.open("w") as progress:
        command += ["--progress-fd", str(progress.fileno())]
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            pass_fds=(progress.fileno(),),
            timeout=120,
        )
    require(
        completed.returncode == expected,
        f"{label}: exit {completed.returncode}, expected {expected}: {completed.stderr}",
    )
    if expected:
        require(
            not completed.stdout and not progress_path.read_text(),
            "init refusal emitted a result or billable trial",
        )
        require(not trials_path.exists(), "init refusal opened/truncated trials output")
        return completed
    result = json.loads(completed.stdout)
    lines = [json.loads(line) for line in progress_path.read_text().splitlines()]
    require(lines == result["trials"], "fd-3 differs from ordered new-trial array")
    require(
        progress_path.read_bytes() == trials_path.read_bytes(),
        "trials-file differs from fd-3",
    )
    require(len(lines) == trials, f"billing count {len(lines)} != new budget {trials}")
    result_path = directory / f"{label}.json"
    result_path.write_text(completed.stdout)
    return completed, result, result_path, progress_path


def vectors(result):
    return [trial["parameters"] for trial in result["trials"]]


def main():
    from pineforge_hpo.continuation import space_info
    from pineforge_hpo.study_spec import load_study_spec

    native, plugin = [Path(value).resolve() for value in sys.argv[1:3]]
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        csv = directory / "bars.csv"
        csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            "1700000000000,100,102,99,101,10\n"
            "1700000060000,101,103,100,102,11\n"
        )
        spec_path = directory / "study.json"
        spec = json.loads((ROOT / "examples/single_strategy/study.json").read_text())
        spec["strategies"][0].update(
            source="missing.pine",
            search_space={"Length": {"kind": "integer", "low": 0, "high": 399}},
        )
        spec["objective"].update(expression="metrics.all.net_profit", constraints=[])
        spec["sampler"].update(kind="tpe", trials=200)
        spec_path.write_text(json.dumps(spec))
        study = load_study_spec(spec_path)
        for sampler, policy in (
            ("grid", "sampler_default"),
            ("random", "sampler_default"),
            ("tpe", "sampler_default"),
            ("tpe", "without_replacement"),
        ):
            for batch in (2, 5, 8):
                label = f"{sampler}-{policy}-{batch}"
                _, parent, parent_path, parent_lines = run(
                    native,
                    plugin,
                    csv,
                    directory,
                    sampler,
                    batch,
                    policy=policy,
                    label=label + "-parent",
                )
                child_stdout, child, child_path, _ = run(
                    native,
                    plugin,
                    csv,
                    directory,
                    sampler,
                    batch,
                    warm=parent_lines,
                    policy=policy,
                    label=label + "-child",
                )
                replay_stdout, replay, _, _ = run(
                    native,
                    plugin,
                    csv,
                    directory,
                    sampler,
                    batch,
                    warm=parent_lines,
                    policy=policy,
                    label=label + "-replay",
                )
                require(
                    child_stdout.stdout == replay_stdout.stdout,
                    "continuation not byte-identical",
                )
                _, from_result, _, _ = run(
                    native,
                    plugin,
                    csv,
                    directory,
                    sampler,
                    batch,
                    warm=parent_path,
                    policy=policy,
                    label=label + "-result-parent",
                )
                require(
                    child["trials"] == from_result["trials"],
                    "JSONL/result parent proposals differ",
                )
                _, uninterrupted, _, _ = run(
                    native,
                    plugin,
                    csv,
                    directory,
                    sampler,
                    batch,
                    trials=400,
                    policy=policy,
                    label=label + "-long",
                )
                require(
                    child["trials_completed"] == 200
                    and child["warm_start"]["trials"] == 200,
                    "warm trials counted as new trials",
                )
                require(
                    child["warm_start"]["source_sha256"]
                    == hashlib.sha256(parent_lines.read_bytes()).hexdigest(),
                    "source digest differs",
                )
                require(
                    [trial["trial_id"] for trial in child["trials"]]
                    == list(range(200, 400)),
                    "trial IDs did not continue",
                )
                if sampler in {"grid", "tpe"}:
                    require(
                        parent["trials"] + child["trials"] == uninterrupted["trials"],
                        "uninterrupted equivalence failed at a complete batch boundary",
                    )
                if sampler == "random":
                    parent_points = {
                        json.dumps(value, sort_keys=True) for value in vectors(parent)
                    }
                    require(
                        all(
                            json.dumps(value, sort_keys=True) not in parent_points
                            for value in vectors(child)
                        ),
                        "random replayed a parent vector",
                    )
                for warm in (parent_path, parent_lines):
                    info_result = subprocess.run(
                        [
                            str(native),
                            "space-info",
                            "--spec",
                            str(spec_path),
                            "--warm-start",
                            str(warm),
                        ],
                        text=True,
                        capture_output=True,
                        timeout=15,
                    )
                    require(info_result.returncode == 0, info_result.stderr)
                    require(
                        json.loads(info_result.stdout) == space_info(study, warm),
                        "native/Python coverage or space hashes differ",
                    )
                if sampler == "grid" or policy == "without_replacement":
                    info = space_info(study, child_path)
                    require(
                        (info["tried"], info["remaining"]) == (400, 0),
                        "full set coverage differs",
                    )
                    refused = run(
                        native,
                        plugin,
                        csv,
                        directory,
                        sampler,
                        batch,
                        warm=child_path,
                        policy=policy,
                        expected=5,
                        label=label + "-exhausted",
                    )
                    require(
                        "space exhausted" in refused.stderr, "wrong exhaustion message"
                    )
                print(
                    f"PASS {label}: 200 + 200; fd3=200; byte replay; JSONL/result; "
                    f"space-info; equivalence={'full ordered sequence' if sampler != 'random' else 'not defined'}"
                )

        _, baseline, baseline_path, _ = run(
            native, plugin, csv, directory, "grid", 2, label="mismatch-parent"
        )
        for option in (
            ("--direction", "minimize"),
            ("--constraint", "metrics.all.num_trades > 0"),
            ("--objective", "metrics.all.num_trades"),
        ):
            refused = run(
                native,
                plugin,
                csv,
                directory,
                "grid",
                2,
                warm=baseline_path,
                extra=option,
                expected=4,
                label="mismatch-" + option[0][2:],
            )
            require(
                "warm-start incompatible" in refused.stderr, "wrong mismatch message"
            )
        for change in (
            lambda trial: trial.update(status="unknown"),
            lambda trial: trial["space"]["parameters"]["Length"].update(high=400),
            lambda trial: trial["space"]["parameters"]["Length"].update(step=2),
            lambda trial: trial["space"]["parameters"]["Length"].update(log=True),
            lambda trial: trial["space"]["parameters"]["Length"].update(kind="real"),
            lambda trial: trial["space"]["parameters"].update(
                Other={"kind": "boolean"}
            ),
        ):
            corrupt = copy.deepcopy(baseline)
            change(corrupt["trials"][0])
            path = directory / "corrupt.json"
            path.write_text(json.dumps(corrupt))
            run(
                native,
                plugin,
                csv,
                directory,
                "grid",
                2,
                warm=path,
                expected=4,
                label="corrupt-" + str(id(change)),
            )
        near_limit = copy.deepcopy(baseline)
        near_limit["trials"] = near_limit["trials"][:1]
        near_limit["trials_completed"] = 1
        near_limit["trials"][0]["trial_id"] = (1 << 64) - 4
        near_limit_path = directory / "near-id-limit.json"
        near_limit_path.write_text(json.dumps(near_limit))
        for sampler in ("grid", "random", "tpe"):
            _, last_ids, _, _ = run(
                native,
                plugin,
                csv,
                directory,
                sampler,
                5,
                trials=2,
                warm=near_limit_path,
                extra=("--max-trials", "0", "--max-wall-seconds", "5"),
                label=sampler + "-wall-only-id-limit",
            )
            require(
                [trial["trial_id"] for trial in last_ids["trials"]]
                == [(1 << 64) - 3, (1 << 64) - 2],
                "time-capped continuation wrapped trial IDs",
            )
        near_limit["trials"][0]["trial_id"] = (1 << 64) - 2
        near_limit_path.write_text(json.dumps(near_limit))
        run(
            native,
            plugin,
            csv,
            directory,
            "random",
            2,
            warm=near_limit_path,
            extra=("--max-trials", "0", "--max-wall-seconds", "5"),
            expected=4,
            label="wall-only-no-trial-ids",
        )
        older = copy.deepcopy(baseline)
        older.update(space_hash_version=0, space_hash="old")
        for trial in older["trials"]:
            trial.update(space_hash_version=0, space_hash="old")
        old_path = directory / "old.json"
        old_path.write_text(json.dumps(older))
        run(
            native,
            plugin,
            csv,
            directory,
            "grid",
            8,
            warm=old_path,
            label="old-version",
        )
        for sampler in ("grid", "tpe"):
            _, exhaustive, _, _ = run(
                native,
                plugin,
                csv,
                directory,
                sampler,
                5,
                warm=baseline_path,
                policy="exhaustive",
                label=sampler + "-warm-exhaustive",
            )
            require(
                {
                    trial["parameters"]["Length"]
                    for trial in baseline["trials"] + exhaustive["trials"]
                }
                == set(range(400)),
                "warm exhaustive set differs",
            )
            for budget in (199, 201):
                run(
                    native,
                    plugin,
                    csv,
                    directory,
                    sampler,
                    5,
                    trials=budget,
                    warm=baseline_path,
                    policy="exhaustive",
                    expected=4,
                    label=f"{sampler}-bad-exhaustive-{budget}",
                )
        run(
            native,
            plugin,
            csv,
            directory,
            "tpe",
            5,
            trials=201,
            warm=baseline_path,
            policy="without_replacement",
            expected=4,
            label="over-remaining-budget",
        )
        _, middle, middle_path, _ = run(
            native,
            plugin,
            csv,
            directory,
            "grid",
            5,
            trials=100,
            warm=baseline_path,
            label="chain-middle",
        )
        _, final, _, _ = run(
            native,
            plugin,
            csv,
            directory,
            "grid",
            8,
            trials=100,
            warm=middle_path,
            label="chain-final",
        )
        require(final["warm_start"]["trials"] == 300, "chained ancestry was lost")
        require(
            [
                trial["parameters"]["Length"]
                for trial in baseline["trials"] + middle["trials"] + final["trials"]
            ]
            == list(range(400)),
            "chain order differs",
        )
        sparse = [
            copy.deepcopy(baseline["trials"][99]),
            copy.deepcopy(baseline["trials"][7]),
        ]
        sparse[0]["trial_id"], sparse[1]["trial_id"] = 711, 700
        sparse_path = directory / "sparse.json"
        sparse_path.write_text(json.dumps(sparse))
        _, holes, _, _ = run(
            native,
            plugin,
            csv,
            directory,
            "grid",
            2,
            trials=3,
            warm=sparse_path,
            label="sparse-grid",
        )
        require(
            [trial["trial_id"] for trial in holes["trials"]] == [712, 713, 714],
            "sparse max ID was ignored",
        )
        require(
            [trial["parameters"]["Length"] for trial in holes["trials"]] == [0, 1, 2],
            "grid did not fill sparse holes in ordinal order",
        )
        lagged_bytes, lagged, _, _ = run(
            native,
            plugin,
            csv,
            directory,
            "tpe",
            5,
            warm=baseline_path,
            extra=("--batch-lag", "1"),
            label="warm-lag-one",
        )
        lag_replay_bytes, _, _, _ = run(
            native,
            plugin,
            csv,
            directory,
            "tpe",
            5,
            warm=baseline_path,
            extra=("--batch-lag", "1"),
            label="warm-lag-one-replay",
        )
        require(
            lagged_bytes.stdout == lag_replay_bytes.stdout
            and lagged["warm_start_model"] == "rebuilt_history",
            "lag-one rebuild is not deterministic",
        )
        mixed_dimensions = (
            "--int-dim",
            "Length",
            "0",
            "399",
            "1",
            "--bool-dim",
            "enabled",
            "--categorical-int-choice",
            "choice",
            "3",
            "--categorical-real-choice",
            "choice",
            "4.0",
            "--categorical-bool-choice",
            "choice",
            "true",
            "--categorical-choice",
            "choice",
            "臺北\n",
            "--real-dim",
            "step",
            "0",
            "0.9",
            "0.3",
            "--log-real-dim",
            "scale",
            "0.1",
            "10",
        )
        mixed_spec = copy.deepcopy(spec)
        mixed_spec["strategies"][0]["search_space"].update(
            enabled={"kind": "boolean"},
            choice={"kind": "categorical", "choices": [3, 4.0, True, "臺北\n"]},
            step={"kind": "real", "low": 0.0, "high": 0.9, "step": 0.3},
            scale={"kind": "real", "low": 0.1, "high": 10.0, "log": True},
        )
        mixed_spec_path = directory / "mixed-study.json"
        mixed_spec_path.write_text(json.dumps(mixed_spec))
        mixed_study = load_study_spec(mixed_spec_path)
        for batch in (2, 5, 8):
            label = f"mixed-b{batch}"
            _, mixed_parent, mixed_path, _ = run(
                native,
                plugin,
                csv,
                directory,
                "tpe",
                batch,
                trials=40,
                dimensions=mixed_dimensions,
                label=label + "-parent",
            )
            mixed_output, mixed_child, _, _ = run(
                native,
                plugin,
                csv,
                directory,
                "tpe",
                batch,
                trials=40,
                dimensions=mixed_dimensions,
                warm=mixed_path,
                label=label + "-child",
            )
            mixed_replay, _, _, _ = run(
                native,
                plugin,
                csv,
                directory,
                "tpe",
                batch,
                trials=40,
                dimensions=mixed_dimensions,
                warm=mixed_path,
                label=label + "-replay",
            )
            _, mixed_long, _, _ = run(
                native,
                plugin,
                csv,
                directory,
                "tpe",
                batch,
                trials=80,
                dimensions=mixed_dimensions,
                label=label + "-long",
            )
            require(
                mixed_output.stdout == mixed_replay.stdout
                and mixed_parent["trials"] + mixed_child["trials"]
                == mixed_long["trials"],
                "mixed typed/continuous JSON history did not replay exactly",
            )
            mixed_info = subprocess.run(
                [
                    str(native),
                    "space-info",
                    "--spec",
                    str(mixed_spec_path),
                    "--warm-start",
                    str(mixed_path),
                ],
                text=True,
                capture_output=True,
                timeout=15,
            )
            require(
                mixed_info.returncode == 0
                and json.loads(mixed_info.stdout) == space_info(mixed_study, mixed_path)
                and json.loads(mixed_info.stdout)["cardinality"] is None,
                "mixed typed/continuous native/Python space-info differs",
            )
        pruner_csv = directory / "pruner.csv"
        pruner_csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            + "\n".join(
                f"{1700000000000 + index * 60000},100,102,99,101,10"
                for index in range(80)
            )
            + "\n"
        )
        pruning = (
            "--fixed-input",
            "BatchPrefixTest",
            "1",
            "--fixed-input",
            "BatchPrefixJitter",
            "0",
            "--pruner",
            "median",
            "--pruner-rungs",
            "0.25,0.5",
        )
        _, pruned_parent, pruned_path, _ = run(
            native,
            plugin,
            pruner_csv,
            directory,
            "tpe",
            5,
            extra=pruning,
            label="pruning-parent",
        )
        _, pruned_child, _, _ = run(
            native,
            plugin,
            pruner_csv,
            directory,
            "tpe",
            5,
            warm=pruned_path,
            extra=pruning,
            label="pruning-child",
        )
        _, pruned_long, _, _ = run(
            native,
            plugin,
            pruner_csv,
            directory,
            "tpe",
            5,
            trials=400,
            extra=pruning,
            label="pruning-long",
        )
        require(
            any(trial["status"] == "pruned" for trial in pruned_long["trials"]),
            "pruning fixture never pruned",
        )
        require(
            pruned_parent["trials"] + pruned_child["trials"] == pruned_long["trials"],
            "warm pruning rung history differs from uninterrupted execution",
        )
        print(
            "PASS exhaustive/remaining-budget, chained ancestry, sparse grid, lag-one rebuild, "
            "mixed typed/continuous JSON replay, pruning equivalence; "
            "compatibility/status exit=4, exhaustion exit=5; no warm billing"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
