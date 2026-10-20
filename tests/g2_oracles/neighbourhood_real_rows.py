#!/usr/bin/env python3
"""Real-rows end to end for ``pineforge_hpo.neighbourhood`` (and ``sensitivity`` with ``--pair-k``).

The neighbourhood pin names no input for this proof. Its drafted proof plan speaks of "a small real
study (``grid``, a few parameters, ``--trials-file``)", and the only study this repository ships is
``examples/single_strategy/study.json`` (a nine-vector grid over two stepped real dimensions), but
no pin names it for this module. So this script takes the finished native result files as arguments,
makes no rows up, and leaves the choice of study to whoever runs it. Any result file written by the
native runner works, for example the ``--out`` file of ``pareto_real_rows.py --run``.

With real rows it
1. refuses input that does not look like native output: every result needs its recorded ``space``,
   ``trials_out`` of ``all`` and, in every row, a ``backtest`` object counting bars (necessary, not
   sufficient: hand-made rows lack it);
2. picks the centres (``--top-k K`` best distinct vectors, chosen by the oracle's own
   ``top_ids``, or explicit ``--centre SOURCE:ID`` pairs), runs
   ``pineforge_hpo.neighbourhood.neighbourhood`` and prints the reference, each centre's
   per-dimension profile (class, objective, gap, near flag, row ids), counts, share near the
   best, coverage, the neighbour vectors that were never evaluated, the pair maps and the work
   figure;
3. recomputes all of it from the same files with the oracle of ``neighbourhood_oracle.py``
   (enumerated domains, exact arithmetic, the rows partitioned by typed-tuple distance from the
   centre and classified from the contract's table, pair ranking by repeated selection) and
   compares it with the module's document field by field. It also compares the module's ``top_ids``
   with the oracle's.

The band, fraction, ratio and ``pair_k`` have no default (pin N4, N5, N10, N12): give them.

Usage::

    python3 tests/g2_oracles/neighbourhood_real_rows.py --result /path/to/result.json \\
        --band absolute:5.0 --top-k 3 --pair-k 1
    # add --result again for a second result (its source index is its position on the line);
    # add --fraction F for a continuous real dimension and --ratio R for a log dimension.

What the oracle cannot judge is set aside, not guessed. A continuous or log real value whose moves
(the value plus or minus the fraction of its range, or times or divided by the ratio) are not
exactly binary64 numbers makes the oracle decline that dimension of that centre. That makes only
the comparisons it touches inconclusive: that dimension's profile; that centre's counts, dropped,
share_near_best, coverage and, with --pair-k, pair maps (the pairs are ranked by every dimension's
count); and the document's missing_vectors, lookups and work. Every other figure, the other
dimensions and the other centres included, is still compared. The count and the ids of the set-aside
(centre, dimension) pairs are always printed. If the oracle declines the whole study (a stepped real
whose values it cannot enumerate), every comparison with the oracle is set aside, the module chooses
the centres of --top-k, and only the pin properties are compared.

Exit codes, in the order of the verdict: 1 a disagreement outside the set-aside, whatever the
set-aside count (also a failed pin property, and rows that the oracle rejects although the module
accepted them, for example a value that is not legal in the space, or scored rows of one vector that
differ); 6 nothing disagrees, but at least one comparison was set aside: inconclusive, never an
agreement; 0 nothing disagrees and nothing was set aside; 2 the module refused (printed as
``MODULE REFUSED``; a usage error also exits 2); 5 the input does not look like native output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

# Write nothing: no bytecode cache for the oracle module imported below or for the package.
sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))

# This directory's oracle imports nothing of pineforge_hpo.
import neighbourhood_oracle as oracle  # noqa: E402


def parse_band(text: str) -> dict:
    mode, separator, amount = text.partition(":")
    if not separator or mode not in ("absolute", "relative"):
        raise argparse.ArgumentTypeError(f"{text!r}: expected absolute:VALUE or relative:VALUE")
    try:
        value = float(amount)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r}: the band value is not a number") from None
    return {"mode": mode, "value": value}


def parse_centre(text: str) -> tuple:
    source, separator, trial = text.partition(":")
    if not separator or not source.isdigit() or not trial.isdigit():
        raise argparse.ArgumentTypeError(f"{text!r}: expected SOURCE:ID, two non-negative integers")
    return (int(source), int(trial))


def load_result(path: Path) -> dict:
    def reject(token: str):
        raise ValueError(f"non-finite JSON number {token} in {path}")

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)


def gate(documents: list) -> list:
    """The reasons the files cannot be native results (empty when they can)."""
    problems = []
    for index, document in enumerate(documents):
        if not isinstance(document, dict) or not isinstance(document.get("trials"), list):
            problems.append(f"source {index}: not a result object with a trials list")
            continue
        if not isinstance(document.get("space"), dict):
            problems.append(f"source {index}: no recorded space")
        if document.get("trials_out", "all") != "all":
            problems.append(f"source {index}: trials_out is not all")
        for row in document["trials"]:
            backtest = row.get("backtest") if isinstance(row, dict) else None
            bars = backtest.get("input_bars_processed") if isinstance(backtest, dict) else None
            if type(bars) is not int or bars < 1:
                problems.append(
                    f"source {index}: trial "
                    f"{row.get('trial_id') if isinstance(row, dict) else None} has no bar counter"
                )
    return problems


def oracle_sources(documents: list) -> list:
    """What the oracle reads: the recorded space and every row, ancestors first as the module
    does."""
    return [
        {
            "space": document["space"],
            "trials": list(document.get("warm_start_trials", [])) + list(document["trials"]),
        }
        for document in documents
    ]


def print_result(result: dict) -> None:
    reference = result["reference"]
    print(
        f"direction {result['direction']}; reference objective {reference['objective']} "
        f"at source {reference['source']} trial {reference['trial_id']}"
    )
    print(f"band {result['band']}")
    print(f"rules {result['rules']}")
    print(
        f"duplicate rows {result['duplicate_rows']}; "
        f"constant dimensions {result['constant_dimensions']}"
    )
    for centre in result["centres"]:
        print(
            f"centre {centre['source']}:{centre['trial_id']} objective {centre['objective']} "
            f"gap {centre['centre_gap']} near {centre['centre_near']}"
        )
        print(f"  counts {centre['counts']}; dropped {centre['dropped']}")
        print(f"  share near the best {centre['share_near_best']}; coverage {centre['coverage']}")
        for name, cells in centre["profile"].items():
            for item in cells:
                print(
                    f"    {name} = {item['value']!r}: {item['class']} "
                    f"objective {item['objective']} "
                    f"gap {item['gap']} near {item['near']} rows {item['rows']}"
                )
        for pair in centre["pair_maps"]:
            print(
                f"  pair map {pair['dimensions']} score {pair['score']}: "
                f"{len(pair['cells'])} cells, "
                f"diagonals total {pair['diagonal_total']} evaluated {pair['diagonal_evaluated']} "
                f"scored {pair['diagonal_scored']} near {pair['diagonal_near']}"
            )
    print(f"never-evaluated neighbour vectors: {len(result['missing_vectors'])}")
    for vector in result["missing_vectors"][:20]:
        print(f"    {vector}")
    print(f"admission {result['admission']}")
    print(f"interpretation: {result['interpretation']}")


def print_refusal(error) -> None:
    print(
        f"MODULE REFUSED: {error.code}: {error.detail}; rows {list(error.ids)[:20]} "
        f"names {list(error.names)}"
    )


class RealLedger(oracle.Ledger):
    """The oracle's strict ledger with a third verdict, the set-aside: a comparison the oracle
    declined to make. It is counted as made, and it is neither a failure nor an agreement."""

    def __init__(self, echo: bool = True) -> None:
        super().__init__(echo=echo, strict=True)
        self.set_aside: list[str] = []

    def undecided(self, kind: str, label: str, detail: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.made += 1
        self.set_aside.append(f"{self.name}: {label}")
        self.say(f"SET ASIDE {kind:8s} {label} | {detail}")


def judge(ledger: RealLedger, label: str, wanted, found, why: str) -> None:
    """Compare the module's figure with the oracle's; if the oracle marked the figure DECLINED, set
    it aside instead."""
    if wanted is oracle.DECLINED:
        ledger.undecided("module", label, why)
    else:
        ledger.compare("module", label, wanted, found)


def compare_document(
    ledger: RealLedger, expected: dict, result: dict, max_work, declined: list
) -> None:
    """Compare the module's document with the oracle's figure by figure. A figure the oracle marked
    DECLINED is set aside, every other one is compared. ``max_work`` is the bound the module was
    given: the oracle is not given it, and the module must report it back."""
    named: dict = {}
    for source, trial, name, _reason in declined:
        named.setdefault((source, trial), []).append(name)
    why = f"the oracle declined {len(declined)} (centre, dimension) pair(s), listed above"
    for key in (
        "direction",
        "reference",
        "band",
        "sources",
        "duplicate_rows",
        "constant_dimensions",
    ):
        ledger.compare("module", key, expected[key], result[key])
    judge(ledger, "missing_vectors", expected["missing_vectors"], result["missing_vectors"], why)
    admission = {**expected["admission"], "max_work": max_work}
    ledger.compare("module", "admission fields", sorted(admission), sorted(result["admission"]))
    for key in ("rows", "lookups", "work", "max_work"):
        judge(ledger, f"admission {key}", admission[key], result["admission"].get(key), why)
    for index, (found, wanted) in enumerate(zip(result["centres"], expected["centres"])):
        here = named.get((wanted["source"], wanted["trial_id"]), [])
        why_here = f"the oracle declined dimension(s) {here} of this centre, listed above"
        for key in ("source", "trial_id", "objective", "centre_gap", "centre_near"):
            ledger.compare("module", f"centre {index} {key}", wanted[key], found[key])
        for key in ("counts", "dropped", "share_near_best", "coverage"):
            judge(ledger, f"centre {index} {key}", wanted[key], found[key], why_here)
        ledger.compare(
            "module",
            f"centre {index} profile dimensions",
            sorted(wanted["profile"]),
            sorted(found["profile"]),
        )
        for name in wanted["profile"]:
            label = f"centre {index} profile {name}"
            judge(ledger, label, wanted["profile"][name], found["profile"].get(name), why_here)
        label = f"centre {index} pair_maps"
        judge(ledger, label, wanted["pair_maps"], found["pair_maps"], why_here)


def document_comparisons(expected: dict) -> int:
    """How many comparisons ``compare_document`` makes or sets aside for an oracle document: 12 for
    the document, and for each centre 11 and one more for each of its dimensions."""
    return 12 + sum(11 + len(centre["profile"]) for centre in expected["centres"])


def print_set_aside(declined: list, whole) -> None:
    """Always printed, before the comparisons: how many (centre, dimension) pairs the oracle
    declines to judge, which ones, and why."""
    if whole is not None:
        print(f"SET ASIDE: everything the oracle would compare; it declines the study ({whole})")
        return
    print(f"SET ASIDE: {len(declined)} (centre, dimension) pair(s) the oracle declines to judge")
    for source, trial, name, reason in declined:
        print(f"  centre {source}:{trial} dimension {name!r}: {reason}")


def verdict(ledger: RealLedger, declined: list, whole) -> tuple:
    """(exit code, RESULT line), in the order the ruling fixes. A pin property that failed, or any
    disagreement outside the set-aside, gives DISAGREE (1) whatever the set-aside count; with none,
    at least one set-aside gives INCONCLUSIVE (6), never an agreement; with neither, AGREE (0)."""
    if whole is not None:
        aside = f"everything it would compare (it declines the whole study: {whole})"
    else:
        pairs = [(source, trial, name) for source, trial, name, _reason in declined]
        aside = (
            f"{len(ledger.set_aside)} comparison(s) for {len(declined)} (centre, dimension) "
            f"pair(s) {pairs}"
        )
    if ledger.failures:
        return 1, (
            "RESULT: DISAGREE or a pin property failed (see above). "
            f"The oracle set aside {aside}."
        )
    if ledger.set_aside:
        return 6, (
            f"RESULT: INCONCLUSIVE. Nothing disagrees, but the oracle set aside {aside} (the SET "
            "ASIDE lines above), so the module is not shown to agree on those. Every other "
            "comparison agreed."
        )
    return 0, (
        "RESULT: AGREE (module and independent oracle give the same document on these real rows). "
        f"The oracle set aside {aside}."
    )


def verdict_self_test() -> None:
    """Exit at once unless the verdicts follow the ruling, tried on the small study of
    ``oracle.set_aside_sources`` (one of its two centres has a dimension the oracle declines). The
    module's document is the oracle's with a value of its own where the oracle declined and, in the
    cases that need one, one figure changed. By hand the study has 38 comparisons (12 for the
    document, 13 for each centre), of which 8 are set aside: missing_vectors, lookups and work, and
    for the centre with the declined dimension its counts, dropped, share_near_best, coverage and
    the profile of that dimension. Mutation: let ``verdict`` test the set-aside before the failures,
    and the cases with a planted disagreement return 6, not 1."""

    def fill(value):
        if value is oracle.DECLINED:
            return "a value of the module's own"
        if isinstance(value, dict):
            return {key: fill(item) for key, item in value.items()}
        if isinstance(value, list):
            return [fill(item) for item in value]
        return value

    def run(wanted: dict, found: dict, pairs: list, max_work=None) -> tuple:
        ledger = RealLedger(echo=False)
        ledger.begin("verdict self-test")
        compare_document(ledger, wanted, found, max_work, pairs)
        ledger.end(document_comparisons(wanted))
        code, line = verdict(ledger, pairs, None)
        return ledger, code, line

    centres = oracle.SET_ASIDE_CENTRES
    sources = oracle.set_aside_sources()
    pairs: list = []
    expected = oracle.analyse(
        sources,
        centres,
        oracle.ABSOLUTE_1,
        fraction=oracle.SET_ASIDE_FRACTION,
        pair_k=0,
        set_aside=pairs,
    )
    clean = oracle.analyse(sources, centres, oracle.ABSOLUTE_1, fraction=0.25, pair_k=0)
    wrong = []
    ledger, code, line = run(expected, fill(expected), pairs)
    if (ledger.failures, len(ledger.set_aside), ledger.made, code) != ([], 8, 38, 6):
        wrong.append("a set-aside alone must leave 8 of 38 comparisons inconclusive, exit 6")
    if not line.startswith("RESULT: INCONCLUSIVE"):
        wrong.append("a set-aside alone must print RESULT: INCONCLUSIVE")
    plants = (
        ("a centre without a decline", lambda doc: doc["centres"][0]["profile"]["Fast"][0]),
        ("what a declined centre still has", lambda doc: doc["centres"][1]["profile"]["Fast"][0]),
        ("the document's reference", lambda doc: doc["reference"]),
    )
    for what, pick in plants:
        found = fill(expected)
        pick(found)["objective"] = 123.0
        ledger, code, line = run(expected, found, pairs)
        if (len(ledger.failures), len(ledger.set_aside), code) != (1, 8, 1):
            wrong.append(f"a difference in {what}, beside the set-aside, must give exit 1")
        if not line.startswith("RESULT: DISAGREE"):
            wrong.append(f"a difference in {what}, beside the set-aside, must print DISAGREE")
    ledger, code, line = run(clean, fill(clean), [])
    if (ledger.failures, ledger.set_aside, ledger.made, code) != ([], [], 38, 0):
        wrong.append("a clean run must compare all 38 figures and give exit 0")
    if not line.startswith("RESULT: AGREE"):
        wrong.append("a clean run must print RESULT: AGREE")
    found = fill(expected)
    found["admission"]["max_work"] = 7
    if run(expected, found, pairs, 7)[0].failures:
        wrong.append("the bound the module was given must be what it reports back")
    if len(run(expected, fill(expected), pairs, 7)[0].failures) != 1:
        wrong.append("a module that does not report the bound it was given must disagree")
    if wrong:
        raise SystemExit("verdict self-test failed: " + "; ".join(wrong))
    print(
        "verdict self-test passed (a disagreement beside a set-aside gives exit 1, a set-aside "
        "alone exit 6, a clean run exit 0)"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Real-rows end to end for pineforge_hpo.neighbourhood"
    )
    parser.add_argument(
        "--result",
        action="append",
        required=True,
        help="native result JSON; repeat for more sources",
    )
    parser.add_argument(
        "--band", required=True, type=parse_band, metavar="absolute:V|relative:V", help="no default"
    )
    parser.add_argument(
        "--top-k", type=int, default=None, help="centres: the k best distinct vectors"
    )
    parser.add_argument(
        "--centre",
        action="append",
        type=parse_centre,
        default=None,
        metavar="SOURCE:ID",
        help="explicit centre",
    )
    parser.add_argument(
        "--fraction", type=float, default=None, help="for continuous real dimensions; no default"
    )
    parser.add_argument("--ratio", type=float, default=None, help="for log dimensions; no default")
    parser.add_argument(
        "--pair-k",
        type=int,
        required=True,
        help="pair maps per centre; no default (pin N12), 0 builds none",
    )
    parser.add_argument("--max-work", type=int, default=None, help="admission bound for the module")
    args = parser.parse_args(argv)
    if (args.top_k is None) == (args.centre is None):
        parser.error("give exactly one of --top-k and --centre")
    oracle.harness_self_test()
    verdict_self_test()
    paths = [Path(item) for item in args.result]
    for path in paths:
        data = path.read_bytes()
        print(f"input: {path} ({len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()})")
    documents = [load_result(path) for path in paths]
    problems = gate(documents)
    if problems:
        print("NOT NATIVE RESULTS, this is not real-rows input:")
        for problem in problems[:20]:
            print(f"  - {problem}")
        return 5
    print(
        f"gates held: {len(documents)} source(s), "
        f"{sum(len(d['trials']) for d in documents)} native rows"
    )
    sources = oracle_sources(documents)

    # With --top-k the oracle chooses the centres. If it cannot (it declines the whole study, or it
    # rejects the rows), the module chooses them and the oracle's verdict on the study comes below.
    trouble = None
    centres = None
    try:
        if args.top_k is not None:
            centres = oracle.oracle_top_ids(sources, args.top_k)
        else:
            centres = list(args.centre)
    except (oracle.InexactOracle, ValueError) as error:
        trouble = error

    sys.path.insert(0, str(oracle.ROOT / "python"))
    sys.path.insert(0, str(oracle.ROOT / "external" / "pineforge-codegen-oss"))
    from pineforge_hpo import neighbourhood as module

    by_module = centres is None
    if by_module:
        print(f"the oracle cannot choose the centres ({trouble}); the module chooses them")
        try:
            centres = module.top_ids(documents, args.top_k)
        except module.NeighbourhoodError as error:
            print_refusal(error)
            return 2
    print(f"centres: {centres}")

    try:
        result = module.neighbourhood(
            documents,
            centres=centres,
            band=args.band,
            fraction=args.fraction,
            ratio=args.ratio,
            pair_k=args.pair_k,
            max_work=args.max_work,
        )
    except module.NeighbourhoodError as error:
        print_refusal(error)
        return 2
    print_result(result)

    ledger = RealLedger()
    ledger.begin("properties the pin states for the document")
    ledger.compare(
        "pin",
        "contract id",
        result["contract"],
        "pineforge-hpo-neighbourhood/v1",
        "pin N15, draft clause 3.6",
    )
    ledger.compare(
        "pin",
        "one result per centre, in ascending identity",
        [(c["source"], c["trial_id"]) for c in result["centres"]],
        sorted(centres),
        "pin N13",
    )
    ledger.end(2)

    # A (centre, dimension) the oracle declines goes to ``declined`` and its figures are set aside;
    # a decline of the whole study (``whole``) sets everything aside. Rows the oracle rejects
    # although the module accepted them are a disagreement.
    declined: list = []
    whole = None
    expected = None
    try:
        expected = oracle.analyse(
            sources,
            centres,
            args.band,
            fraction=args.fraction,
            ratio=args.ratio,
            pair_k=args.pair_k,
            set_aside=declined,
        )
    except oracle.InexactOracle as error:
        whole = str(error)
    except ValueError as error:
        ledger.fail(f"the oracle rejects rows that the module accepted: {error}")
    print_set_aside(declined, whole)
    if expected is not None:
        ledger.begin("module against the independent oracle on the same rows")
        compare_document(ledger, expected, result, args.max_work, declined)
        extra = 0
        if args.top_k is not None:
            extra = 1
            label = "top_ids against the oracle's top_ids"
            if by_module:
                ledger.undecided("module", label, "the oracle could not choose the centres")
            else:
                found_ids = module.top_ids(documents, args.top_k)
                ledger.compare("module", label, centres, found_ids, "pin N13")
        ledger.end(document_comparisons(expected) + extra)
    elif whole is not None:
        ledger.begin("module against the independent oracle on the same rows")
        ledger.undecided(
            "module", "every figure of the document, and top_ids", f"the oracle declines: {whole}"
        )
        ledger.end(1)
    code, line = verdict(ledger, declined, whole)
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
