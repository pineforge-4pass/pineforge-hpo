#!/usr/bin/env python3
"""Real-rows end to end for ``pineforge_hpo.pareto`` on ``examples/single_strategy/study.json``.

The Pareto pin names that study for the real proof. It is a grid over two stepped real dimensions
(``Entry Level`` 101..103 step 1, ``Exit Level`` 98..100 step 1: nine vectors) whose objective is
``metrics.all.net_profit - 0.5 * metrics.equity.max_equity_drawdown``, so ``metrics.all.net_profit``
and ``metrics.equity.max_equity_drawdown`` are two recordable metrics. The native runner records a
metric in each trial's ``metrics`` only when asked with ``--record-metric``, and the Python ``run``
wrapper does not forward that flag, so this script builds the native command line with
``pineforge_hpo.prepare_run`` and appends the flags itself (``--run``), or reads a result file that
was made that way (``--result``).

The axes and their directions are required arguments with no default (pin P1): the module must not
infer a direction and neither does this script.

What it does with the real result
1. gates the input: the result's ``space_hash`` must be the golden digest of this study (documented
   in docs/study-spec.md), all nine rows must be there, and every row must carry the backtest bar
   counters the native runner writes. ``--other-study`` skips the digest and row-count gates and
   labels the output as not being the pin's input;
2. runs ``pineforge_hpo.pareto.pareto_front`` and prints the front, the exclusions, a table of every
   row with the number of rows that dominate it, and the ``interpretation`` words;
3. recomputes the front, counts and exclusions from the same rows with the independent oracle of
   ``pareto_oracle.py`` (signs, componentwise rule, sort-and-filter) and compares them.

Usage (on the box, after the native runner is built)::

    python3 -B tests/g2_oracles/pareto_real_rows.py --run --engine-root external/pineforge-engine \\
        --native build/bin/pineforge-hpo-native --out /scratch/pareto/result.json \\
        --cache-dir /scratch/pareto/cache \\
        --axis metrics.all.net_profit=maximize --axis metrics.equity.max_equity_drawdown=minimize
    python3 tests/g2_oracles/pareto_real_rows.py --result /path/to/result.json --axis ... --axis ...

Exit codes: 0 module and oracle agree and every gate held; 1 they disagree; 2 the module refused
(printed as ``MODULE REFUSED``; a usage error also exits 2); 3 they agree on a study the pin
does not name (``--other-study`` skipped the digest and row-count gates: not the pin's proof,
never exit 0); 5 a gate failed (not this study, incomplete, or not native output); 7 the native
run failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

# Write nothing here beyond what --run is told to write: no bytecode cache for the oracle module
# imported below or for the package.
sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pareto_oracle  # noqa: E402  (this directory; imports nothing of pineforge_hpo)

STUDY = pareto_oracle.ROOT / "examples" / "single_strategy" / "study.json"
# docs/study-spec.md:1066-1067, "The golden digest for examples/single_strategy/study.json".
GOLDEN_SPACE_HASH = "1188c07588652f2b365a1fc233815d5c51d350e92106051309d9e84ad586071a"
EXPECTED_ROWS = 9  # 3 x 3 grid vectors
# Exit code of an agreement on a study the pin does not name (--other-study); 0 is kept for the
# pin's input.
OTHER_STUDY_EXIT = 3
INTERPRETATION_MARKERS = tuple(f"({letter}) " for letter in "abcdefg")


def parse_axis(text: str) -> dict:
    name, separator, direction = text.rpartition("=")
    if not separator or not name or direction not in ("maximize", "minimize"):
        raise argparse.ArgumentTypeError(f"{text!r}: expected NAME=maximize or NAME=minimize")
    return {"name": name, "direction": direction}


def run_native(args, axes: list) -> dict:
    """Run the example study natively with every axis recorded; return the parsed result."""
    sys.path.insert(0, str(pareto_oracle.ROOT / "python"))
    sys.path.insert(0, str(pareto_oracle.ROOT / "external" / "pineforge-codegen-oss"))
    from pineforge_hpo import prepare_run

    command, _artifact = prepare_run(
        STUDY, args.engine_root, args.cache_dir, native=args.native
    )
    command = [str(part) for part in command]
    for axis in axes:
        command += ["--record-metric", axis["name"]]
    print("native command:", " ".join(command))
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    print(f"native exit code {completed.returncode}")
    if completed.returncode not in (0, 2):
        print(completed.stderr.strip() or completed.stdout.strip() or "no diagnostics")
        raise SystemExit(7)
    result = json.loads(completed.stdout)
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def gate(result: dict, other_study: bool) -> list:
    """The reasons the result cannot be the pin's real-rows input (empty when it can)."""
    problems = []
    trials = result.get("trials")
    if not isinstance(trials, list):
        return ["the result has no trials list"]
    if result.get("trials_out", "all") != "all":
        problems.append("the result keeps only some of its trials (trials_out is not all)")
    if not other_study:
        if result.get("space_hash") != GOLDEN_SPACE_HASH:
            problems.append(
                f"space_hash {result.get('space_hash')!r} is not the golden digest of the "
                "example study"
            )
        if len(trials) != EXPECTED_ROWS:
            problems.append(f"{len(trials)} rows, the 3 x 3 grid has {EXPECTED_ROWS}")
    for row in trials:
        backtest = row.get("backtest") if isinstance(row, dict) else None
        bars = backtest.get("input_bars_processed") if isinstance(backtest, dict) else None
        if type(bars) is not int or bars < 1:
            problems.append(
                f"trial {row.get('trial_id') if isinstance(row, dict) else None} "
                "has no bar counter"
            )
    return problems


def print_rows(document: dict, rows: list, axes: list) -> None:
    names = [axis["name"] for axis in axes]
    by_id = {row["trial_id"]: row for row in rows}
    front = set(document["pareto_front"])
    print(
        "rows that competed (id, on front, number of rows dominating it, axis values, parameters):"
    )
    for trial_id, count in document["domination_counts"].items():
        row = by_id[trial_id]
        values = [row["metrics"][name] for name in names]
        mark = "FRONT" if trial_id in front else "-"
        print(f"  {trial_id:>4} {mark:5} {count:>3}  {values}  {row.get('parameters')}")
    print("rows excluded (reason: ids):")
    for reason, entry in document["excluded"].items():
        print(
            f"  {reason}: {entry['ids']}" + (f" (axes {entry['axes']})" if "axes" in entry else "")
        )
    if not document["excluded"]:
        print("  none")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Real-rows end to end for pineforge_hpo.pareto")
    parser.add_argument("--result", help="result JSON of a native run of the example study")
    parser.add_argument(
        "--run", action="store_true", help="run the native runner on the example study"
    )
    parser.add_argument("--engine-root", help="pineforge-engine tree (with --run)")
    parser.add_argument("--native", help="pineforge-hpo-native executable (with --run)")
    parser.add_argument(
        "--cache-dir",
        help="artifact cache directory, required with --run: the native artifacts and their locks "
        "go there and not under the user's cache directory; give a scratch directory",
    )
    parser.add_argument(
        "--out", help="where --run writes the result JSON; give a path in a scratch directory"
    )
    parser.add_argument(
        "--axis",
        action="append",
        required=True,
        type=parse_axis,
        metavar="NAME=maximize|minimize",
        help="a recorded metric and its direction; give at least two; there is no default",
    )
    parser.add_argument(
        "--other-study",
        action="store_true",
        help="exploratory: skip the golden-digest and nine-row gates; the output says it is "
        "not the pin's input and an agreement exits 3, never 0",
    )
    args = parser.parse_args(argv)
    pareto_oracle.harness_self_test()
    if bool(args.result) == bool(args.run):
        parser.error("give exactly one of --result and --run")
    if args.run and not (args.engine_root and args.native and args.out and args.cache_dir):
        parser.error(
            "--run needs --engine-root, --native, --out and --cache-dir (the last two in a "
            "scratch directory)"
        )
    axes = args.axis
    if args.run:
        result = run_native(args, axes)
        source = Path(args.out)
    else:
        source = Path(args.result)
        result = json.loads(source.read_text(encoding="utf-8"))
    data = source.read_bytes()
    print(f"result file: {source} ({len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()})")
    problems = gate(result, args.other_study)
    if args.other_study:
        print(
            "NOTE: --other-study: this is NOT the input the Pareto pin names; "
            "nothing here is the pin's proof"
        )
    if problems:
        print("GATE FAILED, this is not the pin's real-rows input:")
        for problem in problems[:20]:
            print(f"  - {problem}")
        return 5
    print(f"gates held: {len(result['trials'])} native rows, space_hash {result.get('space_hash')}")

    sys.path.insert(0, str(pareto_oracle.ROOT / "python"))
    sys.path.insert(0, str(pareto_oracle.ROOT / "external" / "pineforge-codegen-oss"))
    from pineforge_hpo import pareto

    try:
        document = pareto.pareto_front(result, axes, with_domination_counts=True)
    except pareto.ParetoError as error:
        print(
            f"MODULE REFUSED: {error.code}: {error.detail}; ids {list(error.ids)} "
            f"names {list(error.names)}"
        )
        return 2
    print(f"front (ascending trial id): {document['pareto_front']}")
    print(
        f"n_front {document['n_front']}, distinct metric vectors on the front "
        f"{document['n_distinct_vectors_on_front']}, rows read {document['rows_read']}, "
        f"eligible {document['rows_eligible']}, excluded {document['rows_excluded']}"
    )
    print_rows(document, list(result.get("warm_start_trials", [])) + list(result["trials"]), axes)
    print("interpretation (must state the pin's clause 9, items (a) to (g)):")
    print(f"  {document['interpretation']}")

    ledger = pareto_oracle.Ledger(strict=True)
    ledger.begin("properties the pin states for the document")
    text = document["interpretation"]
    positions = [text.find(marker) for marker in INTERPRETATION_MARKERS]
    ledger.compare(
        "pin",
        "the interpretation holds the items (a) to (g) in order",
        (min(positions) >= 0, positions == sorted(positions)),
        (True, True),
        "pin section 3, open item 4",
    )
    ledger.compare(
        "pin", "contract id", document["contract"], "pineforge-hpo-pareto-front/v1", "pin P10"
    )
    ledger.compare(
        "pin",
        "the front is ordered by ascending trial id",
        document["pareto_front"],
        sorted(document["pareto_front"]),
        "pin P7",
    )
    ledger.end(3)

    rows = list(result.get("warm_start_trials", [])) + list(result["trials"])
    oracle = pareto_oracle.oracle_front(rows, axes)
    ledger.begin("module against the independent oracle on the same rows")
    if oracle["status"] != "ok":
        print(f"DISAGREE: the oracle refuses ({oracle['code']}) but the module returned a front")
        return 1
    expected = pareto_oracle.view_of(oracle)
    found = pareto_oracle.view_of(document)
    for key in ("front", "n_front", "distinct", "counts", "rows", "excluded"):
        ledger.compare("module", key, expected[key], found[key])
    ledger.compare(
        "internal",
        "the oracle's three dominance formulations agree",
        oracle["formulations_agree"],
        True,
    )
    ledger.end(7)
    if ledger.failures:
        print("RESULT: DISAGREE or a pin property failed (see above)")
        return 1
    if args.other_study:
        print(
            f"RESULT: AGREE ON ANOTHER STUDY (exit {OTHER_STUDY_EXIT}): the module and the "
            "independent oracle give the same front, counts and exclusions on these rows, but the "
            "rows are NOT the study the Pareto pin names (the golden-digest and nine-row gates "
            "were skipped), so this is not the pin's real-rows proof"
        )
        return OTHER_STUDY_EXIT
    print(
        "RESULT: AGREE (module and independent oracle give the same front, counts and exclusions "
        "on these real rows)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
