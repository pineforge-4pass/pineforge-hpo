"""Real compiled-Pine candidate-list gate: the shipped native CLI and the Python StudySpec route
against the engine's own harness (docker/run_json.py). Artifacts stay in an ignored output
directory. Run by hand on a spot box, like test_warm_start_e2e.py:

    python3 tests/test_candidate_list_e2e.py --native build/pineforge-hpo-native --output <dir>

UNEXECUTED until the spot phase. HPO captures no trades, so the proof is the trade count and the
trade-derived aggregate metrics of every row against the harness, value for value; no per-trade
data is fabricated here (the per-trade bit comparison of the C ABI against the harness is the
existing test_warm_start_e2e.py gate).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))

METRICS = (
    "metrics.all.net_profit",
    "metrics.all.num_trades",
    "metrics.all.gross_profit",
    "metrics.equity.max_equity_drawdown",
)
FORMAT = "pineforge_candidates_v1"
LEVELS = (50, 101, 250, 101)  # a duplicate on purpose: one row per occurrence


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def list_sha256(levels) -> str:
    """The documented list digest, rebuilt independently of the product."""
    material = FORMAT + "\n"
    for level in levels:
        bits = struct.pack(">d", float(level)).hex()
        material += json.dumps({"Entry Level": ["real", bits]}, separators=(",", ":")) + "\n"
    return hashlib.sha256(material.encode()).hexdigest()


def main():
    from pineforge_hpo.cli import prepare_run

    parser = argparse.ArgumentParser()
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    native = args.native.resolve()

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
    candidates = directory / "candidates.jsonl"
    candidates.write_text("".join(json.dumps({"Entry Level": level}) + "\n" for level in LEVELS))

    # The Python route: StudySpec kind "candidates" with a path relative to the study file.
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
    spec["sampler"] = {
        "kind": "candidates",
        "seed": 73,
        "trials": len(LEVELS),
        "config": {"candidates_file": candidates.name},
    }
    spec["execution"].update(workers=4, batch_size=2, batch_lag=0, pruner="none")
    spec_path = directory / "study.json"
    spec_path.write_text(json.dumps(spec))
    command, artifact = prepare_run(
        spec_path,
        ROOT / "external/pineforge-engine",
        directory / "cache",
        native=native,
        compiler="g++",
        eigen_include="/usr/include/eigen3",
    )
    (directory / "artifact.json").write_text(json.dumps(artifact, indent=2))
    plugin = artifact["plugin"]
    require(command[command.index("--sampler") + 1] == "candidates", "route lost the sampler")
    require(
        Path(command[command.index("--candidates") + 1]) == candidates.resolve(),
        "route did not resolve the list against the study file",
    )
    require(command[command.index("--max-trials") + 1] == str(len(LEVELS)), "route lost trials")

    recorded = [argument for metric in METRICS for argument in ("--record-metric", metric)]

    # The shipped CLI directly (the argv shape of the existing canonical gate), across workers
    # and a fixed batch.
    base = [
        str(native), "run", "--strategy", plugin, "--ohlcv", str(csv),
        "--objective", "metrics.all.net_profit", "--input-tf", "1", "--script-tf", "1",
        "--real-dim", "Entry Level", "1", "400", "1", "--fixed-input", "Exit Level", "98",
        "--sampler", "candidates", "--candidates", str(candidates), "--batch-size", "2",
    ]
    reference = None
    for workers in (1, 2, 4, 8):
        process = subprocess.run(base + ["--workers", str(workers)] + recorded,
                                 capture_output=True, timeout=300)
        require(process.returncode == 0, process.stderr.decode())
        if reference is None:
            reference = process.stdout
            (directory / "native-direct.json").write_bytes(process.stdout)
        require(json.dumps(json.loads(process.stdout)["trials"], sort_keys=True)
                == json.dumps(json.loads(reference)["trials"], sort_keys=True),
                f"rows changed at workers={workers}")
    result = json.loads(reference)

    # Ordered, duplicate-preserving rows with positional ids.
    rows = result["trials"]
    require([row["trial_id"] for row in rows] == list(range(len(LEVELS))), "ids are not positions")
    require(
        [row["parameters"]["Entry Level"] for row in rows] == [float(v) for v in LEVELS],
        "rows are not in list order",
    )
    require(result["stop_reason"] == "trial_budget_reached", "wrong natural stop reason")
    block = result["candidate_list"]
    require(block["format"] == FORMAT and block["count"] == len(LEVELS), "wrong block identity")
    require(block["source_sha256"] == hashlib.sha256(candidates.read_bytes()).hexdigest(),
            "source digest is not the file's")
    require(block["list_sha256"] == list_sha256(LEVELS), "list digest differs from the recipe")
    require(block["evaluated"] == len(LEVELS) and block["complete"]
            and block["unevaluated_ranges"] == [], "coverage block is wrong")
    require(rows[1]["metrics"] == rows[3]["metrics"]
            and rows[1]["objective"] == rows[3]["objective"]
            and rows[1]["total_trades"] == rows[3]["total_trades"], "duplicate rows differ")

    # Every distinct vector against the engine harness, value for value.
    for level in sorted(set(LEVELS)):
        canonical_process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "external/pineforge-engine/docker/run_json.py"),
                "--so", plugin,
                "--ohlcv", str(csv),
                "--inputs", json.dumps({"Entry Level": str(level), "Exit Level": "98"}),
                "--input-tf", "1",
                "--script-tf", "1",
            ],
            capture_output=True,
            timeout=60,
        )
        require(canonical_process.returncode == 0,
                f"canonical harness: {canonical_process.stderr.decode()}")
        canonical = json.loads(canonical_process.stdout)
        (directory / f"canonical-{level}.json").write_bytes(canonical_process.stdout)
        for row in (row for row in rows if row["parameters"]["Entry Level"] == float(level)):
            require(row["total_trades"] == len(canonical["trades"]),
                    f"Entry Level {level}: trade count differs from the harness")
            for metric in METRICS:
                value = canonical
                for segment in metric.split("."):
                    value = value[segment]
                require(row["metrics"][metric] == value,
                        f"Entry Level {level}: {metric} differs from the harness")
        print(f"PASS CANONICAL: Entry Level={level}, trades={len(canonical['trades'])}, "
              f"identical aggregate metrics={','.join(METRICS)}", flush=True)

    # The Python route runs the same list: identity, order and coverage must agree. Its argv
    # states the study's dataset timezone explicitly, so only that is compared, not the bytes.
    routed = subprocess.run(command + recorded, capture_output=True, timeout=300)
    require(routed.returncode == 0, f"routed run: {routed.stderr.decode()}")
    (directory / "native-routed.json").write_bytes(routed.stdout)
    routed_result = json.loads(routed.stdout)
    require(routed_result["candidate_list"] == block, "the Python route changed the block")
    require([row["parameters"] for row in routed_result["trials"]]
            == [row["parameters"] for row in rows], "the Python route changed the rows' order")
    print(f"PASS CANDIDATE LIST: rows={len(LEVELS)} identical across workers 1/2/4/8; the "
          "Python route reports the same list identity and coverage", flush=True)


if __name__ == "__main__":
    main()
