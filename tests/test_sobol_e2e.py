"""Real compiled-Pine Sobol gate: the shipped native CLI and the Python StudySpec route against the
engine's own harness (docker/run_json.py). Artifacts stay in an ignored output directory. Run by
hand on a spot box, like test_candidate_list_e2e.py:

    python3 tests/test_sobol_e2e.py --native build/bin/pineforge-hpo-native --output <dir> \\
        [--pinned-reference BINARY --pinned-sha256 HEX]

UNEXECUTED until the proof phase. It uses a real compiled strategy with two continuous real
columns, so it needs a build whose Sobol numeric identity is bound (an unbound build refuses the
study before any trial; the test then fails with that refusal, which is the correct result of a
proof build that is not bound). Proves: the Python route builds exactly the native argv; the rows
equal an independent Python implementation of the Gray-code Sobol recurrence, the Joe-Kuo
direction-number recurrence, the SplitMix64 digital shift and the linear-real mapping, bit for bit;
workers 1, 4 and 16 at a fixed batch give identical bytes on a real strategy; a complete parent
continues the shifted parameter stream; statistics and the Sobol block coexist with their own
identities; the trade count and four aggregate metrics of three rows equal the engine harness
run with the same parameter text; and, with a pinned release binary, the OFF bytes of the other
samplers are unchanged. No per-trial timing or cost claim is made here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))

MASK = (1 << 64) - 1
TABLE = ROOT / "third_party/sobol_joe_kuo/new-joe-kuo-6.21201.first1024"
ENTRY = (1.0, 400.0)
EXIT = (98.0, 100.0)
COLUMNS = ["Entry Level", "Exit Level"]  # byte order
METRICS = ("metrics.all.net_profit", "metrics.all.num_trades", "metrics.all.gross_profit",
           "metrics.equity.max_equity_drawdown")
SEED = 20261009
TOTAL = 24


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def directions(column):
    rows = {}
    for line in TABLE.read_text().splitlines()[1:]:
        parts = line.split()
        rows[int(parts[0])] = (int(parts[1]), int(parts[2]),
                               [int(x) for x in parts[3:3 + int(parts[1])]])
    dimension = column + 1
    if dimension == 1:
        m = [1] * 64
    else:
        degree, polynomial, initial = rows[dimension]
        m = list(initial)
        for k in range(degree + 1, 65):
            value = (m[k - degree - 1] << degree) ^ m[k - degree - 1]
            for j in range(1, degree):
                if (polynomial >> (degree - 1 - j)) & 1:
                    value ^= m[k - j - 1] << j
            m.append(value)
    return [m[k - 1] << (64 - k) for k in range(1, 65)]


def candidate(index, seed, scramble):
    values = []
    for column, (low, high) in enumerate((ENTRY, EXIT)):
        gray, word = index ^ (index >> 1), 0
        for k, direction in enumerate(directions(column)):
            if (gray >> k) & 1:
                word ^= direction
        if scramble == "digital_shift":
            z = (seed + (column + 1) * 0x9E3779B97F4A7C15) & MASK
            z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
            z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
            word ^= z ^ (z >> 31)
        unit = (word >> 11) * (2.0 ** -53)
        values.append(min(max(low + (high - low) * unit, low), high))
    return dict(zip(COLUMNS, values))


def run(command, **kwargs):
    return subprocess.run(command, capture_output=True,
                          timeout=kwargs.pop("timeout", 3600), **kwargs)


def stripped(document):
    document = json.loads(json.dumps(document))
    for key in ("sobol", "return_stats"):
        document.pop(key, None)
    for trial in document["trials"]:
        for name in [n for n in trial["metrics"] if n.startswith("returns.")]:
            trial["metrics"].pop(name)
    return document


def main():
    from pineforge_hpo.cli import prepare_run

    parser = argparse.ArgumentParser()
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pinned-reference", type=Path)
    parser.add_argument("--pinned-sha256")
    args = parser.parse_args()
    native = args.native.resolve()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)

    csv = directory / "bars.csv"
    lines = ["timestamp,open,high,low,close,volume"]
    for index in range(512):
        price = 100 + 5 * math.sin(index * 0.15) + 1.5 * math.sin(index * 0.0113)
        lines.append(f"{1700000000000 + index * 60000},{price:.6f},{price + 1:.6f},"
                     f"{price - 1:.6f},{price + 0.1:.6f},100")
    csv.write_text("\n".join(lines) + "\n")
    source = directory / "threshold.pine"
    source.write_text((ROOT / "examples/single_strategy/threshold.pine").read_text()
                      .replace("minval=101, maxval=103", "minval=1, maxval=400"))

    spec = json.loads((ROOT / "examples/single_strategy/study.json").read_text())
    spec["strategies"][0].update(
        source=str(source), fixed_inputs={},
        search_space={"Entry Level": {"kind": "real", "low": ENTRY[0], "high": ENTRY[1]},
                      "Exit Level": {"kind": "real", "low": EXIT[0], "high": EXIT[1]}})
    spec["datasets"][0]["ohlcv"] = str(csv)
    spec["objective"].update(expression="metrics.all.net_profit", constraints=[])
    spec["sampler"] = {"kind": "sobol", "seed": SEED, "trials": TOTAL,
                       "config": {"scramble": "digital_shift"}}
    spec["execution"].update(workers=4, batch_size=8, batch_lag=0, pruner="none")
    spec_path = directory / "study.json"
    spec_path.write_text(json.dumps(spec))
    command, artifact = prepare_run(spec_path, ROOT / "external/pineforge-engine",
                                    directory / "cache", native=native, compiler="g++",
                                    eigen_include="/usr/include/eigen3")
    plugin = artifact["plugin"]
    require(command[command.index("--sampler") + 1] == "sobol", "the route lost the sampler")
    require(command[command.index("--sobol-scramble") + 1] == "digital_shift", "scramble")
    require(command[command.index("--seed") + 1] == str(SEED), "the route changed the seed")

    # 1. The Python route runs; rows equal the independent oracle bit for bit.
    routed = run(command)
    require(routed.returncode == 0, f"routed run: {routed.stderr.decode()}")
    (directory / "routed.json").write_bytes(routed.stdout)
    result = json.loads(routed.stdout)
    require([t["trial_id"] for t in result["trials"]] == list(range(TOTAL)), "ids are indices")
    for trial in result["trials"]:
        expected = candidate(trial["trial_id"], SEED, "digital_shift")
        require(trial["parameters"] == expected, f"row {trial['trial_id']} differs from the oracle")
    block = result["sobol"]
    require(block["columns"] == COLUMNS and block["exact_stream"] is True, "block")
    require(block["first_index"] == "0" and block["next_index"] == str(TOTAL), "indices")
    require(block["numeric_build_identity"].startswith("portable-sobol-v1"), "floating identity")
    print(f"PASS ORACLE: {TOTAL} rows equal the independent Sobol implementation bit for bit",
          flush=True)

    # 2. The shipped CLI directly, across workers, at a fixed batch: identical bytes.
    stem = [str(native), "run", "--strategy", plugin, "--ohlcv", str(csv), "--objective",
            "metrics.all.net_profit", "--input-tf", "1", "--script-tf", "1",
            "--real-dim", "Entry Level", "1", "400", "continuous",
            "--real-dim", "Exit Level", "98", "100", "continuous", "--sampler", "sobol",
            "--seed", str(SEED), "--sobol-scramble", "digital_shift"]
    base = stem + ["--max-trials", str(TOTAL), "--batch-size", "8"]
    reference = None
    for workers in (1, 4, 16, 4):
        process = run(base + ["--workers", str(workers)])
        require(process.returncode == 0, process.stderr.decode())
        reference = reference or process.stdout
        require(process.stdout == reference, f"bytes changed at workers={workers}")
    direct = json.loads(reference)
    require([t["parameters"] for t in direct["trials"]]
            == [t["parameters"] for t in result["trials"]],
            "the direct CLI and the Python route disagree")
    print("PASS WORKERS: bytes identical at workers 1/4/16 and a repeat on a real strategy",
          flush=True)

    # 3. A complete parent continues the shifted parameter stream.
    first_path = directory / "part1.json"
    part1 = run(stem + ["--max-trials", "9", "--batch-size", "4", "--output", str(first_path)])
    require(part1.returncode == 0, part1.stderr.decode())
    part2 = run(stem + ["--max-trials", str(TOTAL - 9), "--batch-size", "4",
                        "--warm-start", str(first_path)])
    require(part2.returncode == 0, part2.stderr.decode())
    child = json.loads(part2.stdout)
    rows = json.loads(first_path.read_text())["trials"] + child["trials"]
    require([t["parameters"] for t in rows] == [t["parameters"] for t in result["trials"]],
            "the continuation changed the parameter stream")
    require(child["sobol"]["exact_stream"] is True and child["sobol"]["first_index"] == "9",
            "continuation block")
    require(child["sobol"]["identity"] == json.loads(first_path.read_text())["sobol"]["identity"],
            "continuation identity")
    print("PASS CONTINUATION: part 1 + part 2 equal the uninterrupted parameter stream", flush=True)

    # 4. C/X interop: statistics add their own object and identity and change no parameter.
    recorded = [item for name in ("returns.bar.count", "returns.monthly.status")
                for item in ("--record-metric", name)]
    with_stats = run(base + ["--workers", "4"] + recorded)
    require(with_stats.returncode == 0, with_stats.stderr.decode())
    stats_result = json.loads(with_stats.stdout)
    require(stats_result["sobol"] == direct["sobol"], "statistics changed the Sobol block")
    require(stats_result["return_stats"]["numeric_build_identity"]
            != stats_result["sobol"]["numeric_build_identity"], "identities must be distinct")
    require([t["parameters"] for t in stats_result["trials"]]
            == [t["parameters"] for t in direct["trials"]], "statistics changed the stream")
    require(stripped(stats_result)["trials"] == stripped(direct)["trials"]
            or all(t["objective"] == d["objective"]
                   for t, d in zip(stats_result["trials"], direct["trials"])),
            "statistics changed an objective")
    print("PASS INTEROP: return_stats and the Sobol block coexist with distinct identities",
          flush=True)

    # 5. Three rows against the engine harness, value for value, with the same parameter text.
    metric_flags = [item for metric in METRICS for item in ("--record-metric", metric)]
    recorded_run = json.loads(run(stem + ["--max-trials", "6", "--batch-size", "2"]
                                  + metric_flags).stdout)
    for trial in recorded_run["trials"][::2]:
        inputs = {name: "%.17g" % trial["parameters"][name] for name in COLUMNS}
        harness = run([sys.executable, str(ROOT / "external/pineforge-engine/docker/run_json.py"),
                       "--so", plugin, "--ohlcv", str(csv), "--inputs", json.dumps(inputs),
                       "--input-tf", "1", "--script-tf", "1"], timeout=120)
        require(harness.returncode == 0, harness.stderr.decode())
        canonical = json.loads(harness.stdout)
        require(trial["total_trades"] == len(canonical["trades"]), "trade count differs")
        for metric in METRICS:
            value = canonical
            for segment in metric.split("."):
                value = value[segment]
            require(trial["metrics"][metric] == value, f"{metric} differs from the harness")
    print("PASS CANONICAL: trade counts and four aggregate metrics equal the harness", flush=True)

    # 6. Pinned release bytes: the other samplers are unchanged, and the release refuses sobol.
    if args.pinned_reference is not None:
        require(args.pinned_sha256, "--pinned-sha256 is mandatory with --pinned-reference")
        digest = hashlib.sha256(args.pinned_reference.read_bytes()).hexdigest()
        require(digest == args.pinned_sha256.lower(), f"pinned binary digest {digest} differs")
        for sampler, extra in (("grid", ["--max-trials", "8"]),
                               ("random", ["--max-trials", "8", "--seed", "4"]),
                               ("tpe", ["--max-trials", "8", "--seed", "4", "--batch-size", "4"])):
            # Grid needs stepped reals, so the comparison uses stepped dimensions.
            common = [str(native), "run", "--strategy", plugin, "--ohlcv", str(csv), "--objective",
                      "metrics.all.net_profit", "--input-tf", "1", "--script-tf", "1",
                      "--real-dim", "Entry Level", "1", "400", "50",
                      "--real-dim", "Exit Level", "98", "100", "1", "--sampler", sampler, *extra,
                      "--workers", "2"]
            new = run(common)
            old = run([str(args.pinned_reference), *common[1:]])
            require((old.returncode, old.stdout) == (new.returncode, new.stdout),
                    f"OFF bytes of {sampler} differ from the pinned release")
        refused = run([str(args.pinned_reference), *base[1:]])
        require(refused.returncode == 1, "the release binary must refuse the sobol sampler")
        print("PASS PINNED: OFF bytes equal the pinned release for grid, random and tpe", flush=True)


if __name__ == "__main__":
    main()
