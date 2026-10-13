#!/usr/bin/env python3
"""Independent oracle for the holdout comparison of two finished studies.

The module joins two sets of finished rows by the exact parameter vector and
reports what the second source recorded for the vectors the first scored best.
This script never takes an expected number from the module:

* it reads parsed result objects and lists of rows itself. Its reader layer
  reproduces the shared reader, and eligibility uses the same per-row ladder
  as the module. File layout dispatch is not reproduced;
* it rebuilds canonical join keys for categorical, boolean, integer and real
  dimensions instead of calling the package's key builder;
* selection ranks come from counting vectors with a better value, or an equal
  value and lower representative trial id. Counting and sorting routes are
  checked against each other;
* pool and correlation mid-ranks come from enumeration: a value's tie group
  starts at ``p = 1 + #{strictly better}`` and ends at ``q = #{not worse}``,
  giving the exact ``Fraction`` mid-rank ``(p + q) / 2``;
* the three correlation terms use the two-pass deviations form:
  ``d_x = x - SUM(x)/n``, then ``n * SUM(d_x*d_y)`` and its two companions.
  These exact fractions are compared with the module's one-pass terms;
* the binary64 correlation comes from square roots of the exact ratio's
  numerator and denominator in ``decimal`` at 50 significant digits, followed
  by one binary64 rounding and the sign of ``Sxy``. The module rounds the
  rational first, then takes ``math.sqrt``. Only this field allows one ulp,
  measured at the larger magnitude; exact terms and ranks must agree exactly;
* the better-half share is counted in ``Fraction``s. The digest is rebuilt
  from compact, key-sorted, ASCII JSON of each row's source index, trial id,
  status, value and vector key, hashing each row, sorting and hashing the blob.

The independent routes are the arithmetic and ranks, not the eligibility ladder.
Phase 1 recomputes the worked fixtures and compares them with typed pins, without
importing the package. Phase 2 imports ``pineforge_hpo.holdout``, compares the
module's documents with the oracle's, and repeats this on seeded random inputs
with two dimensions, repeated vectors, ties and six statuses.

Five controls mutate only the oracle's copy. Four change fixture inputs; the
fifth moves only its computed Spearman by two ulps through the same comparison
used for random documents. The module receives unmutated inputs. Control checks
print AGREE when they detect the expected disagreement.

Usage: ``python3 tests/g3_oracles/holdout_oracle.py`` with optional
``--no-module``, ``--cases N`` and ``--seed S``.
Exit codes: 0 for a full pass including module comparisons, 1 for a failure,
2 for a usage error, and 3 for phase 1 only with ``--no-module``.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import random
import struct
import sys

# Write nothing: no bytecode cache beside this script or inside the package that phase 2 imports.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
# Exit code of a run that did not compare the module (--no-module); 0 is kept for a full pass.
NO_MODULE_EXIT = 3

CONTRACT = "pineforge-hpo-holdout/v1"
ALGORITHM = "exact_join_midrank_v1"
DIRECTIONS = ("maximize", "minimize")
ID_LIMIT = (1 << 64) - 1
COUNTERS = (
    "input_bars_processed",
    "script_bars_processed",
    "input_tf_seconds",
    "script_tf_seconds",
)
# A row whose status is not ok is excluded under the reason named here.
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
STATUSES = ("ok", *STATUS_REASONS)

# The fixed interpretation text is compared as a constant. Agreement shows that
# the module carries this text, not that the text is right.
INTERPRETATION = (
    "(a) The module splits no data and runs no trial: it reads two sets of rows that "
    "two finished studies already wrote, and joins them by the exact parameter vector. "
    "It cannot know what data either study used, so a value recorded by the second "
    "study is only as held-out as the caller made it. "
    "(b) A study is a holdout only when the caller ran it on data set aside before "
    "the selection rows were read; the module cannot check that, and reading the best "
    "selection rows while the second study's values are in view contaminates it. "
    "(c) Equality of parameter vectors is exact: no tolerance and no lattice snapping, "
    "so a near-equal vector is a different vector and is reported unmatched, never "
    "merged. "
    "(d) No significance test, no threshold and no verdict: a few vectors give a noisy "
    "rank correlation, and the module states no probability, interval or error bar. "
    "(e) The selection side is the best of many trials, so its values are optimistic; "
    "every number is in-sample for the selection source and only as held-out as the "
    "caller made it for the other. "
    "(f) v1 analyses finished studies; native in-loop walk-forward comes later. "
    "(g) The scoring-context check is necessary, not sufficient, and it runs on each "
    "source on its own, never between the two studies: a space hash names the "
    "parameter space and the objective, not the data, so an equal hash says nothing "
    "about the data, and this module requires the two studies to share that space, "
    "so their hashes are normally equal and can differ only through a space-hash "
    "version change. "
    "(h) When a source records no space, its rows are keyed with the space the other "
    "source records or the caller passes, so the module cannot tell whether the "
    "second study ran in that space. "
    "(i) A complete result may carry the ancestor rows of a study it continued from: "
    "the module counts them for each source, reports the counts, and refuses a "
    "holdout source whose ancestor rows repeat a trial id of the selection source, "
    "because such a source may be reporting the selection study's own rows; an "
    "overlap that the trial ids do not show cannot be detected, and an id that "
    "repeats by a coincidence of numbering is refused like a real one. "
    "(j) No claim is made about the data of either source, about the run that produced "
    "it, or about any strategy."
)


# ---------------------------------------------------------------------------------------------
# Harness: every comparison is counted and printed, a disagreement is recorded and never
# swallowed.
# ---------------------------------------------------------------------------------------------


def same(a, b, strict: bool = False) -> bool:
    """Exact equality: numbers by value (int, float and Fraction mix; with ``strict`` the types
    must match too), bool only with bool, containers element by element (list and tuple are
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
            "{"
            + ", ".join(f"{show(k, limit)}: {show(v, limit)}" for k, v in value.items())
            + "}"
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
        self,
        kind: str,
        label: str,
        checker,
        other,
        source: str = "",
        *,
        quiet: bool = False,
    ) -> bool:
        """Compare the oracle's value with ``other`` (kind: fixture, internal, control, module)."""
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
            f"{status} {kind:8s} {label} | oracle={show(checker)} | "
            f"{kind}={show(other)}{where}"
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
            raise SystemExit(
                f"harness self-test failed: same({left!r}, {right!r}) must be True"
            )
    for left, right in differing:
        if same(left, right) is not False:
            raise SystemExit(
                f"harness self-test failed: same({left!r}, {right!r}) must be False"
            )
    for left, right in (
        (1, 1.0),
        (Fraction(1, 2), 0.5),
        (1.0, Fraction(1)),
        ({3: 1}, {3.0: 1}),
    ):
        if same(left, right, True) is not False:
            raise SystemExit(
                "harness self-test failed: "
                f"strict same({left!r}, {right!r}) must be False"
            )
    if (
        same(1.5, 1.5, True) is not True
        or same([1, 2.0], (1, 2.0), True) is not True
        or same({3: 1.5}, {3: 1.5}, True) is not True
    ):
        raise SystemExit(
            "harness self-test failed: strict same must accept equal values "
            "of equal types"
        )
    probe = Ledger(echo=False)
    probe.name = "probe"
    probe.compare("control", "equal", 1, 1)
    probe.compare("control", "different", 1, 2)
    if (
        probe.failures != ["probe: different"]
        or probe.counts != {"control": 2}
        or probe.made != 2
    ):
        raise SystemExit(
            "harness self-test failed: the ledger must record exactly the one failure"
        )
    strict_probe = Ledger(echo=False, strict=True)
    strict_probe.name = "strict probe"
    strict_probe.compare("control", "int against float", 1, 1.0)
    if strict_probe.failures != ["strict probe: int against float"]:
        raise SystemExit(
            "harness self-test failed: a strict ledger must reject an int "
            "against a float"
        )
    print(
        "harness self-test passed (same() and the ledger tell different values apart)"
    )


# ---------------------------------------------------------------------------------------------
# Worked fixtures are built here as parsed lists of rows or parsed result objects.
# No fixture file is read.
# ---------------------------------------------------------------------------------------------

ARM_CHOICES = ("A", "B", "C", "D", "E", "F")
S1 = {
    "parameters": {"arm": {"kind": "categorical", "choices": list(ARM_CHOICES)}},
    "objective": {
        "expression": "metrics.all.net_profit_pct",
        "direction": "maximize",
        "constraints": [],
    },
}
S2 = {
    "parameters": {"arm": {"kind": "categorical", "choices": ["A", "B"]}},
    "objective": {
        "expression": "metrics.all.net_profit_pct",
        "direction": "maximize",
        "constraints": [],
    },
}
S1_NARROW = {
    "parameters": {"arm": {"kind": "categorical", "choices": ["A", "B"]}},
    "objective": {
        "expression": "metrics.all.net_profit_pct",
        "direction": "maximize",
        "constraints": [],
    },
}
S1_TWO_DIMENSIONS = {
    "parameters": {"a": {"kind": "categorical", "choices": ["A", "B"]}},
    "objective": {
        "expression": "metrics.all.net_profit_pct",
        "direction": "maximize",
        "constraints": [],
    },
}


def row(
    trial_id,
    status,
    objective,
    arm,
    metrics=None,
    parameters=None,
    backtest=None,
    space=None,
):
    """Build a row with a trial id, status, objective, arm and feasible flag."""
    raw = {
        "trial_id": trial_id,
        "status": status,
        "feasible": status == "ok",
        "objective": objective,
        "parameters": {"arm": arm} if parameters is None else parameters,
    }
    if metrics is not None:
        raw["metrics"] = metrics
    if backtest is not None:
        raw["backtest"] = backtest
    if space is not None:
        raw["space"] = space
    return raw


KEY_A = '{"arm":["string","A"]}'
KEY_B = '{"arm":["string","B"]}'
KEY_C = '{"arm":["string","C"]}'
KEY_D = '{"arm":["string","D"]}'
KEY_E = '{"arm":["string","E"]}'
KEY_G = '{"arm":["string","G"]}'

# The first worked fixture: six selection rows and five holdout rows.
BASE_SELECTION = [
    row(0, "ok", 10.0, "C"),
    row(1, "ok", 12.0, "B"),
    row(2, "ok", 9.0, "A"),
    row(3, "ok", 11.0, "E"),
    row(4, "ok", 12.0, "D"),
    row(5, "engine_error", None, "A"),
]
BASE_HOLDOUT = [
    row(11, "ok", 6.0, "F"),
    row(8, "ok", 4.0, "B"),
    row(7, "ok", 5.0, "A"),
    row(10, "constraint_violation", 99.0, "D"),
    row(9, "ok", 4.0, "C"),
]
# A named-metric fixture with two vectors, ranked under minimize.
MINIMIZE_SELECTION = [
    row(0, "ok", 1.0, "A", {"m": 1.0}),
    row(1, "ok", 2.0, "B", {"m": 2.0}),
]
MINIMIZE_HOLDOUT = [
    row(5, "ok", 9.0, "A", {"m": 3.0}),
    row(6, "ok", 9.0, "B", {"m": 0.5}),
]
# Four selection vectors, four holdout vectors, an even pool with a straddling tie group.
EVEN_SELECTION = [
    row(0, "ok", 4.0, "A"),
    row(1, "ok", 3.0, "B"),
    row(2, "ok", 2.0, "C"),
    row(3, "ok", 1.0, "D"),
]
EVEN_HOLDOUT = [
    row(7, "ok", 5.0, "A"),
    row(8, "ok", 4.0, "B"),
    row(9, "ok", 4.0, "C"),
    row(10, "ok", 1.0, "D"),
]
# The exclusion ladder for a named metric: an absent key, a null cell and no metrics object.
NAMED_SELECTION = [
    row(0, "ok", 1.0, "A", {"m": 1.0}),
    row(1, "ok", 1.0, "B", {}),
    row(2, "ok", 1.0, "C", {"m": None}),
    row(3, "ok", 1.0, "D"),
]
NAMED_HOLDOUT = [row(7, "ok", 1.0, "A", {"m": 2.0})]
# Two complete results that each record the space and no result-level direction.
RECORDED_SELECTION = {"trials": [row(0, "ok", 3.0, "A")], "space": S1}
RECORDED_HOLDOUT = {"trials": [row(7, "ok", 2.0, "A")], "space": S1}
# A constant holdout rank vector.
FLAT_H_SELECTION = [
    row(0, "ok", 3.0, "A"),
    row(1, "ok", 2.0, "B"),
    row(2, "ok", 1.0, "C"),
]
FLAT_H_HOLDOUT = [
    row(7, "ok", 1.0, "A"),
    row(8, "ok", 1.0, "B"),
    row(9, "ok", 1.0, "C"),
]
# A repeated vector whose two eligible rows agree, and the same pair with values that differ.
REPEAT_SELECTION = [row(0, "ok", 5.0, "A"), row(1, "ok", 5.0, "A")]
REPEAT_HOLDOUT = [row(7, "ok", 6.0, "A")]
REPEAT_DISAGREEING = [row(0, "ok", 5.0, "A"), row(1, "ok", 6.0, "A")]
# An ancestor row of the holdout source, and a pair whose ancestor ids overlap.
ANCESTOR_SELECTION = [row(0, "ok", 3.0, "A")]
ANCESTOR_HOLDOUT = {
    "warm_start_trials": [row(7, "ok", 1.0, "B")],
    "trials": [row(20, "ok", 2.0, "A")],
}
ANCESTOR_OVERLAP_SELECTION = {
    "warm_start_trials": [row(0, "ok", 9.0, "A")],
    "trials": [row(30, "ok", 3.0, "B")],
}
ANCESTOR_OVERLAP_HOLDOUT = {
    "warm_start_trials": [row(0, "ok", 9.0, "A")],
    "trials": [row(20, "ok", 2.0, "B")],
}
# The pool holds a vector of rank four that is not followed, so the pool mid-rank and the
# matched-set mid-rank of the holdout side are different numbers.
POOL_SELECTION = [
    row(0, "ok", 4.0, "A"),
    row(1, "ok", 3.0, "B"),
    row(2, "ok", 2.0, "C"),
    row(3, "ok", 1.0, "D"),
]
POOL_HOLDOUT = [
    row(7, "ok", 1.0, "A"),
    row(8, "ok", 3.0, "B"),
    row(9, "ok", 4.0, "C"),
    row(10, "ok", 2.0, "D"),
]
# A constant selection rank vector.
FLAT_S_SELECTION = [
    row(0, "ok", 5.0, "A"),
    row(1, "ok", 5.0, "B"),
    row(2, "ok", 5.0, "C"),
]
FLAT_S_HOLDOUT = [
    row(7, "ok", 1.0, "A"),
    row(8, "ok", 2.0, "B"),
    row(9, "ok", 3.0, "C"),
]


def follow(
    sel_id,
    key,
    sel_value,
    sel_rank,
    hol_id,
    hol_value,
    hol_rank,
    difference,
    improved,
):
    return {
        "selection_trial_id": sel_id,
        "vector_key": key,
        "selection_value": sel_value,
        "selection_rank": sel_rank,
        "holdout_trial_id": hol_id,
        "holdout_value": hol_value,
        "holdout_rank": hol_rank,
        "difference": difference,
        "improved": improved,
    }


def unmatch(sel_id, key, sel_value, sel_rank, reason, holdout_ids):
    return {
        "selection_trial_id": sel_id,
        "vector_key": key,
        "selection_value": sel_value,
        "selection_rank": sel_rank,
        "reason": reason,
        "holdout_ids": holdout_ids,
    }


def source_object(
    index,
    rows,
    ancestor_rows,
    rows_eligible,
    vectors_eligible,
    excluded,
):
    return {
        "index": index,
        "rows": rows,
        "ancestor_rows": ancestor_rows,
        "rows_eligible": rows_eligible,
        "vectors_eligible": vectors_eligible,
        "rows_excluded": rows - rows_eligible,
        "excluded": excluded,
        "space_hash": None,
    }


def stats(spearman, terms, ranks, undefined, numerator, denominator, value):
    return {
        "spearman": spearman,
        "spearman_terms": terms,
        "spearman_ranks": ranks,
        "spearman_undefined": undefined,
        "better_half": {
            "numerator": numerator,
            "denominator": denominator,
            "value": value,
        },
    }


def pool(size, cut, only_count, only_ids):
    return {
        "size": size,
        "median_rank_cut": cut,
        "holdout_only": {"count": only_count, "ids": only_ids},
    }


def admit(rows_read, selection_pairs, holdout_pairs, max_work=None):
    return {
        "rows_read": rows_read,
        "selection_pairs": selection_pairs,
        "holdout_pairs": holdout_pairs,
        "work": rows_read + selection_pairs + holdout_pairs,
        "max_work": max_work,
    }


BASE_SELECTION_OBJECT = source_object(
    0,
    6,
    0,
    5,
    5,
    {"status_engine_error": {"count": 1, "ids": [5]}},
)
BASE_HOLDOUT_OBJECT = source_object(
    1,
    5,
    0,
    4,
    4,
    {"status_constraint_violation": {"count": 1, "ids": [10]}},
)
BASE_FOLLOWED = [
    follow(1, KEY_B, 12.0, 1, 8, 4.0, [5, 2], -8.0, False),
    follow(0, KEY_C, 10.0, 4, 9, 4.0, [5, 2], -6.0, False),
    follow(2, KEY_A, 9.0, 5, 7, 5.0, [1, 1], -4.0, False),
]
BASE_UNMATCHED = [
    unmatch(4, KEY_D, 12.0, 2, "holdout_row_not_eligible", [10]),
    unmatch(3, KEY_E, 11.0, 3, "absent_in_holdout", []),
]
BASE_STATISTICS = stats(
    -0.8660254037844386,
    {"xy": [-9, 2], "xx": [6, 1], "yy": [9, 2]},
    {"x": [[1, 1], [2, 1], [3, 1]], "y": [[5, 2], [5, 2], [1, 1]]},
    None,
    1,
    3,
    0.3333333333333333,
)
BASE_DOCUMENT = {
    "contract": CONTRACT,
    "status": "ok",
    "metric": "objective",
    "direction": "maximize",
    "k": 5,
    "k_followed": 5,
    "n_sel": 5,
    "n_hol": 4,
    "selection": BASE_SELECTION_OBJECT,
    "holdout": BASE_HOLDOUT_OBJECT,
    "n_matched": 3,
    "algorithm": ALGORITHM,
}

# Each case holds a label, sources, options, typed expected keys and pin description.
WORKED = [
    (
        "k = 5: the join, the pool, the mid-ranks and the correlation",
        BASE_SELECTION,
        BASE_HOLDOUT,
        {"metric": "objective", "direction": "maximize", "k": 5, "space": S1},
        {
            **BASE_DOCUMENT,
            "followed": BASE_FOLLOWED,
            "unmatched": BASE_UNMATCHED,
            "pool": pool(3, [2, 1], 1, [11]),
            "statistics": BASE_STATISTICS,
            "admission": admit(11, 10, 6),
        },
        "six selection rows and five holdout rows",
    ),
    (
        "k = 7: the same five followed vectors, k clipped and not refused",
        BASE_SELECTION,
        BASE_HOLDOUT,
        {"metric": "objective", "direction": "maximize", "k": 7, "space": S1},
        {
            **BASE_DOCUMENT,
            "k": 7,
            "followed": BASE_FOLLOWED,
            "unmatched": BASE_UNMATCHED,
            "pool": pool(3, [2, 1], 1, [11]),
            "statistics": BASE_STATISTICS,
            "admission": admit(11, 10, 6),
        },
        "six selection rows and five holdout rows, k clipped",
    ),
    (
        "k = 3: the pool is built from every eligible selection vector",
        BASE_SELECTION,
        BASE_HOLDOUT,
        {"metric": "objective", "direction": "maximize", "k": 3, "space": S1},
        {
            "k": 3,
            "k_followed": 3,
            "n_matched": 1,
            "followed": [follow(1, KEY_B, 12.0, 1, 8, 4.0, [5, 2], -8.0, False)],
            "unmatched": BASE_UNMATCHED,
            "pool": pool(3, [2, 1], 1, [11]),
            "statistics": stats(
                None,
                {"xy": [0, 1], "xx": [0, 1], "yy": [0, 1]},
                {"x": [[1, 1]], "y": [[1, 1]]},
                "fewer_than_three_matched",
                0,
                1,
                0.0,
            ),
            "admission": admit(11, 10, 6),
        },
        "six selection rows and five holdout rows, k = 3",
    ),
    (
        "a named metric under minimize, the terms reported although the value is null",
        MINIMIZE_SELECTION,
        MINIMIZE_HOLDOUT,
        {"metric": "m", "direction": "minimize", "k": 2, "space": S2},
        {
            "metric": "m",
            "direction": "minimize",
            "k": 2,
            "k_followed": 2,
            "n_sel": 2,
            "n_hol": 2,
            "n_matched": 2,
            "selection": source_object(0, 2, 0, 2, 2, {}),
            "holdout": source_object(1, 2, 0, 2, 2, {}),
            "followed": [
                follow(0, KEY_A, 1.0, 1, 5, 3.0, [2, 1], 2.0, False),
                follow(1, KEY_B, 2.0, 2, 6, 0.5, [1, 1], -1.5, True),
            ],
            "unmatched": [],
            "pool": pool(2, [3, 2], 0, []),
            "statistics": stats(
                None,
                {"xy": [-1, 1], "xx": [1, 1], "yy": [1, 1]},
                {"x": [[1, 1], [2, 1]], "y": [[2, 1], [1, 1]]},
                "fewer_than_three_matched",
                1,
                2,
                0.5,
            ),
            "admission": admit(4, 1, 1),
        },
        "two-vector named-metric fixture under minimize",
    ),
    (
        "an even pool whose tie group straddles the median cut",
        EVEN_SELECTION,
        EVEN_HOLDOUT,
        {"metric": "objective", "direction": "maximize", "k": 4, "space": S1},
        {
            "k_followed": 4,
            "n_sel": 4,
            "n_hol": 4,
            "n_matched": 4,
            "followed": [
                follow(0, KEY_A, 4.0, 1, 7, 5.0, [1, 1], 1.0, True),
                follow(1, KEY_B, 3.0, 2, 8, 4.0, [5, 2], 1.0, True),
                follow(2, KEY_C, 2.0, 3, 9, 4.0, [5, 2], 2.0, True),
                follow(3, KEY_D, 1.0, 4, 10, 1.0, [4, 1], 0.0, False),
            ],
            "unmatched": [],
            "pool": pool(4, [5, 2], 0, []),
            "statistics": stats(
                0.9486832980505138,
                {"xy": [18, 1], "xx": [20, 1], "yy": [18, 1]},
                {
                    "x": [[1, 1], [2, 1], [3, 1], [4, 1]],
                    "y": [[1, 1], [5, 2], [5, 2], [4, 1]],
                },
                None,
                3,
                4,
                0.75,
            ),
            "admission": admit(8, 6, 6),
        },
        "four-vector fixture",
    ),
    (
        "the exclusion reasons of a named metric",
        NAMED_SELECTION,
        NAMED_HOLDOUT,
        {"metric": "m", "direction": "maximize", "k": 1, "space": S1},
        {
            "metric": "m",
            "k": 1,
            "k_followed": 1,
            "n_sel": 1,
            "n_hol": 1,
            "n_matched": 1,
            "selection": source_object(
                0,
                4,
                0,
                1,
                1,
                {
                    "metric_not_recorded": {"count": 2, "ids": [1, 3]},
                    "metric_unavailable": {"count": 1, "ids": [2]},
                },
            ),
            "holdout": source_object(1, 1, 0, 1, 1, {}),
            "followed": [follow(0, KEY_A, 1.0, 1, 7, 2.0, [1, 1], 1.0, True)],
            "unmatched": [],
            "pool": pool(1, [1, 1], 0, []),
            "statistics": stats(
                None,
                {"xy": [0, 1], "xx": [0, 1], "yy": [0, 1]},
                {"x": [[1, 1]], "y": [[1, 1]]},
                "fewer_than_three_matched",
                1,
                1,
                1.0,
            ),
            "admission": admit(5, 0, 0),
        },
        "named-metric exclusion fixture",
    ),
    (
        "the direction taken from the recorded space of two results",
        RECORDED_SELECTION,
        RECORDED_HOLDOUT,
        {"metric": "objective", "direction": None, "k": 1, "space": None},
        {
            "direction": "maximize",
            "k_followed": 1,
            "n_sel": 1,
            "n_hol": 1,
            "n_matched": 1,
            "followed": [follow(0, KEY_A, 3.0, 1, 7, 2.0, [1, 1], -1.0, False)],
            "unmatched": [],
            "pool": pool(1, [1, 1], 0, []),
            "statistics": stats(
                None,
                {"xy": [0, 1], "xx": [0, 1], "yy": [0, 1]},
                {"x": [[1, 1]], "y": [[1, 1]]},
                "fewer_than_three_matched",
                1,
                1,
                1.0,
            ),
            "admission": admit(2, 0, 0),
        },
        "recorded-direction fixture",
    ),
    (
        "a constant holdout rank vector, the division-by-zero branch",
        FLAT_H_SELECTION,
        FLAT_H_HOLDOUT,
        {"metric": "objective", "direction": "maximize", "k": 3, "space": S1},
        {
            "k_followed": 3,
            "n_sel": 3,
            "n_hol": 3,
            "n_matched": 3,
            "followed": [
                # All holdout values tie over positions 1..3: (1 + 3)/2 = 2.
                follow(0, KEY_A, 3.0, 1, 7, 1.0, [2, 1], -2.0, False),
                follow(1, KEY_B, 2.0, 2, 8, 1.0, [2, 1], -1.0, False),
                follow(2, KEY_C, 1.0, 3, 9, 1.0, [2, 1], 0.0, False),
            ],
            "unmatched": [],
            "pool": pool(3, [2, 1], 0, []),
            "statistics": stats(
                None,
                {"xy": [0, 1], "xx": [6, 1], "yy": [0, 1]},
                {"x": [[1, 1], [2, 1], [3, 1]], "y": [[2, 1], [2, 1], [2, 1]]},
                "constant_holdout_ranks",
                3,
                3,
                1.0,
            ),
            "admission": admit(6, 3, 3),
        },
        "constant-holdout-rank fixture",
    ),
    (
        "nothing matched: the document is still complete",
        [row(0, "ok", 3.0, "A")],
        [],
        {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
        {
            "k_followed": 1,
            "n_sel": 1,
            "n_hol": 0,
            "n_matched": 0,
            "selection": source_object(0, 1, 0, 1, 1, {}),
            "holdout": source_object(1, 0, 0, 0, 0, {}),
            "followed": [],
            "unmatched": [unmatch(0, KEY_A, 3.0, 1, "absent_in_holdout", [])],
            "pool": pool(0, [1, 2], 0, []),
            "statistics": stats(
                None,
                {"xy": None, "xx": None, "yy": None},
                {"x": None, "y": None},
                "no_matched_vectors",
                0,
                0,
                None,
            ),
            "admission": admit(1, 0, 0),
        },
        "unmatched fixture",
    ),
    (
        "a repeated vector whose eligible rows agree collapses to its lowest trial id",
        REPEAT_SELECTION,
        REPEAT_HOLDOUT,
        {"metric": "objective", "direction": "maximize", "k": 2, "space": S1},
        {
            "k": 2,
            "k_followed": 1,
            "n_sel": 1,
            "n_hol": 1,
            "n_matched": 1,
            "selection": source_object(0, 2, 0, 2, 1, {}),
            "holdout": source_object(1, 1, 0, 1, 1, {}),
            "followed": [follow(0, KEY_A, 5.0, 1, 7, 6.0, [1, 1], 1.0, True)],
            "unmatched": [],
            "pool": pool(1, [1, 1], 0, []),
            "statistics": stats(
                None,
                {"xy": [0, 1], "xx": [0, 1], "yy": [0, 1]},
                {"x": [[1, 1]], "y": [[1, 1]]},
                "fewer_than_three_matched",
                1,
                1,
                1.0,
            ),
            "admission": admit(3, 0, 0),
        },
        "repeated-vector fixture, values equal",
    ),
    (
        "an ancestor row of the holdout source that repeats no selection row",
        ANCESTOR_SELECTION,
        ANCESTOR_HOLDOUT,
        {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
        {
            "k_followed": 1,
            "n_sel": 1,
            "n_hol": 2,
            "n_matched": 1,
            "selection": source_object(0, 1, 0, 1, 1, {}),
            "holdout": source_object(1, 2, 1, 2, 2, {}),
            "followed": [follow(0, KEY_A, 3.0, 1, 20, 2.0, [1, 1], -1.0, False)],
            "unmatched": [],
            # Ancestor B (id 7) is eligible but selection holds only A.
            "pool": pool(1, [1, 1], 1, [7]),
            "statistics": stats(
                None,
                {"xy": [0, 1], "xx": [0, 1], "yy": [0, 1]},
                {"x": [[1, 1]], "y": [[1, 1]]},
                "fewer_than_three_matched",
                1,
                1,
                1.0,
            ),
            "admission": admit(3, 0, 1),
        },
        "ancestor fixture, no overlap",
    ),
    (
        "the correlation ranks the matched set, not the pool",
        POOL_SELECTION,
        POOL_HOLDOUT,
        {"metric": "objective", "direction": "maximize", "k": 3, "space": S1},
        {
            "k_followed": 3,
            "n_sel": 4,
            "n_hol": 4,
            "n_matched": 3,
            "followed": [
                follow(0, KEY_A, 4.0, 1, 7, 1.0, [4, 1], -3.0, False),
                follow(1, KEY_B, 3.0, 2, 8, 3.0, [2, 1], 0.0, False),
                follow(2, KEY_C, 2.0, 3, 9, 4.0, [1, 1], 2.0, True),
            ],
            "unmatched": [],
            "pool": pool(4, [5, 2], 0, []),
            "statistics": stats(
                -1.0,
                {"xy": [-6, 1], "xx": [6, 1], "yy": [6, 1]},
                {"x": [[1, 1], [2, 1], [3, 1]], "y": [[3, 1], [2, 1], [1, 1]]},
                None,
                2,
                3,
                0.6666666666666666,
            ),
            "admission": admit(8, 6, 6),
        },
        "pool-versus-matched fixture",
    ),
    (
        "a constant selection rank vector, reached directly",
        FLAT_S_SELECTION,
        FLAT_S_HOLDOUT,
        {"metric": "objective", "direction": "maximize", "k": 3, "space": S1},
        {
            "k_followed": 3,
            "n_sel": 3,
            "n_hol": 3,
            "n_matched": 3,
            "followed": [
                follow(0, KEY_A, 5.0, 1, 7, 1.0, [3, 1], -4.0, False),
                follow(1, KEY_B, 5.0, 2, 8, 2.0, [2, 1], -3.0, False),
                follow(2, KEY_C, 5.0, 3, 9, 3.0, [1, 1], -2.0, False),
            ],
            "unmatched": [],
            "pool": pool(3, [2, 1], 0, []),
            "statistics": stats(
                None,
                {"xy": [0, 1], "xx": [0, 1], "yy": [6, 1]},
                {"x": [[2, 1], [2, 1], [2, 1]], "y": [[3, 1], [2, 1], [1, 1]]},
                "constant_selection_ranks",
                2,
                3,
                0.6666666666666666,
            ),
            "admission": admit(6, 3, 3),
        },
        "constant-selection-rank fixture",
    ),
]


# ---------------------------------------------------------------------------------------------
# The oracle: parsed sources, its own refusal type and its own document.
# ---------------------------------------------------------------------------------------------


class Refused(Exception):
    """A refusal produced by the oracle's input checks."""

    def __init__(self, outcome: dict) -> None:
        super().__init__(outcome["code"])
        self.outcome = outcome


def refusal(code: str, ids=(), names=()) -> dict:
    return {
        "status": "refused",
        "code": code,
        "ids": tuple(sorted(ids)),
        "names": tuple(names),
    }


def outcome_view(document: dict):
    """The comparable form of a document: its own keys, or the refusal it is."""
    if document.get("status") == "refused":
        return (
            "refused",
            document["code"],
            tuple(document["ids"]),
            tuple(document["names"]),
        )
    return document


def within_one_ulp(left, right) -> bool:
    """Whether two binary64 values are equal or one unit in the last place apart.

    The ulp is measured at the larger magnitude, making the comparison symmetric
    for negative values at powers of two as well. The oracle rounds once after
    decimal square roots; the module rounds the ratio first, then square-roots
    it. Exact terms are compared without tolerance.
    """
    if left is None or right is None:
        return left is None and right is None
    if left == right:
        return True
    if (left < 0) != (right < 0):
        return False
    return abs(left - right) <= math.ulp(max(abs(left), abs(right)))


def blank_spearman(statistics: dict) -> dict:
    """A copy of a statistics object with the one tolerated field blanked out."""
    part = copy.deepcopy(statistics)
    part["spearman"] = "one ulp"
    return part


def document_views(oracle: dict, other: dict):
    """Compare all fields exactly and the Spearman tolerance verdict against True.

    A failed tolerance check must differ from the oracle's literal True, rather
    than being compared with a second copy of the same failed verdict.
    """
    if oracle.get("status") == "refused" or other.get("status") == "refused":
        return outcome_view(oracle), outcome_view(other)
    oracle_view = copy.deepcopy(oracle)
    other_view = copy.deepcopy(other)
    oracle_view["statistics"] = blank_spearman(oracle["statistics"])
    other_view["statistics"] = blank_spearman(other["statistics"])
    return (oracle_view, True), (
        other_view,
        within_one_ulp(
            oracle["statistics"]["spearman"], other["statistics"]["spearman"]
        ),
    )


def accounted_rows(part: dict) -> int:
    """Count eligible rows and exclusion ids without using rows_excluded."""
    return part["rows_eligible"] + sum(
        len(reason["ids"]) for reason in part["excluded"].values()
    )


def spearman_control(ledger: Ledger, oracle: dict, other: dict) -> None:
    """Move only the oracle's Spearman by two ulps away from the other value.

    The planted disagreement uses the same document comparison as random cases.
    A mirrored tolerance verdict would make this control fail.
    """
    ledger.compare(
        "control",
        "the unshifted documents agree before the two-ulp Spearman change",
        *document_views(oracle, other),
    )
    changed = copy.deepcopy(oracle)
    value = changed["statistics"]["spearman"]
    target = -math.inf if value < other["statistics"]["spearman"] else math.inf
    changed["statistics"]["spearman"] = math.nextafter(
        math.nextafter(value, target), target
    )
    probe = Ledger(echo=False, strict=True)
    probe.name = "one-sided Spearman probe"
    agrees = probe.compare(
        "control",
        "the whole-document comparison rejects a two-ulp Spearman change",
        *document_views(changed, other),
    )
    ledger.check(
        "control",
        "the one-sided two-ulp Spearman control returns DISAGREE",
        agrees is False,
    )


@dataclass(frozen=True)
class Parsed:
    """One row of a source, read by the oracle's own field rules."""

    index: int
    trial_id: int
    status: str
    objective: object
    parameters: object
    metrics: object
    backtest: object
    space_hash: object
    space: object


@dataclass(frozen=True)
class OSourc:
    """One parsed source: its rows in file order, its recorded space and its ancestors."""

    index: int
    rows: tuple
    space: object
    direction: object
    ancestor_ids: tuple


def read_row(raw, index: int) -> Parsed:
    if not isinstance(raw, dict):
        raise Refused(refusal("holdout_input_invalid"))
    trial_id = raw.get("trial_id")
    ids = [(index, trial_id)] if type(trial_id) is int else []
    if type(trial_id) is not int or not 0 <= trial_id < ID_LIMIT:
        raise Refused(refusal("holdout_input_invalid", ids))
    status = raw.get("status")
    if type(status) is not str or status not in STATUSES:
        raise Refused(refusal("holdout_input_invalid", ids))
    feasible = raw.get("feasible")
    if type(feasible) is not bool or feasible != (status == "ok"):
        raise Refused(refusal("holdout_input_invalid", ids))
    if "objective" not in raw:
        raise Refused(refusal("holdout_input_invalid", ids))
    objective = raw["objective"]
    if objective is not None:
        if type(objective) not in (int, float):
            raise Refused(refusal("holdout_input_invalid", ids))
        try:
            objective = float(objective)
        except OverflowError:
            raise Refused(refusal("holdout_input_invalid", ids)) from None
        if not math.isfinite(objective):
            raise Refused(refusal("holdout_input_invalid", ids))
    if objective is None and status in ("ok", "constraint_violation"):
        raise Refused(refusal("holdout_input_invalid", ids))
    members = {}
    for key in ("parameters", "metrics", "backtest", "space"):
        if key in raw and not isinstance(raw[key], dict):
            raise Refused(refusal("holdout_input_invalid", ids))
        members[key] = raw.get(key)
    space_hash = raw.get("space_hash")
    if "space_hash" in raw and type(space_hash) is not str:
        raise Refused(refusal("holdout_input_invalid", ids))
    return Parsed(
        index,
        trial_id,
        status,
        objective,
        members["parameters"],
        members["metrics"],
        members["backtest"],
        space_hash,
        members["space"],
    )


def read_rows(values, index: int) -> tuple:
    rows = tuple(read_row(raw, index) for raw in values)
    seen: set = set()
    repeated: set = set()
    for parsed in rows:
        if parsed.trial_id in seen:
            repeated.add(parsed.trial_id)
        seen.add(parsed.trial_id)
    if repeated:
        raise Refused(
            refusal("holdout_input_invalid", [(index, tid) for tid in repeated])
        )
    return rows


def space_direction(space) -> object:
    objective = space.get("objective") if isinstance(space, dict) else None
    direction = objective.get("direction") if isinstance(objective, dict) else None
    return direction if direction in DIRECTIONS else None


def common_direction(rows) -> object:
    found = {
        space_direction(parsed.space) for parsed in rows if parsed.space is not None
    }
    found.discard(None)
    return found.pop() if len(found) == 1 else None


def read_source(value, index: int) -> OSourc:
    """Read one source from a parsed result object or a parsed list of rows.

    Paths and file layout dispatch are not reproduced. Every fixture here
    passes a dictionary or a list.
    """
    if isinstance(value, (str, os.PathLike)):
        raise ValueError(
            "the oracle reads no files; pass a parsed result or a list of rows"
        )
    if isinstance(value, list):
        rows = read_rows(value, index)
        return OSourc(index, rows, None, common_direction(rows), ())
    if not isinstance(value, dict):
        raise Refused(refusal("holdout_input_invalid"))
    if value.get("trials_out", "all") != "all":
        raise Refused(refusal("holdout_rows_incomplete"))
    own = value.get("trials")
    inherited = value.get("warm_start_trials", [])
    if not isinstance(own, list) or not isinstance(inherited, list):
        raise Refused(refusal("holdout_input_invalid"))
    if "trials_completed" in value and (
        type(value["trials_completed"]) is not int
        or value["trials_completed"] != len(own)
    ):
        raise Refused(refusal("holdout_rows_incomplete"))
    if "warm_start" in value:
        warm = value["warm_start"]
        count = warm.get("trials") if isinstance(warm, dict) else None
        if type(count) is not int or count != len(inherited):
            raise Refused(refusal("holdout_rows_incomplete"))
    space = value.get("space")
    if "space" in value and not isinstance(space, dict):
        raise Refused(refusal("holdout_input_invalid"))
    direction = space_direction(space)
    if "direction" in value:
        given = value["direction"]
        if type(given) is not str or given not in DIRECTIONS:
            raise Refused(refusal("holdout_input_invalid"))
        if direction is not None and direction != given:
            raise Refused(refusal("holdout_input_invalid"))
        direction = given
    rows = read_rows([*inherited, *own], index)
    return OSourc(
        index, rows, space, direction, tuple(raw["trial_id"] for raw in inherited)
    )


def typed_scalar(name, value, dimension):
    """The typed scalar of a parameter value, rebuilt by hand for the kinds used here."""
    kind = dimension.get("kind") if isinstance(dimension, dict) else None
    if kind == "integer":
        if type(value) is not int:
            raise ValueError(name)
        return ["integer", str(value)]
    if kind == "real":
        if type(value) not in (int, float):
            raise ValueError(name)
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(name)
        return ["real", struct.pack(">d", 0.0 if number == 0.0 else number).hex()]
    if kind == "boolean":
        if type(value) is not bool:
            raise ValueError(name)
        return ["boolean", value]
    if kind == "categorical":
        choices = dimension["choices"]
        if any(type(value) is type(choice) and value == choice for choice in choices):
            return (
                ["string", value] if isinstance(value, str) else _typed_fallback(value)
            )
        raise ValueError(name)
    raise ValueError(name)


def _typed_fallback(value):
    """A categorical choice that is not a string: the same scalars as the other kinds."""
    kind = "real" if isinstance(value, float) else "integer"
    return typed_scalar("choice", value, {"kind": kind})


def vector_key(parameters, space):
    """(key, None) for a keyable vector, (None, offending names) when it cannot be keyed."""
    dimensions = space.get("parameters") if isinstance(space, dict) else None
    if not isinstance(dimensions, dict):
        return None, []
    if not isinstance(parameters, dict) or parameters.keys() != dimensions.keys():
        given = set(parameters) if isinstance(parameters, dict) else set()
        return None, sorted(given ^ set(dimensions))
    body = {}
    for name, dimension in dimensions.items():
        try:
            body[name] = typed_scalar(name, parameters[name], dimension)
        except (ValueError, KeyError, TypeError, OverflowError):
            return None, [name]
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text, None


def space_form(space) -> str:
    """A comparable string for a recorded space; an exception means the space is malformed."""
    parameters = {}
    for name, dimension in space["parameters"].items():
        kind = dimension["kind"]
        if kind in ("integer", "real"):
            parameters[name] = (
                kind,
                str(dimension["low"]),
                str(dimension["high"]),
                str(dimension["step"]),
                bool(dimension["log"]),
            )
        elif kind == "categorical":
            parameters[name] = (
                kind,
                tuple(
                    json.dumps(choice, sort_keys=True)
                    for choice in dimension["choices"]
                ),
            )
        else:
            parameters[name] = (kind,)
    objective = space["objective"]
    return json.dumps(
        {
            "parameters": parameters,
            "objective": {
                "expression": objective["expression"],
                "direction": objective["direction"],
                "constraints": sorted(objective["constraints"]),
            },
        },
        sort_keys=True,
    )


def common_space(sources, passed) -> dict:
    """The one space the join is keyed with, from the sources or from the caller."""
    holders = []
    for source in sources:
        if source.space is not None:
            holders.append((source.index, -1, source.space))
        holders.extend(
            (parsed.index, parsed.trial_id, parsed.space)
            for parsed in source.rows
            if parsed.space is not None
        )
    if not holders:
        if passed is None:
            raise Refused(refusal("holdout_space_missing"))
        if not isinstance(passed, dict):
            raise Refused(refusal("holdout_parameter_invalid", names=("space",)))
        try:
            space_form(passed)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise Refused(refusal("holdout_input_invalid")) from None
        return copy.deepcopy(passed)
    forms = {}
    for holder in holders:
        try:
            forms[id(holder)] = space_form(holder[2])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise Refused(refusal("holdout_input_invalid")) from None
    reference = min(holders, key=lambda holder: holder[:2])
    expected = forms[id(reference)]
    apart = [holder for holder in holders if forms[id(holder)] != expected]
    if apart:
        names = set()
        for holder in apart:
            left = json.loads(expected)["parameters"]
            right = json.loads(forms[id(holder)])["parameters"]
            names.update(
                name
                for name in left.keys() | right.keys()
                if left.get(name) != right.get(name)
            )
        raise Refused(
            refusal(
                "holdout_space_mismatch",
                [holder[:2] for holder in apart if holder[1] >= 0],
                sorted(names),
            )
        )
    return copy.deepcopy(reference[2])


def check_context(rows) -> None:
    """One source on its own: scored rows must agree on the counters and on the space hash."""
    scored = [
        parsed
        for parsed in rows
        if parsed.status == "ok" and parsed.backtest is not None
    ]
    if scored:
        reference = min(scored, key=lambda parsed: (parsed.index, parsed.trial_id))
        expected = tuple(reference.backtest.get(name) for name in COUNTERS)
        apart = [
            parsed
            for parsed in scored
            if tuple(parsed.backtest.get(name) for name in COUNTERS) != expected
        ]
        if apart:
            names = [
                name
                for position, name in enumerate(COUNTERS)
                if any(
                    tuple(parsed.backtest.get(inner) for inner in COUNTERS)[position]
                    != expected[position]
                    for parsed in scored
                )
            ]
            raise Refused(
                refusal(
                    "holdout_context_mismatch",
                    [(parsed.index, parsed.trial_id) for parsed in [reference, *apart]],
                    names,
                )
            )
    hashed = [parsed for parsed in rows if parsed.space_hash is not None]
    if hashed:
        reference = min(hashed, key=lambda parsed: (parsed.index, parsed.trial_id))
        apart = [
            parsed for parsed in hashed if parsed.space_hash != reference.space_hash
        ]
        if apart:
            raise Refused(
                refusal(
                    "holdout_context_mismatch",
                    [(parsed.index, parsed.trial_id) for parsed in [reference, *apart]],
                    ("space_hash",),
                )
            )


def cell_of(parsed: Parsed, metric: str):
    """(cell, present, null) for the metric the caller named."""
    if metric == "objective":
        return parsed.objective, True, parsed.objective is None
    metrics = parsed.metrics
    if not isinstance(metrics, dict) or metric not in metrics:
        return None, False, False
    cell = metrics[metric]
    return cell, True, cell is None


def check_cell(parsed: Parsed, metric: str) -> None:
    """Refuse invalid metric cells before digesting them, whatever the status."""
    if metric == "objective":
        return
    cell, present, _null = cell_of(parsed, metric)
    if not present or cell is None:
        return
    pair = [(parsed.index, parsed.trial_id)]
    if type(cell) in (int, float):
        try:
            number = float(cell)
        except OverflowError:
            raise Refused(refusal("holdout_value_invalid", pair)) from None
        if not math.isfinite(number):
            raise Refused(refusal("holdout_value_invalid", pair))
    elif parsed.status == "ok":
        raise Refused(refusal("holdout_value_invalid", pair))
    try:
        json.dumps(
            cell,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        raise Refused(refusal("holdout_value_invalid", pair)) from None


def digest_value(parsed: Parsed, metric: str):
    """Return the metric cell as it is recorded in the row digest."""
    if metric == "objective":
        return parsed.objective
    metrics = parsed.metrics
    if not isinstance(metrics, dict) or metric not in metrics:
        return None
    cell = metrics[metric]
    if cell is None:
        return None
    if type(cell) in (int, float):
        return float(cell)
    return cell


def better_value(left: float, right: float, direction: str) -> bool:
    return left > right if direction == "maximize" else left < right


def count_ranks(entries, direction: str) -> dict:
    """Rank each vector by counting how many vectors come strictly before it."""
    ranks = {}
    for entry in entries:
        before = sum(
            1
            for other in entries
            if better_value(other[1], entry[1], direction)
            or (other[1] == entry[1] and other[2] < entry[2])
        )
        ranks[entry[0]] = before + 1
    return ranks


def sort_ranks(entries, direction: str) -> dict:
    """The same ranks by sorting best first, ties by ascending trial id; the other route."""
    sign = -1.0 if direction == "maximize" else 1.0
    order = sorted(entries, key=lambda entry: (sign * entry[1], entry[2]))
    return {entry[0]: position + 1 for position, entry in enumerate(order)}


def count_mid_ranks(entries, direction: str) -> dict:
    """Mid-rank each value from p = 1 + strictly better and q = not worse."""
    mids = {}
    for entry in entries:
        better = sum(
            1
            for other in entries
            if other[1] != entry[1] and better_value(other[1], entry[1], direction)
        )
        not_worse = sum(
            1
            for other in entries
            if other[1] == entry[1] or better_value(other[1], entry[1], direction)
        )
        mids[entry[0]] = Fraction(1 + better + not_worse, 2)
    return mids


def sort_mid_ranks(entries, direction: str) -> dict:
    """The same mid-ranks by sorting and grouping equal values; the other route."""
    sign = -1.0 if direction == "maximize" else 1.0
    order = sorted(entries, key=lambda entry: (sign * entry[1], entry[2]))
    mids = {}
    start = 0
    while start < len(order):
        stop = start
        while stop + 1 < len(order) and order[stop + 1][1] == order[start][1]:
            stop += 1
        mid = Fraction((start + 1) + (stop + 1), 2)
        for position in range(start, stop + 1):
            mids[order[position][0]] = mid
        start = stop + 1
    return mids


def decimal_sqrt(sxy: Fraction, sxx: Fraction, syy: Fraction) -> float:
    """The binary64 correlation: the exact rational square-rooted at 50 digits, rounded once."""
    if sxy == 0:
        return 0.0
    exact = Fraction(sxy * sxy, sxx * syy)
    with localcontext() as context:
        context.prec = 50
        root = Decimal(exact.numerator).sqrt() / Decimal(exact.denominator).sqrt()
        value = float(root)
    return value if sxy > 0 else -value


def pair_list(value: Fraction) -> list:
    return [value.numerator, value.denominator]


def digest_of(selections, metric: str, keys: dict) -> str:
    parts = []
    for source in selections:
        for parsed in source.rows:
            entry = [
                parsed.index,
                parsed.trial_id,
                parsed.status,
                digest_value(parsed, metric),
                keys[parsed.index, parsed.trial_id],
            ]
            text = json.dumps(
                entry,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            parts.append(hashlib.sha256(text.encode("ascii")).digest())
    parts.sort()
    return hashlib.sha256(b"".join(parts)).hexdigest()


def analyse(
    selection,
    holdout,
    *,
    metric,
    direction,
    k,
    space=None,
    max_work=None,
    key_map=None,
):
    """The oracle's document for one call, or its own refusal, from the inputs alone.

    ``key_map`` maps a source index to a dict that rewrites that source's row keys after they are
    built; it exists so that a control can edit a key in the oracle's own copy without touching
    the other side.
    """
    if metric is None:
        raise Refused(refusal("holdout_parameter_missing", names=("metric",)))
    if k is None:
        raise Refused(refusal("holdout_parameter_missing", names=("k",)))
    if type(metric) is str and direction is None and metric != "objective":
        raise Refused(refusal("holdout_parameter_missing", names=("direction",)))
    if type(metric) is not str:
        raise Refused(refusal("holdout_parameter_invalid", names=("metric",)))
    if type(k) is not int or k < 1:
        raise Refused(refusal("holdout_parameter_invalid", names=("k",)))
    if direction is not None and (
        type(direction) is not str or direction not in DIRECTIONS
    ):
        raise Refused(refusal("holdout_parameter_invalid", names=("direction",)))
    if max_work is not None and (type(max_work) is not int or max_work < 1):
        raise Refused(refusal("holdout_parameter_invalid", names=("max_work",)))
    if space is not None and not isinstance(space, dict):
        raise Refused(refusal("holdout_parameter_invalid", names=("space",)))

    selections = [read_source(selection, 0), read_source(holdout, 1)]
    keyed_space = common_space(selections, space)
    if direction is None:
        recorded = {source.direction for source in selections}
        if len(recorded) != 1 or None in recorded:
            raise Refused(refusal("holdout_parameter_missing", names=("direction",)))
        direction = recorded.pop()

    for source in selections:
        check_context(source.rows)

    selection_ids = {parsed.trial_id for parsed in selections[0].rows}
    overlap = sorted(
        parsed.trial_id
        for parsed in selections[1].rows
        if parsed.trial_id in selections[1].ancestor_ids
        and parsed.trial_id in selection_ids
    )
    if overlap:
        raise Refused(
            refusal("holdout_ancestor_rows", [(1, trial_id) for trial_id in overlap])
        )

    keys = {}
    rewrites = key_map or {}
    for source in selections:
        table = rewrites.get(source.index, {})
        for parsed in sorted(source.rows, key=lambda item: (item.index, item.trial_id)):
            key, wrong = vector_key(parsed.parameters, keyed_space)
            if key is None:
                raise Refused(
                    refusal(
                        "holdout_input_invalid",
                        [(parsed.index, parsed.trial_id)],
                        wrong,
                    )
                )
            keys[parsed.index, parsed.trial_id] = table.get(key, key)

    if metric != "objective":
        for source in selections:
            if not source.rows:
                continue
            recorded = any(
                isinstance(parsed.metrics, dict) and metric in parsed.metrics
                for parsed in source.rows
            )
            if not recorded:
                raise Refused(refusal("holdout_metric_unknown", names=(metric,)))

    for source in selections:
        for parsed in sorted(source.rows, key=lambda item: (item.index, item.trial_id)):
            check_cell(parsed, metric)

    excluded = ({}, {})
    eligible_rows = ([], [])
    for position, source in enumerate(selections):
        for parsed in sorted(source.rows, key=lambda item: item.trial_id):
            if parsed.status != "ok":
                excluded[position].setdefault(f"status_{parsed.status}", []).append(
                    parsed.trial_id
                )
                continue
            cell, present, null = cell_of(parsed, metric)
            if not present:
                excluded[position].setdefault("metric_not_recorded", []).append(
                    parsed.trial_id
                )
                continue
            if null:
                excluded[position].setdefault("metric_unavailable", []).append(
                    parsed.trial_id
                )
                continue
            eligible_rows[position].append(
                (keys[parsed.index, parsed.trial_id], float(cell), parsed.trial_id)
            )

    if not eligible_rows[0]:
        reasons = ", ".join(
            f"{reason} {len(ids)}" for reason, ids in sorted(excluded[0].items())
        )
        detail = (
            "no eligible row remains after exclusions "
            f"({len(selections[0].rows)} rows read: "
            f"{reasons})"
            if selections[0].rows
            else "no eligible row remains after exclusions (the source holds no rows)"
        )
        raise Refused(
            {
                "status": "refused",
                "code": "holdout_no_eligible_rows",
                "detail": detail,
                "ids": (),
                "names": (),
            }
        )

    for position, entries in enumerate(eligible_rows):
        groups = {}
        for key, value, trial_id in entries:
            groups.setdefault(key, []).append((value, trial_id))
        conflicts = [
            (position, key, sorted(trial_id for _value, trial_id in group))
            for key, group in groups.items()
            if len({value for value, _trial_id in group}) > 1
        ]
        if conflicts:
            # Refuse the first conflicting source; include every row of each of
            # its conflicting vectors, even rows agreeing with the representative.
            ids = [
                (position, trial_id)
                for position, _key, trial_ids in conflicts
                for trial_id in trial_ids
            ]
            raise Refused(refusal("holdout_duplicate_vector", ids))

    def collapse(entries):
        """One representative per vector: its eligible row of lowest trial id."""
        reps = {}
        for key, value, trial_id in entries:
            if key not in reps or trial_id < reps[key][1]:
                reps[key] = (value, trial_id)
        return [(key, value, trial_id) for key, (value, trial_id) in reps.items()]

    sel_vectors = collapse(eligible_rows[0])
    hol_vectors = collapse(eligible_rows[1])
    n_sel = len(sel_vectors)
    n_hol = len(hol_vectors)
    selection_pairs = n_sel * (n_sel - 1) // 2
    holdout_pairs = n_hol * (n_hol - 1) // 2
    rows_read = len(selections[0].rows) + len(selections[1].rows)
    work = rows_read + selection_pairs + holdout_pairs
    if max_work is not None and work > max_work:
        raise Refused(refusal("holdout_resource_exceeded"))

    ranks = count_ranks(sel_vectors, direction)
    ranks_by_sort = sort_ranks(sel_vectors, direction)
    k_followed = min(k, n_sel)
    followed_keys = sorted(
        (key for key, rank in ranks.items() if rank <= k_followed),
        key=lambda key: ranks[key],
    )

    selection_values = {key: value for key, value, _trial_id in sel_vectors}
    selection_trials = {key: trial_id for key, _value, trial_id in sel_vectors}
    selection_keys = set(selection_trials)
    holdout_representatives = {
        key: (value, trial_id) for key, value, trial_id in hol_vectors
    }
    pool_keys = selection_keys & set(holdout_representatives)
    pool_representatives = {key: holdout_representatives[key] for key in pool_keys}
    pool_entries = [
        (key, value, trial_id)
        for key, (value, trial_id) in pool_representatives.items()
    ]
    size = len(pool_entries)
    cut = Fraction(size + 1, 2)
    pool_mid = count_mid_ranks(pool_entries, direction)
    pool_mid_by_sort = sort_mid_ranks(pool_entries, direction)

    holdout_only = sorted(
        holdout_representatives[key][1]
        for key in holdout_representatives
        if key not in selection_keys
    )

    matched = []
    unmatched = []
    for key in followed_keys:
        trial_id = selection_trials[key]
        if key in pool_representatives:
            value, hol_id = pool_representatives[key]
            difference = value - selection_values[key]
            if not math.isfinite(difference):
                raise Refused(
                    refusal(
                        "holdout_value_invalid",
                        [(0, trial_id), (1, hol_id)],
                    )
                )
            matched.append(
                {
                    "selection_trial_id": trial_id,
                    "vector_key": key,
                    "selection_value": selection_values[key],
                    "selection_rank": ranks[key],
                    "holdout_trial_id": hol_id,
                    "holdout_value": value,
                    "holdout_rank": pair_list(pool_mid[key]),
                    "difference": difference,
                    "improved": better_value(value, selection_values[key], direction),
                }
            )
        else:
            holders = [
                parsed
                for parsed in selections[1].rows
                if keys[1, parsed.trial_id] == key
            ]
            if holders:
                reason = "holdout_row_not_eligible"
                ids = sorted(parsed.trial_id for parsed in holders)
            else:
                reason = "absent_in_holdout"
                ids = []
            unmatched.append(
                {
                    "selection_trial_id": trial_id,
                    "vector_key": key,
                    "selection_value": selection_values[key],
                    "selection_rank": ranks[key],
                    "reason": reason,
                    "holdout_ids": ids,
                }
            )

    n = len(matched)
    x_mid = {}
    y_mid = {}
    x_sorted = {}
    y_sorted = {}
    sxy = sxx = syy = Fraction(0)
    deviations_xy = Fraction(0)
    terms_agree = True
    if n:
        x_entries = [
            (item["vector_key"], item["selection_value"], item["selection_trial_id"])
            for item in matched
        ]
        y_entries = [
            (item["vector_key"], item["holdout_value"], item["holdout_trial_id"])
            for item in matched
        ]
        x_mid = count_mid_ranks(x_entries, direction)
        y_mid = count_mid_ranks(y_entries, direction)
        x_sorted = sort_mid_ranks(x_entries, direction)
        y_sorted = sort_mid_ranks(y_entries, direction)
        xs = [x_mid[item["vector_key"]] for item in matched]
        ys = [y_mid[item["vector_key"]] for item in matched]
        sum_x = sum(xs, Fraction(0))
        sum_y = sum(ys, Fraction(0))
        xbar = Fraction(sum_x, n)
        ybar = Fraction(sum_y, n)
        deviations_xy = sum(
            ((x - xbar) * (y - ybar) for x, y in zip(xs, ys)), Fraction(0)
        )
        deviations_xx = sum(((x - xbar) * (x - xbar) for x in xs), Fraction(0))
        deviations_yy = sum(((y - ybar) * (y - ybar) for y in ys), Fraction(0))
        sxy = n * deviations_xy
        sxx = n * deviations_xx
        syy = n * deviations_yy
        one_pass_xy = (
            n * sum((x * y for x, y in zip(xs, ys)), Fraction(0)) - sum_x * sum_y
        )
        one_pass_xx = n * sum((x * x for x in xs), Fraction(0)) - sum_x * sum_x
        one_pass_yy = n * sum((y * y for y in ys), Fraction(0)) - sum_y * sum_y
        terms_agree = sxy == one_pass_xy and sxx == one_pass_xx and syy == one_pass_yy
    ranks_agree = (
        ranks == ranks_by_sort
        and pool_mid == pool_mid_by_sort
        and x_mid == x_sorted
        and y_mid == y_sorted
    )

    if n == 0:
        undefined = "no_matched_vectors"
        spearman = None
    elif n <= 2:
        undefined = "fewer_than_three_matched"
        spearman = None
    elif sxx == 0:
        undefined = "constant_selection_ranks"
        spearman = None
    elif syy == 0:
        undefined = "constant_holdout_ranks"
        spearman = None
    else:
        undefined = None
        spearman = decimal_sqrt(sxy, sxx, syy)

    numerator = sum(1 for item in matched if pool_mid[item["vector_key"]] <= cut)
    denominator = n
    better_half_value = numerator / denominator if denominator else None

    document = {
        "contract": CONTRACT,
        "status": "ok",
        "metric": metric,
        "direction": direction,
        "k": k,
        "k_followed": k_followed,
        "n_sel": n_sel,
        "n_hol": n_hol,
        "selection": {
            "index": 0,
            "rows": len(selections[0].rows),
            "ancestor_rows": len(selections[0].ancestor_ids),
            "rows_eligible": len(eligible_rows[0]),
            "vectors_eligible": n_sel,
            "rows_excluded": len(selections[0].rows) - len(eligible_rows[0]),
            "excluded": {
                reason: {"count": len(ids), "ids": ids}
                for reason, ids in sorted(excluded[0].items())
            },
            "space_hash": _space_hash(selections[0].rows),
        },
        "holdout": {
            "index": 1,
            "rows": len(selections[1].rows),
            "ancestor_rows": len(selections[1].ancestor_ids),
            "rows_eligible": len(eligible_rows[1]),
            "vectors_eligible": n_hol,
            "rows_excluded": len(selections[1].rows) - len(eligible_rows[1]),
            "excluded": {
                reason: {"count": len(ids), "ids": ids}
                for reason, ids in sorted(excluded[1].items())
            },
            "space_hash": _space_hash(selections[1].rows),
        },
        "followed": matched,
        "unmatched": unmatched,
        "n_matched": n,
        "pool": {
            "size": size,
            "median_rank_cut": pair_list(cut),
            "holdout_only": {"count": len(holdout_only), "ids": holdout_only},
        },
        "statistics": {
            "spearman": spearman,
            "spearman_terms": (
                {"xy": None, "xx": None, "yy": None}
                if n == 0
                else {
                    "xy": pair_list(sxy),
                    "xx": pair_list(sxx),
                    "yy": pair_list(syy),
                }
            ),
            "spearman_ranks": (
                {"x": None, "y": None}
                if n == 0
                else {
                    "x": [pair_list(x_mid[item["vector_key"]]) for item in matched],
                    "y": [pair_list(y_mid[item["vector_key"]]) for item in matched],
                }
            ),
            "spearman_undefined": undefined,
            "better_half": {
                "numerator": numerator,
                "denominator": denominator,
                "value": better_half_value,
            },
        },
        "algorithm": ALGORITHM,
        "input_sha256": digest_of(selections, metric, keys),
        "admission": {
            "rows_read": rows_read,
            "selection_pairs": selection_pairs,
            "holdout_pairs": holdout_pairs,
            "work": work,
            "max_work": max_work,
        },
        "interpretation": INTERPRETATION,
    }
    extra = {
        "ranks_agree": ranks_agree,
        "terms_agree": terms_agree,
        "deviations_xy": deviations_xy,
        "sxy": sxy,
    }
    return document, extra


def _space_hash(rows) -> object:
    for parsed in rows:
        if parsed.space_hash is not None:
            return parsed.space_hash
    return None


def oracle_outcome(selection, holdout, options):
    """(document or refusal, the oracle's own internal notes for the case)."""
    try:
        return analyse(selection, holdout, **options)
    except Refused as stop:
        return stop.outcome, {}


# ---------------------------------------------------------------------------------------------
# Phase 1 sections (no module).
# ---------------------------------------------------------------------------------------------


def section_worked(ledger: Ledger) -> None:
    ledger.begin("worked fixtures, recomputed by the oracle")
    for label, selection, holdout, options, expected, source in WORKED:
        document, _extra = oracle_outcome(
            copy.deepcopy(selection), copy.deepcopy(holdout), options
        )
        if document.get("status") == "refused":
            ledger.fail(
                f"{label}: the oracle refused the fixture: {outcome_view(document)}"
            )
            continue
        for key, literal in expected.items():
            ledger.compare("fixture", f"{label}: {key}", document[key], literal, source)
            if key in ("selection", "holdout"):
                ledger.compare(
                    "fixture",
                    f"{label}: {key} eligible and excluded counts cover rows read",
                    accounted_rows(literal),
                    document[key]["rows"],
                    source,
                )
    ledger.end(
        sum(
            len(case[4]) + sum(name in case[4] for name in ("selection", "holdout"))
            for case in WORKED
        )
    )


def section_internal(ledger: Ledger) -> None:
    """Two routes inside the oracle must agree, on every worked fixture."""
    ledger.begin(
        "internal: counting against sorting, and the deviations against "
        "the one-pass sums"
    )
    for label, selection, holdout, options, _expected, _source in WORKED:
        document, extra = oracle_outcome(
            copy.deepcopy(selection), copy.deepcopy(holdout), options
        )
        if document.get("status") == "refused":
            continue
        ledger.compare(
            "internal",
            f"{label}: the counting ranks equal the sorted ranks",
            extra["ranks_agree"],
            True,
        )
        ledger.compare(
            "internal",
            f"{label}: the deviations sums equal the one-pass sums",
            extra["terms_agree"],
            True,
        )
    ledger.end(2 * len(WORKED))


def section_refusals(ledger: Ledger) -> None:
    ledger.begin("refusal fixtures, recomputed by the oracle")
    note = "typed refusal pins"
    cases = [
        (
            "missing k before an invalid metric",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {"metric": 7, "direction": "maximize", "k": None, "space": S1},
            # Missing arguments precede invalid ones, so only k is named.
            ("refused", "holdout_parameter_missing", (), ("k",)),
            False,
        ),
        (
            "missing named-metric direction before an invalid k",
            NAMED_SELECTION,
            NAMED_HOLDOUT,
            {"metric": "m", "direction": None, "k": 0, "space": S1},
            # A named metric needs a direction before k's validity is judged.
            ("refused", "holdout_parameter_missing", (), ("direction",)),
            False,
        ),
        (
            "a non-string metric with direction given as None",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {"metric": 7, "direction": None, "k": 1, "space": S1},
            # A non-string metric does not trigger the missing-direction rule.
            ("refused", "holdout_parameter_invalid", (), ("metric",)),
            False,
        ),
        (
            "k given as None",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {"metric": "objective", "direction": "maximize", "k": None, "space": S1},
            ("refused", "holdout_parameter_missing"),
            True,
        ),
        (
            "metric given as None",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {"metric": None, "direction": "maximize", "k": 5, "space": S1},
            ("refused", "holdout_parameter_missing"),
            True,
        ),
        (
            "direction None under a named metric",
            NAMED_SELECTION,
            NAMED_HOLDOUT,
            {"metric": "m", "direction": None, "k": 1, "space": S1},
            ("refused", "holdout_parameter_missing"),
            True,
        ),
        (
            "direction None with two bare rows lists that record no direction",
            [(row(0, "ok", 1.0, "A"))],
            [(row(7, "ok", 2.0, "A"))],
            {"metric": "objective", "direction": None, "k": 1, "space": S1},
            ("refused", "holdout_parameter_missing"),
            True,
        ),
        (
            "k below one",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {"metric": "objective", "direction": "maximize", "k": 0, "space": S1},
            ("refused", "holdout_parameter_invalid"),
            True,
        ),
        (
            "k as a boolean",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {"metric": "objective", "direction": "maximize", "k": True, "space": S1},
            ("refused", "holdout_parameter_invalid"),
            True,
        ),
        (
            "k as a float",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {"metric": "objective", "direction": "maximize", "k": 1.0, "space": S1},
            ("refused", "holdout_parameter_invalid"),
            True,
        ),
        (
            "a direction word that is not maximize or minimize",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {"metric": "objective", "direction": "up", "k": 1, "space": S1},
            ("refused", "holdout_parameter_invalid"),
            True,
        ),
        (
            "a metric that is not a string",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {"metric": 7, "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_parameter_invalid"),
            True,
        ),
        (
            "max_work below one",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {
                "metric": "objective",
                "direction": "maximize",
                "k": 1,
                "max_work": 0,
                "space": S1,
            },
            ("refused", "holdout_parameter_invalid"),
            True,
        ),
        (
            "a space that is neither None nor a recorded space",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {"metric": "objective", "direction": "maximize", "k": 1, "space": 42},
            ("refused", "holdout_parameter_invalid"),
            True,
        ),
        (
            "a source that is not a path, a result or a list of rows",
            42,
            BASE_HOLDOUT,
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_input_invalid"),
            True,
        ),
        (
            "a result that keeps only some of its trials",
            {"trials_out": "best", "trials": []},
            [],
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_rows_incomplete"),
            True,
        ),
        (
            "no space recorded anywhere and none passed",
            [row(0, "ok", 1.0, "A")],
            [row(7, "ok", 2.0, "A")],
            {"metric": "objective", "direction": "maximize", "k": 1, "space": None},
            ("refused", "holdout_space_missing"),
            True,
        ),
        (
            "two recorded spaces that differ in a parameter dimension",
            {"trials": [row(0, "ok", 1.0, "A", space=S1)]},
            {"trials": [row(7, "ok", 2.0, "A", space=S2)]},
            {"metric": "objective", "direction": "maximize", "k": 1, "space": None},
            ("refused", "holdout_space_mismatch", ((1, 7),), ("arm",)),
            False,
        ),
        (
            "a parameter name that is not a dimension of the space",
            [
                row(0, "ok", 1.0, "A"),
                row(1, "ok", 2.0, "B", parameters={"b": 1.0}),
            ],
            [row(7, "ok", 3.0, "A")],
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_input_invalid", ((0, 1),), ("arm", "b")),
            False,
        ),
        (
            "one source whose two ok rows disagree on a backtest counter",
            [
                row(0, "ok", 1.0, "A", backtest={"input_bars_processed": 100}),
                row(1, "ok", 2.0, "B", backtest={"input_bars_processed": 200}),
            ],
            [row(7, "ok", 3.0, "A")],
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            (
                "refused",
                "holdout_context_mismatch",
                ((0, 0), (0, 1)),
                ("input_bars_processed",),
            ),
            False,
        ),
        (
            "a holdout ancestor row that repeats a selection trial id",
            ANCESTOR_OVERLAP_SELECTION,
            ANCESTOR_OVERLAP_HOLDOUT,
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_ancestor_rows", ((1, 0),), ()),
            False,
        ),
        (
            "a metric name that no row records",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {"metric": "nope", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_metric_unknown", (), ("nope",)),
            False,
        ),
        (
            "a metric recorded by the selection rows and by no holdout row",
            [row(0, "ok", 1.0, "A", {"m": 1.0})],
            [row(7, "ok", 2.0, "A")],
            {"metric": "m", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_metric_unknown", (), ("m",)),
            False,
        ),
        (
            "a selected source with no eligible row",
            [row(0, "pruned", None, "A")],
            [],
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_no_eligible_rows"),
            True,
        ),
        (
            "two eligible rows of one vector whose values differ",
            REPEAT_DISAGREEING,
            REPEAT_HOLDOUT,
            {"metric": "objective", "direction": "maximize", "k": 2, "space": S1},
            ("refused", "holdout_duplicate_vector", ((0, 0), (0, 1)), ()),
            False,
        ),
        (
            "an admission figure above max_work",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {
                "metric": "objective",
                "direction": "maximize",
                "k": 5,
                "space": S1,
                "max_work": 26,
            },
            ("refused", "holdout_resource_exceeded"),
            True,
        ),
        (
            "conflicting groups in both sources: all rows of selection groups only",
            [
                row(8, "ok", 5.0, "A"),
                row(2, "ok", 6.0, "A"),
                row(5, "ok", 5.0, "A"),
                row(4, "ok", 1.0, "C"),
                row(1, "ok", 2.0, "C"),
                row(0, "pruned", None, "A"),
            ],
            [row(7, "ok", 3.0, "A"), row(9, "ok", 4.0, "A")],
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            # Both eligible selection groups conflict; pruned A contributes no id.
            (
                "refused",
                "holdout_duplicate_vector",
                ((0, 1), (0, 2), (0, 4), (0, 5), (0, 8)),
                (),
            ),
            False,
        ),
        (
            "a holdout conflict after an unambiguous selection source",
            [row(0, "ok", 5.0, "A")],
            [
                row(7, "ok", 3.0, "A"),
                row(9, "ok", 4.0, "A"),
                row(6, "ok", 3.0, "A"),
            ],
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            # All three eligible rows belong to A's conflicting holdout group.
            ("refused", "holdout_duplicate_vector", ((1, 6), (1, 7), (1, 9)), ()),
            False,
        ),
        (
            "a cell of an ok row that is a string",
            [row(0, "ok", 1.0, "A", {"m": "x"})],
            [row(7, "ok", 2.0, "A", {"m": 2.0})],
            {"metric": "m", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_value_invalid", ((0, 0),), ()),
            False,
        ),
        (
            "a cell that is not a number in a row that is not ok but cannot be written",
            [row(0, "pruned", None, "A", {"m": [float("nan")]})],
            [row(7, "ok", 2.0, "A", {"m": 2.0})],
            {"metric": "m", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_value_invalid", ((0, 0),), ()),
            False,
        ),
        (
            "an integer cell too large for a binary64",
            [row(0, "ok", 1.0, "A", {"m": 2**1024})],
            [row(7, "ok", 2.0, "A", {"m": 2.0})],
            {"metric": "m", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_value_invalid", ((0, 0),), ()),
            False,
        ),
        (
            "a pruned row's object cell whose mixed-type keys cannot be sorted",
            [row(0, "pruned", None, "A", {"m": {1: "a", "b": 2}})],
            [row(7, "ok", 2.0, "A", {"m": 2.0})],
            {"metric": "m", "direction": "maximize", "k": 1, "space": S1},
            # The canonical writer cannot sort int and str keys; row 0 is named.
            ("refused", "holdout_value_invalid", ((0, 0),), ()),
            False,
        ),
        (
            "a cell that cannot be written as JSON at all",
            [row(0, "pruned", None, "A", {"m": {1, 2}})],
            [row(7, "ok", 2.0, "A", {"m": 2.0})],
            {"metric": "m", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_value_invalid", ((0, 0),), ()),
            False,
        ),
        (
            "a difference of two finite values that overflows binary64",
            [row(0, "ok", -1e308, "A")],
            [row(7, "ok", 1e308, "A")],
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_value_invalid", ((0, 0), (1, 7)), ()),
            False,
        ),
    ]
    for label, selection, holdout, options, expected, code_only in cases:
        found = oracle_outcome(
            copy.deepcopy(selection), copy.deepcopy(holdout), options
        )[0]
        if code_only:
            found_view = ("refused", found.get("code"))
        else:
            found_view = outcome_view(found)
        ledger.compare("fixture", f"refusal: {label}", found_view, expected, note)
    # The same call at max_work equal to the figure is admitted: the boundary, not a refusal.
    admitted = oracle_outcome(
        copy.deepcopy(BASE_SELECTION),
        copy.deepcopy(BASE_HOLDOUT),
        {
            "metric": "objective",
            "direction": "maximize",
            "k": 5,
            "space": S1,
            "max_work": 27,
        },
    )[0]
    ledger.compare(
        "fixture",
        "refusal: max_work equal to the figure 27 is admitted",
        admitted.get("status"),
        "ok",
        note,
    )
    # A string cell of a row that is not ok is not refused; the row is excluded by its status.
    stray = [
        row(0, "pruned", None, "A", {"m": "x"}),
        row(1, "ok", 1.0, "B", {"m": 1.0}),
    ]
    document = oracle_outcome(
        copy.deepcopy(stray),
        [row(7, "ok", 2.0, "B", {"m": 2.0})],
        {"metric": "m", "direction": "maximize", "k": 1, "space": S1},
    )[0]
    ledger.compare(
        "fixture",
        "refusal: a string cell of a pruned row is an exclusion, not a refusal",
        document.get("selection", {}).get("excluded"),
        {"status_pruned": {"count": 1, "ids": [0]}},
        note,
    )
    ledger.end(len(cases) + 2)


def section_controls(ledger: Ledger) -> None:
    """Detect one-sided mutations of the oracle's own copy."""
    ledger.begin(
        "controls: the comparison can fail, and the oracle follows a changed input"
    )
    baseline = oracle_outcome(
        copy.deepcopy(BASE_SELECTION),
        copy.deepcopy(BASE_HOLDOUT),
        {"metric": "objective", "direction": "maximize", "k": 5, "space": S1},
    )[0]
    for label, selection, holdout, options, differing, agreeing in CONTROL_CASES:
        document = oracle_outcome(
            copy.deepcopy(selection), copy.deepcopy(holdout), options
        )[0]
        for path in differing:
            ledger.check(
                "control",
                f"{label}: {'.'.join(str(step) for step in path)} differs",
                not same(dig(document, path), dig(baseline, path)),
            )
        for path in agreeing:
            ledger.check(
                "control",
                f"{label}: {'.'.join(str(step) for step in path)} is unchanged",
                same(dig(document, path), dig(baseline, path)),
            )
        ledger.check(
            "control",
            f"{label}: the whole document differs",
            not same(document, baseline),
        )
    ledger.note(
        "each mutation touches the oracle's own copy of the fixture only; the same "
        "cases are run against the module in phase 2 with the module fed the "
        "unmutated input"
    )
    spearman_control(ledger, baseline, baseline)
    ledger.end(sum(len(case[4]) + len(case[5]) + 1 for case in CONTROL_CASES) + 2)


def control_swapped_tie():
    """Break the tie of two 12.0 vectors the other way: trial id 4 comes first."""
    rows = copy.deepcopy(BASE_SELECTION)
    for raw in rows:
        if raw["parameters"]["arm"] == "B":
            raw["trial_id"] = 4
        elif raw["parameters"]["arm"] == "D":
            raw["trial_id"] = 1
    return rows, copy.deepcopy(BASE_HOLDOUT)


def control_nudged_value():
    """Move one holdout value by one ulp, breaking its tie group."""
    rows = copy.deepcopy(BASE_HOLDOUT)
    for raw in rows:
        if raw["parameters"]["arm"] == "B":
            raw["objective"] = math.nextafter(4.0, math.inf)
    return copy.deepcopy(BASE_SELECTION), rows


def control_raised_selection():
    """Raise one selection value so the followed set reorders."""
    rows = copy.deepcopy(BASE_SELECTION)
    for raw in rows:
        if raw["parameters"]["arm"] == "A":
            raw["objective"] = 13.0
    return rows, copy.deepcopy(BASE_HOLDOUT)


CONTROL_CASES = [
    (
        "the tie of two selection values broken the other way",
        *control_swapped_tie(),
        {"metric": "objective", "direction": "maximize", "k": 5, "space": S1},
        [("followed", 0, "selection_rank"), ("unmatched", 0, "selection_rank")],
        [("statistics", "spearman")],
    ),
    (
        "one holdout value nudged by one unit in the last place",
        *control_nudged_value(),
        {"metric": "objective", "direction": "maximize", "k": 5, "space": S1},
        [
            ("followed", 0, "holdout_rank"),
            ("statistics", "spearman_ranks", "y"),
            ("statistics", "better_half", "numerator"),
            ("statistics", "spearman"),
        ],
        [("pool", "size")],
    ),
    (
        "one join key edited in the oracle's own copy of the selection source",
        copy.deepcopy(BASE_SELECTION),
        copy.deepcopy(BASE_HOLDOUT),
        {
            "metric": "objective",
            "direction": "maximize",
            "k": 5,
            "space": S1,
            "key_map": {0: {KEY_B: KEY_G}},
        },
        [("followed",), ("unmatched",), ("pool", "size"), ("pool", "holdout_only")],
        [],
    ),
    (
        "one selection value raised so that the followed set reorders",
        *control_raised_selection(),
        {"metric": "objective", "direction": "maximize", "k": 5, "space": S1},
        [
            ("followed",),
            ("statistics", "spearman"),
            ("statistics", "spearman_ranks", "y"),
        ],
        [("pool", "size")],
    ),
]


def dig(document, path):
    node = document
    for step in path:
        node = node[step]
    return node


# ---------------------------------------------------------------------------------------------
# Seeded random instances.
# ---------------------------------------------------------------------------------------------


def random_instance(rng: random.Random):
    """A small two-dimension space with rows drawn from a handful of values, so that ties and
    repeated vectors occur. Returns (space, selection rows, holdout rows, options)."""
    choices = ["A", "B", "C", "D"]
    space = {
        "parameters": {
            "arm": {"kind": "categorical", "choices": list(choices)},
            "n": {"kind": "integer", "low": 0, "high": 2, "step": 1, "log": False},
        },
        "objective": {
            "expression": "metrics.all.net_profit_pct",
            "direction": "maximize",
            "constraints": [],
        },
    }
    statuses = [
        "ok",
        "ok",
        "ok",
        "ok",
        "ok",
        "ok",
        "pruned",
        "constraint_violation",
        "engine_error",
        "partial",
        "trial_timeout",
    ]
    named = rng.random() < 0.5
    metric = "m" if named else "objective"
    direction = rng.choice(DIRECTIONS)
    k = rng.randint(1, 6)

    def build(index):
        rows = []
        for trial_id in rng.sample(range(80), rng.randint(1, 7)):
            status = rng.choice(statuses)
            value = float(rng.randint(0, 3))
            raw = {
                "trial_id": trial_id,
                "status": status,
                "feasible": status == "ok",
                "objective": (
                    value if status in ("ok", "constraint_violation") else None
                ),
                "parameters": {"arm": rng.choice(choices), "n": rng.randint(0, 2)},
            }
            if named:
                draw = rng.random()
                if draw < 0.7:
                    raw["metrics"] = {"m": float(rng.randint(0, 3))}
                elif draw < 0.85:
                    raw["metrics"] = {"m": None}
                else:
                    raw["metrics"] = {}
            rows.append(raw)
        return rows

    return (
        space,
        build(0),
        build(1),
        {"metric": metric, "direction": direction, "k": k, "space": space},
    )


def generate_instances(rng: random.Random, count: int) -> list:
    instances = []
    for _number in range(count):
        space, selection, holdout, options = random_instance(rng)
        document, extra = oracle_outcome(selection, holdout, options)
        instances.append((selection, holdout, options, document, extra))
    return instances


def section_random_internal(ledger: Ledger, instances: list) -> None:
    ledger.begin("seeded random instances: the oracle's own routes and its accounting")
    outcomes: dict = {}
    checked = 0
    source_names = ("selection", "holdout")
    for number, instance in enumerate(instances):
        selection, holdout, _options, document, extra = instance
        if document.get("status") == "refused":
            outcomes[document["code"]] = outcomes.get(document["code"], 0) + 1
            continue
        outcomes["ok"] = outcomes.get("ok", 0) + 1
        checked += 1
        ledger.compare(
            "internal",
            f"instance {number}: the counting ranks equal the sorted ranks",
            extra["ranks_agree"],
            True,
            quiet=True,
        )
        ledger.compare(
            "internal",
            f"instance {number}: the deviations sums equal the one-pass sums",
            extra["terms_agree"],
            True,
            quiet=True,
        )
        for name, rows in zip(source_names, (selection, holdout)):
            part = document[name]
            ledger.compare(
                "internal",
                f"instance {number}: {name} eligible and excluded counts "
                "cover rows read",
                accounted_rows(part),
                len(rows),
                quiet=True,
            )
    ledger.note(f"outcomes {outcomes}")
    if outcomes.get("ok", 0) < len(instances) // 3:
        ledger.fail("too few random instances produced a document to be meaningful")
    if not any(code != "ok" for code in outcomes):
        ledger.fail("the random instances never exercised a refusal")
    # Two route comparisons plus one row-accounting comparison for each source.
    # Keep this independent of the ledger's actual count to detect skipped checks.
    ledger.end((2 + len(source_names)) * checked)


# ---------------------------------------------------------------------------------------------
# Phase 2: the module, imported only now.
# ---------------------------------------------------------------------------------------------


def load_module():
    sys.path.insert(0, str(ROOT / "python"))
    sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))
    from pineforge_hpo import holdout

    return holdout


def module_call(module, selection, holdout, options) -> dict:
    """The module's document, or its refusal; only the module's own refusal type is caught."""
    try:
        return module.holdout_compare(selection, holdout, **options)
    except module.HoldoutError as error:
        return refusal(error.code, error.ids, error.names)


def module_worked(ledger: Ledger, module) -> None:
    ledger.begin("module: worked fixtures, key by key")
    for label, selection, holdout, options, expected, source in WORKED:
        oracle = oracle_outcome(
            copy.deepcopy(selection), copy.deepcopy(holdout), options
        )[0]
        found = module_call(
            module, copy.deepcopy(selection), copy.deepcopy(holdout), options
        )
        if found.get("status") == "refused" or oracle.get("status") == "refused":
            ledger.compare(
                "module",
                f"{label}: the outcome",
                outcome_view(oracle),
                outcome_view(found),
                source,
            )
            continue
        for key in expected:
            if key == "statistics":
                ledger.compare(
                    "module",
                    f"{label}: statistics, with spearman within one ulp",
                    (
                        blank_spearman(oracle["statistics"]),
                        within_one_ulp(
                            oracle["statistics"]["spearman"],
                            found["statistics"]["spearman"],
                        ),
                    ),
                    (blank_spearman(found["statistics"]), True),
                    source,
                )
                continue
            ledger.compare(
                "module",
                f"{label}: {key}",
                oracle[key],
                found.get(key),
                source,
                quiet=True,
            )
        ledger.compare(
            "module",
            f"{label}: input_sha256",
            oracle["input_sha256"],
            found.get("input_sha256"),
            source,
        )
        ledger.compare(
            "module",
            f"{label}: interpretation",
            oracle["interpretation"],
            found.get("interpretation"),
            source,
            quiet=True,
        )
        for name in ("selection", "holdout"):
            ledger.compare(
                "module",
                f"{label}: {name} eligible and excluded counts cover rows read",
                oracle[name]["rows"],
                accounted_rows(found[name]),
                source,
            )
    ledger.end(sum(len(case[4]) + 4 for case in WORKED))


def module_refusals(ledger: Ledger, module) -> None:
    ledger.begin("module: the refusals, and the boundary that is admitted")
    note = "refusal code, ids and names"
    table = [
        (
            "missing k before an invalid metric",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {"metric": 7, "direction": "maximize", "k": None, "space": S1},
            ("refused", "holdout_parameter_missing", (), ("k",)),
        ),
        (
            "missing named-metric direction before an invalid k",
            NAMED_SELECTION,
            NAMED_HOLDOUT,
            {"metric": "m", "direction": None, "k": 0, "space": S1},
            ("refused", "holdout_parameter_missing", (), ("direction",)),
        ),
        (
            "a non-string metric with direction given as None",
            BASE_SELECTION,
            BASE_HOLDOUT,
            {"metric": 7, "direction": None, "k": 1, "space": S1},
            ("refused", "holdout_parameter_invalid", (), ("metric",)),
        ),
        (
            "a pruned row's object cell whose mixed-type keys cannot be sorted",
            [row(0, "pruned", None, "A", {"m": {1: "a", "b": 2}})],
            [row(7, "ok", 2.0, "A", {"m": 2.0})],
            {"metric": "m", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_value_invalid", ((0, 0),), ()),
        ),
        (
            "a difference of two finite values that overflows binary64",
            [row(0, "ok", -1e308, "A")],
            [row(7, "ok", 1e308, "A")],
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_value_invalid", ((0, 0), (1, 7)), ()),
        ),
        (
            "two eligible rows of one vector whose values differ",
            REPEAT_DISAGREEING,
            REPEAT_HOLDOUT,
            {"metric": "objective", "direction": "maximize", "k": 2, "space": S1},
            ("refused", "holdout_duplicate_vector", ((0, 0), (0, 1)), ()),
        ),
        (
            "conflicting groups in both sources: all rows of selection groups only",
            [
                row(8, "ok", 5.0, "A"),
                row(2, "ok", 6.0, "A"),
                row(5, "ok", 5.0, "A"),
                row(4, "ok", 1.0, "C"),
                row(1, "ok", 2.0, "C"),
                row(0, "pruned", None, "A"),
            ],
            [row(7, "ok", 3.0, "A"), row(9, "ok", 4.0, "A")],
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            (
                "refused",
                "holdout_duplicate_vector",
                ((0, 1), (0, 2), (0, 4), (0, 5), (0, 8)),
                (),
            ),
        ),
        (
            "a holdout conflict after an unambiguous selection source",
            [row(0, "ok", 5.0, "A")],
            [
                row(7, "ok", 3.0, "A"),
                row(9, "ok", 4.0, "A"),
                row(6, "ok", 3.0, "A"),
            ],
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_duplicate_vector", ((1, 6), (1, 7), (1, 9)), ()),
        ),
        (
            "a holdout ancestor row that repeats a selection trial id",
            ANCESTOR_OVERLAP_SELECTION,
            ANCESTOR_OVERLAP_HOLDOUT,
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_ancestor_rows", ((1, 0),), ()),
        ),
        (
            "one source whose two ok rows disagree on a backtest counter",
            [
                row(0, "ok", 1.0, "A", backtest={"input_bars_processed": 100}),
                row(1, "ok", 2.0, "B", backtest={"input_bars_processed": 200}),
            ],
            [row(7, "ok", 3.0, "A")],
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            (
                "refused",
                "holdout_context_mismatch",
                ((0, 0), (0, 1)),
                ("input_bars_processed",),
            ),
        ),
        (
            "a parameter name that is not a dimension of the space",
            [row(0, "ok", 1.0, "A"), row(1, "ok", 2.0, "B", parameters={"b": 1.0})],
            [row(7, "ok", 3.0, "A")],
            {"metric": "objective", "direction": "maximize", "k": 1, "space": S1},
            ("refused", "holdout_input_invalid", ((0, 1),), ("arm", "b")),
        ),
        (
            "a space mismatch between two recorded spaces",
            {"trials": [row(0, "ok", 1.0, "A", space=S1)]},
            {"trials": [row(7, "ok", 2.0, "A", space=S2)]},
            {"metric": "objective", "direction": "maximize", "k": 1, "space": None},
            ("refused", "holdout_space_mismatch", ((1, 7),), ("arm",)),
        ),
    ]
    for label, selection, holdout, options, expected in table:
        oracle = oracle_outcome(
            copy.deepcopy(selection), copy.deepcopy(holdout), options
        )[0]
        found = module_call(
            module, copy.deepcopy(selection), copy.deepcopy(holdout), options
        )
        ledger.compare(
            "module",
            f"refusal: {label}",
            outcome_view(oracle),
            outcome_view(found),
            note,
        )
    admitted_oracle = oracle_outcome(
        copy.deepcopy(BASE_SELECTION),
        copy.deepcopy(BASE_HOLDOUT),
        {
            "metric": "objective",
            "direction": "maximize",
            "k": 5,
            "space": S1,
            "max_work": 27,
        },
    )[0]
    admitted_module = module_call(
        module,
        copy.deepcopy(BASE_SELECTION),
        copy.deepcopy(BASE_HOLDOUT),
        {
            "metric": "objective",
            "direction": "maximize",
            "k": 5,
            "space": S1,
            "max_work": 27,
        },
    )
    ledger.compare(
        "module",
        "the figure 27 is admitted and 26 is refused",
        (
            (admitted_oracle.get("status"), None),
            (
                oracle_outcome(
                    copy.deepcopy(BASE_SELECTION),
                    copy.deepcopy(BASE_HOLDOUT),
                    {
                        "metric": "objective",
                        "direction": "maximize",
                        "k": 5,
                        "space": S1,
                        "max_work": 26,
                    },
                )[0].get("code"),
                None,
            ),
        ),
        (
            (admitted_module.get("status"), None),
            (
                module_call(
                    module,
                    copy.deepcopy(BASE_SELECTION),
                    copy.deepcopy(BASE_HOLDOUT),
                    {
                        "metric": "objective",
                        "direction": "maximize",
                        "k": 5,
                        "space": S1,
                        "max_work": 26,
                    },
                ).get("code"),
                None,
            ),
        ),
        note,
    )
    ledger.end(len(table) + 1)


def module_controls(ledger: Ledger, module) -> None:
    ledger.begin("module: the one-sided controls, the module fed the unmutated input")
    for label, selection, holdout, options, _differing, _agreeing in CONTROL_CASES:
        # The module is given the unmutated fixture; only the oracle's copy is mutated.
        clean_options = {
            key: value for key, value in options.items() if key != "key_map"
        }
        found = module_call(
            module,
            copy.deepcopy(BASE_SELECTION),
            copy.deepcopy(BASE_HOLDOUT),
            clean_options,
        )
        oracle = oracle_outcome(
            copy.deepcopy(selection), copy.deepcopy(holdout), options
        )[0]
        ledger.check(
            "control",
            f"{label}: the mutated oracle disagrees with the module",
            not same(outcome_view(oracle), outcome_view(found)),
        )
    options = {"metric": "objective", "direction": "maximize", "k": 5, "space": S1}
    baseline = oracle_outcome(
        copy.deepcopy(BASE_SELECTION), copy.deepcopy(BASE_HOLDOUT), options
    )[0]
    found = module_call(
        module, copy.deepcopy(BASE_SELECTION), copy.deepcopy(BASE_HOLDOUT), options
    )
    if (
        found.get("status") == "refused"
        or type(found["statistics"]["spearman"]) is not float
    ):
        ledger.compare(
            "control",
            "the unshifted documents agree before the two-ulp Spearman change",
            *document_views(baseline, found),
        )
        ledger.check(
            "control", "the unmutated control fixture has a numeric Spearman", False
        )
    else:
        spearman_control(ledger, baseline, found)
    ledger.end(len(CONTROL_CASES) + 2)


def module_random(ledger: Ledger, module, instances: list, seed: int) -> None:
    ledger.begin(
        f"module: the seeded random instances (seed {seed}), whole documents compared"
    )
    shuffler = random.Random(seed + 1)
    accounted = 0
    for number, (selection, holdout, options, document, _extra) in enumerate(instances):
        found = module_call(
            module, copy.deepcopy(selection), copy.deepcopy(holdout), options
        )
        ledger.compare(
            "module",
            f"instance {number}: the whole document, spearman within one ulp",
            *document_views(document, found),
            quiet=True,
        )
        if document.get("status") == "ok" and found.get("status") == "ok":
            for name in ("selection", "holdout"):
                ledger.compare(
                    "module",
                    f"instance {number}: {name} eligible and excluded counts "
                    "cover rows read",
                    document[name]["rows"],
                    accounted_rows(found[name]),
                    quiet=True,
                )
            accounted += 1
        moved = copy.deepcopy(selection)
        shuffler.shuffle(moved)
        shuffled = oracle_outcome(moved, copy.deepcopy(holdout), options)[0]
        answer = module_call(
            module, copy.deepcopy(moved), copy.deepcopy(holdout), options
        )
        ledger.compare(
            "module",
            f"instance {number}: the selection rows shuffled, spearman within one ulp",
            *document_views(shuffled, answer),
            quiet=True,
        )
    ledger.end(2 * len(instances) + 2 * accounted)


def phase_two(ledger: Ledger, instances: list, seed: int) -> None:
    print("\n##### phase 2: the module under test")
    ledger.begin("independence: nothing of the package was imported before phase 2")
    ledger.check(
        "internal",
        "'pineforge_hpo' is not in sys.modules before phase 2",
        "pineforge_hpo" not in sys.modules,
    )
    ledger.end(1)
    module = load_module()
    module_worked(ledger, module)
    module_refusals(ledger, module)
    module_controls(ledger, module)
    module_random(ledger, module, instances, seed)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Independent oracle for the holdout comparison of two finished studies"
        )
    )
    parser.add_argument(
        "--no-module",
        action="store_true",
        help=(
            "run phase 1 only (the oracle against the pinned fixture values); "
            "the module is not imported, the verdict says so and the exit code "
            "is 3, never 0"
        ),
    )
    parser.add_argument(
        "--cases", type=int, default=120, help="seeded random instances (default 120)"
    )
    parser.add_argument(
        "--seed", type=int, default=20261012, help="random seed (default 20261012)"
    )
    args = parser.parse_args(argv)
    if args.cases < 30:
        parser.error("--cases must be at least 30")
    ledger = Ledger(strict=True)
    print(
        f"holdout oracle: root {ROOT}, python {sys.version.split()[0]}, "
        f"cases {args.cases}, seed {args.seed}"
    )
    harness_self_test()
    print("##### phase 1: the oracle alone (no module imported)")
    section_worked(ledger)
    section_internal(ledger)
    section_refusals(ledger)
    section_controls(ledger)
    instances = generate_instances(random.Random(args.seed), args.cases)
    section_random_internal(ledger, instances)
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
            f"RESULT: PHASE 1 ONLY (exit {NO_MODULE_EXIT}): the oracle agrees with "
            "the pinned values, but the module is NOT run in this mode. No comparison "
            "with the module is made, so this run says nothing about the module."
        )
        return NO_MODULE_EXIT
    print("RESULT: PASS (oracle agrees with the pinned values and with the module)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
