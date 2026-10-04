"""Real compiled-Pine continuation gate; artifacts stay in an ignored output directory."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import subprocess
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    from pineforge_hpo.cli import prepare_run
    from pineforge_hpo.continuation import space_info
    from pineforge_hpo.study_spec import load_study_spec
    from pineforge_hpo.warm_binary import encode_warm_block

    parser = argparse.ArgumentParser()
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    csv = directory / "bars.csv"
    bars = ["timestamp,open,high,low,close,volume"]
    for index in range(256):
        price = 100 + 5 * math.sin(index * 0.15)
        bars.append(
            f"{1700000000000 + index * 60000},{price},{price + 1},"
            f"{price - 1},{price + 0.1},100"
        )
    csv.write_text("\n".join(bars) + "\n")
    source = directory / "threshold.pine"
    source.write_text(
        (ROOT / "examples/single_strategy/threshold.pine")
        .read_text()
        .replace("minval=101, maxval=103", "minval=1, maxval=400")
    )
    spec = json.loads((ROOT / "examples/single_strategy/study.json").read_text())
    spec["strategies"][0].update(
        source=str(source),
        fixed_inputs={"Exit Level": 98.0},
        search_space={
            "Entry Level": {"kind": "real", "low": 1.0, "high": 400.0, "step": 1.0}
        },
    )
    spec["datasets"][0]["ohlcv"] = str(csv)
    spec["objective"].update(expression="metrics.all.net_profit", constraints=[])
    spec["sampler"].update(
        kind="tpe", seed=73, trials=200, config={"history_switch": 32}
    )
    spec["execution"].update(workers=8, batch_size=8, batch_lag=0, pruner="none")
    spec_path = directory / "study.json"
    spec_path.write_text(json.dumps(spec))
    study = load_study_spec(spec_path)
    command, artifact = prepare_run(
        spec_path,
        ROOT / "external/pineforge-engine",
        directory / "cache",
        native=args.native,
        compiler="g++",
        eigen_include="/usr/include/eigen3",
    )
    (directory / "artifact.json").write_text(json.dumps(artifact, indent=2))
    plugin = artifact["plugin"]
    base = [
        str(args.native.resolve()),
        "run",
        "--strategy",
        plugin,
        "--ohlcv",
        str(csv),
        "--objective",
        "metrics.all.net_profit",
        "--seed",
        "73",
        "--workers",
        "8",
        "--input-tf",
        "1",
        "--script-tf",
        "1",
        "--real-dim",
        "Entry Level",
        "1",
        "400",
        "1",
        "--fixed-input",
        "Exit Level",
        "98",
    ]
    canonical_process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "external/pineforge-engine/docker/run_json.py"),
            "--so",
            plugin,
            "--ohlcv",
            str(csv),
            "--inputs",
            '{"Entry Level":"101","Exit Level":"98"}',
            "--input-tf",
            "1",
            "--script-tf",
            "1",
        ],
        capture_output=True,
        timeout=60,
    )
    require(
        canonical_process.returncode == 0,
        f"canonical harness: {canonical_process.stderr.decode()}",
    )
    canonical = json.loads(canonical_process.stdout)
    (directory / "canonical.json").write_bytes(canonical_process.stdout)
    fixed_base = list(base)
    dimension = fixed_base.index("--real-dim")
    fixed_base[dimension + 2 : dimension + 4] = ["101", "101"]
    metrics = (
        "metrics.all.net_profit",
        "metrics.all.num_trades",
        "metrics.all.gross_profit",
        "metrics.equity.max_equity_drawdown",
    )
    fixed_process = subprocess.run(
        fixed_base
        + ["--sampler", "grid", "--max-trials", "1"]
        + [argument for metric in metrics for argument in ("--record-metric", metric)],
        capture_output=True,
        timeout=60,
    )
    require(fixed_process.returncode == 0, fixed_process.stderr.decode())
    (directory / "native-fixed.json").write_bytes(fixed_process.stdout)
    fixed = json.loads(fixed_process.stdout)["trials"][0]
    require(
        fixed["total_trades"] == len(canonical["trades"]) > 0,
        "canonical/native trade count differs",
    )
    for metric in metrics:
        value = canonical
        for segment in metric.split("."):
            value = value[segment]
        require(fixed["metrics"][metric] == value, f"canonical/native {metric} differs")
    trade_probe = directory / "real-strategy-trades"
    subprocess.run(
        ["g++", "-std=c++17", "-O2", "-ffp-contract=off", "-I", str(ROOT / "include"),
         "-I", str(ROOT / "external/pineforge-engine/include"),
         "-I", str(ROOT / "external/pineforge-engine/build/include"),
         str(ROOT / "tests/real_strategy_trades.cpp"),
         str(ROOT / "src/engine_adapter/strategy_plugin.cpp"),
         str(ROOT / "src/engine_adapter/dataset.cpp"), "-ldl", "-o", str(trade_probe)],
        check=True, timeout=120,
    )
    trade_process = subprocess.run(
        [str(trade_probe), plugin, str(csv)], capture_output=True, check=True, timeout=60,
    )
    (directory / "native-trades.json").write_bytes(trade_process.stdout)
    actual_trades = json.loads(trade_process.stdout)
    require(len(actual_trades) == len(canonical["trades"]), "canonical trade rows differ")
    for actual_trade, expected_trade in zip(actual_trades, canonical["trades"]):
        for field, value in actual_trade.items():
            expected = expected_trade[field]
            if type(value) is float:
                require(struct.pack("<d", value) == struct.pack("<d", expected),
                        f"canonical/native trade {field} bits differ")
            else:
                require(value == expected, f"canonical/native trade {field} differs")
    print(
        f"PASS CANONICAL: fixed Entry Level=101, trades={fixed['total_trades']}, "
        f"identical C-ABI trade fields and metrics={','.join(metrics)}",
        flush=True,
    )

    def run(label, sampler, policy, batch, trials=200, warm=None, extra=(), expected=0):
        argv = base + [
            "--sampler",
            sampler,
            "--candidate-policy",
            policy,
            "--batch-size",
            str(batch),
            "--max-trials",
            str(trials),
        ]
        if sampler == "tpe":
            argv += ["--tpe-history-switch", "32"]
        if warm is not None:
            argv += ["--warm-start", str(warm)]
        trial_file = directory / f"{label}.trials.jsonl"
        progress_file = directory / f"{label}.fd3.jsonl"
        argv += ["--trials-file", str(trial_file), *extra]
        with progress_file.open("wb") as progress:
            process = subprocess.run(
                argv + ["--progress-fd", str(progress.fileno())],
                capture_output=True,
                pass_fds=(progress.fileno(),),
                timeout=180,
            )
        require(
            process.returncode == expected,
            f"{label}: exit {process.returncode}; {process.stderr.decode()}",
        )
        if expected:
            require(
                not progress_file.read_bytes() and not process.stdout,
                "initialization refusal emitted billable output",
            )
            require(
                not trial_file.exists(), "initialization refusal opened a trials file"
            )
            return process
        result = json.loads(process.stdout)
        result_path = directory / f"{label}.json"
        result_path.write_bytes(process.stdout)
        lines = [json.loads(line) for line in progress_file.read_text().splitlines()]
        require(
            len(lines) == trials and lines == result["trials"], "fd3 billing mismatch"
        )
        require(
            progress_file.read_bytes() == trial_file.read_bytes(),
            "trials file mismatch",
        )
        return process.stdout, result, result_path, progress_file

    cases = [
        ("grid", "sampler_default"),
        ("random", "sampler_default"),
        ("tpe", "sampler_default"),
        ("tpe", "without_replacement"),
    ]
    for sampler, policy in cases:
        for batch in (2, 5, 8):
            label = f"{sampler}-{policy}-b{batch}"
            _, parent, parent_path, parent_jsonl = run(
                label + "-parent", sampler, policy, batch
            )
            exact_parent = parent_path if sampler == "tpe" else parent_jsonl
            child_bytes, child, child_path, _ = run(
                label + "-child", sampler, policy, batch, warm=exact_parent
            )
            binary_path = directory / (label + ".bin")
            state = parent.get("tpe_sampler_state")
            binary_path.write_bytes(encode_warm_block(study, parent["trials"],
                                                    sampler_state=state))
            multi_path = directory / (label + "-multi.bin")
            multi_path.write_bytes(b"".join(
                encode_warm_block(study, parent["trials"][begin:begin + 37],
                                  sampler_state=state if begin == 0 else None)
                for begin in range(0, 200, 37)))
            for mode, warm_path in (("binary", binary_path), ("multi", multi_path)):
                _, binary_child, _, _ = run(
                    label + "-" + mode, sampler, policy, batch, warm=warm_path)
                require(child["trials"] == binary_child["trials"],
                        f"real strategy {mode} differs from JSON continuation")
            replay_bytes, _, _, _ = run(
                label + "-replay", sampler, policy, batch, warm=exact_parent
            )
            require(child_bytes == replay_bytes, "continuation is not byte-identical")
            _, from_result, _, _ = run(
                label + "-result", sampler, policy, batch, warm=parent_path
            )
            require(child["trials"] == from_result["trials"], "parent formats differ")
            _, long_run, _, _ = run(label + "-long", sampler, policy, batch, trials=400)
            require(
                child["warm_start"]["source_sha256"]
                == hashlib.sha256(exact_parent.read_bytes()).hexdigest(),
                "source SHA mismatch",
            )
            require(
                child["warm_start"]["trials"] == 200
                and child["trials_completed"] == 200,
                "warm trials counted as new",
            )
            require(
                [trial["trial_id"] for trial in child["trials"]]
                == list(range(200, 400)),
                "continuation ID mismatch",
            )
            if sampler != "random":
                require(
                    parent["trials"] + child["trials"] == long_run["trials"],
                    "200 + 200 differs from uninterrupted 400",
                )
            else:
                tried = {
                    trial["parameters"]["Entry Level"] for trial in parent["trials"]
                }
                require(
                    all(
                        trial["parameters"]["Entry Level"] not in tried
                        for trial in child["trials"]
                    ),
                    "random replayed parent points",
                )
            for warm in (parent_path, parent_jsonl):
                info = subprocess.run(
                    [
                        str(args.native),
                        "space-info",
                        "--spec",
                        str(spec_path),
                        "--warm-start",
                        str(warm),
                    ],
                    capture_output=True,
                    timeout=15,
                )
                require(
                    info.returncode == 0
                    and json.loads(info.stdout) == space_info(study, warm),
                    "native/Python read-only space-info mismatch",
                )
            if sampler == "grid" or policy == "without_replacement":
                require(
                    space_info(study, child_path)["remaining"] == 0,
                    "exhaustive set mismatch",
                )
                refusal = run(
                    label + "-exhausted",
                    sampler,
                    policy,
                    batch,
                    warm=child_path,
                    expected=5,
                    extra=("--strategy", "/missing.so", "--ohlcv", "/missing.csv"),
                )
                require(
                    b"space exhausted" in refusal.stderr, "wrong exhaustion diagnostic"
                )
            for option in (
                ("--direction", "minimize"),
                ("--constraint", "1 == 0"),
                ("--objective", "metrics.all.num_trades"),
            ):
                refusal = run(
                    label + "-mismatch-" + option[0][2:],
                    sampler,
                    policy,
                    batch,
                    warm=parent_path,
                    extra=option,
                    expected=4,
                )
                require(
                    b"warm-start incompatible" in refusal.stderr,
                    "wrong mismatch diagnostic",
                )
            bad = copy.deepcopy(parent)
            bad["trials"][0]["status"] = "unknown"
            bad_path = directory / f"{label}-bad.json"
            bad_path.write_text(json.dumps(bad))
            run(label + "-unknown", sampler, policy, batch, warm=bad_path, expected=4)
            print(
                f"PASS REAL {label}: parent=200 child=200 long=400 fd3=200 "
                f"replay_sha256={hashlib.sha256(child_bytes).hexdigest()} "
                f"equivalence={'ordered 400' if sampler != 'random' else 'parent exclusion'} "
                "space-info/formats/mismatch/exhaustion=PASS",
                flush=True,
            )
    print(
        f"PASS REAL: compiled artifact once, artifact_key={artifact['artifact_key']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
