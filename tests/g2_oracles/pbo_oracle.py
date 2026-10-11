#!/usr/bin/env python3
"""Independent oracle for ``pineforge_hpo.pbo`` (CSCV and the probability of backtest overfitting).

The module turns a table of block moments (count, sum, centred second moment) into the share of
splits whose training winner lands in the lower half out of sample. It merges blocks pairwise
(Chan/Pebay), compares binary64 statistics and enumerates splits with ``itertools.combinations``.
This script reaches the same quantities by other routes and never takes a number from the module:

* the splits are enumerated by scanning every S-bit mask for a popcount of S/2; their number is
  also built with Pascal's triangle (``math.comb`` is not used);
* the ranking statistic comes from the raw interval returns by the textbook two-pass definition
  (mean over sample standard deviation of all intervals of the half), in exact rational
  arithmetic. Trials are compared through ``sign(mean) * mean**2 / variance``, a strictly
  increasing function of that statistic, so no square root is taken and ties are exact;
* when only moments exist, the pooled second moment is the within-plus-between (parallel axis)
  decomposition of the blocks, not a pairwise merge;
* the out-of-sample rank is the mid-rank, omega = rank / (N + 1) and odds = omega / (1 - omega)
  as exact fractions; a split counts below, at or above lambda = 0 by odds against 1, and k tied
  training winners carry weight 1/k each.

Phase 1 needs no module: it compares those numbers with the values the test file pins for the
fixtures (hand-derived there; re-derived here from the inputs), and with three cases derived by
hand in this file that the fixtures lack: negative means, a zero mean and returns that change sign.
Phase 2 imports
``pineforge_hpo.pbo`` only after phase 1 is finished (and checks that nothing of the package was
imported before), runs the module on the same inputs and compares its outputs with the checker's.

Usage: ``python3 tests/g2_oracles/pbo_oracle.py [--no-module] [--cases N] [--seed S]``.
Exit code 0 only if every comparison agreed, every section made the comparisons it must make and the
module was compared; 1 for any failure; 2 for a usage error; 3 when ``--no-module`` ran phase 1 only
(the checker against the pinned values, the module NOT compared). A run that compared nothing
with the module is never exit 0.
"""

from __future__ import annotations

import argparse
import copy
from decimal import Decimal, localcontext
from fractions import Fraction
from functools import lru_cache
import json
import math
from pathlib import Path
import random
import sys

# Write nothing: no bytecode cache beside this script or inside the package that phase 2 imports.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "tests" / "python" / "fixtures" / "pbo"
# Exit code of a run that did not compare the module (--no-module); 0 is kept for a full pass.
NO_MODULE_EXIT = 3

# ---------------------------------------------------------------------------------------------
# Values the test file pins. Each was re-derived by hand from the fixture inputs by the author of
# this oracle (not copied unchecked); the source column names the test that pins it.
# ---------------------------------------------------------------------------------------------

# test_pbo.py EnumerationTests.test_split_counts_for_the_dossier_sizes: C(S, S/2) for the dossier's
# sizes.
SPLIT_COUNTS = {2: 2, 4: 6, 8: 70, 12: 924, 16: 12870, 20: 184756, 24: 2704156}


def two_point(means_by_trial: list[tuple[int, ...]]) -> list[list[list[int]]]:
    """Blocks of the two intervals (m - 1, m + 1) for each block mean m (fixture descriptions)."""
    return [[[mean - 1, mean + 1] for mean in trial] for trial in means_by_trial]


# Raw interval returns, series[trial][block] = the block's intervals, from each fixture's own
# description (``description`` member of the JSON and the docstring of the test that uses it).
RAW: dict[str, tuple[int, list[list[list[int]]]]] = {
    "splits_s4_n4": (
        4,
        [
            [[6], [2], [7], [3]],
            [[4], [9], [3], [11]],
            [[8], [5], [1], [9]],
            [[2], [4], [10], [7]],
        ],
    ),
    "tie_s2_n3": (2, two_point([(4, 3), (4, 1), (1, 2)])),
    "f4_atom_s2_n3": (2, two_point([(5, 2), (1, 3), (3, 1)])),
}

F = Fraction
# per_split maps (train blocks, test blocks) to [(winning trial, odds)], odds = rank / (N+1-rank).
EXPECTED: dict[str, dict] = {
    "splits_s4_n4": {  # test_pbo.py HandCountTests.test_s4_counts_and_lambdas
        "n_splits": 6,
        "counts": (3, 0, 3),
        "rates": (0.5, 0.5),
        "per_split": {
            ((0, 1), (2, 3)): [(2, F(1, 4))],
            ((2, 3), (0, 1)): [(3, F(3, 2))],
            ((0, 2), (1, 3)): [(0, F(3, 2))],
            ((1, 3), (0, 2)): [(1, F(3, 2))],
            ((0, 3), (1, 2)): [(2, F(1, 4))],
            ((1, 2), (0, 3)): [(3, F(1, 4))],
        },
        "odds": [F(1, 4), F(3, 2), F(3, 2), F(3, 2), F(1, 4), F(1, 4)],
        "tie_sizes": [1, 1, 1, 1, 1, 1],
        "source": "test_pbo.py HandCountTests.test_s4_counts_and_lambdas",
    },
    "tie_s2_n3": {  # test_pbo.py HandCountTests.test_tied_training_bests_split_their_weight
        "n_splits": 2,
        "counts": (F(1, 2), 0, F(3, 2)),
        "rates": (0.25, 0.25),
        "per_split": {
            ((0,), (1,)): [(0, F(3, 1)), (1, F(1, 3))],
            ((1,), (0,)): [(0, F(5, 3))],
        },
        "odds": [F(3, 1), F(1, 3), F(5, 3)],
        "tie_sizes": [2, 2, 1],
        "source": "test_pbo.py HandCountTests.test_tied_training_bests_split_their_weight",
    },
    "f4_atom_s2_n3": {  # test_pbo.py HandCountTests.test_lambda_zero_atom_gives_two_different_rates
        "n_splits": 2,
        "counts": (1, 1, 0),
        "rates": (1.0, 0.5),
        "per_split": {
            ((0,), (1,)): [(0, F(1, 1))],
            ((1,), (0,)): [(1, F(1, 3))],
        },
        "odds": [F(1, 1), F(1, 3)],
        "tie_sizes": [1, 1],
        "source": "test_pbo.py HandCountTests.test_lambda_zero_atom_gives_two_different_rates",
    },
}

# Cases with negative, zero and sign-changing means (finding P1-2). The three fixture files above
# hold only non-negative means, so nothing there shows the module a negative or a zero pooled mean.
# These cases are not in tests/python: every number below is derived by hand in this file, from the
# raw returns and the pin's statistic (the pooled mean over the pooled sample deviation of the
# intervals of the half). Hand arithmetic used throughout: a block of the two intervals
# (m - 1, m + 1) has mean m and sample variance 2, so its ranking key sign(m) * m**2 / 2 is
# -m**2 / 2 for a negative mean and 0 for m = 0; two single intervals (a, b) pool to the mean
# (a + b) / 2 and the sample variance (a - b)**2 / 2, so their key is
# sign(a + b) * (a + b)**2 / (2 * (a - b)**2).
SIGNED_RAW: dict[str, tuple[int, list[list[list[int]]]]] = {
    "neg_s2_n3": (2, two_point([(-4, -3), (-4, -1), (-1, -2)])),
    "zero_s2_n3": (2, two_point([(0, 2), (-2, 0), (-3, -1)])),
    "sign_s4_n3": (
        4,
        [
            [[-5], [2], [-1], [4]],
            [[3], [-2], [6], [-4]],
            [[1], [5], [-3], [-6]],
        ],
    ),
}
SIGNED_EXPECTED: dict[str, dict] = {
    # Block means (-4, -3), (-4, -1), (-1, -2); keys -8, -8, -1/2 on block 0 and -9/2, -1/2, -2 on
    # block 1. Train {0}: trial 2 wins alone (-1/2); on block 1 one key is smaller (-9/2) and its
    # own is the only equal one: mid-rank 1 + 1 = 2 of N + 1 = 4, omega 1/2, odds 1. Train {1}:
    # trial 1 wins (-1/2); on block 0 its key -8 ties with trial 0's: nothing smaller, two equal,
    # mid-rank 3/2, omega 3/8, odds 3/5. A ranking by mean**2 or abs(mean) would pick trials 0 and
    # 1 together on block 0, a ranking clamped at zero would tie all three: both give other counts.
    "neg_s2_n3": {
        "signs": [-1],
        "n_splits": 2,
        "counts": (1, 1, 0),
        "rates": (1.0, 0.5),
        "per_split": {
            ((0,), (1,)): [(2, F(1, 1))],
            ((1,), (0,)): [(1, F(3, 5))],
        },
        "odds": [F(1, 1), F(3, 5)],
        "tie_sizes": [1, 1],
    },
    # Block means (0, 2), (-2, 0), (-3, -1); keys 0, -2, -9/2 on block 0 and 2, 0, -1/2 on block 1.
    # Train {0}: trial 0 wins with the zero key, above both negative keys; on block 1 its key 2 has
    # two smaller keys: rank 3 of 4, omega 3/4, odds 3. Train {1}: trial 0 wins (2); on block 0 its
    # key 0 has two smaller keys (-2, -9/2): rank 3, odds 3. A module that refused or mis-ranked a
    # zero mean, or ranked by mean**2 (trial 2 would win block 0), gives other counts.
    "zero_s2_n3": {
        "signs": [-1, 0, 1],
        "n_splits": 2,
        "counts": (0, 0, 2),
        "rates": (0.0, 0.0),
        "per_split": {
            ((0,), (1,)): [(0, F(3, 1))],
            ((1,), (0,)): [(0, F(3, 1))],
        },
        "odds": [F(3, 1), F(3, 1)],
        "tie_sizes": [1, 1],
    },
    # Returns that change sign inside every trial. Keys (trial 0, trial 1, trial 2) by half:
    #   {0,1}: -9/98, 1/50, 9/8       {2,3}: 9/50, 1/50, -9/2
    #   {0,2}: -9/8, 9/2, -1/8        {1,3}: 9/2, -9/2, -1/242
    #   {0,3}: -1/162, -1/98, -25/98  {1,2}: 1/18, 1/8, 1/32
    # Winners by training half: {0,1} trial 2, {2,3} trial 0, {0,2} trial 1, {1,3} trial 0, {0,3}
    # trial 0, {1,2} trial 1. The first four winners hold the smallest key of the testing half (rank
    # 1 of 4, omega 1/4, odds 1/3); on {1,2} trial 0 and on {0,3} trial 1 have one smaller key (rank
    # 2, odds 1). So 4 splits below, 2 at lambda = 0, none above; rates 6/6 and 4/6.
    "sign_s4_n3": {
        "signs": [-1, 1],
        "n_splits": 6,
        "counts": (4, 2, 0),
        "rates": (1.0, 0.6666666666666666),
        "per_split": {
            ((0, 1), (2, 3)): [(2, F(1, 3))],
            ((2, 3), (0, 1)): [(0, F(1, 3))],
            ((0, 2), (1, 3)): [(1, F(1, 3))],
            ((1, 3), (0, 2)): [(0, F(1, 3))],
            ((0, 3), (1, 2)): [(0, F(1, 1))],
            ((1, 2), (0, 3)): [(1, F(1, 1))],
        },
        "odds": [F(1, 3), F(1, 3), F(1, 3), F(1, 3), F(1, 1), F(1, 1)],
        "tie_sizes": [1, 1, 1, 1, 1, 1],
    },
}

# docs/pbo.md D6 (line 185) and the result table (line 236): the roster label of a study
# follows from its sampler and from whether its result had a no-improvement early stop. The
# early stop is its own input (the ``early_stop`` object beside the sampler in the native
# result), not a sampler name: any name other than the six below refuses, ``early_stop``
# included (line 356). Each entry is read off the D6 text:
#   "the samplers tpe, dlib_global and candidates" are diagnostic_adaptive_roster, stopped or not;
#   "any study whose result had a no-improvement early stop" is diagnostic_adaptive_roster whatever
#   its sampler, grid, random and sobol included;
#   "the label paper_conformant is used only for grid, random and sobol without an early stop".
# Key: (sampler, whether the study had an early stop); value: the label.
ROSTER_LABELS = {
    ("grid", False): "paper_conformant",
    ("random", False): "paper_conformant",
    ("sobol", False): "paper_conformant",
    ("tpe", False): "diagnostic_adaptive_roster",
    ("dlib_global", False): "diagnostic_adaptive_roster",
    ("candidates", False): "diagnostic_adaptive_roster",
    ("grid", True): "diagnostic_adaptive_roster",
    ("random", True): "diagnostic_adaptive_roster",
    ("sobol", True): "diagnostic_adaptive_roster",
    ("tpe", True): "diagnostic_adaptive_roster",
    ("dlib_global", True): "diagnostic_adaptive_roster",
    ("candidates", True): "diagnostic_adaptive_roster",
}

# test_pbo.py ArithmeticTests.test_work_figures, the admission figure (n_splits, merges,
# comparisons, work); the S = 24 row is pinned for n_splits and merges only.
WORK_CASES = [
    ((2, 3), {"n_splits": 2, "merges": 0, "comparisons": 12, "work": 12}),
    ((4, 4), {"n_splits": 6, "merges": 24, "comparisons": 48, "work": 72}),
    (
        (16, 1000),
        {"n_splits": 12870, "merges": 90090000, "comparisons": 25740000, "work": 115830000},
    ),
    ((24, 1000), {"n_splits": 2704156, "merges": 29745716000}),
]

# Deliberately D2-violating layouts. docs/pbo.md D2 (line 181): block sizes may differ by at most
# one interval and a larger spread refuses; the Refusals table (line 342):
# ``pbo_block_sizes_uneven`` when the largest n_b minus the smallest is more than 1. Each entry is
# (label, interval count of every block, spread by hand = largest count minus smallest). The
# layouts are chosen so that a one-line change of the module's test is caught by at least one of
# them (the mutations are in the docstring of ``module_refusals``): (2, 4) is the smallest
# violation; (4, 3, 2, 3) starts at its maximum and its neighbours differ by one; (2, 4, 8, 4) has
# the spread 6 of the counts that the random cases drew before D2; (8, 9, 10, 9) differs by two at
# larger counts, where a ratio rule (the largest more than twice the smallest) would accept it.
UNEVEN_LAYOUTS = [
    ("S=2, blocks of 2 and 4 intervals", (2, 4), 2),
    ("S=4, blocks of 4, 3, 2 and 3 intervals", (4, 3, 2, 3), 2),
    ("S=4, blocks of 2, 4, 8 and 4 intervals", (2, 4, 8, 4), 6),
    ("S=4, blocks of 8, 9, 10 and 9 intervals", (8, 9, 10, 9), 2),
]

# ---------------------------------------------------------------------------------------------
# Harness: every comparison is counted and printed, a disagreement is recorded and never swallowed.
# ---------------------------------------------------------------------------------------------


def same(a, b) -> bool:
    """Exact equality: numbers by value (int, float and Fraction mix), bool only with bool,
    containers element by element (list and tuple are interchangeable), the rest by type and value.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    numeric = (int, float, Fraction)
    if isinstance(a, numeric) and isinstance(b, numeric):
        return a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[key], b[key]) for key in a)
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
    def __init__(self, echo: bool = True) -> None:
        self.counts: dict[str, int] = {}
        self.failures: list[str] = []
        self.name = ""
        self.made = 0
        self.echo = echo

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
        ulps: int = 0,
    ) -> bool:
        """Compare the checker's value with ``other`` (kind: fixture, internal, control, module)."""
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.made += 1
        if ulps and isinstance(checker, float) and isinstance(other, float):
            scale = math.ulp(max(abs(checker), abs(other)))
            agree = abs(checker - other) <= ulps * scale
        else:
            agree = same(checker, other)
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
    probe = Ledger(echo=False)
    probe.name = "probe"
    probe.compare("control", "equal", 1, 1)
    probe.compare("control", "different", 1, 2)
    if probe.failures != ["probe: different"] or probe.counts != {"control": 2} or probe.made != 2:
        raise SystemExit("harness self-test failed: the ledger must record exactly the one failure")
    ulp_probe = Ledger(echo=False)
    ulp_probe.name = "ulp probe"
    step = math.ulp(1.0)
    ulp_probe.compare("control", "two ulps apart", 1.0, 1.0 + 2 * step, ulps=2)
    ulp_probe.compare("control", "three ulps apart", 1.0, 1.0 + 3 * step, ulps=2)
    if ulp_probe.failures != ["ulp probe: three ulps apart"]:
        raise SystemExit(
            "harness self-test failed: the ulps comparison must accept two ulps and reject three"
        )
    print("harness self-test passed (same() and the ledger tell different values apart)")


# ---------------------------------------------------------------------------------------------
# The oracle: enumeration, exact statistics, exact CSCV.
# ---------------------------------------------------------------------------------------------


def blocks_of(mask: int, size: int) -> tuple[int, ...]:
    return tuple(block for block in range(size) if (mask >> block) & 1)


def train_masks(size: int) -> list[int]:
    """Every training set of size/2 blocks as a bitmask, found by scanning all 2**size integers."""
    half = size // 2
    return [mask for mask in range(1 << size) if mask.bit_count() == half]


def pascal_central(size: int) -> int:
    """C(size, size/2) from Pascal's triangle."""
    row = [1]
    for _ in range(size):
        row = [1] + [row[index] + row[index + 1] for index in range(len(row) - 1)] + [1]
    return row[size // 2]


@lru_cache(maxsize=None)
def documented_order(size: int) -> tuple:
    """Ordered (train, test) splits in the order docs/pbo.md states: the half that holds block 0
    in lexicographic order, as train first and then as test."""
    everything = (1 << size) - 1
    with_block_zero = [mask for mask in train_masks(size) if mask & 1]
    with_block_zero.sort(key=lambda mask: blocks_of(mask, size))
    order = []
    for mask in with_block_zero:
        first = blocks_of(mask, size)
        second = blocks_of(everything ^ mask, size)
        order.append((first, second))
        order.append((second, first))
    return tuple(order)


def exact_moments(values) -> tuple:
    """(n, sum, M2) of raw values by the two-pass definition, in exact rational arithmetic."""
    n = len(values)
    total = sum(Fraction(value) for value in values)
    mean = total / n
    return n, total, sum((Fraction(value) - mean) ** 2 for value in values)


def key_from_moments(n: int, total: Fraction, m2: Fraction):
    """sign(mean) * mean**2 / sample variance: strictly increasing in mean / sample sigma.
    None when the statistic is undefined (fewer than 2 intervals or zero variance)."""
    if n < 2 or m2 == 0:
        return None
    mean = total / n
    variance = m2 / (n - 1)
    sign = (mean > 0) - (mean < 0)
    return sign * mean * mean / variance


def raw_keys(series):
    """key(trial, half) from raw returns: pool the half's intervals, then the two-pass formula."""

    def key(trial: int, half: tuple) -> object:
        values = [value for block in half for value in series[trial][block]]
        n, total, m2 = exact_moments(values)
        return key_from_moments(n, total, m2)

    return key


def moment_keys(counts, sums, m2s):
    """key(trial, half) from block moments by within + between decomposition (parallel axis)."""

    def key(trial: int, half: tuple) -> object:
        n = sum(counts[block] for block in half)
        total = sum(Fraction(sums[trial][block]) for block in half)
        mean = total / n
        within = sum(Fraction(m2s[trial][block]) for block in half)
        between = sum(
            counts[block] * (Fraction(sums[trial][block]) / counts[block] - mean) ** 2
            for block in half
        )
        return key_from_moments(n, total, within + between)

    return key


def exact_cscv(key_of, trials: int, size: int) -> dict:
    """Every ordered split, exact. A zero-variance trial refuses at the first half (partition by
    partition, the half that holds block 0 first) in which one occurs, as the contract says."""
    order = documented_order(size)
    cache: dict[tuple, list] = {}

    def keys(half: tuple) -> list:
        if half not in cache:
            cache[half] = [key_of(trial, half) for trial in range(trials)]
        return cache[half]

    for position in range(0, len(order), 2):
        for half in (order[position][0], order[position][1]):
            flat = [trial for trial, value in enumerate(keys(half)) if value is None]
            if flat:
                return {
                    "status": "refused",
                    "code": "pbo_zero_variance",
                    "ids": tuple(flat),
                    "blocks": half,
                }
    per_split: dict[tuple, list] = {}
    below = atom = above = Fraction(0)
    odds_in_order: list[Fraction] = []
    tie_sizes: list[int] = []
    for train, test in order:
        train_keys = keys(train)
        test_keys = keys(test)
        best = max(train_keys)
        winners = [trial for trial, value in enumerate(train_keys) if value == best]
        weight = Fraction(1, len(winners))
        record = []
        for winner in winners:
            reference = test_keys[winner]
            less = sum(1 for value in test_keys if value < reference)
            equal = sum(1 for value in test_keys if value == reference)
            rank = Fraction(less) + Fraction(equal + 1, 2)  # mid-rank, the winner itself included
            omega = rank / (trials + 1)
            odds = omega / (1 - omega)
            if odds < 1:
                below += weight
            elif odds == 1:
                atom += weight
            else:
                above += weight
            record.append((winner, odds))
            odds_in_order.append(odds)
            tie_sizes.append(len(winners))
        per_split[(train, test)] = record
    n_splits = len(order)
    if below + atom + above != n_splits:
        raise AssertionError("oracle accounting error: the weights do not add up to n_splits")
    return {
        "status": "ok",
        "n_splits": n_splits,
        "n_lt0": below,
        "n_eq0": atom,
        "n_gt0": above,
        "rate_le0": float((below + atom) / n_splits),
        "rate_lt0": float(below / n_splits),
        "per_split": per_split,
        "odds": odds_in_order,
        "tie_sizes": tie_sizes,
    }


def lambdas_of(result: dict) -> list[float]:
    """lambda = ln(odds); the odds are the correctly rounded float of the exact fraction."""
    return [math.log(float(odds)) for odds in result["odds"]]


def reported_count(count: Fraction):
    """What the module can report for an exact weighted split count (finding P2-1): an int when the
    count is a whole number, otherwise the nearest binary64, which is what ``float`` of a Fraction
    gives (it rounds correctly). docs/pbo.md, Exactness: 'every count and every rate (an exact
    fraction converted once to binary64)'; D4 makes a count fractional when a tie weighs 1/k."""
    return int(count) if count.denominator == 1 else float(count)


def typed(value) -> tuple:
    """(type name, value): a comparison of these also tells an int from a float."""
    return (type(value).__name__, value)


def oracle_counts(result: dict) -> tuple:
    """(n_lt0, n_eq0, n_gt0) of an exact result as the module can report them, with their types."""
    return tuple(typed(reported_count(result[name])) for name in ("n_lt0", "n_eq0", "n_gt0"))


def module_counts(result: dict) -> tuple:
    """(n_lt0, n_eq0, n_gt0) of the module's result, with their types."""
    return tuple(typed(result[name]) for name in ("n_lt0", "n_eq0", "n_gt0"))


def work_figures(size: int, trials: int) -> dict:
    """The admission figure of contract clause 3.7 by replaying its accounting: each complementary
    pair is evaluated once and each of its two halves is merged left to right for every trial;
    each ordered split scans the trials once for the winner and once for its rank."""
    n_splits = pascal_central(size)
    merges = 0
    for _ in range(n_splits // 2):
        for _half in range(2):
            merges += trials * (size // 2 - 1)
    comparisons = 0
    for _ in range(n_splits):
        comparisons += 2 * trials
    return {
        "n_splits": n_splits,
        "merges": merges,
        "comparisons": comparisons,
        "work": merges + comparisons,
    }


def decimal_sqrt(value: Fraction) -> Decimal:
    with localcontext() as context:
        context.prec = 60
        return (Decimal(value.numerator) / Decimal(value.denominator)).sqrt()


def naive_m2(values: list[float]) -> float:
    """The sum-of-squares form, left to right in binary64 (the rejected design, fixture F5)."""
    squares = 0.0
    total = 0.0
    for value in values:
        squares += value * value
        total += value
    return squares - total * total / len(values)


# ---------------------------------------------------------------------------------------------
# Fixture access and payload builders.
# ---------------------------------------------------------------------------------------------


def read_rows(name: str) -> list[dict]:
    path = FIXTURE_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))["rows"]


def file_payload(rows: list[dict]) -> list[tuple]:
    return [
        (
            row["trial_id"],
            list(row["block_returns"]["count"]),
            [Fraction(value) for value in row["block_returns"]["sum"]],
            [Fraction(value) for value in row["block_returns"]["m2"]],
        )
        for row in rows
    ]


def derived_payload(series: list) -> list[tuple]:
    out = []
    for trial, blocks in enumerate(series):
        moments = [exact_moments(values) for values in blocks]
        out.append(
            (
                trial,
                [item[0] for item in moments],
                [item[1] for item in moments],
                [item[2] for item in moments],
            )
        )
    return out


def oracle_from_rows(rows: list[dict], size: int) -> dict:
    ordered = sorted(rows, key=lambda row: row["trial_id"])
    counts = ordered[0]["block_returns"]["count"]
    sums = [row["block_returns"]["sum"] for row in ordered]
    m2s = [row["block_returns"]["m2"] for row in ordered]
    return exact_cscv(moment_keys(counts, sums, m2s), len(ordered), size)


def make_row(trial_id: int, counts, sums, m2s) -> dict:
    return {
        "trial_id": trial_id,
        "status": "ok",
        "block_returns": {
            "status": "ok",
            "count": list(counts),
            "sum": list(sums),
            "m2": list(m2s),
        },
    }


def rows_of_series(series: list) -> list[dict]:
    """Module-ready rows from raw series; every sum and M2 must be exact in binary64."""
    counts = [len(values) for values in series[0]]
    rows = []
    for trial, blocks in enumerate(series):
        sums = []
        m2s = []
        for values in blocks:
            _, total, m2 = exact_moments(values)
            sums.append(float(total))
            m2s.append(float(m2))
            if Fraction(sums[-1]) != total or Fraction(m2s[-1]) != m2:
                raise AssertionError("a block moment is not exact in binary64")
        rows.append(make_row(trial, counts, sums, m2s))
    return rows


def moment_rows(counts, trials: int = 3) -> list[dict]:
    """Rows for a layout of ``counts`` intervals per block, built from moments and not from returns
    (a refusal of the layout reads no return). In block b of trial t the mean is t + b + 1, so the
    sum is count * (t + b + 1), and the centred second moment is count. Every number is a whole
    number in binary64, every count is 2 or more and every pooled half has a positive variance, so
    the module would go on to compute a result for these rows if it accepted the layout.
    """
    return [
        make_row(
            trial,
            list(counts),
            [float(count * (trial + block + 1)) for block, count in enumerate(counts)],
            [float(count) for count in counts],
        )
        for trial in range(trials)
    ]


# The parts and the roster of a module call. The module takes its rows in parts, a list of
# (declared object, rows) pairs, and the declared roster (docs/pbo.md, "The roster, the parts and
# the declared object"); the declared object of a part is the result-level ``return_blocks``
# object that its rows were reduced under plus a dataset identity that the caller states. A case of
# this script is not a study and carries none of it, so every call here is ONE part whose object
# and roster come from the rows themselves: the call that handed the module a bare list of rows
# meant that the rows were the whole lineage, and this is that call in the shape the module takes
# now. Nothing here reads the module, and phase 1 does not use it.
#
# The two pinned texts (pin section 1; docs/pbo.md, the keys ``contract`` and ``series``):
RETURN_BLOCKS_CONTRACT = "pineforge-hpo-return-blocks/v1"
RETURN_BLOCKS_SERIES = "bar"
# No case was reduced by a build: the block moments are hand-derived (the JSON fixtures) or exact
# rationals (``exact_moments``), so the text says so and names no build.
CASE_BUILD_IDENTITY = (
    "g2 oracle case: no reducer build, the block moments are hand-derived or exact rationals"
)


def declared_object(counts, dataset: str) -> dict:
    """The declared object of a part whose blocks hold ``counts`` intervals, field by field.

    ``contract`` and ``series`` are the pinned texts. ``S``, ``K`` and ``n_b`` come from the rows'
    own counts: the number of blocks, the number of intervals (their sum, docs/pbo.md, ``K``) and
    the counts themselves. The cases have no clock, so ``boundary_time_ms`` is a convention that no
    comparison reads: interval k sits at k milliseconds, and the boundary of block b is the number
    of intervals before it. ``numeric_build_identity`` is ``CASE_BUILD_IDENTITY`` and
    ``dataset_identity`` is ``dataset``, the name of the fixture or of the case.
    """
    boundaries = [0]
    for count in counts:
        boundaries.append(boundaries[-1] + count)
    return {
        "contract": RETURN_BLOCKS_CONTRACT,
        "series": RETURN_BLOCKS_SERIES,
        "S": len(counts),
        "K": sum(counts),
        "n_b": list(counts),
        "boundary_time_ms": boundaries,
        "numeric_build_identity": CASE_BUILD_IDENTITY,
        "dataset_identity": dataset,
    }


def lineage_of(rows: list, dataset: str) -> tuple:
    """``(parts, roster)`` of a module call on ``rows``: one part, with the object that the counts
    of the first row declare, and the ids of all the rows as the declared roster."""
    counts = rows[0]["block_returns"]["count"]
    return [(declared_object(counts, dataset), rows)], [row["trial_id"] for row in rows]


def replace_block(series: list, trial: int, block: int, values: list) -> list:
    changed = copy.deepcopy(series)
    changed[trial][block] = list(values)
    return changed


def replace_trial(series: list, trial: int, blocks: list) -> list:
    changed = copy.deepcopy(series)
    changed[trial] = copy.deepcopy(blocks)
    return changed


def pooled_signs(series: list, size: int) -> list:
    """The distinct signs (-1, 0, 1) of the pooled mean of every trial over every half. The sign of
    the mean is the sign of the sum of the half's intervals, so no division is needed."""
    found = set()
    for mask in train_masks(size):
        half = blocks_of(mask, size)
        for blocks in series:
            total = sum(sum(blocks[block]) for block in half)
            found.add((total > 0) - (total < 0))
    return sorted(found)


# ---------------------------------------------------------------------------------------------
# Phase 1 sections (no module).
# ---------------------------------------------------------------------------------------------


def section_enumeration(ledger: Ledger) -> None:
    ledger.begin("enumeration of the C(S, S/2) ordered splits (bitmask scan, Pascal's triangle)")
    scanned = [size for size in SPLIT_COUNTS if size <= 20]
    for size, literal in SPLIT_COUNTS.items():
        ledger.compare(
            "fixture",
            f"C({size},{size // 2}) by Pascal's triangle",
            pascal_central(size),
            literal,
            "test_pbo.py EnumerationTests.test_split_counts_for_the_dossier_sizes",
        )
    for size in scanned:
        ledger.compare(
            "fixture",
            f"C({size},{size // 2}) by scanning all {size}-bit masks",
            len(train_masks(size)),
            SPLIT_COUNTS[size],
            "test_pbo.py EnumerationTests.test_split_counts_for_the_dossier_sizes",
        )
    ledger.compare(
        "fixture",
        "ordered splits for S=2",
        documented_order(2),
        [((0,), (1,)), ((1,), (0,))],
        "test_pbo.py EnumerationTests.test_s2_has_two_ordered_splits",
    )
    ledger.compare(
        "fixture",
        "ordered splits for S=4",
        documented_order(4),
        [
            ((0, 1), (2, 3)),
            ((2, 3), (0, 1)),
            ((0, 2), (1, 3)),
            ((1, 3), (0, 2)),
            ((0, 3), (1, 2)),
            ((1, 2), (0, 3)),
        ],
        "test_pbo.py EnumerationTests.test_s4_has_six_ordered_splits",
    )
    order16 = documented_order(16)
    ledger.compare(
        "fixture",
        "S=16 number of ordered splits (eq. 2.3 gives 12870, the paper's text 12780)",
        len(order16),
        12870,
        "test_pbo.py EnumerationTests.test_s16_is_12870_not_12780",
    )
    trained = [sum(1 for train, _ in order16 if block in train) for block in range(16)]
    ledger.compare(
        "fixture",
        "S=16 splits in which each block is in the training half",
        trained,
        [6435] * 16,
        "test_pbo.py EnumerationTests.test_s16_is_12870_not_12780",
    )
    trains = {train for train, _ in order16}
    tests = {test for _, test in order16}
    ledger.compare(
        "fixture",
        "S=16 distinct training sets, and they are exactly the testing sets",
        (len(trains), trains == tests),
        (12870, True),
        "test_pbo.py EnumerationTests.test_s16_is_12870_not_12780 (distinct), "
        "test_pbo.py EnumerationTests.test_s4_has_six_ordered_splits "
        "(same sets, shown for S=4 there)",
    )
    ledger.end(len(SPLIT_COUNTS) + len(scanned) + 5)


def section_fixtures(ledger: Ledger) -> None:
    ledger.begin("hand fixtures recomputed from raw interval returns (exact, two-pass)")
    for name, (size, series) in RAW.items():
        expected = EXPECTED[name]
        source = expected["source"]
        rows = read_rows(name)
        ledger.compare(
            "fixture",
            f"{name}: JSON payload equals the moments of the raw series",
            derived_payload(series),
            file_payload(rows),
            f"fixtures/pbo/{name}.json",
        )
        result = exact_cscv(raw_keys(series), len(series), size)
        ledger.compare(
            "fixture", f"{name}: n_splits", result["n_splits"], expected["n_splits"], source
        )
        ledger.compare(
            "fixture",
            f"{name}: (train, test) -> [(winner, odds)] for every ordered split",
            result["per_split"],
            expected["per_split"],
            source,
        )
        ledger.compare(
            "fixture",
            f"{name}: (n_lt0, n_eq0, n_gt0)",
            (result["n_lt0"], result["n_eq0"], result["n_gt0"]),
            expected["counts"],
            source,
        )
        ledger.compare(
            "fixture",
            f"{name}: (rate_le0, rate_lt0)",
            (result["rate_le0"], result["rate_lt0"]),
            expected["rates"],
            source,
        )
        ledger.compare(
            "fixture", f"{name}: odds in split order", result["odds"], expected["odds"], source
        )
        ledger.compare(
            "fixture",
            f"{name}: tie sizes in split order",
            result["tie_sizes"],
            expected["tie_sizes"],
            source,
        )
        from_payload = oracle_from_rows(rows, size)
        ledger.compare(
            "internal",
            f"{name}: the moments route (parallel axis) equals the raw-series route",
            from_payload,
            result,
        )
    ledger.end(8 * len(RAW))


def section_signed(ledger: Ledger) -> None:
    ledger.begin(
        "negative, zero and sign-changing means: hand cases against the exact oracle (P1-2)"
    )
    source = "derived by hand in this file"
    for name, (size, series) in SIGNED_RAW.items():
        expected = SIGNED_EXPECTED[name]
        # The module can only be shown a negative or a zero mean if the case really holds one, so
        # the signs of the pooled means are pinned too: editing a case into a non-negative one
        # fails here.
        ledger.compare(
            "internal",
            f"{name}: signs of the pooled mean over every trial and every half",
            pooled_signs(series, size),
            expected["signs"],
            source,
        )
        result = exact_cscv(raw_keys(series), len(series), size)
        ledger.compare(
            "fixture", f"{name}: n_splits", result["n_splits"], expected["n_splits"], source
        )
        ledger.compare(
            "fixture",
            f"{name}: (train, test) -> [(winner, odds)] for every ordered split",
            result["per_split"],
            expected["per_split"],
            source,
        )
        ledger.compare(
            "fixture",
            f"{name}: (n_lt0, n_eq0, n_gt0)",
            (result["n_lt0"], result["n_eq0"], result["n_gt0"]),
            expected["counts"],
            source,
        )
        ledger.compare(
            "fixture",
            f"{name}: (rate_le0, rate_lt0)",
            (result["rate_le0"], result["rate_lt0"]),
            expected["rates"],
            source,
        )
        ledger.compare(
            "fixture", f"{name}: odds in split order", result["odds"], expected["odds"], source
        )
        ledger.compare(
            "fixture",
            f"{name}: tie sizes in split order",
            result["tie_sizes"],
            expected["tie_sizes"],
            source,
        )
        ledger.compare(
            "internal",
            f"{name}: the moments route (parallel axis) equals the raw-series route",
            oracle_from_rows(rows_of_series(series), size),
            result,
        )
    ledger.end(8 * len(SIGNED_RAW))


def section_arithmetic(ledger: Ledger) -> None:
    ledger.begin("pooled moments and the ranking statistic from raw values (D1, D7)")
    pooled = [
        (
            "B0+B1 pooled over 1,3,3,5",
            [1, 3, 3, 5],
            (4, 12.0, 8.0),
            "test_pbo.py ArithmeticTests.test_merge_matches_the_direct_two_pass_form",
        ),
        (
            "B0+B1+B2 pooled over 1,3,3,5,4,5",
            [1, 3, 3, 5, 4, 5],
            (6, 21.0, 11.5),
            "test_pbo.py ArithmeticTests.test_merge_matches_the_direct_two_pass_form",
        ),
        (
            "B1+B2 pooled over 3,5,4,5",
            [3, 5, 4, 5],
            (4, 17.0, 2.75),
            "test_pbo.py ArithmeticTests.test_merge_matches_the_direct_two_pass_form",
        ),
        (
            "B0+B2 pooled over 1,3,4,5",
            [1, 3, 4, 5],
            (4, 13.0, 8.75),
            "test_pbo.py ArithmeticTests.test_merge_matches_the_direct_two_pass_form",
        ),
        (
            "negative blocks pooled over -1,-3,-3,-5 (total -12, mean -3, squares 4+0+0+4)",
            [-1, -3, -3, -5],
            (4, -12.0, 8.0),
            "derived by hand in this file",
        ),
        (
            "blocks that change sign pooled over -3,-1,1,5 "
            "(total 2, mean 1/2, squares 49/4+9/4+1/4+81/4)",
            [-3, -1, 1, 5],
            (4, 2.0, 35.0),
            "derived by hand in this file",
        ),
    ]
    for label, values, literal, source in pooled:
        ledger.compare("fixture", label, exact_moments(values), literal, source)
    n, total, m2 = exact_moments([1, 3, 3, 5])
    squared = (total / n) ** 2 / (m2 / (n - 1))
    ledger.compare(
        "fixture",
        "statistic squared over 1,3,3,5: (mean / sample sigma)^2",
        squared,
        Fraction(27, 8),
        "test_pbo.py ArithmeticTests.test_statistic_is_pooled_mean_over_sample_sigma "
        "(3 / sqrt(8/3), squared)",
    )
    exact_statistic = decimal_sqrt(Fraction(27, 8))
    population = decimal_sqrt(Fraction(9, 2))  # 3 / sqrt(8/4)
    average_of_blocks = (decimal_sqrt(Fraction(2)) + decimal_sqrt(Fraction(8))) / 2
    ledger.compare(
        "fixture",
        "the pooled statistic differs from the population form and from the mean of block "
        "statistics",
        (
            abs(exact_statistic - population) > Decimal("0.1"),
            abs(exact_statistic - average_of_blocks) > Decimal("0.1"),
        ),
        (True, True),
        "test_pbo.py ArithmeticTests.test_statistic_is_pooled_mean_over_sample_sigma",
    )
    eps = 2.0**-52
    x = 1.0 + 2 * eps
    y = 1.0 + 4 * eps
    ledger.compare(
        "fixture",
        "F5 exact M2 of x,x,x,y,y,y (e = 2**-52, x = 1+2e, y = 1+4e)",
        exact_moments([x] * 3 + [y] * 3)[2],
        Fraction(3, 2**103),
        "test_pbo.py ArithmeticTests.test_near_constant_series_keeps_the_centred_m2_non_negative",
    )
    ledger.compare(
        "fixture",
        "F5 exact M2 of x,x,x",
        exact_moments([x] * 3)[2],
        0,
        "test_pbo.py ArithmeticTests.test_near_constant_series_keeps_the_centred_m2_non_negative",
    )
    ledger.compare(
        "fixture",
        "F5 sum-of-squares form on x,x,x in binary64",
        naive_m2([x, x, x]),
        -(2.0**-51),
        "test_pbo.py ArithmeticTests.test_near_constant_series_keeps_the_centred_m2_non_negative",
    )
    ledger.compare(
        "fixture",
        "F5 sum-of-squares form on x,x,x,y,y,y in binary64",
        naive_m2([x, x, x, y, y, y]),
        2.0**-50,
        "test_pbo.py ArithmeticTests.test_near_constant_series_keeps_the_centred_m2_non_negative",
    )
    ledger.compare(
        "fixture",
        "a zero-variance pool has no statistic",
        key_from_moments(4, Fraction(12), Fraction(0)),
        None,
        "test_pbo.py ArithmeticTests.test_statistic_is_pooled_mean_over_sample_sigma",
    )
    ledger.compare(
        "fixture",
        "ranking key of a negative mean: 4 intervals summing to -12 with M2 8 "
        "(mean -3, variance 8/3)",
        key_from_moments(4, Fraction(-12), Fraction(8)),
        Fraction(-27, 8),
        "derived by hand in this file (-1 * 9 / (8/3))",
    )
    ledger.compare(
        "fixture",
        "ranking key of a zero mean is zero: 4 intervals summing to 0 with M2 8",
        key_from_moments(4, Fraction(0), Fraction(8)),
        0,
        "derived by hand in this file (sign 0)",
    )
    ledger.end(len(pooled) + 9)


def section_work(ledger: Ledger) -> None:
    ledger.begin("admission figure replayed from the accounting of clause 3.7")
    for (size, trials), literal in WORK_CASES:
        figures = work_figures(size, trials)
        ledger.compare(
            "fixture",
            f"S={size}, N={trials}: {', '.join(literal)}",
            {key: figures[key] for key in literal},
            literal,
            "test_pbo.py ArithmeticTests.test_work_figures",
        )
    ledger.end(len(WORK_CASES))


def section_zero_variance(ledger: Ledger) -> None:
    ledger.begin("zero-variance refusals: the first flat half in the module's evaluation order")
    cases = [
        (
            "S=2: trial 1, block 0 holds the intervals 1, 1",
            2,
            replace_block(RAW["f4_atom_s2_n3"][1], 1, 0, [1, 1]),
            ("pbo_zero_variance", (1,), (0,)),
            "test_pbo.py RefusalTests.test_pbo_zero_variance",
        ),
        (
            "S=4: trial 2 returns 3, 3, 3, 3",
            4,
            replace_trial(RAW["splits_s4_n4"][1], 2, [[3], [3], [3], [3]]),
            ("pbo_zero_variance", (2,), (0, 1)),
            "test_pbo.py RefusalTests.test_pbo_zero_variance",
        ),
        (
            "S=4: trial 2 returns 5, 5, 2, 9",
            4,
            replace_trial(RAW["splits_s4_n4"][1], 2, [[5], [5], [2], [9]]),
            ("pbo_zero_variance", (2,), (0, 1)),
            "test_pbo.py RefusalTests.test_pbo_zero_variance",
        ),
    ]
    for label, size, series, literal, source in cases:
        result = exact_cscv(raw_keys(series), len(series), size)
        ledger.compare(
            "fixture",
            label + ": (code, flat trials, blocks of the first flat half)",
            (result.get("code"), result.get("ids"), result.get("blocks")),
            literal,
            source,
        )
    ledger.end(len(cases))


def section_uneven_layouts(ledger: Ledger) -> None:
    """The deliberately D2-violating layouts really are: the spread that the oracle measures on the
    counts of their rows equals the hand-derived one and is more than one interval. Editing a
    layout into a legal one fails here, and the module is not needed for it."""
    ledger.begin(
        "deliberately uneven block layouts: the spread of the counts, by hand and measured (D2)"
    )
    source = "derived by hand in this file"
    for label, counts, spread in UNEVEN_LAYOUTS:
        found = moment_rows(counts)[0]["block_returns"]["count"]
        measured = max(found) - min(found)
        ledger.compare(
            "fixture",
            f"{label}: largest minus smallest interval count of the rows",
            measured,
            spread,
            source,
        )
        ledger.check(
            "internal",
            f"{label}: the spread is more than one interval, so default D2 refuses the layout",
            measured > 1,
        )
    ledger.end(2 * len(UNEVEN_LAYOUTS))


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
    # tie_s2_n3 with trial 1's block 0 changed from the intervals 3, 5 to 2, 4 (block mean 4 -> 3).
    # By hand: train {0} has block-0 means 4, 3, 1, so trial 0 wins alone; on block 1 (means 3, 1,
    # 2) it beats both (rank 3 of 4, odds 3); train {1} has block-1 means 3, 1, 2, trial 0 wins
    # again and on block 0 (means 4, 3, 1) it is first (rank 3, odds 3): two splits above
    # lambda = 0.
    size, series = RAW["tie_s2_n3"]
    changed = replace_block(series, 1, 0, [2, 4])
    result = exact_cscv(raw_keys(changed), len(changed), size)
    ledger.compare(
        "control",
        "tie fixture with the tie removed: (n_lt0, n_eq0, n_gt0)",
        (result["n_lt0"], result["n_eq0"], result["n_gt0"]),
        (0, 0, 2),
        "derived by hand in this file",
    )
    ledger.compare(
        "control",
        "tie fixture with the tie removed: (rate_le0, rate_lt0) and tie sizes",
        ((result["rate_le0"], result["rate_lt0"]), result["tie_sizes"]),
        ((0.0, 0.0), [1, 1]),
        "derived by hand in this file",
    )
    # The rule for the module's counts (finding P2-1): an int for a whole count, the nearest
    # binary64 for a fractional one, compared by value and by type. These controls show that the
    # rule is exact and not a tolerance, and why the exact Fraction itself cannot be compared with
    # the module's float.
    four_thirds = reported_count(Fraction(4, 3))
    ledger.check(
        "control",
        "the exact count 4/3 is not equal to its binary64 (so the exact value cannot be compared)",
        (Fraction(4, 3) == four_thirds) is False,
    )
    ledger.check(
        "control",
        "the count rule turns 4/3 into the nearest binary64 and accepts exactly that value",
        same(typed(four_thirds), ("float", 1.3333333333333333)) is True,
    )
    ledger.check(
        "control",
        "the count rule rejects the neighbouring binary64 (no tolerance)",
        same(typed(four_thirds), typed(math.nextafter(four_thirds, 2.0))) is False,
    )
    ledger.check(
        "control",
        "the count rule rejects the float 3.0 for the whole count 6/2",
        same(typed(reported_count(Fraction(6, 2))), typed(3.0)) is False,
    )
    ledger.check(
        "control",
        "the count rule accepts the int 3 for the whole count 6/2",
        same(typed(reported_count(Fraction(6, 2))), typed(3)) is True,
    )
    ledger.end(len(must_differ) + len(must_match) + 7)


# ---------------------------------------------------------------------------------------------
# Seeded random cases: raw route against moments route (phase 1) and against the module (phase 2).
# ---------------------------------------------------------------------------------------------


def exact_block(rng: random.Random, count: int) -> list:
    """The ``count`` integer returns of one block, from -40 to 40: symmetric around zero, so
    negative and zero pooled means occur (finding P1-2).

    A count that is a power of two (2, 4, 8) is drawn freely, as it always was. Any other count (3,
    5 or 9, which a block holds when it has one interval more than the base) is drawn so that the
    sum is a multiple of the count: the block mean is then a whole number and the block moments
    that the module receives are exact in binary64. Without that, ``rows_of_series`` refuses the
    block, because M2 = sum of squares - sum**2 / count has a denominator that is not a power of
    two. The last value is drawn among the values in range that make the sum a multiple of the
    count (there are at least nine of them for every residue).
    """
    values = [rng.randint(-40, 40) for _ in range(count - 1)]
    if (count & (count - 1)) == 0:
        values.append(rng.randint(-40, 40))
    else:
        partial = sum(values)
        fitting = [value for value in range(-40, 41) if (partial + value) % count == 0]
        values.append(rng.choice(fitting))
    return values


def random_series(rng: random.Random) -> tuple:
    size = rng.choice((2, 4, 6, 8, 10, 12))
    trials = rng.randint(2, 8)
    # Default D2 (docs/pbo.md line 181): block sizes may differ by at most one interval. A case has
    # a base of 2, 4 or 8 intervals and each block holds the base or one more, so its counts are
    # all alike or differ by exactly one. The layouts that D2 refuses are not drawn here: they are
    # the deliberate ones of ``UNEVEN_LAYOUTS``.
    base = rng.choice((2, 4, 8))
    counts = [base + rng.randint(0, 1) for _ in range(size)]
    series: list = []
    for _ in range(trials):
        roll = rng.random()
        if series and roll < 0.2:
            blocks = [list(block) for block in series[rng.randrange(len(series))]]
        else:
            blocks = [exact_block(rng, counts[b]) for b in range(size)]
            if series and roll < 0.4:
                donor = series[rng.randrange(len(series))]
                position = rng.randrange(size)
                blocks[position] = list(donor[position])
        series.append(blocks)
    return size, counts, series


def ambiguous(series: list, size: int) -> bool:
    """True when two trials have exactly equal statistics in some half although their block moments
    in that half differ. Binary64 equality (default D4) may then differ from exact equality, so the
    case cannot be used to judge the module. A zero key is not such a tie: the returns are integers,
    so the module's pooled sum is exact and a zero mean gives the statistic 0.0 whatever the
    variance (the module and the oracle both tie there, and the comparison keeps these cases)."""
    trials = len(series)
    moments = [[exact_moments(values) for values in blocks] for blocks in series]
    key_of = raw_keys(series)
    for mask in train_masks(size):
        half = blocks_of(mask, size)
        keys = [key_of(trial, half) for trial in range(trials)]
        for first in range(trials):
            for second in range(first + 1, trials):
                if keys[first] is not None and keys[first] != 0 and keys[first] == keys[second]:
                    if [moments[first][b] for b in half] != [moments[second][b] for b in half]:
                        return True
    return False


def generate_cases(rng: random.Random, wanted: int) -> tuple:
    """(the ``wanted`` unambiguous cases, the attempt numbers set aside as tie-ambiguous). A
    set-aside attempt is replaced by the next one, so ``wanted`` cases are always compared; the
    attempt numbers are the ids of the set-aside cases (the same seed generates them again)."""
    cases = []
    skipped: list = []
    attempts = 0
    while len(cases) < wanted:
        attempts += 1
        if attempts > 50 * wanted:
            raise RuntimeError("could not generate enough unambiguous random cases")
        size, counts, series = random_series(rng)
        if ambiguous(series, size):
            skipped.append(attempts)
            continue
        cases.append((size, counts, series, exact_cscv(raw_keys(series), len(series), size)))
    return cases, skipped


def section_random_internal(ledger: Ledger, cases: list, skipped: list, seed: int) -> None:
    ledger.begin(f"seeded random cases (seed {seed}): raw-series route against moments route")
    refused = negative = zero = one_apart = too_far = 0
    for number, (size, counts, series, raw_result) in enumerate(cases):
        rows = rows_of_series(series)
        moments_result = oracle_from_rows(rows, size)
        ledger.compare(
            "internal",
            f"case {number}: S={size}, N={len(series)}",
            raw_result,
            moments_result,
            quiet=True,
        )
        refused += raw_result["status"] == "refused"
        signs = pooled_signs(series, size)
        negative += -1 in signs
        zero += 0 in signs
        one_apart += max(counts) - min(counts) == 1
        too_far += max(counts) - min(counts) > 1
    done = len(cases) - refused
    ledger.note(
        f"{done} cases reached the ok path, {refused} refused (zero variance), "
        f"{len(skipped)} generated cases were set aside as tie-ambiguous and replaced "
        f"(attempt numbers {skipped})"
    )
    ledger.note(f"{negative} cases hold a negative pooled mean and {zero} a zero one")
    ledger.note(
        f"{one_apart} cases hold blocks that differ by exactly one interval, "
        f"{len(cases) - one_apart - too_far} blocks of one size, {too_far} blocks that differ by "
        "more (default D2 refuses those, so none may be drawn)"
    )
    if too_far:
        ledger.fail("a random case holds blocks that differ by more than one interval (default D2)")
    if one_apart < 1:
        ledger.fail("no random case showed the module blocks that differ by exactly one interval")
    if done < max(1, len(cases) // 3):
        ledger.fail("too few random cases reached the ok path to be meaningful")
    if negative < max(1, len(cases) // 2):
        ledger.fail("too few random cases showed the module a negative pooled mean")
    ledger.end(len(cases))


# ---------------------------------------------------------------------------------------------
# Phase 2: the module, imported only now.
# ---------------------------------------------------------------------------------------------


def load_module():
    sys.path.insert(0, str(ROOT / "python"))
    sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))
    from pineforge_hpo import pbo

    return pbo


def module_run(
    pbo, rows: list, size: int, sampler: str = "grid", *, dataset: str, early_stop=None
) -> dict:
    """The module's result, or its refusal; only the module's own refusal type is caught.

    The rows are the one part of the call (``lineage_of``) and ``dataset`` names them. The cases
    are fixtures, not studies, so ``early_stop`` is None (the study had no early stop) unless the
    caller passes True, as the roster-label rows do for a study that had one.
    """
    parts, roster = lineage_of(rows, dataset)
    try:
        return pbo.pbo(
            parts, S=size, roster=roster, sampler=sampler, early_stop=early_stop, with_lambdas=True
        )
    except pbo.PboError as error:
        return {"status": "refused", "code": error.code, "ids": tuple(error.ids)}


def module_view(result: dict) -> dict:
    if result["status"] == "refused":
        return {"status": "refused", "code": result["code"], "ids": result["ids"]}
    return {
        "status": "ok",
        "n_splits": result["n_splits"],
        "counts": module_counts(result),
        "rates": (result["rate_le0"], result["rate_lt0"]),
        "lambdas": result["lambdas"],
        "tie_sizes": result["lambda_tie_sizes"],
    }


def oracle_view(result: dict) -> dict:
    if result["status"] == "refused":
        return {"status": "refused", "code": result["code"], "ids": result["ids"]}
    return {
        "status": "ok",
        "n_splits": result["n_splits"],
        "counts": oracle_counts(result),
        "rates": (result["rate_le0"], result["rate_lt0"]),
        "lambdas": lambdas_of(result),
        "tie_sizes": result["tie_sizes"],
    }


def refusal_of(pbo, call) -> tuple:
    try:
        call()
    except pbo.PboError as error:
        return ("refused", error.code, tuple(error.ids))
    return ("returned", None, ())


def module_enumeration(ledger: Ledger, pbo) -> None:
    ledger.begin("module: enumeration of the splits")
    sizes = (2, 4, 8, 12, 16)
    for size in sizes:
        listed = [(tuple(train), tuple(test)) for train, test in pbo.iter_ordered_splits(size)]
        ledger.compare(
            "module",
            f"iter_ordered_splits({size}) in the documented order",
            documented_order(size),
            listed,
            quiet=size > 4,
        )
    for size, literal in SPLIT_COUNTS.items():
        ledger.compare(
            "module",
            f"estimate_work({size}, 1)['n_splits']",
            pascal_central(size),
            pbo.estimate_work(size, 1)["n_splits"],
        )
    ledger.end(len(sizes) + len(SPLIT_COUNTS))


def module_layout(ledger: Ledger, name: str, counts, found: dict, *, quiet: bool = False) -> None:
    """Two comparisons of the block layout that a result reports (default D2; docs/pbo.md, the
    result table, rows ``equal_blocks`` and ``block_counts``) with the layout the case declares.

    ``counts`` is the interval count of every block as the CASE holds it: the list a random case
    was generated from, or the length of each block of a fixture's raw series. It is never read
    off ``found``. The oracle side is ``len(set(counts)) == 1`` and ``list(counts)``. A refusal
    has neither key, so it fails both. Both mutations below leave every fixture as it is (their
    blocks all hold one size) and fail every random case whose blocks hold two sizes; the run
    fails if no random case that the oracle computes is such a case (``module_random``).
    """
    # Mutation: pbo.py:1214 (in `pbo`) `len(set(table.counts)) == 1` -> `True` fails every case
    # of two sizes.
    ledger.compare(
        "module",
        f"{name}: equal_blocks",
        len(set(counts)) == 1,
        found.get("equal_blocks"),
        quiet=quiet,
    )
    # Mutation: pbo.py:1215 (in `pbo`) `list(table.counts)` ->
    # `[min(table.counts)] * len(table.counts)`.
    ledger.compare(
        "module",
        f"{name}: block_counts",
        list(counts),
        found.get("block_counts"),
        quiet=quiet,
    )


def module_rate_and_coverage(
    ledger: Ledger, name: str, rate_le0: float, trials: int, found: dict, *, quiet: bool = False
) -> None:
    """Two more fields of a result, compared with values that the oracle derives from the case.

    ``pbo`` is the primary rate and equals ``rate_le0`` (default D3; docs/pbo.md, the result
    table). The oracle side is ``rate_le0``, its own exact share of the ordered splits at or below
    lambda = 0, converted to binary64 once. The module's ``rate_le0`` is compared with it already
    (the ``rates`` of the view); here the module's ``pbo`` is read too. ``coverage`` is
    ``{declared, supplied, usable}``, and in a result each of the three is the number of trials
    (same table): the oracle side is ``trials``, the number of trials of the case. A refusal has
    neither key and fails both. The first mutation fails every case whose count ``n_eq0`` is not
    zero, the second fails every case.
    """
    # Mutation: pbo.py:1221 (in `pbo`) `"pbo": rate_le0` -> `"pbo": float(n_lt0 / n_splits)`.
    ledger.compare("module", f"{name}: pbo", rate_le0, found.get("pbo"), quiet=quiet)
    # Mutation: pbo.py:1153 (in `pbo`) `"usable": n_trials` -> `"usable": n_trials - 1` fails
    # every case.
    ledger.compare(
        "module",
        f"{name}: coverage",
        {"declared": trials, "supplied": trials, "usable": trials},
        found.get("coverage"),
        quiet=quiet,
    )


def module_case(ledger: Ledger, pbo, name: str, size: int, series: list, rows: list) -> None:
    """Ten comparisons of the module with the oracle on one case: the five fields of the result's
    view, the work figure, the two fields of the block layout (``module_layout``), and the primary
    rate and the coverage (``module_rate_and_coverage``). A refusal by the module is recorded as a
    failure and compared too."""
    expected_view = oracle_view(exact_cscv(raw_keys(series), len(series), size))
    found = module_run(pbo, rows, size, dataset=name)
    got = module_view(found)
    labels = ("n_splits", "counts", "rates", "lambdas", "tie_sizes")
    if got["status"] != "ok":
        ledger.fail(f"{name}: the module refused: {got}")
        for label in labels:
            ledger.compare("module", f"{name}: {label}", expected_view.get(label), got.get(label))
    else:
        for label in labels:
            ledger.compare("module", f"{name}: {label}", expected_view[label], got[label])
    ledger.compare(
        "module",
        f"{name}: work_estimate",
        work_figures(size, len(series))["work"],
        found.get("work_estimate"),
    )
    # The case's own layout: how many intervals each block of its raw series holds. For a fixture,
    # phase 1 compares that series with the JSON rows; the signed cases build their rows from it.
    module_layout(ledger, name, [len(values) for values in series[0]], found)
    # The oracle's own rate_le0 (the first entry of the rates) and the case's number of trials.
    module_rate_and_coverage(ledger, name, expected_view["rates"][0], len(series), found)


def module_fixtures(ledger: Ledger, pbo) -> None:
    ledger.begin("module: the three hand fixtures")
    for name, (size, series) in RAW.items():
        module_case(ledger, pbo, name, size, series, read_rows(name))
    ledger.end(10 * len(RAW))


def module_signed(ledger: Ledger, pbo) -> None:
    ledger.begin("module: negative, zero and sign-changing means (P1-2)")
    for name, (size, series) in SIGNED_RAW.items():
        module_case(ledger, pbo, name, size, series, rows_of_series(series))
    ledger.end(10 * len(SIGNED_RAW))


def module_arithmetic(ledger: Ledger, pbo) -> None:
    ledger.begin("module: merge, pooled moments and the ranking statistic")
    b0 = (2, 4.0, 2.0)
    b1 = (2, 8.0, 2.0)
    b2 = (2, 9.0, 0.5)
    ledger.compare(
        "module", "merge_moments(B0, B1)", exact_moments([1, 3, 3, 5]), pbo.merge_moments(b0, b1)
    )
    ledger.compare(
        "module",
        "pooled_moments([B0, B1, B2])",
        exact_moments([1, 3, 3, 5, 4, 5]),
        pbo.pooled_moments([b0, b1, b2]),
    )
    ledger.compare(
        "module",
        "merge_moments(B0, merge_moments(B1, B2))",
        exact_moments([1, 3, 3, 5, 4, 5]),
        pbo.merge_moments(b0, pbo.merge_moments(b1, b2)),
    )
    ledger.compare(
        "module", "merge_moments(B1, B2)", exact_moments([3, 5, 4, 5]), pbo.merge_moments(b1, b2)
    )
    ledger.compare(
        "module",
        "pooled_moments([B0, B2])",
        exact_moments([1, 3, 4, 5]),
        pbo.pooled_moments([b0, b2]),
    )
    eps = 2.0**-52
    x = 1.0 + 2 * eps
    y = 1.0 + 4 * eps
    count, _total, centred = pbo.pooled_moments([(3, x + x + x, 0.0), (3, y + y + y, 0.0)])
    ledger.compare(
        "module",
        "F5 pooled (n, M2) over x,x,x and y,y,y",
        (6, exact_moments([x] * 3 + [y] * 3)[2]),
        (count, centred),
    )
    nearest = float(decimal_sqrt(Fraction(27, 8)))
    ledger.compare(
        "module",
        "ranking_statistic(4, 12.0, 8.0) within 2 ulp of the exact real sqrt(27/8)",
        nearest,
        pbo.ranking_statistic(4, 12.0, 8.0),
        ulps=2,
    )
    ledger.compare(
        "module",
        "ranking_statistic(4, 12.0, 0.0) (zero variance)",
        key_from_moments(4, Fraction(12), Fraction(0)),
        pbo.ranking_statistic(4, 12.0, 0.0),
    )
    ledger.compare(
        "module",
        "merge_moments of blocks with negative sums (-1,-3 and -3,-5)",
        exact_moments([-1, -3, -3, -5]),
        pbo.merge_moments((2, -4.0, 2.0), (2, -8.0, 2.0)),
    )
    ledger.compare(
        "module",
        "merge_moments of blocks whose sums have opposite signs (-3,-1 and 1,5)",
        exact_moments([-3, -1, 1, 5]),
        pbo.merge_moments((2, -4.0, 2.0), (2, 6.0, 8.0)),
    )
    ledger.compare(
        "module",
        "ranking_statistic(4, -12.0, 8.0) within 2 ulp of the exact real -sqrt(27/8)",
        -nearest,
        pbo.ranking_statistic(4, -12.0, 8.0),
        ulps=2,
    )
    ledger.compare(
        "module",
        "ranking_statistic(4, 0.0, 8.0) (a zero mean has the statistic 0)",
        key_from_moments(4, Fraction(0), Fraction(8)),
        pbo.ranking_statistic(4, 0.0, 8.0),
    )
    sums = (-12, -4, 0, 4)
    keys = [key_from_moments(4, Fraction(total), Fraction(8)) for total in sums]
    stats = [pbo.ranking_statistic(4, float(total), 8.0) for total in sums]
    ledger.compare(
        "module",
        "ranking_statistic increases with the sum across zero (sums -12, -4, 0, 4 with M2 8)",
        [keys[index] < keys[index + 1] for index in range(3)],
        [stats[index] < stats[index + 1] for index in range(3)],
    )
    ledger.compare(
        "module",
        "ranking_statistic has the sign of the sum (sums -12, -4, 0, 4 with M2 8)",
        [(key > 0) - (key < 0) for key in keys],
        [(stat > 0) - (stat < 0) for stat in stats],
    )
    ledger.end(14)


def module_work(ledger: Ledger, pbo) -> None:
    ledger.begin("module: admission figure")
    for (size, trials), literal in WORK_CASES:
        figures = work_figures(size, trials)
        found = pbo.estimate_work(size, trials)
        ledger.compare(
            "module",
            f"estimate_work({size}, {trials}): {', '.join(literal)}",
            {key: figures[key] for key in literal},
            {key: found[key] for key in literal},
        )
    ledger.end(len(WORK_CASES))


def module_refusals(ledger: Ledger, pbo) -> None:
    """The refusals of the module, each compared as (status, code, ids) with a literal.

    A call is one part whose declared object and roster come from its own rows (``lineage_of``),
    with ``early_stop`` None, so the roster of a case is its rows: the old call took the rows as
    the whole lineage. Where each case is refused in python/pineforge_hpo/pbo.py, and its one-line
    mutation of the module (read off the code; nothing has been run):

    * Zero variance, three cases: ``_half_statistics`` raises ``pbo_zero_variance`` with the flat
      trial ids once every row was accepted (pbo.py:1094, in ``_half_statistics``, ``if flat:``).
      Mutation: make ``ranking_statistic`` return 0.0 instead of None for a zero ``m2``
      (pbo.py:986, in ``ranking_statistic``, ``return None`` after ``if not m2 > 0.0:``).
    * Odd S: ``_check_s`` raises ``pbo_odd_s`` first, before the roster or the parts are read
      (pbo.py:294, in ``_check_s``, ``raise PboError("pbo_odd_s"``; called at pbo.py:1144, in
      ``pbo``, ``S = _check_s(S)``). Mutation: change the code ``pbo_odd_s`` to
      ``pbo_input_invalid`` there.
    * One trial: the roster is the one id of the row, so ``_declared_ids`` raises
      ``pbo_too_few_trials`` (pbo.py:394, in ``_declared_ids``, ``raise PboError(`` with
      ``"pbo_too_few_trials",`` on the next line) before the parts are read. ``_table_ids``
      (pbo.py:703, in ``_table_ids``, the same two lines) raises the same code for a table of one,
      so no one-line mutation of the module is caught by this case alone. Mutation of the case
      instead: take the roster from the three ids of the fixture and not from the one row; the
      call then reaches ``pbo_roster_mismatch``.
    * Unknown sampler, two cases (``bogus``, and ``early_stop``, which since commit 556d654 is
      the study's own input and no sampler name; docs/pbo.md line 356): ``roster_label`` raises
      ``pbo_sampler_unknown`` before the roster or the parts are read (pbo.py:1035, in
      ``roster_label``, ``"pbo_sampler_unknown",``; called at pbo.py:1147, in ``pbo``,
      ``label = roster_label(sampler, early_stop=stopped)``). Mutation: change that code to
      ``pbo_input_invalid`` (both cases fail). Mutation for the ``early_stop`` case alone: add
      ``"early_stop"`` to ``DIAGNOSTIC_SAMPLERS`` (pbo.py:93); ``roster_label`` then returns a
      label for the name, the call returns a result and the case fails. The case keeps the
      refusal that the old roster row for this name stood for covered.
    * An error row: ``_parse_row`` raises ``pbo_status_error`` for a row whose status is one of
      ``ERROR_ROW_STATUSES`` (pbo.py:744-747, in ``_parse_row``, the test
      ``if status in ERROR_ROW_STATUSES:`` and the ``raise _RowProblem(`` below it with
      ``"pbo_status_error",`` on its next line), the code that commit 92a9c7b gave to the
      failed-trial statuses (docs/pbo.md D5, line 184, and the table at line 346); ``_load_parts``
      reports it with the id of the row (pbo.py:874, in ``_load_parts``,
      ``failures.setdefault(problem.code, []).append((ids[index], detail))``; pbo.py:920-926,
      the ``raise PboError(`` whose first argument is ``code``). The literal below is that code,
      read off the page and not off the module. The planted status is ``engine_error``: one of the
      six failed-trial statuses that the page lists (lines 65-66 and 346) and one of the nine of
      the shared vocabulary (``_rows.STATUSES``), so the module is shown a status of the
      vocabulary. It was ``error``, which the page lists and the vocabulary does not hold, so a
      ruling that removed ``error`` from the module's list would have flipped the case. Mutation:
      delete the line ``"engine_error",`` of ``ERROR_ROW_STATUSES`` (pbo.py:52); the row then
      reaches the ``ROSTER_ROW_STATUSES`` test, the call refuses with ``pbo_rows_incomplete`` and
      the same id, and the case fails (it would not have failed with the old literal). The other
      mutation, which fails either literal: change the code ``pbo_status_error`` at pbo.py:745 (in
      ``_parse_row``, ``raise _RowProblem(`` with ``"pbo_status_error",`` on the next line) to
      ``pbo_rows_incomplete``.
    * Uneven blocks, four deliberate layouts (``UNEVEN_LAYOUTS``; docs/pbo.md D2, line 181, and
      line 342): ``_check_return_blocks`` raises ``pbo_block_sizes_uneven`` when the largest
      ``n_b`` minus the smallest is more than one (pbo.py:478-483, in ``_check_return_blocks``,
      ``if max(n_b) - min(n_b) > 1:`` and the ``raise PboError(`` of ``"pbo_block_sizes_uneven",``
      below it), while the declared object is checked and before any row is read, so the refusal
      names no id. Each layout has the rows of ``moment_rows``, which the module would compute if
      it accepted the layout, so a mutation that lets the layout through makes the call return a
      result and the case fail. One-line mutations of the test at pbo.py:478 (in
      ``_check_return_blocks``, ``if max(n_b) - min(n_b) > 1:``), one for each layout: (2, 4):
      change ``> 1`` to ``> 2``; (4, 3, 2, 3): change ``max(n_b) - min(n_b)`` to
      ``max(n_b) - n_b[0]`` (4 - 4 is 0); (2, 4, 8, 4): change ``> 1`` to ``> 6``; (8, 9, 10, 9):
      change the test to ``max(n_b) > 2 * min(n_b)`` (10 > 16 is false). These layouts are no
      longer drawn by the random cases (``random_series`` keeps the counts within one of each
      other), so these four cases are what keeps the refusal of default D2 covered.
    """
    ledger.begin("module: refusals")
    for label, size, series, source in (
        (
            "S=2 flat block",
            2,
            replace_block(RAW["f4_atom_s2_n3"][1], 1, 0, [1, 1]),
            "test_pbo.py RefusalTests.test_pbo_zero_variance",
        ),
        (
            "S=4 constant trial",
            4,
            replace_trial(RAW["splits_s4_n4"][1], 2, [[3], [3], [3], [3]]),
            "test_pbo.py RefusalTests.test_pbo_zero_variance",
        ),
        (
            "S=4 flat first half",
            4,
            replace_trial(RAW["splits_s4_n4"][1], 2, [[5], [5], [2], [9]]),
            "test_pbo.py RefusalTests.test_pbo_zero_variance",
        ),
    ):
        oracle = exact_cscv(raw_keys(series), len(series), size)
        found = module_run(pbo, rows_of_series(series), size, dataset=f"zero variance, {label}")
        ledger.compare(
            "module",
            f"zero variance, {label}: (status, code, ids)",
            (oracle["status"], oracle["code"], oracle["ids"]),
            (found["status"], found.get("code"), found.get("ids")),
            source,
        )
    base = read_rows("f4_atom_s2_n3")
    odd_rows = [make_row(i, [2, 2, 2], [4.0, 6.0, 8.0], [2.0, 2.0, 2.0]) for i in range(3)]
    errored = copy.deepcopy(base)
    errored[2]["status"] = "engine_error"

    def planted_call(rows: list, size: int, sampler: str, dataset: str) -> dict:
        parts, roster = lineage_of(rows, dataset)
        return pbo.pbo(parts, S=size, roster=roster, sampler=sampler, early_stop=None)

    planted = [
        (
            "odd S",
            lambda: planted_call(odd_rows, 3, "grid", "odd S"),
            ("refused", "pbo_odd_s", ()),
            "test_pbo.py RefusalTests.test_pbo_odd_s, rule: S must be even",
        ),
        (
            "one trial",
            lambda: planted_call(base[:1], 2, "grid", "one trial"),
            ("refused", "pbo_too_few_trials", ()),
            "test_pbo.py RefusalTests.test_pbo_too_few_trials, rule: N < 2",
        ),
        (
            "unknown sampler",
            lambda: planted_call(base, 2, "bogus", "unknown sampler"),
            ("refused", "pbo_sampler_unknown", ()),
            "test_pbo.py RefusalTests.test_pbo_sampler_unknown, default D6",
        ),
        (
            "early_stop as a sampler name",
            lambda: planted_call(base, 2, "early_stop", "early_stop as a sampler name"),
            # docs/pbo.md line 356: the sampler must be one of grid, random, sobol, tpe,
            # dlib_global, candidates, and "`early_stop` is not a sampler name"; D6 (line 185):
            # the early stop is "its own input and not a sampler name".
            ("refused", "pbo_sampler_unknown", ()),
            "docs/pbo.md D6 (line 185) and line 356; "
            "test_pbo.py RefusalTests.test_pbo_sampler_unknown",
        ),
        (
            "an error row",
            lambda: planted_call(errored, 2, "grid", "an error row"),
            # docs/pbo.md D5 (line 184: any error, pruned, partial or non-positive-equity row
            # refuses "each with a code of its own") and the Refusals table (line 346:
            # `pbo_status_error`, "a row has a failed-trial status: `error`, `objective_error`,
            # `constraint_error`, `engine_error`, ..."). The ids stay (2,): the one row whose
            # status is set to "engine_error" above.
            ("refused", "pbo_status_error", (2,)),
            "docs/pbo.md D5 (line 184) and line 346; "
            "test_pbo.py RefusalTests.test_pbo_status_error",
        ),
    ]
    # The code is the page's for a spread of more than one interval (docs/pbo.md D2, line 181, and
    # line 342); the ids are empty, as for every refusal of the declared object. That these
    # layouts do violate D2 is checked in phase 1 (``section_uneven_layouts``).
    planted += [
        (
            f"uneven blocks, {label}",
            lambda counts=counts, label=label: planted_call(
                moment_rows(counts), len(counts), "grid", f"uneven blocks, {label}"
            ),
            ("refused", "pbo_block_sizes_uneven", ()),
            "docs/pbo.md D2 (line 181) and line 342; "
            "test_pbo.py RefusalTests.test_pbo_block_sizes_uneven",
        )
        for label, counts, _spread in UNEVEN_LAYOUTS
    ]
    for label, call, literal, source in planted:
        ledger.compare("module", f"refusal: {label}", literal, refusal_of(pbo, call), source)
    ledger.end(3 + len(planted))


def module_rosters(ledger: Ledger, pbo) -> None:
    """The roster label of every real sampler, without and with an early stop (default D6).

    One row for each key of ``ROSTER_LABELS``, expected as ``("ok", label, sampler, early stop)``
    with the label read off the D6 text (see the table) and the early stop echoed as a boolean
    (docs/pbo.md, line 236: "whether the study had an early stop (a boolean)"). A study with an
    early stop is called with ``early_stop=True`` (line 219: the result's object, or True), one
    without with None. ``early_stop`` is no longer a sampler name, so the old row that gave it as
    one is gone; the module's refusal of that name has a case of its own in ``module_refusals``.
    Mutations of the module (read off the code; nothing has been run): replace
    ``"diagnostic_adaptive_roster" if stopped else "paper_conformant"`` at pbo.py:1033 (in
    ``roster_label``) by ``"paper_conformant"`` and the grid, random and sobol rows with an early
    stop fail; replace ``return "diagnostic_adaptive_roster"`` at pbo.py:1031 (in
    ``roster_label``, under ``if sampler in DIAGNOSTIC_SAMPLERS:``) by
    ``return "paper_conformant"`` and the six adaptive rows fail; replace
    ``"early_stop": stopped`` at pbo.py:1227 (in ``pbo``) by ``"early_stop": False`` and the six
    rows with an early stop fail on the echoed flag.
    """
    ledger.begin("module: the roster label of every sampler, without and with an early stop (D6)")
    rows = read_rows("f4_atom_s2_n3")
    for (sampler, stopped), label in ROSTER_LABELS.items():
        found = module_run(
            pbo, rows, 2, sampler, dataset="f4_atom_s2_n3", early_stop=True if stopped else None
        )
        ledger.compare(
            "module",
            f"sampler {sampler}, early stop {stopped}: "
            "(status, roster_label, sampler echoed, early_stop echoed)",
            ("ok", label, sampler, stopped),
            (
                found["status"],
                found.get("roster_label"),
                found.get("sampler"),
                found.get("early_stop"),
            ),
            "docs/pbo.md D6 (line 185), result table (line 236)",
        )
    ledger.end(len(ROSTER_LABELS))


def module_random(ledger: Ledger, pbo, cases: list, seed: int) -> None:
    ledger.begin(f"module: the seeded random cases (seed {seed})")
    # A case that the oracle computes has a result, so its layout is compared; a case that the
    # oracle refuses (zero variance) has none. Both numbers come from the oracle's own cases.
    computed = sum(1 for _size, _counts, _series, oracle in cases if oracle["status"] == "ok")
    two_sizes = sum(
        1
        for _size, counts, _series, oracle in cases
        if oracle["status"] == "ok" and len(set(counts)) > 1
    )
    for number, (size, counts, series, oracle) in enumerate(cases):
        found = module_run(
            pbo, rows_of_series(series), size, dataset=f"random case {number} of seed {seed}"
        )
        ledger.compare(
            "module",
            f"case {number}: S={size}, N={len(series)}",
            oracle_view(oracle),
            module_view(found),
            quiet=True,
        )
        if oracle["status"] == "ok":
            module_layout(ledger, f"case {number}", counts, found, quiet=True)
            module_rate_and_coverage(
                ledger, f"case {number}", oracle["rate_le0"], len(series), found, quiet=True
            )
            # Mutation: pbo.py:1007 `* (S - 2)` -> `* S` in `estimate_work` fails every case.
            ledger.compare(
                "module",
                f"case {number}: work_estimate",
                work_figures(size, len(series))["work"],
                found.get("work_estimate"),
                quiet=True,
            )
    if not two_sizes:
        ledger.fail(
            "no random case that the oracle computes holds blocks of two sizes, so "
            "equal_blocks: false was never compared"
        )
    ledger.end(len(cases) + 5 * computed)


def phase_two(ledger: Ledger, cases: list, seed: int) -> None:
    print("\n##### phase 2: the module under test")
    ledger.begin("independence: nothing of the package was imported before phase 2")
    ledger.check(
        "internal",
        "'pineforge_hpo' is not in sys.modules before phase 2",
        "pineforge_hpo" not in sys.modules,
    )
    ledger.end(1)
    pbo = load_module()
    module_enumeration(ledger, pbo)
    module_fixtures(ledger, pbo)
    module_signed(ledger, pbo)
    module_arithmetic(ledger, pbo)
    module_work(ledger, pbo)
    module_refusals(ledger, pbo)
    module_rosters(ledger, pbo)
    module_random(ledger, pbo, cases, seed)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Independent oracle for pineforge_hpo.pbo")
    parser.add_argument(
        "--no-module",
        action="store_true",
        help="run phase 1 only (the checker against the pinned fixture values); the module is "
        "not imported, the verdict says so and the exit code is 3, never 0",
    )
    parser.add_argument("--cases", type=int, default=60, help="seeded random cases (default 60)")
    parser.add_argument("--seed", type=int, default=20261011, help="random seed (default 20261011)")
    args = parser.parse_args(argv)
    if args.cases < 10:
        parser.error("--cases must be at least 10")
    ledger = Ledger()
    print(
        f"pbo oracle: root {ROOT}, python {sys.version.split()[0]}, "
        f"cases {args.cases}, seed {args.seed}"
    )
    harness_self_test()
    print("##### phase 1: the checker alone (no module imported)")
    section_enumeration(ledger)
    section_fixtures(ledger)
    section_signed(ledger)
    section_arithmetic(ledger)
    section_work(ledger)
    section_zero_variance(ledger)
    section_uneven_layouts(ledger)
    section_controls(ledger)
    cases, skipped = generate_cases(random.Random(args.seed), args.cases)
    section_random_internal(ledger, cases, skipped, args.seed)
    if args.no_module:
        print("\n##### phase 2 NOT RUN (--no-module)")
    else:
        phase_two(ledger, cases, args.seed)
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
