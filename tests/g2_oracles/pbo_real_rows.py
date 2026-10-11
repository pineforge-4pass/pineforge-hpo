#!/usr/bin/env python3
"""Real-rows end to end for ``pineforge_hpo.pbo``: the module on the rows of a finished study.

The PBO pin names no input for this proof ("one real study to a PBO report"), and the native runner
of the tree this script was written against does not write the block payload (``block_returns``)
that the module reads. The script therefore takes the rows as an argument, makes none up, and
stops with exit code 3 and the word BLOCKED when no row carries a ``block_returns`` object. It is
meant for the day the native payload exists. The PBO fixture files carry ``block_returns`` but not
the ``backtest`` object that marks native output, so the script refuses them with exit code 5.

With real rows it
1. refuses input that does not look like native output: every row must carry a ``backtest`` object
   with ``input_bars_processed`` >= 1 (necessary, not sufficient: hand-made rows lack it);
2. runs ``pineforge_hpo.pbo.pbo`` and prints the result as the pin describes it (counts, both rates,
   ``calibrated: false``, no threshold, the roster label of default D6, the work figure, the
   digest);
3. recomputes the counts and rates from the same rows with the exact oracle of ``pbo_oracle.py``
   (within-plus-between pooling in rational arithmetic, exact ties) and compares them. The oracle's
   counts are exact fractions; the module reports an int for a whole count and the nearest binary64
   for a fractional one (docs/pbo.md, Exactness), so a count is compared in that form, by value and
   by type, with no tolerance (``pbo_oracle.reported_count``).

The module takes its rows in parts, a list of (declared object, rows) pairs, together with the
declared roster and the early stop of the study. The script reads all of it from the result and
fills in none of it (``lineage_of``). The part is the rows of the result with the result-level
``return_blocks`` object of the result plus the dataset identity that the caller states
(``--dataset-identity``; it is not a key of the wire). The roster is the distinct trial ids of
those rows: a result of this tree declares no id set of its own, so the module's check that the
rows are the declared roster finds a repeated id but cannot find a gap or an unknown trial here.
``early_stop`` is the ``early_stop`` object of the result, None when the result has none. A rows
list or a rows JSONL file is no result and declares none of this: nothing is passed for it and
the module refuses it (exit 2).

Exact ties can make the two differ legitimately: the module ties on binary64 equality of its
statistics (default D4), the oracle on exact equality, and they can differ for two trials whose
statistics are exactly equal in a half although their block moments in that half differ. The
script finds the ordered splits such a tie can touch (a tie among the training winners, or a
testing statistic equal to a winner's) and prints them under TIES. It compares ``n_splits``
exactly. It asks the module for its lambdas, which carry every ordered split (one lambda per
tied winner, with the tie size), and compares the (tie size, lambdas) of every split that no tie
touches with the oracle's, exactly. It compares the touched splits, the counts and the rates with
what the ties allow (the counts and rates against the range the touched splits leave open), and
it checks the module's counts against its own lambdas. A difference at a touched split, or in the
counts and rates inside that range, is OPEN: inconclusive, never an agreement. A difference at a
split no tie touches, outside the range, or between the module's counts and its own lambdas is a
disagreement. One tie therefore cannot hide a difference at any other split, and a disagreement
is reported whatever else is open.

Usage: ``python3 tests/g2_oracles/pbo_real_rows.py --rows PATH --S 16 --sampler grid
--dataset-identity TEXT``
``PATH`` is a result JSON (``trials`` and optional ``warm_start_trials``) that carries the
declarations above, or a rows JSON list or a rows JSONL file that carries none. Exit codes: 0
module and oracle agree; 1 they disagree; 2 a refusal, the module's (printed as ``MODULE
REFUSED``) or the script's own for a result that keeps only some of its trials (``REFUSED BY THE
SCRIPT``), and also a usage error; 3 BLOCKED, no ``block_returns`` in the rows; 4 oracle skipped
as too large (``--oracle-limit``); 5 the rows do not look like native output; 6 inconclusive
(nothing disagrees, but a comparison that exact ties touch differs inside the range they allow).
"""

from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import sys

# Write nothing: no bytecode cache for the oracle module imported below or for the package.
sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pbo_oracle  # noqa: E402  (this directory; imports nothing of pineforge_hpo)

def load_rows(path: Path) -> tuple:
    """Return (rows, result object or None) from a result, a rows list or a rows JSONL file."""

    def reject(token: str):
        raise ValueError(f"non-finite JSON number {token} in {path}")

    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text, parse_constant=reject)
    except json.JSONDecodeError:
        value = None
    if value is None:
        rows = []
        for line in text.split("\n"):
            if line.strip():
                rows.append(json.loads(line, parse_constant=reject))
        return rows, None
    if isinstance(value, list):
        return value, None
    if isinstance(value, dict) and "trials" in value:
        return list(value.get("warm_start_trials", [])) + list(value["trials"]), value
    return [value], None


def looks_native(rows: list) -> list:
    """Trial ids of the rows that lack the backtest counters the native runner writes."""
    missing = []
    for row in rows:
        backtest = row.get("backtest") if isinstance(row, dict) else None
        bars = backtest.get("input_bars_processed") if isinstance(backtest, dict) else None
        if type(bars) is not int or bars < 1:
            missing.append(row.get("trial_id") if isinstance(row, dict) else None)
    return missing


def counts_add_up(document: dict) -> bool:
    """Whether the three reported counts sum to n_splits, allowing for what rounding can do. Each
    fractional count is the binary64 nearest to an exact fraction, so it is off by at most half an
    ulp; the sum of the exact values is n_splits, so the sum of the reported values may be off by
    at most the sum of those half ulps (a whole count is an int and carries no error). Exact
    otherwise."""
    values = [document["n_lt0"], document["n_eq0"], document["n_gt0"]]
    slack = sum(
        (Fraction(math.ulp(value)) / 2 for value in values if type(value) is float), Fraction(0)
    )
    return (
        abs(sum((Fraction(value) for value in values), Fraction(0)) - document["n_splits"])
        <= slack
    )


class RealLedger(pbo_oracle.Ledger):
    """The oracle's ledger plus a third verdict (finding P2-2): a comparison that exact ties leave
    open. An open comparison is counted as made; it is never a failure and never an agreement."""

    def __init__(self) -> None:
        super().__init__()
        self.open: list[str] = []

    def undecided(self, kind: str, label: str, detail: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.made += 1
        self.open.append(f"{self.name}: {label}")
        self.say(f"OPEN     {kind:8s} {label} | {detail}")


def tie_report(rows: list, size: int) -> list:
    """The ordered splits whose outcome an exact tie through different block moments could change.

    The module ties on binary64 equality of its statistics (default D4) and the oracle on exact
    equality. They can differ only for trials whose statistics are exactly equal in a half while
    their block moments in that half differ (identical moments give identical binary64
    statistics). Such a tie matters to a split in two places only: among the training winners
    (the winner set, and with it every weight, can differ) and among the testing statistics equal
    to a winner's (its rank can differ). A tie anywhere else changes nothing. Returns (train,
    test, reasons) for each touched split, in split order; the trial numbers in the reasons are
    trial ids. Call it only when the exact oracle returned a result, so that no statistic is
    undefined."""
    ordered = sorted(rows, key=lambda row: row["trial_id"])
    counts = ordered[0]["block_returns"]["count"]
    sums = [row["block_returns"]["sum"] for row in ordered]
    m2s = [row["block_returns"]["m2"] for row in ordered]
    key_of = pbo_oracle.moment_keys(counts, sums, m2s)
    trials = len(ordered)

    def moments(trial: int, half: tuple) -> list:
        return [
            (counts[block], Fraction(sums[trial][block]), Fraction(m2s[trial][block]))
            for block in half
        ]

    touched = []
    order = pbo_oracle.documented_order(size)
    for position in range(0, len(order), 2):
        first, second = order[position]
        keys_first = [key_of(trial, first) for trial in range(trials)]
        keys_second = [key_of(trial, second) for trial in range(trials)]
        for train, test, train_keys, test_keys in (
            (first, second, keys_first, keys_second),
            (second, first, keys_second, keys_first),
        ):
            best = max(train_keys)
            winners = [trial for trial in range(trials) if train_keys[trial] == best]
            reasons = []
            loose = [
                (ordered[left]["trial_id"], ordered[right]["trial_id"])
                for left in winners
                for right in winners
                if left < right and moments(left, train) != moments(right, train)
            ]
            if loose:
                reasons.append(
                    f"training winners tied through different block moments: trial ids {loose}"
                )
            for winner in winners:
                partners = [
                    ordered[other]["trial_id"]
                    for other in range(trials)
                    if other != winner
                    and test_keys[other] == test_keys[winner]
                    and moments(other, test) != moments(winner, test)
                ]
                if partners:
                    reasons.append(
                        f"the testing statistic of training winner {ordered[winner]['trial_id']} "
                        f"ties with trial ids {partners} through different block moments"
                    )
            if reasons:
                touched.append((train, test, reasons))
    return touched


def certain_buckets(oracle: dict, touched: list) -> dict:
    """The exact weight of each bucket (below, at and above lambda = 0) from the splits that no tie
    touches. Each touched split carries a total weight of 1 into some bucket, so a bucket's count
    can exceed this figure by at most the number of touched splits."""
    skipped = {(train, test) for train, test, _reasons in touched}
    certain = {"lt0": Fraction(0), "eq0": Fraction(0), "gt0": Fraction(0)}
    for split, record in oracle["per_split"].items():
        if split in skipped:
            continue
        weight = Fraction(1, len(record))
        for _winner, odds in record:
            bucket = "lt0" if odds < 1 else ("eq0" if odds == 1 else "gt0")
            certain[bucket] += weight
    return certain


def decide(
    ledger: RealLedger, label: str, expected, found, touched: int, inside: bool, detail: str
) -> None:
    """One comparison of the module's value with the exact oracle's. With no touched split it is
    exact. With touched splits an equal value is still an agreement; a different value is left
    OPEN when it lies inside the range those splits allow and is a disagreement when it lies
    outside."""
    if touched == 0 or pbo_oracle.same(expected, found):
        ledger.compare("module", label, expected, found)
    elif inside:
        ledger.undecided(
            "module",
            label,
            f"oracle={pbo_oracle.show(expected)} | module={pbo_oracle.show(found)} | "
            f"inside {detail}",
        )
    else:
        ledger.compare("module", f"{label} (outside {detail})", expected, found)


def oracle_split_records(oracle: dict, size: int) -> list:
    """The oracle's record of every ordered split in the documented order: (tie size,
    [lambda, ...]) with one lambda per tied winner, formed as the module forms it: the logarithm
    of the correctly rounded odds."""
    records = []
    for split in pbo_oracle.documented_order(size):
        record = oracle["per_split"][split]
        records.append((len(record), [math.log(float(odds)) for _winner, odds in record]))
    return records


def module_split_records(document: dict):
    """The module's record of every ordered split, rebuilt from its lambdas and their tie sizes: a
    split with k tied winners contributes k lambdas whose tie size is k each. None when the tie
    sizes do not form whole splits."""
    lambdas = document["lambdas"]
    sizes = document["lambda_tie_sizes"]
    records = []
    position = 0
    while position < len(sizes):
        k = sizes[position]
        if k < 1 or position + k > len(lambdas) or sizes[position : position + k] != [k] * k:
            return None
        records.append((k, lambdas[position : position + k]))
        position += k
    return records


def counts_of_records(records: list) -> tuple:
    """(n_lt0, n_eq0, n_gt0) in the module's form (type and value), recomputed from per-split
    records: every lambda below, at or above zero carries the weight 1/k of its split."""
    below = atom = above = Fraction(0)
    for k, lambdas in records:
        weight = Fraction(1, k)
        for value in lambdas:
            if value < 0:
                below += weight
            elif value == 0:
                atom += weight
            else:
                above += weight
    return tuple(
        pbo_oracle.typed(pbo_oracle.reported_count(count)) for count in (below, atom, above)
    )


def lineage_of(result, rows: list, dataset_identity) -> tuple:
    """The ``(parts, roster, keywords)`` of the module call, read from the real result only.

    Nothing is filled in. One part: ``rows`` with the result-level ``return_blocks`` object of the
    result plus the dataset identity that the caller states (a result carries none, so without it
    the module refuses the object as incomplete). The roster is the distinct trial ids of
    ``rows``: a result of this tree declares only counters (``trials_completed``,
    ``warm_start.trials``) and no id set, so the module's check that the rows are the declared
    roster cannot find a gap or an unknown trial here; a repeated id still refuses.
    ``early_stop`` is the ``early_stop`` object of the result, None when it has none (it is
    present only when the study enabled it). A list of rows is no result and declares none of
    this: nothing is passed for it, and the module refuses.
    """
    if not isinstance(result, dict):
        return [(None, rows)], None, {}
    declared = result.get("return_blocks")
    if isinstance(declared, dict) and dataset_identity is not None:
        declared = {**declared, "dataset_identity": dataset_identity}
    roster = []
    seen = set()
    for row in rows:
        trial_id = row.get("trial_id") if isinstance(row, dict) else None
        if type(trial_id) in (int, str) and trial_id not in seen:
            seen.add(trial_id)
            roster.append(trial_id)
    return [(declared, rows)], roster, {"early_stop": result.get("early_stop")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Real-rows end to end for pineforge_hpo.pbo")
    parser.add_argument("--rows", required=True, help="result JSON, rows JSON list or rows JSONL")
    parser.add_argument(
        "--S", type=int, required=True, help="number of blocks, even, fixed before the study"
    )
    parser.add_argument(
        "--sampler", required=True, help="the sampler of the study (default D6 label)"
    )
    parser.add_argument("--max-work", type=int, default=None, help="admission bound for the module")
    parser.add_argument(
        "--dataset-identity",
        default=None,
        help="the text that names the dataset the rows were reduced over: the caller states it (it "
        "is not a key of the wire) and the module refuses a declared object without it",
    )
    parser.add_argument(
        "--oracle-limit",
        type=int,
        default=2_000_000,
        help="skip the exact oracle above this many ordered splits times trials (default 2000000)",
    )
    args = parser.parse_args(argv)
    pbo_oracle.harness_self_test()
    path = Path(args.rows)
    data = path.read_bytes()
    print(f"input: {path} ({len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()})")
    rows, result = load_rows(path)
    if result is not None and result.get("trials_out", "all") != "all":
        print(
            "REFUSED BY THE SCRIPT: the result keeps only some of its trials "
            "(trials_out is not all)"
        )
        return 2
    carrying = sum(
        1 for row in rows if isinstance(row, dict) and isinstance(row.get("block_returns"), dict)
    )
    print(f"rows read: {len(rows)}; rows carrying block_returns: {carrying}")
    if carrying == 0:
        print(
            "BLOCKED: no row carries a block_returns payload. The native runner of this tree "
            "does not\n"
            "write one (docs/pbo.md: the native block payload is a later piece of work), "
            "so there are\n"
            "no real rows to run the module on. Nothing was run and nothing is claimed."
        )
        return 3
    missing = looks_native(rows)
    if missing:
        print(f"NOT REAL ROWS: trial ids without a backtest object counting bars: {missing[:20]}")
        return 5
    sys.path.insert(0, str(pbo_oracle.ROOT / "python"))
    sys.path.insert(0, str(pbo_oracle.ROOT / "external" / "pineforge-codegen-oss"))
    from pineforge_hpo import pbo

    parts, roster, declared_keywords = lineage_of(result, rows, args.dataset_identity)
    try:
        document = pbo.pbo(
            parts,
            S=args.S,
            roster=roster,
            sampler=args.sampler,
            max_work=args.max_work,
            with_lambdas=True,
            **declared_keywords,
        )
    except pbo.PboError as error:
        print(f"MODULE REFUSED: {error.code}: {error.detail}; trial ids {list(error.ids)[:20]}")
        return 2
    print(
        "module result (the lambdas and their tie sizes, one per tied winner of each split, "
        "are not printed):"
    )
    shown = {
        name: value
        for name, value in document.items()
        if name not in ("lambdas", "lambda_tie_sizes")
    }
    print(json.dumps(shown, indent=2, sort_keys=True, allow_nan=False))

    ledger = RealLedger()
    ledger.begin("properties the pin states for the result")
    ledger.compare(
        "pin",
        "calibrated is false",
        document["calibrated"],
        False,
        "pin section 1: calibrated:false",
    )
    ledger.compare(
        "pin",
        "no standard error, threshold or p-value in the result",
        [name for name in ("standard_error", "threshold", "p_value") if name in document],
        [],
        "pin section 1: no standard error, no pass/fail threshold",
    )
    ledger.compare(
        "pin", "pbo is the lambda <= 0 rate", document["pbo"], document["rate_le0"], "pin D3"
    )
    ledger.compare(
        "pin",
        "n_lt0 + n_eq0 + n_gt0 equals n_splits, up to the rounding of the fractional counts",
        counts_add_up(document),
        True,
        "pin D3, D4",
    )
    # Pin D6 (docs/pbo.md line 185): the label follows from the sampler AND from whether the result
    # had a no-improvement early stop. Whether it had one is read from the result the way the call
    # reads it (``declared_keywords``, not from the module's output): the result's ``early_stop``
    # object, or True, is a stop; None or False is none (docs/pbo.md, lines 219 and 236). The
    # expected label is the entry of ``pbo_oracle.ROSTER_LABELS`` for (sampler, stopped); a sampler
    # that is not one of the six has no entry, and the module refuses it before this point.
    early_stop = declared_keywords.get("early_stop")
    stopped = early_stop is True or isinstance(early_stop, dict)
    ledger.compare(
        "pin",
        f"roster label of the sampler {args.sampler} with early stop {stopped}",
        pbo_oracle.ROSTER_LABELS.get((args.sampler, stopped)),
        document["roster_label"],
        "pin D6",
    )
    ledger.end(5)
    pin_failed = bool(ledger.failures)

    if document["n_splits"] * document["N"] > args.oracle_limit:
        print(
            f"ORACLE SKIPPED: {document['n_splits']} splits x {document['N']} trials is above "
            f"--oracle-limit {args.oracle_limit}; the module's numbers are NOT cross-checked"
        )
        return 1 if pin_failed else 4
    oracle = pbo_oracle.oracle_from_rows(rows, args.S)
    if oracle["status"] == "refused":
        print(
            f"DISAGREE: the oracle finds a zero-variance trial {oracle['ids']} over blocks "
            f"{oracle['blocks']} but the module returned a result"
        )
        return 1
    ledger.begin("module against the exact oracle on the same rows")
    touched = tie_report(rows, args.S)
    reach = len(touched)
    certain = certain_buckets(oracle, touched)
    n_splits = oracle["n_splits"]
    if touched:
        print(
            f"TIES: {reach} of {n_splits} ordered splits are touched by an exact tie through "
            "different block moments (binary64 equality, default D4, may differ from exact "
            "equality there). n_splits and every split no tie touches are compared exactly; the "
            "touched splits, the counts and the rates are compared against what the ties allow."
        )
        for train, test, reasons in touched[:20]:
            print(f"  train blocks {list(train)}, test blocks {list(test)}: " + "; ".join(reasons))
        if reach > 20:
            print(f"  the other {reach - 20} touched splits, named without their reasons:")
            for train, test, _reasons in touched[20:]:
                print(f"  train blocks {list(train)}, test blocks {list(test)}")
    ledger.compare("module", "n_splits", n_splits, document["n_splits"])
    # Split by split: the oracle's (tie size, lambdas) of every ordered split against the module's,
    # which are rebuilt from the module's lambdas and tie sizes. A split no tie touches must agree
    # exactly.
    order = pbo_oracle.documented_order(args.S)
    place = {split: position for position, split in enumerate(order)}
    touched_at = sorted(place[(train, test)] for train, test, _reasons in touched)
    touched_set = set(touched_at)
    clear_at = [position for position in range(len(order)) if position not in touched_set]
    expected_records = oracle_split_records(oracle, args.S)
    found_records = module_split_records(document)
    usable = found_records is not None and len(found_records) == len(order)
    ledger.compare(
        "module",
        f"the (tie size, lambdas) of each of the {len(clear_at)} ordered splits that no tie "
        "touches",
        [expected_records[position] for position in clear_at],
        [found_records[position] for position in clear_at] if usable else None,
    )
    if touched:
        decide(
            ledger,
            f"the (tie size, lambdas) of each of the {reach} ordered splits that a tie touches",
            [expected_records[position] for position in touched_at],
            [found_records[position] for position in touched_at] if usable else None,
            reach,
            usable,
            "a touched split may differ (they are listed under TIES)",
        )
    ledger.compare(
        "module",
        "the module's counts against its own lambdas, each lambda weighing 1/(the tie size of "
        "its split)",
        counts_of_records(found_records) if usable else None,
        pbo_oracle.module_counts(document),
    )
    count_names = ("n_lt0", "n_eq0", "n_gt0")
    spans = [(certain[name], certain[name] + reach) for name in ("lt0", "eq0", "gt0")]
    decide(
        ledger,
        "(n_lt0, n_eq0, n_gt0) as (type, value): an int when whole, else the nearest binary64",
        pbo_oracle.oracle_counts(oracle),
        pbo_oracle.module_counts(document),
        reach,
        all(
            float(low) <= document[name] <= float(high)
            for name, (low, high) in zip(count_names, spans)
        ),
        "the range of each count: "
        + ", ".join(f"[{float(low)}, {float(high)}]" for low, high in spans),
    )
    below, atom = certain["lt0"], certain["eq0"]
    decide(
        ledger,
        "(rate_le0, rate_lt0)",
        (oracle["rate_le0"], oracle["rate_lt0"]),
        (document["rate_le0"], document["rate_lt0"]),
        reach,
        float((below + atom) / n_splits)
        <= document["rate_le0"]
        <= float((below + atom + reach) / n_splits)
        and float(below / n_splits) <= document["rate_lt0"] <= float((below + reach) / n_splits),
        f"the range of each rate: [{float((below + atom) / n_splits)}, "
        f"{float((below + atom + reach) / n_splits)}] and "
        f"[{float(below / n_splits)}, {float((below + reach) / n_splits)}]",
    )
    ledger.end(5 + (1 if touched else 0))
    left_open = (
        f"{len(ledger.open)} comparison(s) were left OPEN by the {reach} tie-touched ordered splits"
        " (see TIES and the OPEN lines above)."
    )
    if pin_failed:
        print(
            "RESULT: FAIL (a property the pin states for the result does not hold; see above). "
            + left_open
        )
        return 1
    if ledger.failures:
        print(
            "RESULT: DISAGREE (the module and the exact oracle differ where no exact tie can "
            "explain it; see the DISAGREE lines above). " + left_open
        )
        return 1
    if ledger.open:
        print(
            f"RESULT: INCONCLUSIVE. Nothing disagrees, but {len(ledger.open)} of the "
            f"{ledger.made} comparisons differ only where the {reach} tie-touched ordered splits "
            "allow it (the OPEN lines above), and a tie through different block moments is where "
            "binary64 equality (default D4) may legitimately differ from exact equality. Inspect "
            "the splits listed under TIES before judging the module. The other comparisons "
            "agreed, among them every split that no tie touches."
        )
        return 6
    print("RESULT: AGREE (module and exact oracle give the same counts and rates on these rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
