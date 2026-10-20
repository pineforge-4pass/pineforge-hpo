#!/usr/bin/env python3
"""Independent oracle for ``pineforge_hpo.pareto`` (the post hoc Pareto front).

The module flips minimised axes by negation, compares each unordered pair once in both directions
and lets the shared row reader (``_rows``) validate every row. This script reaches the same
front, domination counts, distinct-vector count, exclusions and admission figure by other routes
and never takes a number from the module:

* it reads the fixture files itself with ``json`` (strict about NaN, Infinity and overflowing
  numbers), not through the module's reader;
* dominance is judged three ways that must agree: the signs of the per-axis comparisons turned
  towards each axis's direction (a dominates b iff no sign is negative and one is positive); the
  componentwise rule on direction-oriented vectors ("at least as good everywhere and not the same
  vector"); and, for the front only, a sort-and-filter pass (sort the oriented vectors descending,
  keep a vector unless a kept one dominates it) that never compares all pairs;
* counts come from scanning every ordered pair (b, a), a != b, rather than each unordered pair once;
* eligibility and the refusals are decided from the contract's tables, not by the module's ladder
  of tests: the kind of every axis cell of every row (absent, null, number, nonfinite, other) is
  computed first, a status table names the reason of a row that is not ok, the exclusion families
  are sets (an ok row with an absent key, then an ok row with a null value); the pinned counts of
  the fixtures cover that every row is eligible or excluded once. The order of the refusals is the one the module's own
  documentation states; the pin does not state it, so that order is a reproduction, not an
  independent reading.

Phase 1 needs no module: the checker's numbers are compared with the values the test file pins for
the fixtures PF1-PF4 and the dominance rules (hand-derived there, re-derived here), with a
hand-derived perturbation of PF1, with hand cases for the eligibility table (an absent key beats a
null value, axes listed in the order of the request) and with the refusal fixtures of the contract
(expected codes, ids and names written from the contract text). Phase 2 imports
``pineforge_hpo.pareto`` only after phase 1 is finished, runs the module on the same inputs and
compares its documents with the checker's, then does the same on seeded random instances (ties,
duplicates, -0.0, absent and null values, and the statuses ok, pruned, constraint_violation,
engine_error and partial; not constraint_error, trial_error or trial_timeout), also with the rows
shuffled and the axes reversed.

Usage: ``python3 tests/g2_oracles/pareto_oracle.py [--no-module] [--cases N] [--seed S]``.
Exit code 0 only if every comparison agreed, every section made the comparisons it must make and the
module was compared; 1 for any failure; 2 for a usage error; 3 when ``--no-module`` ran phase 1 only
(the checker against the pinned values, the module NOT compared). A run that compared nothing
with the module is never exit 0.
"""

from __future__ import annotations

import argparse
import copy
from fractions import Fraction
import json
import math
from pathlib import Path
import random
import sys

# Write nothing: no bytecode cache beside this script or inside the package that phase 2 imports.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "tests" / "python" / "fixtures" / "pareto"
# Exit code of a run that did not compare the module (--no-module); 0 is kept for a full pass.
NO_MODULE_EXIT = 3

PROFIT = "metrics.all.net_profit_pct"
FACTOR = "metrics.all.profit_factor"
DRAWDOWN = "metrics.equity.max_equity_drawdown_pct"
AXES = [
    {"name": PROFIT, "direction": "maximize"},
    {"name": DRAWDOWN, "direction": "minimize"},
]
AXES3 = [
    {"name": PROFIT, "direction": "maximize"},
    {"name": FACTOR, "direction": "maximize"},
    {"name": DRAWDOWN, "direction": "minimize"},
]

# ---------------------------------------------------------------------------------------------
# Values the test file pins. Each was re-derived by hand from the fixture rows by the author of
# this oracle (not copied unchecked); ``source`` names the test that pins it.
# PF1: eligible rows 0 (10,5) 1 (12,8) 2 (8,3) 3 (9,6) 4 (12,8) 5 (7,3) 9 (6,2); row 6 is a
# constraint violation, row 7 an engine error, row 8 has a null drawdown. 0 dominates 3 and 2
# dominates 5, no other pair; rows 1 and 4 are equal and both stay.
# PF2: rows (3,3,5) (3,3,4) (4,1,9) (1,4,2) (3,3,4) (2,2,6); rows 1 and 4 dominate 0 and 5, and 0
# dominates 5; nobody dominates 2 or 3.
# PF3: eligible rows 0 (1,2) and 3 (2,1) only; 3 dominates 0.
# ---------------------------------------------------------------------------------------------

PF1 = {
    "front": [0, 1, 2, 4, 9],
    "n_front": 5,
    "distinct": 4,
    "counts": {0: 0, 1: 0, 2: 0, 3: 1, 4: 0, 5: 1, 9: 0},
    "rows": (10, 7, 3),
    "excluded": {
        "metric_unavailable": {"count": 1, "ids": [8], "axes": [DRAWDOWN]},
        "status_constraint_violation": {"count": 1, "ids": [6]},
        "status_engine_error": {"count": 1, "ids": [7]},
    },
    "figure": 31,  # 10 rows read + 7 * 6 / 2 pairs
    "source": "test_pareto.py PF1_EXPECTED, "
    "WorkedFixtureTests.test_pf1_infeasible_error_missing_and_tie and "
    "ResourceTests.test_admission_figure_boundaries",
}
PF2 = {
    "front": [1, 2, 3, 4],
    "n_front": 4,
    "distinct": 3,
    "counts": {0: 2, 1: 0, 2: 0, 3: 0, 4: 0, 5: 3},
    "rows": (6, 6, 0),
    "excluded": {},
    "figure": 21,  # 6 + 6 * 5 / 2
    "source": "test_pareto.py WorkedFixtureTests.test_pf2_three_axes_with_ties and "
    "ResourceTests.test_admission_figure_boundaries",
}
PF3 = {
    "front": [3],
    "n_front": 1,
    "distinct": 1,
    "counts": {0: 1, 3: 0},
    "rows": (7, 2, 5),
    "excluded": {
        "metric_not_recorded": {"count": 2, "ids": [1, 6], "axes": [PROFIT, DRAWDOWN]},
        "status_objective_error": {"count": 1, "ids": [4]},
        "status_partial": {"count": 1, "ids": [5]},
        "status_pruned": {"count": 1, "ids": [2]},
    },
    "figure": 8,  # 7 + 2 * 1 / 2
    "source": "test_pareto.py "
    "WorkedFixtureTests.test_pf3_statuses_absent_metrics_and_a_single_member_front and "
    "ResourceTests.test_admission_figure_boundaries",
}
FIXTURE_CASES = [("pf1.json", AXES, PF1), ("pf2.json", AXES3, PF2), ("pf3.json", AXES, PF3)]

# (label, vectors, directions, front, domination counts, distinct front vectors, source)
DOMINANCE_CASES = [
    (
        "strict improvement is needed and equal vectors stay",
        [(1, 1), (1, 2), (2, 1), (1, 1)],
        ("maximize", "maximize"),
        [1, 2],
        {0: 2, 1: 0, 2: 0, 3: 2},
        2,
        "test_pareto.py DominanceTests.test_strict_improvement_needed_and_ties_stay",
    ),
    (
        "two equal vectors both stay",
        [(5, 5), (5, 5)],
        ("maximize", "maximize"),
        [0, 1],
        {0: 0, 1: 0},
        1,
        "test_pareto.py DominanceTests.test_equal_vectors_do_not_dominate_each_other",
    ),
    (
        "minimize on every axis",
        [(1, 2), (2, 1), (2, 2)],
        ("minimize", "minimize"),
        [0, 1],
        {0: 0, 1: 0, 2: 2},
        2,  # distinct count derived by hand; the test pins front and counts
        "test_pareto.py DominanceTests.test_minimize_on_every_axis",
    ),
    (
        "mixed directions",
        [(1, 1), (2, 1), (1, 2)],
        ("maximize", "minimize"),
        [1],
        {0: 1, 1: 0, 2: 2},
        1,  # derived by hand; the test pins front and counts
        "test_pareto.py DominanceTests.test_mixed_directions",
    ),
    (
        "negative zero equals positive zero (three rows)",
        [(-0.0, 1.0), (0.0, 1.0), (0.0, 2.0)],
        ("maximize", "maximize"),
        [2],
        {0: 1, 1: 1, 2: 0},
        1,  # distinct count derived by hand; the test pins front and counts
        "test_pareto.py DominanceTests.test_negative_zero_equals_positive_zero",
    ),
    (
        "negative zero equals positive zero (two equal rows)",
        [(-0.0, 1.0), (0.0, 1.0)],
        ("maximize", "maximize"),
        [0, 1],
        {0: 0, 1: 0},
        1,
        "test_pareto.py DominanceTests.test_negative_zero_equals_positive_zero",
    ),
    (
        "negative zero under minimize",
        [(0.0, -0.0), (0.0, 0.0)],
        ("minimize", "minimize"),
        [0, 1],
        {0: 0, 1: 0},  # counts derived by hand; the test pins front and distinct
        1,
        "test_pareto.py DominanceTests.test_negative_zero_equals_positive_zero",
    ),
    (
        "adjacent doubles are different (no tolerance)",
        [(1.0, 0.0), (1.0 + 2.0**-52, 0.0)],
        ("maximize", "maximize"),
        [1],
        {0: 1, 1: 0},
        1,  # distinct count derived by hand; the test pins front and counts
        "test_pareto.py DominanceTests.test_comparison_is_exact_with_no_tolerance",
    ),
    (
        "1e-300 beats 0.0",
        [(0.0, 0.0), (1e-300, 0.0)],
        ("maximize", "maximize"),
        [1],
        {0: 1, 1: 0},  # counts derived by hand; the test pins the front only
        1,  # distinct count derived by hand; the test pins the front only
        "test_pareto.py DominanceTests.test_comparison_is_exact_with_no_tolerance",
    ),
    (
        "an integer equals the same float",
        [(3, 3), (3.0, 3.0)],
        ("maximize", "maximize"),
        [0, 1],
        {0: 0, 1: 0},  # counts derived by hand; the test pins front and distinct
        1,
        "test_pareto.py DominanceTests.test_integers_and_floats_compare_by_value",
    ),
    (
        "2**53 and 2**53 + 1 are one binary64 value",
        [(2**53, 0), (2**53 + 1, 0)],
        ("maximize", "maximize"),
        [0, 1],
        {0: 0, 1: 0},  # counts derived by hand; the test pins front and distinct
        1,
        "test_pareto.py DominanceTests.test_integers_and_floats_compare_by_value",
    ),
]

# ---------------------------------------------------------------------------------------------
# Harness: every comparison is counted and printed, a disagreement is recorded and never swallowed.
# ---------------------------------------------------------------------------------------------


def same(a, b, strict: bool = False) -> bool:
    """Exact equality: numbers by value (int, float and Fraction mix; with ``strict`` the types must
    match too), bool only with bool, containers element by element (list and tuple are
    interchangeable), the rest by type and value."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    numeric = (int, float, Fraction)
    if isinstance(a, numeric) and isinstance(b, numeric):
        if strict and type(a) is not type(b):
            return False
        return a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y, strict) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        if strict:
            # the keys must match in type too: a count keyed by 3.0 is not a count keyed by 3
            first = {(type(key), key): value for key, value in a.items()}
            second = {(type(key), key): value for key, value in b.items()}
            return first.keys() == second.keys() and all(
                same(first[key], second[key], strict) for key in first
            )
        return a.keys() == b.keys() and all(same(a[key], b[key], strict) for key in a)
    return type(a) is type(b) and a == b


def show(value, limit: int = 150) -> str:
    if isinstance(value, Fraction):
        text = (
            str(value.numerator)
            if value.denominator == 1
            else f"{value.numerator}/{value.denominator}"
        )
    elif isinstance(value, list):
        text = "[" + ", ".join(show(item, limit) for item in value) + "]"
    elif isinstance(value, tuple):
        text = "(" + ", ".join(show(item, limit) for item in value) + ")"
    elif isinstance(value, dict):
        text = (
            "{" + ", ".join(f"{show(k, limit)}: {show(v, limit)}" for k, v in value.items()) + "}"
        )
    else:
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


class Ledger:
    def __init__(self, echo: bool = True, strict: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.failures: list[str] = []
        self.name = ""
        self.made = 0
        self.echo = echo
        self.strict = strict

    def say(self, text: str) -> None:
        if self.echo:
            print(text)

    def begin(self, name: str) -> None:
        self.name = name
        self.made = 0
        self.say(f"\n== {name}")

    def end(self, expected: int) -> None:
        self.say(f"-- {self.name}: {self.made} comparisons made, {expected} expected")
        if self.made != expected:
            self.fail(f"{self.name}: made {self.made} comparisons, expected {expected}")

    def fail(self, message: str) -> None:
        self.failures.append(message)
        self.say(f"FAIL {message}")

    def note(self, message: str) -> None:
        self.say(f"   note: {message}")

    def check(self, kind: str, label: str, condition: bool) -> bool:
        """Record a comparison whose verdict the caller decided with a primitive operator."""
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.made += 1
        if not condition:
            self.failures.append(f"{self.name}: {label}")
        self.say(f"{'AGREE   ' if condition else 'DISAGREE'} {kind:8s} {label}")
        return condition

    def compare(
        self, kind: str, label: str, checker, other, source: str = "", *, quiet: bool = False
    ) -> bool:
        """Compare the checker's value with ``other`` (kind: fixture, internal, control, module)."""
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.made += 1
        agree = same(checker, other, self.strict)
        if not agree:
            self.failures.append(f"{self.name}: {label}")
        if agree and quiet:
            return True
        status = "AGREE   " if agree else "DISAGREE"
        where = f" [{source}]" if source else ""
        self.say(
            f"{status} {kind:8s} {label} | checker={show(checker)} | {kind}={show(other)}{where}"
        )
        return agree


def harness_self_test() -> None:
    """Abort before any comparison if the helpers cannot tell different things apart. The verdicts
    here use ``is``, not ``same``, so a broken ``same`` cannot vouch for itself."""
    matching = [
        (1, 1.0),
        (Fraction(1, 2), 0.5),
        ([1, (2, 3)], (1, [2, 3])),
        ({"a": [1]}, {"a": (1,)}),
        (None, None),
        (True, True),
    ]
    differing = [
        (3, 4),
        (0.5, 0.25),
        (Fraction(1, 2), Fraction(1, 3)),
        (True, 1),
        ([1, 2], [1, 3]),
        ({"a": 1}, {"a": 2}),
        (None, 0),
        ("a", "b"),
        ((1,), (1, 2)),
        (float("nan"), float("nan")),
    ]
    for left, right in matching:
        if same(left, right) is not True:
            raise SystemExit(f"harness self-test failed: same({left!r}, {right!r}) must be True")
    for left, right in differing:
        if same(left, right) is not False:
            raise SystemExit(f"harness self-test failed: same({left!r}, {right!r}) must be False")
    for left, right in ((1, 1.0), (Fraction(1, 2), 0.5), (1.0, Fraction(1)), ({3: 1}, {3.0: 1})):
        if same(left, right, True) is not False:
            raise SystemExit(
                f"harness self-test failed: strict same({left!r}, {right!r}) must be False"
            )
    if (
        same(1.5, 1.5, True) is not True
        or same([1, 2.0], (1, 2.0), True) is not True
        or same({3: 1.5}, {3: 1.5}, True) is not True
    ):
        raise SystemExit(
            "harness self-test failed: strict same must accept equal values of equal types"
        )
    probe = Ledger(echo=False)
    probe.name = "probe"
    probe.compare("control", "equal", 1, 1)
    probe.compare("control", "different", 1, 2)
    if probe.failures != ["probe: different"] or probe.counts != {"control": 2} or probe.made != 2:
        raise SystemExit("harness self-test failed: the ledger must record exactly the one failure")
    strict_probe = Ledger(echo=False, strict=True)
    strict_probe.name = "strict probe"
    strict_probe.compare("control", "int against float", 1, 1.0)
    if strict_probe.failures != ["strict probe: int against float"]:
        raise SystemExit(
            "harness self-test failed: a strict ledger must reject an int against a float"
        )
    print("harness self-test passed (same() and the ledger tell different values apart)")


# ---------------------------------------------------------------------------------------------
# The oracle.
# ---------------------------------------------------------------------------------------------


def finite_float(value):
    """The binary64 of a finite JSON number; None for bool, text, containers, overflow, NaN, inf."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def orient(vector, directions):
    """Turn every axis towards 'larger is better' (the minimised axes are negated)."""
    return tuple(v if d == "maximize" else -v for v, d in zip(vector, directions))


def sign(x, y) -> int:
    return (x > y) - (x < y)


def dominates_by_signs(a, b, directions) -> bool:
    """a dominates b: judged on the signs of the per-axis comparisons, towards each direction."""
    signs = [sign(x, y) * (1 if d == "maximize" else -1) for x, y, d in zip(a, b, directions)]
    return min(signs) >= 0 and max(signs) > 0


def dominates_componentwise(a, b, directions) -> bool:
    """a dominates b: at least as good on every oriented axis and not the same vector."""
    oa = orient(a, directions)
    ob = orient(b, directions)
    return all(x >= y for x, y in zip(oa, ob)) and any(x != y for x, y in zip(oa, ob))


def front_by_sort_filter(vectors, directions) -> list:
    """Indices of the front without comparing all pairs: sort the oriented vectors descending, so
    every dominator precedes what it dominates, and keep a vector unless a kept one dominates it."""
    oriented = [orient(vector, directions) for vector in vectors]
    order = sorted(range(len(vectors)), key=lambda index: oriented[index], reverse=True)
    kept: list[int] = []
    for index in order:
        beaten = any(
            all(x >= y for x, y in zip(oriented[other], oriented[index]))
            and any(x != y for x, y in zip(oriented[other], oriented[index]))
            for other in kept
        )
        if not beaten:
            kept.append(index)
    return sorted(kept)


# The eligibility table of the contract (clause 3 of the proposed dossier, pin P2 to P4), written
# out as data: a row whose status is not ok is excluded under the reason named here. An ok row is
# excluded as metric_not_recorded when some axis key is absent, otherwise as metric_unavailable
# when some axis value is null ("the first reason that applies"), and is eligible when every axis
# value is a finite number.
STATUS_REASONS = {
    "constraint_violation": "status_constraint_violation",
    "objective_error": "status_objective_error",
    "constraint_error": "status_constraint_error",
    "engine_error": "status_engine_error",
    "trial_error": "status_trial_error",
    "trial_timeout": "status_trial_timeout",
    "pruned": "status_pruned",
    "partial": "status_partial",
}


def cell_kind(metrics, name: str) -> str:
    """What one axis cell of a row holds: ``absent`` (no metrics object, or no such key), ``null``,
    ``number`` (a finite number), ``nonfinite`` (a number that is NaN, infinite or overflows) or
    ``other`` (a boolean, text, array or object)."""
    if not isinstance(metrics, dict) or name not in metrics:
        return "absent"
    value = metrics[name]
    if value is None:
        return "null"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "other"
    return "number" if finite_float(value) is not None else "nonfinite"


def refusal(code: str, ids=(), names=()) -> dict:
    return {"status": "refused", "code": code, "ids": tuple(ids), "names": tuple(names)}


def oracle_front(rows: list, axes: list) -> dict:
    """The front document of the pin (P1-P6) for parsed rows, or the refusal the pin calls for.

    Eligibility and refusals are decided from the contract's tables, not by a ladder of tests
    over the rows: the kind of every axis cell of every row is computed first (``cell_kind``),
    the exclusion families are sets, a row's reason follows from which sets contain it
    (``STATUS_REASONS`` for the statuses; the pinned counts of the fixtures cover that each row is
    eligible or excluded once), and
    the front comes from the three dominance formulations below. The order of the refusals (an
    unknown axis, then an unusable value, then no eligible row) is the one the module's own
    documentation states; the pin does not state it."""
    names = [axis["name"] for axis in axes]
    directions = [axis["direction"] for axis in axes]
    stray = sorted({row["status"] for row in rows} - set(STATUS_REASONS) - {"ok"})
    if stray:
        raise ValueError(f"the oracle's table has no row for the status {stray}")
    by_id = {row["trial_id"]: row for row in sorted(rows, key=lambda row: row["trial_id"])}
    kinds = {
        trial_id: [cell_kind(row.get("metrics"), name) for name in names]
        for trial_id, row in by_id.items()
    }

    nowhere = [
        name
        for index, name in enumerate(names)
        if all(kinds[trial_id][index] == "absent" for trial_id in by_id)
    ]
    if by_id and nowhere:
        return refusal("pareto_axis_unknown", names=nowhere)
    # A value of another type refuses only on an ok row (clause 3). A non-finite number refuses on
    # any row: that is the module's reading (its documentation), reproduced here; the contract's
    # wording names ok rows and file tokens only, and no case exercises it on a row that is not ok.
    unusable = {
        trial_id: [
            name
            for name, kind in zip(names, kinds[trial_id])
            if kind == "nonfinite" or (kind == "other" and by_id[trial_id]["status"] == "ok")
        ]
        for trial_id in by_id
    }
    unusable = {trial_id: bad for trial_id, bad in unusable.items() if bad}
    if unusable:
        return refusal(
            "pareto_input_invalid",
            ids=sorted(unusable),
            names=[name for name in names if any(name in bad for bad in unusable.values())],
        )

    ok_ids = {trial_id for trial_id, row in by_id.items() if row["status"] == "ok"}
    lacking = {trial_id for trial_id in ok_ids if "absent" in kinds[trial_id]}
    nulled = {trial_id for trial_id in ok_ids if "null" in kinds[trial_id]} - lacking
    ids = sorted(ok_ids - lacking - nulled)
    excluded: dict = {}
    for status, reason in STATUS_REASONS.items():
        listed = [trial_id for trial_id, row in by_id.items() if row["status"] == status]
        if listed:
            excluded[reason] = listed
    if lacking:
        excluded["metric_not_recorded"] = sorted(lacking)
    if nulled:
        excluded["metric_unavailable"] = sorted(nulled)
    if not ids:
        return refusal("pareto_no_eligible_rows")
    # A row is eligible or excluded exactly once by construction: ``ids`` holds the ok rows with no
    # absent and no null axis value, and ``excluded`` holds every other row under one reason, the
    # status reasons covering every non-ok status. An unhandled status is named by the ``stray``
    # raise above (``set(row["status"]) - STATUS_REASONS - {"ok"}``), which is the check that can
    # fail; a self-comparison of the two sets here could not. The eligibility counts are pinned by
    # the hand cases (``section_eligibility``) and by ``section_fixtures``.
    involved = {
        "metric_not_recorded": [
            name
            for index, name in enumerate(names)
            if any(kinds[trial_id][index] == "absent" for trial_id in lacking)
        ],
        "metric_unavailable": [
            name
            for index, name in enumerate(names)
            if any(kinds[trial_id][index] == "null" for trial_id in nulled)
        ],
    }
    summary = {}
    for reason in sorted(excluded):
        entry: dict = {"count": len(excluded[reason]), "ids": excluded[reason]}
        if reason in involved:
            entry["axes"] = involved[reason]
        summary[reason] = entry

    vectors = [
        tuple(finite_float(by_id[trial_id]["metrics"][name]) for name in names) for trial_id in ids
    ]
    dominated_by = [0] * len(ids)
    for victim in range(len(ids)):
        for other in range(len(ids)):
            if other != victim and dominates_by_signs(vectors[other], vectors[victim], directions):
                dominated_by[victim] += 1
    front_indices = [index for index, count in enumerate(dominated_by) if count == 0]
    formulations_agree = front_indices == front_by_sort_filter(vectors, directions) and all(
        dominates_by_signs(a, b, directions) == dominates_componentwise(a, b, directions)
        for a in vectors
        for b in vectors
    )
    pairs = sum(1 for first in range(len(ids)) for second in range(first + 1, len(ids)))
    return {
        "status": "ok",
        "rows_read": len(rows),
        "rows_eligible": len(ids),
        "rows_excluded": sum(len(listed) for listed in excluded.values()),
        "excluded": summary,
        "pareto_front": [ids[index] for index in front_indices],
        "n_front": len(front_indices),
        "n_distinct_vectors_on_front": len(
            {tuple(v + 0.0 for v in vectors[i]) for i in front_indices}
        ),
        "domination_counts": {ids[index]: dominated_by[index] for index in range(len(ids))},
        "figure": len(rows) + pairs,
        "formulations_agree": formulations_agree,
    }


def view_of(document: dict) -> dict:
    """The fields both the oracle and the module document carry."""
    return {
        "front": document["pareto_front"],
        "n_front": document["n_front"],
        "distinct": document["n_distinct_vectors_on_front"],
        "counts": document["domination_counts"],
        "rows": (document["rows_read"], document["rows_eligible"], document["rows_excluded"]),
        "excluded": document["excluded"],
    }


def outcome_view(document: dict):
    if document["status"] == "refused":
        return ("refused", document["code"], tuple(document["ids"]), tuple(document["names"]))
    return view_of(document)


# ---------------------------------------------------------------------------------------------
# Fixture access.
# ---------------------------------------------------------------------------------------------


def strict_loads(text: str):
    def reject(token: str):
        raise ValueError(f"non-finite JSON number {token}")

    def parse_float(token: str) -> float:
        value = float(token)
        if not math.isfinite(value):
            raise ValueError(f"JSON number {token} overflows")
        return value

    return json.loads(text, parse_constant=reject, parse_float=parse_float)


def read_json_rows(name: str) -> list:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))["trials"]


def read_jsonl_outcome(name: str) -> tuple:
    """('rows', rows) from a JSONL file read strictly, or ('refused', None) on a non-finite
    number."""
    try:
        rows = [
            strict_loads(line)
            for line in (FIXTURE_DIR / name).read_text(encoding="utf-8").split("\n")
            if line.strip()
        ]
    except ValueError:
        return ("refused", None)
    return ("rows", rows)


def rows_of_vectors(vectors: list, directions: tuple) -> tuple:
    names = ["abc"[index] for index in range(len(directions))]
    axes = [{"name": name, "direction": direction} for name, direction in zip(names, directions)]
    rows = [
        {
            "trial_id": index,
            "status": "ok",
            "feasible": True,
            "objective": 1.0,
            "metrics": dict(zip(names, vector)),
        }
        for index, vector in enumerate(vectors)
    ]
    return rows, axes


def make_row(trial_id: int, status: str, metrics) -> dict:
    row = {
        "trial_id": trial_id,
        "status": status,
        "feasible": status == "ok",
        "objective": 1.0 if status in ("ok", "constraint_violation") else None,
    }
    if metrics is not None:
        row["metrics"] = metrics
    return row


# Values that refuse the call when an ok row holds one in an axis cell (clause 7: a boolean, text,
# an array, an object, NaN, an infinity or an overflowing integer).
BAD_VALUES = (
    True,
    False,
    "10.0",
    [1.0],
    {"a": 1},
    float("nan"),
    float("inf"),
    float("-inf"),
    10**400,
)


def eligibility_cases() -> list:
    """Hand cases for the eligibility table of the contract (clause 3): (label, rows, axes,
    expected, source), each derived by hand from that table. An absent key beats a null value
    ("the first reason that applies"), the axes named under a metric reason are those of the
    rows excluded for that reason and are listed in the order of the request, and a row that is
    not ok is excluded under its status. In both ``mixed`` and ``whole`` the rows 0 (1, 2) and 3
    (2, 1) are the only eligible ones, 3 dominates 0 (profit 2 > 1 and drawdown 1 < 2), so the
    front is [3], the counts are {0: 1, 3: 0}, one distinct vector, and the figure is the rows
    read plus 1 pair."""
    mixed = [
        make_row(0, "ok", {PROFIT: 1.0, DRAWDOWN: 2.0}),
        make_row(1, "ok", {PROFIT: None}),
        make_row(2, "ok", {PROFIT: 3.0, DRAWDOWN: None}),
        make_row(3, "ok", {PROFIT: 2.0, DRAWDOWN: 1.0}),
        make_row(4, "pruned", None),
    ]
    whole = [
        make_row(0, "ok", {PROFIT: 1.0, DRAWDOWN: 2.0}),
        make_row(1, "ok", {}),
        make_row(2, "ok", {PROFIT: None, DRAWDOWN: None}),
        make_row(3, "ok", {PROFIT: 2.0, DRAWDOWN: 1.0}),
    ]
    front = {"front": [3], "n_front": 1, "distinct": 1, "counts": {0: 1, 3: 0}}
    source = "contract clause 3, derived by hand in this file"

    def whole_excluded(order: list) -> dict:
        return {
            "metric_not_recorded": {"count": 1, "ids": [1], "axes": order},
            "metric_unavailable": {"count": 1, "ids": [2], "axes": order},
        }

    return [
        (
            "row 1 lacks the drawdown key and has a null profit (absent wins), row 2 a null "
            "drawdown",
            mixed,
            AXES,
            {
                **front,
                "rows": (5, 2, 3),
                "excluded": {
                    "metric_not_recorded": {"count": 1, "ids": [1], "axes": [DRAWDOWN]},
                    "metric_unavailable": {"count": 1, "ids": [2], "axes": [DRAWDOWN]},
                    "status_pruned": {"count": 1, "ids": [4]},
                },
                "figure": 6,
            },
            source,
        ),
        (
            "an empty metrics object and an all-null one, axes (profit, drawdown)",
            whole,
            AXES,
            {
                **front,
                "rows": (4, 2, 2),
                "excluded": whole_excluded([PROFIT, DRAWDOWN]),
                "figure": 5,
            },
            source,
        ),
        (
            "the same rows with the axes requested the other way round (drawdown, profit)",
            whole,
            list(reversed(AXES)),
            {
                **front,
                "rows": (4, 2, 2),
                "excluded": whole_excluded([DRAWDOWN, PROFIT]),
                "figure": 5,
            },
            source,
        ),
    ]


# ---------------------------------------------------------------------------------------------
# Phase 1 sections (no module).
# ---------------------------------------------------------------------------------------------


def section_fixtures(ledger: Ledger) -> None:
    ledger.begin(
        "PF1-PF3 recomputed from the fixture files (signs, componentwise, sort-and-filter)"
    )
    for name, axes, expected in FIXTURE_CASES:
        document = oracle_front(read_json_rows(name), axes)
        source = expected["source"]
        for label, value, literal in (
            ("front", document["pareto_front"], expected["front"]),
            ("n_front", document["n_front"], expected["n_front"]),
            (
                "distinct front vectors",
                document["n_distinct_vectors_on_front"],
                expected["distinct"],
            ),
            ("domination counts", document["domination_counts"], expected["counts"]),
            (
                "(rows read, eligible, excluded)",
                (document["rows_read"], document["rows_eligible"], document["rows_excluded"]),
                expected["rows"],
            ),
            ("excluded", document["excluded"], expected["excluded"]),
            ("admission figure, rows read + N(N-1)/2", document["figure"], expected["figure"]),
        ):
            ledger.compare("fixture", f"{name}: {label}", value, literal, source)
        ledger.compare(
            "internal",
            f"{name}: the three dominance formulations agree",
            document["formulations_agree"],
            True,
        )
    ledger.end(8 * len(FIXTURE_CASES))


def section_invariance_and_files(ledger: Ledger) -> None:
    ledger.begin("PF4 invariance and the JSONL variants of PF1")
    source = "test_pareto.py WorkedFixtureTests.test_pf4_invariance_rows_reversed_and_axes_reversed"
    moved = oracle_front(list(reversed(read_json_rows("pf1.json"))), list(reversed(AXES)))
    ledger.compare(
        "fixture", "PF4: front, rows and axes reversed", moved["pareto_front"], PF1["front"], source
    )
    ledger.compare(
        "fixture",
        "PF4: counts, rows and axes reversed",
        moved["domination_counts"],
        PF1["counts"],
        source,
    )
    ledger.compare(
        "fixture",
        "PF4: exclusions, rows and axes reversed",
        moved["excluded"],
        PF1["excluded"],
        source,
    )
    ledger.compare(
        "fixture",
        "pf1.jsonl read strictly holds the rows of pf1.json",
        read_jsonl_outcome("pf1.jsonl"),
        ("rows", read_json_rows("pf1.json")),
        "test_pareto.py WorkedFixtureTests.test_pf1_files_are_pf1_with_one_change",
    )
    for name in ("pf1_nan.jsonl", "pf1_overflow.jsonl"):
        ledger.compare(
            "fixture",
            f"{name}: a strict read refuses (NaN token, overflowing number)",
            read_jsonl_outcome(name)[0],
            "refused",
            "test_pareto.py SourceRefusalTests.test_nan_token_in_a_file_is_invalid_input and "
            "test_overflowing_number_in_a_file_is_invalid_input",
        )
    ledger.end(6)


def section_dominance(ledger: Ledger) -> None:
    ledger.begin("dominance rules on the hand cases (ties, equal vectors, -0.0, exact doubles)")
    for label, vectors, directions, front, counts, distinct, source in DOMINANCE_CASES:
        rows, axes = rows_of_vectors(vectors, directions)
        document = oracle_front(rows, axes)
        ledger.compare("fixture", f"{label}: front", document["pareto_front"], front, source)
        ledger.compare(
            "fixture", f"{label}: domination counts", document["domination_counts"], counts, source
        )
        ledger.compare(
            "fixture",
            f"{label}: distinct front vectors",
            document["n_distinct_vectors_on_front"],
            distinct,
            source,
        )
        ledger.compare(
            "internal",
            f"{label}: the three dominance formulations agree",
            document["formulations_agree"],
            True,
        )
    ledger.end(4 * len(DOMINANCE_CASES))


def section_eligibility(ledger: Ledger) -> None:
    ledger.begin(
        "eligibility table of the contract: absent beats null, axes in request order (P2-5)"
    )
    cases = eligibility_cases()
    for label, rows, axes, expected, source in cases:
        document = oracle_front(rows, axes)
        for key, value, literal in (
            ("front", document["pareto_front"], expected["front"]),
            ("n_front", document["n_front"], expected["n_front"]),
            (
                "distinct front vectors",
                document["n_distinct_vectors_on_front"],
                expected["distinct"],
            ),
            ("domination counts", document["domination_counts"], expected["counts"]),
            (
                "(rows read, eligible, excluded)",
                (document["rows_read"], document["rows_eligible"], document["rows_excluded"]),
                expected["rows"],
            ),
            ("excluded", document["excluded"], expected["excluded"]),
            ("admission figure, rows read + N(N-1)/2", document["figure"], expected["figure"]),
        ):
            ledger.compare("fixture", f"{label}: {key}", value, literal, source)
        ledger.compare(
            "internal",
            f"{label}: the three dominance formulations agree",
            document["formulations_agree"],
            True,
        )
    ledger.end(8 * len(cases))


def section_refusals(ledger: Ledger) -> None:
    ledger.begin(
        "refusals and non-refusals the contract states (clauses 3 and 7), recomputed by the oracle"
    )
    note = "contract clause 7 (refusal fixtures), derived by hand in this file"
    typo = "metrics.all.net_profit_pc"
    base = read_json_rows("pf1.json")
    ledger.compare(
        "fixture",
        "one misspelled axis: (status, code, ids, names)",
        outcome_view(oracle_front(base, [{"name": typo, "direction": "maximize"}, AXES[1]])),
        ("refused", "pareto_axis_unknown", (), (typo,)),
        note,
    )
    ledger.compare(
        "fixture",
        "two misspelled axes, named in the order of the request",
        outcome_view(
            oracle_front(
                base,
                [
                    {"name": typo, "direction": "maximize"},
                    {"name": "nope", "direction": "minimize"},
                ],
            )
        ),
        ("refused", "pareto_axis_unknown", (), (typo, "nope")),
        note,
    )
    errored = copy.deepcopy(base)
    for row in errored:
        row.update(status="engine_error", feasible=False, objective=None)
    ledger.compare(
        "fixture",
        "every row an engine error: no row remains",
        outcome_view(oracle_front(errored, AXES)),
        ("refused", "pareto_no_eligible_rows", (), ()),
        note,
    )
    for value in BAD_VALUES:
        rows = copy.deepcopy(base)
        rows[0]["metrics"][PROFIT] = value
        ledger.compare(
            "fixture",
            f"row 0 profit {value!r:.20} on an ok row: the offending id and axis",
            outcome_view(oracle_front(rows, AXES)),
            ("refused", "pareto_input_invalid", (0,), (PROFIT,)),
            note,
        )
    # Only an ok row's value can refuse (clause 3: "an ok row whose axis value has another type"):
    # a text value on the engine_error row 7 changes nothing, the row stays excluded under its
    # status.
    stray = copy.deepcopy(base)
    stray[7]["metrics"][PROFIT] = "n/a"
    document = oracle_front(stray, AXES)
    clause = "contract clause 3: only an ok row's axis value can refuse"
    ledger.compare(
        "fixture",
        "text on the engine_error row 7: the front is unchanged",
        document["pareto_front"],
        PF1["front"],
        clause,
    )
    ledger.compare(
        "fixture",
        "text on the engine_error row 7: the exclusions are unchanged",
        document["excluded"],
        PF1["excluded"],
        clause,
    )
    ledger.end(3 + len(BAD_VALUES) + 2)


def section_controls(ledger: Ledger) -> None:
    ledger.begin("controls: the comparison can fail, and the oracle follows a changed input")
    must_differ = [
        (3, 4),
        (0.5, 0.25),
        (Fraction(1, 2), Fraction(1, 3)),
        (True, 1),
        ([1, 2], [1, 3]),
        ({"a": 1}, {"a": 2}),
        (None, 0),
        ("a", "b"),
        ((1,), (1, 2)),
        (float("nan"), float("nan")),
    ]
    must_match = [
        (1, 1.0),
        (Fraction(1, 2), 0.5),
        ([1, (2, 3)], (1, [2, 3])),
        ({"a": [1]}, {"a": (1,)}),
        (None, None),
        (True, True),
    ]
    for left, right in must_differ:
        ledger.check(
            "control",
            f"same({show(left)}, {show(right)}) is False",
            same(left, right) is False,
        )
    for left, right in must_match:
        ledger.check(
            "control",
            f"same({show(left)}, {show(right)}) is True",
            same(left, right) is True,
        )
    # PF1 with row 9 changed from (6, 2) to (13, 2). By hand: row 9 then dominates every other
    # eligible row (13 >= each profit, 2 <= each drawdown, strict in one); among the others 0
    # still dominates 3 and 2 still dominates 5. Dominators: 0 {9}, 1 {9}, 2 {9}, 3 {0, 9},
    # 4 {9}, 5 {2, 9}, 9 {}. Front [9], one distinct vector.
    rows = copy.deepcopy(read_json_rows("pf1.json"))
    rows[9]["metrics"][PROFIT] = 13.0
    document = oracle_front(rows, AXES)
    note = "derived by hand in this file"
    ledger.compare(
        "control", "PF1 with row 9 = (13, 2): front", document["pareto_front"], [9], note
    )
    ledger.compare(
        "control",
        "PF1 with row 9 = (13, 2): domination counts",
        document["domination_counts"],
        {0: 1, 1: 1, 2: 1, 3: 2, 4: 1, 5: 2, 9: 0},
        note,
    )
    ledger.compare(
        "control",
        "PF1 with row 9 = (13, 2): (n_front, distinct front vectors)",
        (document["n_front"], document["n_distinct_vectors_on_front"]),
        (1, 1),
        note,
    )
    ledger.end(len(must_differ) + len(must_match) + 3)


# ---------------------------------------------------------------------------------------------
# Seeded random instances.
# ---------------------------------------------------------------------------------------------


def random_instance(rng: random.Random, number: int) -> tuple:
    axes = [
        {"name": f"m{index}", "direction": rng.choice(("maximize", "minimize"))}
        for index in range(rng.choice((2, 3, 4)))
    ]
    statuses = ["ok"] * 6 + ["pruned", "constraint_violation", "engine_error", "partial"]
    rows = []
    for trial_id in rng.sample(range(200), rng.randint(1, 14)):
        metrics = {}
        for axis in axes:
            draw = rng.random()
            if draw < 0.07:
                continue
            if draw < 0.14:
                metrics[axis["name"]] = None
                continue
            value = rng.randint(0, 3)
            metrics[axis["name"]] = rng.choice(
                (value, float(value), -0.0 if value == 0 else float(value))
            )
        rows.append(
            make_row(trial_id, rng.choice(statuses), metrics if rng.random() < 0.95 else None)
        )
    if number % 11 == 4:  # one axis recorded nowhere
        gone = rng.choice(axes)["name"]
        for row in rows:
            row.get("metrics", {}).pop(gone, None)
    if number % 17 == 6:  # nothing eligible
        for row in rows:
            row.update(status="pruned", feasible=False, objective=None)
    return axes, rows


def generate_instances(rng: random.Random, count: int) -> list:
    instances = []
    for number in range(count):
        axes, rows = random_instance(rng, number)
        instances.append((axes, rows, oracle_front(rows, axes)))
    return instances


def section_random_internal(ledger: Ledger, instances: list, seed: int) -> None:
    ledger.begin(
        f"seeded random instances (seed {seed}): the three formulations against each other"
    )
    outcomes = {"front": 0, "pareto_axis_unknown": 0, "pareto_no_eligible_rows": 0}
    checked = 0
    for number, (_axes, _rows, document) in enumerate(instances):
        if document["status"] == "ok":
            outcomes["front"] += 1
            checked += 1
            ledger.compare(
                "internal",
                f"instance {number}: the three dominance formulations agree",
                document["formulations_agree"],
                True,
                quiet=True,
            )
        else:
            outcomes[document["code"]] = outcomes.get(document["code"], 0) + 1
    ledger.note(f"outcomes {outcomes}")
    if outcomes["front"] < len(instances) // 3:
        ledger.fail("too few random instances produced a front to be meaningful")
    if not outcomes["pareto_axis_unknown"] or not outcomes["pareto_no_eligible_rows"]:
        ledger.fail("the random instances never exercised both refusals")
    ledger.end(checked)


# ---------------------------------------------------------------------------------------------
# Phase 2: the module, imported only now.
# ---------------------------------------------------------------------------------------------


def load_module():
    sys.path.insert(0, str(ROOT / "python"))
    sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))
    from pineforge_hpo import pareto

    return pareto


def module_call(pareto, source, axes, **options) -> dict:
    """The module's document, or its refusal; only the module's own refusal type is caught."""
    try:
        return pareto.pareto_front(source, axes, **options)
    except pareto.ParetoError as error:
        return refusal(error.code, error.ids, error.names)


def module_fixtures(ledger: Ledger, pareto) -> None:
    ledger.begin("module: PF1-PF3 from the fixture files, and the admission figure")
    for name, axes, expected in FIXTURE_CASES:
        document = oracle_front(read_json_rows(name), axes)
        oracle = view_of(document)
        found = module_call(pareto, FIXTURE_DIR / name, axes, with_domination_counts=True)
        if found["status"] != "ok":
            ledger.fail(f"{name}: the module refused: {outcome_view(found)}")
            view = {key: None for key in oracle}
        else:
            view = view_of(found)
        for key in ("front", "n_front", "distinct", "counts", "rows", "excluded"):
            ledger.compare("module", f"{name}: {key}", oracle[key], view[key], expected["source"])
        # the oracle's own figure (phase 1 compares it with the pinned one)
        figure = document["figure"]
        at_figure = module_call(pareto, FIXTURE_DIR / name, axes, max_work=figure)
        below = module_call(pareto, FIXTURE_DIR / name, axes, max_work=figure - 1)
        ledger.compare(
            "module",
            f"{name}: max_work = {figure} runs and {figure - 1} refuses as resource_exceeded",
            (True, "pareto_resource_exceeded"),
            (at_figure["status"] == "ok", below.get("code")),
            expected["source"],
        )
    ledger.end(7 * len(FIXTURE_CASES))


def module_invariance(ledger: Ledger, pareto) -> None:
    ledger.begin("module: PF4, rows and axes reversed")
    rows = list(reversed(read_json_rows("pf1.json")))
    axes = list(reversed(AXES))
    oracle = view_of(oracle_front(rows, axes))
    found = module_call(pareto, rows, axes, with_domination_counts=True)
    view = view_of(found) if found["status"] == "ok" else {key: None for key in oracle}
    for key in ("front", "counts", "excluded"):
        ledger.compare(
            "module",
            f"PF4: {key}",
            oracle[key],
            view[key],
            "test_pareto.py WorkedFixtureTests.test_pf4_invariance_rows_reversed_and_axes_reversed",
        )
    ledger.end(3)


def module_dominance(ledger: Ledger, pareto) -> None:
    ledger.begin("module: the dominance hand cases")
    for label, vectors, directions, _front, _counts, _distinct, source in DOMINANCE_CASES:
        rows, axes = rows_of_vectors(vectors, directions)
        oracle = view_of(oracle_front(rows, axes))
        found = module_call(pareto, rows, axes, with_domination_counts=True)
        view = view_of(found) if found["status"] == "ok" else {key: None for key in oracle}
        for key in ("front", "counts", "distinct"):
            ledger.compare("module", f"{label}: {key}", oracle[key], view[key], source)
    ledger.end(3 * len(DOMINANCE_CASES))


def module_eligibility(ledger: Ledger, pareto) -> None:
    ledger.begin("module: the eligibility hand cases (absent beats null, axes in request order)")
    cases = eligibility_cases()
    for label, rows, axes, expected, source in cases:
        oracle = view_of(oracle_front(rows, axes))
        found = module_call(pareto, rows, axes, with_domination_counts=True)
        if found["status"] != "ok":
            ledger.fail(f"{label}: the module refused: {outcome_view(found)}")
            view = {key: None for key in oracle}
        else:
            view = view_of(found)
        for key in ("front", "n_front", "distinct", "counts", "rows", "excluded"):
            ledger.compare("module", f"{label}: {key}", oracle[key], view[key], source)
        figure = expected["figure"]
        at_figure = module_call(pareto, rows, axes, max_work=figure)
        below = module_call(pareto, rows, axes, max_work=figure - 1)
        ledger.compare(
            "module",
            f"{label}: max_work = {figure} runs and {figure - 1} refuses as resource_exceeded",
            (True, "pareto_resource_exceeded"),
            (at_figure["status"] == "ok", below.get("code")),
            source,
        )
    ledger.end(7 * len(cases))


def module_refusals(ledger: Ledger, pareto) -> None:
    ledger.begin("module: refusals")
    typo = "metrics.all.net_profit_pc"
    base = read_json_rows("pf1.json")

    def with_profit(value) -> list:
        rows = copy.deepcopy(base)
        rows[0]["metrics"][PROFIT] = value
        return rows

    errored = copy.deepcopy(base)
    for row in errored:
        row.update(status="engine_error", feasible=False, objective=None)
    best_k = json.loads((FIXTURE_DIR / "pf1.json").read_text(encoding="utf-8"))
    best_k["trials_out"] = "best-k"
    stray = copy.deepcopy(base)
    stray[7]["metrics"][PROFIT] = "n/a"

    # (label, outcome from the module, expected outcome, source)
    table = [
        (
            "NaN token in a file (code only)",
            module_call(pareto, FIXTURE_DIR / "pf1_nan.jsonl", AXES).get("code"),
            "pareto_input_invalid",
            "test_pareto.py SourceRefusalTests.test_nan_token_in_a_file_is_invalid_input",
        ),
        (
            "overflowing number in a file (code only)",
            module_call(pareto, FIXTURE_DIR / "pf1_overflow.jsonl", AXES).get("code"),
            "pareto_input_invalid",
            "test_pareto.py SourceRefusalTests.test_overflowing_number_in_a_file_is_invalid_input",
        ),
        (
            "one misspelled axis",
            outcome_view(
                module_call(pareto, base, [{"name": typo, "direction": "maximize"}, AXES[1]])
            ),
            outcome_view(oracle_front(base, [{"name": typo, "direction": "maximize"}, AXES[1]])),
            "test_pareto.py AxisAndEligibilityRefusalTests.test_misspelled_axis_is_unknown",
        ),
        (
            "two misspelled axes",
            outcome_view(
                module_call(
                    pareto,
                    base,
                    [
                        {"name": typo, "direction": "maximize"},
                        {"name": "nope", "direction": "minimize"},
                    ],
                )
            ),
            outcome_view(
                oracle_front(
                    base,
                    [
                        {"name": typo, "direction": "maximize"},
                        {"name": "nope", "direction": "minimize"},
                    ],
                )
            ),
            "test_pareto.py AxisAndEligibilityRefusalTests.test_misspelled_axis_is_unknown",
        ),
        (
            "every row an engine error",
            outcome_view(module_call(pareto, errored, AXES)),
            outcome_view(oracle_front(errored, AXES)),
            "test_pareto.py "
            "AxisAndEligibilityRefusalTests.test_every_row_engine_error_has_no_eligible_rows",
        ),
        (
            "a best-k result (code only)",
            module_call(pareto, best_k, AXES).get("code"),
            "pareto_rows_incomplete",
            "test_pareto.py SourceRefusalTests.test_best_k_result_is_incomplete",
        ),
        (
            "axes omitted (code only)",
            module_call(pareto, base, None).get("code"),
            "pareto_parameter_missing",
            "test_pareto.py ParameterRefusalTests.test_axes_omitted_is_missing",
        ),
        (
            "direction 'max' (code only)",
            module_call(
                pareto, base, [AXES[0], {"name": DRAWDOWN, "direction": "max"}]
            ).get("code"),
            "pareto_parameter_invalid",
            "test_pareto.py ParameterRefusalTests.test_direction_words_are_exact",
        ),
        (
            "one axis only (code only)",
            module_call(pareto, base, [AXES[0]]).get("code"),
            "pareto_parameter_invalid",
            "test_pareto.py ParameterRefusalTests.test_one_axis_is_invalid",
        ),
        (
            "max_work 1 against the figure 31 (code only)",
            module_call(pareto, base, AXES, max_work=1).get("code"),
            "pareto_resource_exceeded",
            "test_pareto.py ResourceTests.test_max_work_one_refuses_pf1",
        ),
        (
            "text on the engine_error row 7 is not refused (only an ok row's value can refuse)",
            outcome_view(module_call(pareto, stray, AXES, with_domination_counts=True)),
            outcome_view(oracle_front(stray, AXES)),
            "contract clause 3",
        ),
    ]
    for label, found, expected, source in table:
        ledger.compare("module", f"refusal: {label}", expected, found, source)
    for value in BAD_VALUES:
        rows = with_profit(value)
        ledger.compare(
            "module",
            f"refusal: row 0 profit {value!r:.20} on an ok row",
            outcome_view(oracle_front(rows, AXES)),
            outcome_view(module_call(pareto, rows, AXES)),
            "test_pareto.py "
            "ValueRefusalTests.test_ok_row_axis_value_of_another_type_is_invalid_input",
        )
    ledger.end(len(table) + len(BAD_VALUES))


def module_random(ledger: Ledger, pareto, instances: list, seed: int) -> None:
    ledger.begin(
        f"module: the seeded random instances (seed {seed}), as given and shuffled with "
        "reversed axes"
    )
    shuffler = random.Random(seed + 1)
    for number, (axes, rows, document) in enumerate(instances):
        found = module_call(pareto, rows, axes, with_domination_counts=True)
        ledger.compare(
            "module",
            f"instance {number}: {len(rows)} rows, {len(axes)} axes",
            outcome_view(document),
            outcome_view(found),
            quiet=True,
        )
        moved_rows = list(rows)
        shuffler.shuffle(moved_rows)
        moved_axes = list(reversed(axes))
        moved = module_call(pareto, moved_rows, moved_axes, with_domination_counts=True)
        ledger.compare(
            "module",
            f"instance {number}: rows shuffled, axes reversed",
            outcome_view(oracle_front(rows, moved_axes)),
            outcome_view(moved),
            quiet=True,
        )
    ledger.end(2 * len(instances))


def phase_two(ledger: Ledger, instances: list, seed: int) -> None:
    print("\n##### phase 2: the module under test")
    ledger.begin("independence: nothing of the package was imported before phase 2")
    ledger.check(
        "internal",
        "'pineforge_hpo' is not in sys.modules before phase 2",
        "pineforge_hpo" not in sys.modules,
    )
    ledger.end(1)
    pareto = load_module()
    module_fixtures(ledger, pareto)
    module_invariance(ledger, pareto)
    module_dominance(ledger, pareto)
    module_eligibility(ledger, pareto)
    module_refusals(ledger, pareto)
    module_random(ledger, pareto, instances, seed)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Independent oracle for pineforge_hpo.pareto")
    parser.add_argument(
        "--no-module",
        action="store_true",
        help="run phase 1 only (the checker against the pinned fixture values); the module is "
        "not imported, the verdict says so and the exit code is 3, never 0",
    )
    parser.add_argument(
        "--cases", type=int, default=300, help="seeded random instances (default 300)"
    )
    parser.add_argument("--seed", type=int, default=20261011, help="random seed (default 20261011)")
    args = parser.parse_args(argv)
    if args.cases < 30:
        parser.error("--cases must be at least 30")
    ledger = Ledger(strict=True)
    print(
        f"pareto oracle: root {ROOT}, python {sys.version.split()[0]}, "
        f"cases {args.cases}, seed {args.seed}"
    )
    harness_self_test()
    print("##### phase 1: the checker alone (no module imported)")
    section_fixtures(ledger)
    section_invariance_and_files(ledger)
    section_dominance(ledger)
    section_eligibility(ledger)
    section_refusals(ledger)
    section_controls(ledger)
    instances = generate_instances(random.Random(args.seed), args.cases)
    section_random_internal(ledger, instances, args.seed)
    if args.no_module:
        print("\n##### phase 2 NOT RUN (--no-module)")
    else:
        phase_two(ledger, instances, args.seed)
    print("\nSUMMARY")
    for kind in ("fixture", "internal", "control", "module"):
        print(f"  {kind:8s} comparisons: {ledger.counts.get(kind, 0)}")
    print(f"  failed comparisons and checks: {len(ledger.failures)}")
    for item in ledger.failures:
        print(f"   - {item}")
    if ledger.failures:
        print("RESULT: FAIL")
        return 1
    if args.no_module:
        print(
            f"RESULT: PHASE 1 ONLY (exit {NO_MODULE_EXIT}): the checker agrees with the pinned "
            "values, but the module was NOT run, so no comparison with the module was made and "
            "nothing here supports calling a G2 result accepted"
        )
        return NO_MODULE_EXIT
    print("RESULT: PASS (checker agrees with the pinned values and with the module)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
