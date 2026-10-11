"""Tests for `pineforge_hpo.pbo`: hand-counted fixtures and one forced failure per refusal code.

Every expected number is derived by hand in the docstring of the test that asserts it, so a
reader can check it without running code. A docstring names the dossier clause (3.1-3.7) or the
pinned default (D1-D7) that the test controls. Two values cannot be derived by hand and are
re-derived inside the test instead: the SHA-256 digest (rebuilt from the documented construction
with hashlib) and the logarithms (the odds are exact ratios written in the docstring; the
logarithm is `math.log` of that ratio). UNEXECUTED until the proof phase.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))

from pineforge_hpo import pbo as pbo_module  # noqa: E402
from pineforge_hpo.pbo import PboError  # noqa: E402

FIXTURES = ROOT / "tests" / "python" / "fixtures" / "pbo"
F4 = "f4_atom_s2_n3.json"
TIE = "tie_s2_n3.json"
S4 = "splits_s4_n4.json"
CONSTRAINED = "constrained_s2_n4.json"
UNEVEN = "uneven_s2_n3.json"
UNEVEN_S4 = "uneven_s4_n3.json"

IDENTITY = "pineforge-hpo-return-blocks-build/v1:sha256:" + "0" * 64
DATASET = "dataset-identity-a"
OTHER_DATASET = "dataset-identity-b"
FIRST_BOUNDARY_MS = 1_600_000_000_000
INTERVAL_MS = 3_600_000


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def block_object(counts) -> dict:
    """The declared object of a part whose blocks hold `counts` intervals: the result-level
    return_blocks object, plus the dataset identity that the caller states."""
    boundaries = [FIRST_BOUNDARY_MS]
    for count in counts:
        boundaries.append(boundaries[-1] + count * INTERVAL_MS)
    return {
        "contract": "pineforge-hpo-return-blocks/v1",
        "series": "bar",
        "S": len(counts),
        "K": sum(counts),
        "n_b": list(counts),
        "boundary_time_ms": boundaries,
        "numeric_build_identity": IDENTITY,
        "dataset_identity": DATASET,
    }


def declared(name: str) -> dict:
    """The roster and the declared object that fixture `name` declares by its own rows."""
    rows = fixture(name)["rows"]
    return {
        "roster": [row["trial_id"] for row in rows],
        "return_blocks": block_object(rows[0]["block_returns"]["count"]),
    }


def one_part(rows, name: str, overrides: dict) -> list:
    """`rows` as the one part of a call: the declared object of fixture `name`, or the
    `return_blocks` keyword of the caller (taken out of `overrides`) when it has one."""
    return [(overrides.pop("return_blocks", declared(name)["return_blocks"]), rows)]


def run_pbo(rows, size: int, name: str, **overrides) -> dict:
    """`pbo` on `rows` as one part, with the declared context of fixture `name`; keywords
    override any of it (`return_blocks` is the declared object of the one part)."""
    parts = one_part(rows, name, overrides)
    arguments = {
        "S": size,
        "sampler": "grid",
        "early_stop": None,
        "roster": declared(name)["roster"],
        **overrides,
    }
    return pbo_module.pbo(parts, **arguments)


def run_parts(parts, size: int, name: str, **overrides) -> dict:
    """`pbo` on `parts` with the roster of fixture `name`; keywords override any of it."""
    arguments = {
        "S": size,
        "sampler": "grid",
        "early_stop": None,
        "roster": declared(name)["roster"],
        **overrides,
    }
    return pbo_module.pbo(parts, **arguments)


def load_table(rows, size: int, name: str, **overrides):
    """`load_moment_table` on `rows` as one part, with the declared context of fixture `name`."""
    parts = one_part(rows, name, overrides)
    arguments = {"S": size, "roster": declared(name)["roster"], **overrides}
    return pbo_module.load_moment_table(parts, **arguments)


def canonical_bytes(result: dict) -> bytes:
    return json.dumps(
        result, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def make_row(trial_id, counts, sums, m2s) -> dict:
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


class EnumerationTests(unittest.TestCase):
    def test_s2_has_two_ordered_splits(self) -> None:
        """Controls clause 3.6 (all C(S, S/2) ordered splits) and 3.7 (n_splits exact).

        S = 2: C(2, 1) = 2. The half that holds block 0 is {0}: as train it is the split
        (train {0}, test {1}); its complement as train is (train {1}, test {0}).
        """
        self.assertEqual(
            list(pbo_module.iter_ordered_splits(2)), [((0,), (1,)), ((1,), (0,))]
        )
        result = run_pbo(fixture(F4)["rows"], 2, F4)
        self.assertEqual(result["n_splits"], 2)
        self.assertEqual(result["n_lt0"] + result["n_eq0"] + result["n_gt0"], 2)

    def test_s4_has_six_ordered_splits(self) -> None:
        """Controls clause 3.6 and the paper's Fig. 1 (p. 13: six combinations for S = 4).

        S = 4: C(4, 2) = 4*3/(2*1) = 6 ordered splits, three complementary pairs in both
        directions. Block b trains in C/2 = 3 splits and tests in the other 3, and the six
        training sets are exactly the six 2-subsets of the 4 blocks, each also used once as a
        testing set (p. 21: every training set is reused as a testing set).
        """
        splits = list(pbo_module.iter_ordered_splits(4))
        self.assertEqual(
            splits,
            [
                ((0, 1), (2, 3)),
                ((2, 3), (0, 1)),
                ((0, 2), (1, 3)),
                ((1, 3), (0, 2)),
                ((0, 3), (1, 2)),
                ((1, 2), (0, 3)),
            ],
        )
        for block in range(4):
            self.assertEqual(sum(1 for train, _ in splits if block in train), 3)
            self.assertEqual(sum(1 for _, test in splits if block in test), 3)
        trains = {train for train, _ in splits}
        tests = {test for _, test in splits}
        self.assertEqual(len(trains), 6)
        self.assertEqual(trains, tests)
        for train, test in splits:
            self.assertEqual(list(train), sorted(train))
            self.assertEqual(sorted(train + test), [0, 1, 2, 3])
        result = run_pbo(fixture(S4)["rows"], 4, S4)
        self.assertEqual(result["n_splits"], 6)

    def test_s16_is_12870_not_12780(self) -> None:
        """Controls clauses 3.6 and 3.7 and the pin's open item 2 (the paper's inconsistency).

        Eq. 2.3 gives C(16, 8) = 16!/(8! 8!) = 12870. The paper's text on pp. 11 and 22 says
        12,780 (two digits transposed). This module follows eq. 2.3. Every block trains in
        C/2 = 6435 of the 12870 splits.
        """
        splits = list(pbo_module.iter_ordered_splits(16))
        self.assertEqual(math.comb(16, 8), 12870)
        self.assertEqual(len(splits), 12870)
        self.assertNotEqual(len(splits), 12780)
        self.assertEqual(len({train for train, _ in splits}), 12870)
        for block in range(16):
            self.assertEqual(sum(1 for train, _ in splits if block in train), 6435)
        for train, test in splits:
            self.assertEqual(len(train), 8)
            self.assertEqual(sorted(train + test), list(range(16)))

    def test_split_counts_for_the_dossier_sizes(self) -> None:
        """Controls clause 3.7 (n_splits = C(S, S/2) exact) for the dossier's table.

        S = 2, 4, 8, 12, 16, 20, 24 give 2, 6, 70, 924, 12870, 184756, 2704156.
        """
        expected = {2: 2, 4: 6, 8: 70, 12: 924, 16: 12870, 20: 184756, 24: 2704156}
        for size, splits in expected.items():
            with self.subTest(S=size):
                self.assertEqual(pbo_module.estimate_work(size, 1)["n_splits"], splits)


class HandCountTests(unittest.TestCase):
    def test_s4_counts_and_lambdas(self) -> None:
        """Controls clause 3.6 (counts, both rates, lambda) and defaults D1 and D3.

        Fixture splits_s4_n4.json: N = 4 trials, S = 4 blocks of one interval each. Returns of
        blocks 0..3:  T0 = (6, 2, 7, 3), T1 = (4, 9, 3, 11), T2 = (8, 5, 1, 9),
        T3 = (2, 4, 10, 7). A half {a, b} pools to n = 2, M2 = (ra - rb)^2 / 2, sigma =
        |ra - rb| / sqrt(2), so its statistic is g / sqrt(2) with g = (ra + rb) / |ra - rb|.
        g per half, for (T0, T1, T2, T3):
          {0,1}: 2, 13/5, 13/3, 3         {2,3}: 5/2, 7/4, 5/4, 17/3
          {0,2}: 13, 7, 9/7, 3/2          {1,3}: 5, 10, 7/2, 11/3
          {0,3}: 3, 15/7, 17, 9/5         {1,2}: 9/5, 2, 3/2, 7/3
        N + 1 = 5; omega = rank / 5; odds = omega / (1 - omega) = rank / (5 - rank).
        The six ordered splits, in the order of iter_ordered_splits(4):
          train {0,1} test {2,3}: winner T2; on {2,3} T2 has 5/4, the smallest: rank 1, odds 1/4
          train {2,3} test {0,1}: winner T3; on {0,1} it beats T0, T1, loses to T2: rank 3, odds 3/2
          train {0,2} test {1,3}: winner T0; on {1,3} it beats T2, T3, loses to T1: rank 3, odds 3/2
          train {1,3} test {0,2}: winner T1; on {0,2} it beats T2, T3, loses to T0: rank 3, odds 3/2
          train {0,3} test {1,2}: winner T2; on {1,2} T2 has 3/2, the smallest: rank 1, odds 1/4
          train {1,2} test {0,3}: winner T3; on {0,3} T3 has 9/5, the smallest: rank 1, odds 1/4
        So n_lt0 = 3, n_eq0 = 0, n_gt0 = 3 and both rates are 3/6 = 0.5. N is even, so no
        split can land on lambda = 0. The odds 1/4 and 3/2 are exact in binary64.
        """
        rows = fixture(S4)["rows"]
        result = run_pbo(rows, 4, S4, with_lambdas=True)
        self.assertEqual(result["S"], 4)
        self.assertEqual(result["N"], 4)
        self.assertEqual(result["n_splits"], 6)
        self.assertEqual((result["n_lt0"], result["n_eq0"], result["n_gt0"]), (3, 0, 3))
        self.assertEqual(result["rate_le0"], 0.5)
        self.assertEqual(result["rate_lt0"], 0.5)
        self.assertEqual(result["pbo"], 0.5)
        low = math.log(1 / 4)
        high = math.log(3 / 2)
        self.assertEqual(result["lambdas"], [low, high, high, high, low, low])
        self.assertEqual(result["lambda_tie_sizes"], [1, 1, 1, 1, 1, 1])
        self.assertEqual(result["work_estimate"], 72)

    def test_tied_training_bests_split_their_weight(self) -> None:
        """Controls default D4 (tie mixture 1/k, OOS mid-rank, exact equality) and clause 3.6.

        Fixture tie_s2_n3.json: S = 2, N = 3, blocks of the 2 intervals (m-1, m+1), so the
        statistic of a one-block half is m / sqrt(2) and trials order by m. Block means
        (block 0, block 1): trial 0 = (4, 3), trial 1 = (4, 1), trial 2 = (1, 2).
        N + 1 = 4; odds = rank / (4 - rank) for a whole rank.
        Split train {0}, test {1}: block-0 means 4, 4, 1, so trials 0 and 1 tie exactly
        (identical moments): k = 2, weight 1/2 each.
          trial 0 on block 1 (means 3, 1, 2): beats both, rank 3, omega 3/4, odds 3, lambda > 0
          trial 1 on block 1: the smallest, rank 1, omega 1/4, odds 1/3, lambda < 0
        Split train {1}, test {0}: block-1 means 3, 1, 2, winner trial 0 alone (k = 1). On
        block 0 (means 4, 4, 1) it ties with trial 1 and beats trial 2: mid-rank =
        1 + (2 + 1)/2 = 5/2, omega 5/8, odds 5/3, lambda > 0.
        So n_gt0 = 1/2 + 1 = 3/2, n_lt0 = 1/2, n_eq0 = 0 and both rates are (1/2)/2 = 1/4.
        A deterministic lowest-id winner would give n_lt0 = 0 and n_gt0 = 2: the mixture is
        what separates the two rules.
        """
        rows = fixture(TIE)["rows"]
        result = run_pbo(rows, 2, TIE, sampler="random", with_lambdas=True)
        self.assertEqual(result["n_splits"], 2)
        self.assertEqual(
            (result["n_lt0"], result["n_eq0"], result["n_gt0"]), (0.5, 0, 1.5)
        )
        self.assertEqual(result["rate_le0"], 0.25)
        self.assertEqual(result["rate_lt0"], 0.25)
        self.assertEqual(
            result["lambdas"], [math.log(3 / 1), math.log(1 / 3), math.log(5 / 3)]
        )
        self.assertEqual(result["lambda_tie_sizes"], [2, 2, 1])

    def test_lambda_zero_atom_gives_two_different_rates(self) -> None:
        """Controls default D3 (mass at lambda = 0, both rates) and the dossier's fixture F4.

        Fixture f4_atom_s2_n3.json: S = 2, N = 3 (odd), blocks of the 2 intervals (m-1, m+1).
        Block means (block 0, block 1): trial 0 = (5, 2), trial 1 = (1, 3), trial 2 = (3, 1).
        N + 1 = 4.
        Split train {0}, test {1}: block-0 means 5, 1, 3, winner trial 0. On block 1 (means
        2, 3, 1) its 2 beats trial 2 and loses to trial 1: rank 2 = (N + 1)/2, omega 2/4 =
        1/2, odds 1, lambda = ln 1 = 0 exactly: the atom.
        Split train {1}, test {0}: block-1 means 2, 3, 1, winner trial 1. On block 0 (means
        5, 1, 3) its 1 is the smallest: rank 1, omega 1/4, odds 1/3, lambda = ln(1/3) < 0.
        So n_lt0 = 1, n_eq0 = 1, n_gt0 = 0. The primary rate (lambda <= 0) is 2/2 = 1 and the
        strict rate (lambda < 0) is 1/2: the two readings differ.
        """
        rows = fixture(F4)["rows"]
        result = run_pbo(rows, 2, F4, with_lambdas=True)
        self.assertEqual((result["n_lt0"], result["n_eq0"], result["n_gt0"]), (1, 1, 0))
        self.assertGreater(result["n_eq0"], 0)
        self.assertEqual(result["rate_le0"], 1.0)
        self.assertEqual(result["pbo"], 1.0)
        self.assertEqual(result["rate_lt0"], 0.5)
        self.assertNotEqual(result["rate_le0"], result["rate_lt0"])
        self.assertEqual(result["lambdas"], [0.0, math.log(1 / 3)])
        self.assertEqual(result["lambda_tie_sizes"], [1, 1])

    def test_constraint_violating_trials_are_in_the_roster(self) -> None:
        """Controls default D5 (every tried trial is in the roster, constraint-violating ones
        included: the paper's p. 24) and clause 3.5.

        Fixture constrained_s2_n4.json: S = 2, N = 4, blocks of the 2 intervals (m-1, m+1), so the
        statistic of a one-block half is m / sqrt(2) and trials order by m. Block means (block 0,
        block 1): trial 0 = (4, 1), trial 1 = (2, 3), trial 2 = (1, 2), all with status ok, and
        trial 3 = (5, 4) with status constraint_violation. N + 1 = 5; odds = rank / (5 - rank).
        Split train {0}, test {1}: block-0 means 4, 2, 1, 5, so trial 3 wins. On block 1 (means 1,
        3, 2, 4) it is the largest: rank 4, omega 4/5, odds 4, lambda = ln 4 > 0.
        Split train {1}, test {0}: block-1 means 1, 3, 2, 4, trial 3 wins again. On block 0 (means
        4, 2, 1, 5) it is the largest: rank 4, odds 4, lambda = ln 4 > 0.
        So n_lt0 = 0, n_eq0 = 0, n_gt0 = 2 and both rates are 0; the work figure is 0 merges +
        2 * 2 * 4 = 16 comparisons.
        Without trial 3 (the first three rows, N = 3, N + 1 = 4): train {0}, trial 0 wins (4
        against 2 and 1) and on block 1 (means 1, 3, 2) it is the smallest: rank 1, odds 1/3.
        Train {1}, trial 1 wins (3 against 1 and 2) and on block 0 (means 4, 2, 1) it beats trial 2
        and loses to trial 0: rank 2 = (N + 1)/2, odds 1, lambda = 0 exactly. That gives n_lt0 = 1,
        n_eq0 = 1, n_gt0 = 0 and the rates 1 and 1/2. The violating row changes every count, so a
        module that dropped it, or refused it, would be visible here.

        Mutation: removing CONSTRAINT_VIOLATION from ROSTER_ROW_STATUSES makes trial 3 a refused
        row and this test raise instead of returning the counts.
        """
        rows = fixture(CONSTRAINED)["rows"]
        self.assertEqual(rows[3]["status"], "constraint_violation")
        result = run_pbo(rows, 2, CONSTRAINED, with_lambdas=True)
        self.assertEqual(result["N"], 4)
        self.assertEqual((result["n_lt0"], result["n_eq0"], result["n_gt0"]), (0, 0, 2))
        self.assertEqual(result["pbo"], 0.0)
        self.assertEqual(result["rate_lt0"], 0.0)
        self.assertEqual(result["lambdas"], [math.log(4), math.log(4)])
        self.assertEqual(result["lambda_tie_sizes"], [1, 1])
        self.assertEqual(result["work_estimate"], 16)
        without = run_pbo(rows[:3], 2, CONSTRAINED, roster=[0, 1, 2], with_lambdas=True)
        self.assertEqual(without["N"], 3)
        self.assertEqual(
            (without["n_lt0"], without["n_eq0"], without["n_gt0"]), (1, 1, 0)
        )
        self.assertEqual(without["pbo"], 1.0)
        self.assertEqual(without["rate_lt0"], 0.5)
        self.assertEqual(without["lambdas"], [math.log(1 / 3), 0.0])
        self.assertNotEqual(result["input_sha256"], without["input_sha256"])

    def test_blocks_that_differ_by_one_are_computed_and_labelled(self) -> None:
        """Controls default D2 (block sizes may differ by at most one interval, labelled a
        deviation from the paper's equal blocks) and clause 3.2.

        Fixture uneven_s2_n3.json: S = 2, N = 3, block 0 holds 2 intervals (m-1, m+1) and block 1
        holds 3 (m-1, m, m+1), so n_b = (2, 3) and K = 5. A one-block half orders the trials by
        its block mean m: on block 0 (n = 2, M2 = 2) the statistic is m / sqrt(2), on block 1
        (n = 3, M2 = 2, sample sigma = sqrt(2/2) = 1) it is m. Block means (block 0, block 1):
        trial 0 = (5, 2), trial 1 = (1, 3), trial 2 = (3, 1), the ranking of F4. N + 1 = 4.
        Split train {0}, test {1}: trial 0 wins (5 against 1 and 3). On block 1 (means 2, 3, 1)
        its 2 beats trial 2 and loses to trial 1: rank 2 = (N + 1)/2, odds 1, lambda = 0 exactly.
        Split train {1}, test {0}: block-1 means 2, 3, 1, so trial 1 wins; on block 0 (means 5, 1,
        3) its 1 is the smallest: rank 1, odds 1/3, lambda = ln(1/3) < 0.
        So n_lt0 = 1, n_eq0 = 1, n_gt0 = 0 and the rates are 1 and 1/2, as for F4, and the work
        figure is 12 (no merges, 2 * 2 * 3 comparisons). The result says `equal_blocks` false and
        `block_counts` [2, 3]; equal blocks (F4) say `equal_blocks` true and [2, 2].

        Mutation: replacing `len(set(table.counts)) == 1` by `True` in `pbo` reports the uneven
        blocks as equal and fails the `equal_blocks` assertion.
        """
        rows = fixture(UNEVEN)["rows"]
        result = run_pbo(rows, 2, UNEVEN, with_lambdas=True)
        self.assertEqual((result["n_lt0"], result["n_eq0"], result["n_gt0"]), (1, 1, 0))
        self.assertEqual(result["pbo"], 1.0)
        self.assertEqual(result["rate_lt0"], 0.5)
        self.assertEqual(result["lambdas"], [0.0, math.log(1 / 3)])
        self.assertEqual(result["work_estimate"], 12)
        self.assertIs(result["equal_blocks"], False)
        self.assertEqual(result["block_counts"], [2, 3])
        equal = run_pbo(fixture(F4)["rows"], 2, F4)
        self.assertIs(equal["equal_blocks"], True)
        self.assertEqual(equal["block_counts"], [2, 2])

    def test_s4_blocks_of_unequal_size_are_merged_by_their_own_counts(self) -> None:
        """Controls default D2 at S = 4 (blocks differing by one interval, where halves are
        merged: the S = 2 case above merges nothing), default D7 (the merge) for blocks of unequal
        counts, and clause 3.2.

        Fixture uneven_s4_n3.json: S = 4, N = 3, n_b = (2, 1, 2, 1), K = 6 (the floor rule puts
        intervals 0..5 in blocks 0, 0, 1, 2, 2, 3). Interval returns per trial as blocks
        (b0 | b1 | b2 | b3): trial 0 = (5, 7 | 6 | 1, 3 | 2), trial 1 = (1, 3 | 3 | 5, 8 | 6),
        trial 2 = (3, 5 | 4 | 3, 6 | 5). A half pools the returns of its two blocks and its
        statistic is their mean over their sample sigma (n - 1 in the divisor). The values below
        are computed from the raw returns, not from the merge. For (trial 0, trial 1, trial 2):
          {0,1}: values (5,7,6) (1,3,3) (3,5,4): 6, 2.021, 4
          {2,3}: values (1,3,2) (5,8,6) (3,6,5): 2, 4.146, 3.055
          {0,2}: values (5,7,1,3) (1,3,5,8) (3,5,3,6): 1.549, 1.423, 2.833
          {1,3}: values (6,2) (3,6) (4,5): 1.414, 2.121, 6.364
          {0,3}: values (5,7,2) (1,3,6) (3,5,5): 1.854, 1.325, 3.753
          {1,2}: values (6,1,3) (3,5,8) (4,3,6): 1.325, 2.119, 2.837
        (for example {2,3}, trial 1: mean 19/3, sum of squared deviations 14/3, sample variance
        7/3, statistic (19/3) / sqrt(7/3) = 4.146). No two trials are within 0.1 of each other
        in any half, so no rounding can change an order, and no half has a tie. N + 1 = 4; a
        winner on the bottom has rank 1 (odds 1/3), on the top rank 3 (odds 3). The ordered
        splits:
          train {0,1} test {2,3}: winner trial 0; on {2,3} it has 2, the smallest: odds 1/3
          train {2,3} test {0,1}: winner trial 1; on {0,1} it has 2.021, the smallest: odds 1/3
          train {0,2} test {1,3}: winner trial 2; on {1,3} it has 6.364, the largest: odds 3
          train {1,3} test {0,2}: winner trial 2; on {0,2} it has 2.833, the largest: odds 3
          train {0,3} test {1,2}: winner trial 2; on {1,2} it has 2.837, the largest: odds 3
          train {1,2} test {0,3}: winner trial 2; on {0,3} it has 3.753, the largest: odds 3
        So n_lt0 = 2, n_eq0 = 0, n_gt0 = 4 and both rates are 2/6 = 1/3. The work figure is 18
        merges (3 * 3 * 2) + 36 comparisons (2 * 6 * 3) = 54. The result says `equal_blocks`
        false and `block_counts` [2, 1, 2, 1].

        Mutation: replacing `counts[block]` by `counts[head]` in the `merge_moments` call of
        `_half_statistics` counts every merged block with the size of the first block of its half.
        That changes the statistics of the halves {0,1}, {2,3}, {0,3} and {1,2} (the other two
        merge blocks of one size). On {2,3} they become (1.5, 2.010, 2.079): the training winner
        is trial 2 and not trial 1, and on {0,1} (2.350, 2.021, 2.121) trial 2 is the median, an
        atom with lambda = 0. So n_eq0 is at least 1 where it is 0 here, and the count assertion
        fails.
        """
        rows = fixture(UNEVEN_S4)["rows"]
        result = run_pbo(rows, 4, UNEVEN_S4, with_lambdas=True)
        self.assertEqual(result["S"], 4)
        self.assertEqual(result["N"], 3)
        self.assertEqual(result["n_splits"], 6)
        self.assertEqual((result["n_lt0"], result["n_eq0"], result["n_gt0"]), (2, 0, 4))
        self.assertEqual(result["rate_le0"], 1 / 3)
        self.assertEqual(result["rate_lt0"], 1 / 3)
        self.assertEqual(result["pbo"], 1 / 3)
        low = math.log(1 / 3)
        high = math.log(3)
        self.assertEqual(result["lambdas"], [low, low, high, high, high, high])
        self.assertEqual(result["lambda_tie_sizes"], [1, 1, 1, 1, 1, 1])
        self.assertEqual(result["work_estimate"], 54)
        self.assertIs(result["equal_blocks"], False)
        self.assertEqual(result["block_counts"], [2, 1, 2, 1])

    def test_result_fields_and_wording(self) -> None:
        """Controls clause 3.6 (report fields, `calibrated:false`, wording) and default D6."""
        rows = fixture(F4)["rows"]
        result = run_pbo(rows, 2, F4, sampler="sobol")
        self.assertEqual(result["contract"], "pineforge-hpo-pbo/v1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["algorithm"], pbo_module.ALGORITHM)
        self.assertEqual(result["statistic"], pbo_module.STATISTIC)
        self.assertIs(result["calibrated"], False)
        self.assertEqual(result["sampler"], "sobol")
        self.assertEqual(result["roster_label"], "paper_conformant")
        self.assertEqual(
            result["tie_rules"],
            {
                "equality": "exact_binary64",
                "train_best": "mixture_weight_1_over_k",
                "oos_rank": "mid_rank",
            },
        )
        for phrase in (
            "deterministic frequency",
            "re-labellings of one dataset",
            "roster",
            "diagnostic",
            "not a probability under independent strategy draws",
        ):
            self.assertIn(phrase, result["interpretation"])
        for absent in (
            "lambdas",
            "lambda_tie_sizes",
            "standard_error",
            "threshold",
            "p_value",
        ):
            self.assertNotIn(absent, result)
        self.assertEqual(len(result["input_sha256"]), 64)
        self.assertEqual(
            result["coverage"], {"declared": 3, "supplied": 3, "usable": 3}
        )
        self.assertEqual(result["refusals"], [])
        json.dumps(result, allow_nan=False)

    def test_roster_labels(self) -> None:
        """Controls default D6: paper-conformant only for grid, random, sobol without an early
        stop; adaptive rosters are labelled diagnostic; anything else (and no sampler) refuses.
        `early_stop` is not a sampler name any more (it is its own input, tested below), and the
        caller must say whether the study had one (tested in the refusal tests)."""
        for name in ("grid", "random", "sobol"):
            self.assertEqual(
                pbo_module.roster_label(name, early_stop=None), "paper_conformant"
            )
        for name in ("tpe", "dlib_global", "candidates"):
            self.assertEqual(
                pbo_module.roster_label(name, early_stop=None),
                "diagnostic_adaptive_roster",
            )
        with self.assertRaises(PboError) as caught:
            pbo_module.roster_label("early_stop", early_stop=None)
        self.assertEqual(caught.exception.code, "pbo_sampler_unknown")
        rows = fixture(F4)["rows"]
        result = run_pbo(rows, 2, F4, sampler="tpe")
        self.assertEqual(result["roster_label"], "diagnostic_adaptive_roster")

    def test_early_stop_is_its_own_input_and_labels_the_roster(self) -> None:
        """Controls default D6: a no-improvement early stop makes the roster diagnostic whatever
        the sampler, and it is not a sampler name.

        The native result carries the stop as a separate `early_stop` object beside the sampler,
        so `pbo` takes it as its own argument: the object (or `True`) when the study had one,
        `None` (or `False`) when it had none. A grid, random or sobol study without one is
        `paper_conformant` and says `early_stop` false; with one it is
        `diagnostic_adaptive_roster` and says `early_stop` true; an adaptive sampler is
        diagnostic either way. The stop changes the label only: the counts of F4 stay (1, 1, 0).

        Mutation: dropping the early-stop clause in `roster_label` (returning `"paper_conformant"`
        for a paper-conformant sampler whatever `early_stop` says) fails the `grid` and `random`
        cases that pass a stop.
        """
        rows = fixture(F4)["rows"]
        stop = {"patience_trials": 3, "trigger_trial_id": None}
        cases = [
            ("grid", None, False, "paper_conformant"),
            ("grid", False, False, "paper_conformant"),
            ("grid", True, True, "diagnostic_adaptive_roster"),
            ("random", stop, True, "diagnostic_adaptive_roster"),
            ("sobol", {}, True, "diagnostic_adaptive_roster"),
            ("tpe", None, False, "diagnostic_adaptive_roster"),
            ("tpe", True, True, "diagnostic_adaptive_roster"),
        ]
        for sampler, early_stop, flag, label in cases:
            with self.subTest(sampler=sampler, early_stop=early_stop):
                result = run_pbo(rows, 2, F4, sampler=sampler, early_stop=early_stop)
                self.assertIs(result["early_stop"], flag)
                self.assertEqual(result["roster_label"], label)
                self.assertEqual(
                    (result["n_lt0"], result["n_eq0"], result["n_gt0"]), (1, 1, 0)
                )
        self.assertEqual(
            pbo_module.roster_label("grid", early_stop=True),
            "diagnostic_adaptive_roster",
        )


class ArithmeticTests(unittest.TestCase):
    def test_statistic_is_pooled_mean_over_sample_sigma(self) -> None:
        """Controls default D1 (pooled over the union of intervals, sample sigma, risk-free 0,
        unannualised) and dossier clause 3.4.

        Blocks A = (n 2, sum 4, M2 2) from the values 1, 3 and B = (n 2, sum 8, M2 2) from the
        values 3, 5. Pooled over the union 1, 3, 3, 5: n = 4, sum = 12, mean = 3, M2 = 4 + 0 + 0
        + 4 = 8, sample variance 8/3, statistic = 3 / sqrt(8/3). It is neither the population
        form 3 / sqrt(8/4) nor the average of the two block statistics (2/sqrt(2) + 4/sqrt(2))/2.
        """
        n, s, m2 = pbo_module.pooled_moments([(2, 4.0, 2.0), (2, 8.0, 2.0)])
        self.assertEqual((n, s, m2), (4, 12.0, 8.0))
        statistic = pbo_module.ranking_statistic(n, s, m2)
        self.assertEqual(statistic, 3.0 / math.sqrt(8.0 / 3))
        self.assertNotEqual(statistic, 3.0 / math.sqrt(8.0 / 4))
        average = (2.0 / math.sqrt(2.0) + 4.0 / math.sqrt(2.0)) / 2
        self.assertNotEqual(statistic, average)
        self.assertIsNone(pbo_module.ranking_statistic(4, 12.0, 0.0))

    def test_merge_matches_the_direct_two_pass_form(self) -> None:
        """Controls dossier clause 3.4 and default D7 (Chan/Pebay merge, block order).

        Blocks B0 = (2, 4.0, 2.0) from the values 1, 3; B1 = (2, 8.0, 2.0) from 3, 5;
        B2 = (2, 9.0, 0.5) from 4, 5. All arithmetic below is exact in binary64.
        B0+B1: d = 8/2 - 4/2 = 2, t = ((2*2)*2)*2/4 = 4, M2 = (2 + 2) + 4 = 8, sum 12, n 4.
        (B0+B1)+B2: d = 9/2 - 12/4 = 3/2, t = ((9/4)*4)*2/6 = 3, M2 = (8 + 1/2) + 3 = 23/2.
        Direct two-pass over 1, 3, 3, 5, 4, 5: mean 7/2, squared deviations 25/4, 1/4, 1/4,
        9/4, 1/4, 9/4 sum to 23/2. The other grouping B0+(B1+B2): B1+B2 = (4, 17.0, 11/4);
        d = 17/4 - 2 = 9/4, t = ((81/16)*2)*4/6 = 27/4, M2 = (2 + 11/4) + 27/4 = 23/2.
        The non-contiguous union B0+B2: d = 9/2 - 4/2 = 5/2, t = ((25/4)*2)*2/4 = 25/4,
        M2 = (2 + 1/2) + 25/4 = 35/4, sum 13, n 4. Direct over 1, 3, 4, 5 (mean 13/4): squared
        deviations 81/16, 1/16, 9/16, 49/16 sum to 140/16 = 35/4.
        """
        b0 = (2, 4.0, 2.0)
        b1 = (2, 8.0, 2.0)
        b2 = (2, 9.0, 0.5)
        self.assertEqual(pbo_module.merge_moments(b0, b1), (4, 12.0, 8.0))
        self.assertEqual(pbo_module.pooled_moments([b0, b1, b2]), (6, 21.0, 11.5))
        regrouped = pbo_module.merge_moments(b0, pbo_module.merge_moments(b1, b2))
        self.assertEqual(pbo_module.merge_moments(b1, b2), (4, 17.0, 2.75))
        self.assertEqual(regrouped, (6, 21.0, 11.5))
        values = [1.0, 3.0, 3.0, 5.0, 4.0, 5.0]
        mean = math.fsum(values) / len(values)
        direct = math.fsum((value - mean) * (value - mean) for value in values)
        self.assertEqual((len(values), math.fsum(values), direct), (6, 21.0, 11.5))
        union = [1.0, 3.0, 4.0, 5.0]
        union_mean = math.fsum(union) / len(union)
        union_direct = math.fsum((v - union_mean) * (v - union_mean) for v in union)
        self.assertEqual(union_direct, 8.75)
        self.assertEqual(pbo_module.pooled_moments([b0, b2]), (4, 13.0, 8.75))

    def test_merge_of_unequal_counts_matches_the_direct_two_pass_form(self) -> None:
        """Controls dossier clause 3.4 and default D7 (Chan/Pebay merge) for blocks of unequal
        counts, which no other merge test in this file uses: with equal counts na and nb are the
        same number, so a merge that weighted the blocks by na * na or nb * nb instead of
        na * nb could not be told from the right one. Every value below is exact in binary64.

        A = (3, 6.0, 2.0) from the values 1, 2, 3 and B = (5, 25.0, 10.0) from 3, 4, 5, 6, 7.
        A+B: n = 8, d = 25/5 - 6/3 = 3, t = (((3*3)*3)*5)/8 = 135/8 = 16.875, M2 = (2 + 10) +
        16.875 = 28.875, sum 31. B+A: d = 6/3 - 25/5 = -3 and the same t, so the same triple.
        Direct two-pass over 1, 2, 3, 3, 4, 5, 6, 7: mean 31/8 = 3.875, squared deviations
        8.265625, 3.515625, 0.765625, 0.765625, 0.015625, 1.265625, 4.515625 and 9.765625 add up
        to 28.875.
        C = (1, 1.0, 0.0) from the value 1 and D = (3, 12.0, 8.0) from 2, 4, 6: C+D has n = 4,
        d = 12/3 - 1/1 = 3, t = (((3*3)*1)*3)/4 = 27/4 = 6.75, M2 = (0 + 8) + 6.75 = 14.75, sum 13;
        direct over 1, 2, 4, 6 (mean 13/4): 5.0625 + 1.5625 + 0.5625 + 7.5625 = 14.75. Merged
        left to right with E = (2, 8.0, 2.0) from 3, 5: d = 8/2 - 13/4 = 0.75,
        t = (((0.75*0.75)*4)*2)/6 = 0.75, M2 = (14.75 + 2) + 0.75 = 17.5, sum 21; direct over
        1, 2, 4, 6, 3, 5 (mean 7/2): 6.25 + 2.25 + 0.25 + 6.25 + 0.25 + 2.25 = 17.5.

        Mutation: replacing `t = t * nb` by `t = t * na` in `merge_moments` gives t = 81/8 and
        M2 = 22.125 for A+B (and 40.125 for B+A) instead of 28.875, and fails the first
        assertions.
        """
        a = (3, 6.0, 2.0)
        b = (5, 25.0, 10.0)
        self.assertEqual(pbo_module.merge_moments(a, b), (8, 31.0, 28.875))
        self.assertEqual(pbo_module.merge_moments(b, a), (8, 31.0, 28.875))
        values = [1.0, 2.0, 3.0, 3.0, 4.0, 5.0, 6.0, 7.0]
        mean = math.fsum(values) / len(values)
        direct = math.fsum((value - mean) * (value - mean) for value in values)
        self.assertEqual((len(values), math.fsum(values), direct), (8, 31.0, 28.875))
        c = (1, 1.0, 0.0)
        d = (3, 12.0, 8.0)
        e = (2, 8.0, 2.0)
        self.assertEqual(pbo_module.merge_moments(c, d), (4, 13.0, 14.75))
        self.assertEqual(pbo_module.pooled_moments([c, d, e]), (6, 21.0, 17.5))
        chain = [1.0, 2.0, 4.0, 6.0, 3.0, 5.0]
        chain_mean = math.fsum(chain) / len(chain)
        chain_direct = math.fsum(
            (value - chain_mean) * (value - chain_mean) for value in chain
        )
        self.assertEqual((len(chain), math.fsum(chain), chain_direct), (6, 21.0, 17.5))

    def test_near_constant_series_keeps_the_centred_m2_non_negative(self) -> None:
        """Controls default D7 and the dossier's fixture F5 (sum of squares is unstable).

        Let e = 2**-52 and x = 1 + 2e (exact). The series x, x, x has true variance 0.
        Sum-of-squares form, evaluated left to right in binary64: fl(x*x) = 1 + 4e (the 4e*e
        term is far below half an ulp), so q = 3 + 12e (exact) and s = 3 + 6e (exact). The
        product s*s = 9 + 36e + 36e*e lies just above the midpoint 9 + 36e of the two doubles
        9 + 32e and 9 + 40e, so fl(s*s) = 9 + 40e; divided by 3 it is 3 + (40/3)e, which rounds
        to 3 + 14e. Hence q - s*s/3 = (3 + 12e) - (3 + 14e) = -2e = -2**-51, a negative variance.
        Centred form: s/3 = 1 + 2e = x exactly, every deviation is 0, so M2 = 0.
        Merged with the blocks of y = 1 + 4e (sum 3 + 12e): d = y - x = 2e, so
        M2 = (0 + 0) + (((2e)*(2e))*3*3)/6 = 6*e*e = 3 * 2**-103, exactly the true M2 of three
        copies of x and three of y (each deviation is e, six of them). The sum-of-squares form on
        the same six values gives q = 6 + 36e, s = 6 + 16e (a tie that rounds to even) and
        q - s*s/6 = (6 + 36e) - (6 + 32e) = 4e = 2**-50, positive but 2**53/3, about 3.0e15,
        times too big.
        """
        eps = 2.0**-52
        x = 1.0 + 2 * eps
        y = 1.0 + 4 * eps

        def naive(values: tuple) -> float:
            q = 0.0
            s = 0.0
            for value in values:
                q += value * value
                s += value
            return q - s * s / len(values)

        self.assertEqual(naive((x, x, x)), -(2.0**-51))
        self.assertLess(naive((x, x, x)), 0.0)
        total = x + x + x
        mean = total / 3
        centred = 0.0
        for value in (x, x, x):
            deviation = value - mean
            centred += deviation * deviation
        self.assertEqual(mean, x)
        self.assertEqual(centred, 0.0)

        block_x = (3, x + x + x, 0.0)
        block_y = (3, y + y + y, 0.0)
        n, _, m2 = pbo_module.pooled_moments([block_x, block_y])
        self.assertEqual(n, 6)
        self.assertEqual(m2, 3 * 2.0**-103)
        self.assertGreaterEqual(m2, 0.0)
        self.assertEqual(naive((x, x, x, y, y, y)), 2.0**-50)

    def test_work_figures(self) -> None:
        """Controls dossier clause 3.7 (admission figure, exact integers).

        n_splits = C(S, S/2); merges = (n_splits/2) * N * (S - 2); comparisons =
        2 * n_splits * N. S = 2, N = 3: 2 splits, 1*3*0 = 0 merges, 2*2*3 = 12 comparisons.
        S = 4, N = 4: 6 splits, 3*4*2 = 24 merges, 2*6*4 = 48 comparisons, work 72.
        S = 16, N = 1000: 12870 splits, 6435*1000*14 = 90,090,000 merges (the dossier's about
        9.0e7), 2*12870*1000 = 25,740,000 comparisons. S = 24, N = 1000: 1,352,078*1000*22 =
        29,745,716,000 merges (the dossier's about 3.0e10).
        """
        self.assertEqual(
            pbo_module.estimate_work(2, 3),
            {"n_splits": 2, "merges": 0, "comparisons": 12, "work": 12},
        )
        self.assertEqual(
            pbo_module.estimate_work(4, 4),
            {"n_splits": 6, "merges": 24, "comparisons": 48, "work": 72},
        )
        self.assertEqual(
            pbo_module.estimate_work(16, 1000),
            {
                "n_splits": 12870,
                "merges": 90090000,
                "comparisons": 25740000,
                "work": 115830000,
            },
        )
        big = pbo_module.estimate_work(24, 1000)
        self.assertEqual(big["n_splits"], 2704156)
        self.assertEqual(big["merges"], 29745716000)


class DeterminismTests(unittest.TestCase):
    def test_shuffled_rows_give_identical_bytes(self) -> None:
        """Controls clause 3.6 (determinism): any row order and any repeat give the same bytes.

        Covers a table with ties (tie_s2_n3.json), the atom table and the S = 4 table, with
        the lambda list requested so the whole result is compared. The two different tables
        give different bytes, so the equality is not trivial.
        """
        outputs = {}
        for name, size in ((S4, 4), (TIE, 2), (F4, 2)):
            with self.subTest(fixture=name):
                rows = fixture(name)["rows"]
                reference = canonical_bytes(
                    run_pbo(rows, size, name, with_lambdas=True)
                )
                orders = [
                    list(reversed(rows)),
                    rows[1:] + rows[:1],
                    rows[2:] + rows[:2],
                    list(rows),
                    list(rows),
                ]
                for order in orders:
                    candidate = canonical_bytes(
                        run_pbo(order, size, name, with_lambdas=True)
                    )
                    self.assertEqual(candidate, reference)
                outputs[name] = reference
        self.assertNotEqual(outputs[TIE], outputs[F4])

    def test_roster_and_object_order_do_not_matter(self) -> None:
        """Controls clause 3.6 (determinism) for the two declared inputs.

        The roster is a set of ids: any order of it, a tuple or a set give the same bytes. The
        return_blocks object is read by key: the order of its keys does not matter, and a copy
        that carries the same values in other containers (tuples for lists) gives the same
        bytes. A different roster order and a different key order must not move the result.

        Not a refusal test, so no mutation check is claimed: the independence is structural (the
        roster is compared as a set and the object is rebuilt key by key), and this test guards
        it against a later rewrite.
        """
        rows = fixture(F4)["rows"]
        reference = canonical_bytes(run_pbo(rows, 2, F4, with_lambdas=True))
        context = declared(F4)
        reversed_keys = dict(reversed(list(context["return_blocks"].items())))
        as_tuples = {
            key: tuple(value) if isinstance(value, list) else value
            for key, value in context["return_blocks"].items()
        }
        variants = [
            {"roster": [2, 0, 1]},
            {"roster": (1, 2, 0)},
            {"roster": {2, 1, 0}},
            {"roster": frozenset([0, 1, 2])},
            {"return_blocks": reversed_keys},
            {"return_blocks": as_tuples},
        ]
        for variant in variants:
            with self.subTest(variant=sorted(variant)):
                candidate = canonical_bytes(
                    run_pbo(rows, 2, F4, with_lambdas=True, **variant)
                )
                self.assertEqual(candidate, reference)

    def test_the_grouping_into_parts_does_not_change_the_result(self) -> None:
        """Controls clause 3.6 (determinism) for the parts: how the rows are grouped into parts,
        in what order the parts come and whether a part holds no row do not move a number, so the
        result is byte for byte the result of the rows as one part.

        Every part declares the same object here (a difference between parts refuses, tested in
        the refusal tests). The tables are the S = 4 table and the table with ties, with the
        lambda list requested so the whole result is compared.

        Not a refusal test, so no mutation check is claimed: the independence is structural (the
        rows are pooled by trial id and the declared objects must be equal), and this test guards
        it against a later rewrite.
        """
        for name, size in ((S4, 4), (TIE, 2)):
            rows = fixture(name)["rows"]
            shared = declared(name)["return_blocks"]
            reference = canonical_bytes(run_pbo(rows, size, name, with_lambdas=True))
            groupings = [
                [rows],
                [rows[:1], rows[1:]],
                [rows[1:], rows[:1]],
                [rows[2:], rows[:2]],
                [[row] for row in reversed(rows)],
                [rows[::2], rows[1::2]],
                [rows, []],
                [[], rows],
            ]
            for groups in groupings:
                shape = [[row["trial_id"] for row in group] for group in groups]
                with self.subTest(fixture=name, groups=shape):
                    parts = [(shared, group) for group in groups]
                    candidate = canonical_bytes(
                        run_parts(parts, size, name, with_lambdas=True)
                    )
                    self.assertEqual(candidate, reference)

    def test_loader_sorts_by_trial_id_and_keeps_every_row(self) -> None:
        """Controls clause 3.5 (never subsets) and 3.6 (sorted by trial id first)."""
        rows = fixture(F4)["rows"]
        table = load_table(list(reversed(rows)), 2, F4)
        self.assertEqual(table.trial_ids, (0, 1, 2))
        self.assertEqual(table.S, 2)
        self.assertEqual(table.counts, (2, 2))
        self.assertEqual(table.sums, ((10.0, 4.0), (2.0, 6.0), (6.0, 2.0)))
        self.assertEqual(table.m2, ((2.0, 2.0), (2.0, 2.0), (2.0, 2.0)))

    def test_input_digest_follows_the_documented_construction(self) -> None:
        """Controls clause 3.6 (SHA-256 of the canonical input).

        A digest cannot be derived by hand, so the test rebuilds it from the documented
        construction: SHA-256 over the sorted concatenation of the per-row SHA-256 digests
        (raw 32-byte values) of the compact, key-sorted JSON of [trial_id, count, sum, m2].
        """
        rows = fixture(F4)["rows"]
        texts = [
            "[0,[2,2],[10.0,4.0],[2.0,2.0]]",
            "[1,[2,2],[2.0,6.0],[2.0,2.0]]",
            "[2,[2,2],[6.0,2.0],[2.0,2.0]]",
        ]
        digests = sorted(
            hashlib.sha256(text.encode("ascii")).digest() for text in texts
        )
        expected = hashlib.sha256(b"".join(digests)).hexdigest()
        table = load_table(rows, 2, F4)
        self.assertEqual(table.input_sha256, expected)
        shuffled = load_table(list(reversed(rows)), 2, F4)
        self.assertEqual(shuffled.input_sha256, expected)
        changed = copy.deepcopy(rows)
        changed[0]["block_returns"]["sum"][0] = 10.5
        self.assertNotEqual(load_table(changed, 2, F4).input_sha256, expected)
        other = load_table(fixture(TIE)["rows"], 2, TIE)
        self.assertNotEqual(other.input_sha256, expected)

    def test_result_names_the_declared_object(self) -> None:
        """Controls clause 3.6 (the result names the input it was computed from).

        `return_blocks_sha256` is the SHA-256 of the compact, key-sorted, ASCII JSON of the
        validated result-level object, rebuilt here with hashlib. Another identity gives
        another digest, and the same moments under it give the same numbers.

        Mutation: hashing `table.input_sha256` instead of `table.return_blocks_json` in `pbo`
        gives the row digest, which differs from the rebuilt object digest.
        """
        rows = fixture(F4)["rows"]
        context = declared(F4)
        text = json.dumps(
            context["return_blocks"],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        expected = hashlib.sha256(text.encode("ascii")).hexdigest()
        result = run_pbo(rows, 2, F4)
        self.assertEqual(result["return_blocks_sha256"], expected)
        other = block_object([2, 2])
        other["numeric_build_identity"] = IDENTITY + "-other"
        moved = run_pbo(rows, 2, F4, return_blocks=other)
        self.assertNotEqual(moved["return_blocks_sha256"], expected)
        self.assertEqual(moved["input_sha256"], result["input_sha256"])
        self.assertEqual(moved["pbo"], result["pbo"])


class RefusalTests(unittest.TestCase):
    def rows(self) -> list:
        return copy.deepcopy(fixture(F4)["rows"])

    def uneven_rows(self) -> list:
        return copy.deepcopy(fixture(UNEVEN)["rows"])

    def table_fields(self) -> dict:
        """The constructor arguments of the table that the F4 rows give."""
        table = load_table(self.rows(), 2, F4)
        return {
            "S": table.S,
            "trial_ids": table.trial_ids,
            "counts": table.counts,
            "sums": table.sums,
            "m2": table.m2,
            "return_blocks_json": table.return_blocks_json,
        }

    def with_block(self, index: int, key: str, position: int, value) -> list:
        rows = self.rows()
        rows[index]["block_returns"][key][position] = value
        return rows

    def refused(self, code: str, call, ids=None) -> PboError:
        with self.assertRaises(PboError) as caught:
            call()
        raised = caught.exception.code
        self.assertEqual(raised, code)
        self.assertIn(raised, pbo_module.CODES)
        if ids is not None:
            self.assertEqual(caught.exception.ids, ids)
        return caught.exception

    def test_refused_checks_the_code_the_module_raised(self) -> None:
        """Controls the helper `refused` itself: it asserts on the code that was raised, not on
        its own argument (an assertion on the argument, such as a prefix test of the expected
        text, cannot fail).

        A call that raises a `pbo_` code that is not in `CODES` is not accepted, even when the
        caller names that very code, so the expected literal alone cannot make a test pass. A
        call that raises no `PboError` is not accepted either.

        Mutation: deleting the line `self.assertIn(raised, pbo_module.CODES)` of `refused`
        accepts the first call and fails this test.
        """

        def outside() -> None:
            raise PboError(
                "pbo_not_in_the_vocabulary", "a code that CODES does not list"
            )

        with self.assertRaises(AssertionError):
            self.refused("pbo_not_in_the_vocabulary", outside)
        with self.assertRaises(AssertionError):
            self.refused("pbo_input_invalid", lambda: None)

    def test_positive_control_accepts_the_valid_rows(self) -> None:
        """Control for every refusal below: the unmodified rows, as one part, load and run."""
        table = load_table(self.rows(), 2, F4)
        self.assertEqual(len(table.trial_ids), 3)
        self.assertEqual(run_pbo(self.rows(), 2, F4)["status"], "ok")
        self.assertEqual(
            set(pbo_module.CODES),
            {
                "pbo_input_invalid",
                "pbo_parameter_missing",
                "pbo_roster_mismatch",
                "pbo_return_blocks_mismatch",
                "pbo_block_sizes_uneven",
                "pbo_dataset_mismatch",
                "pbo_window_mismatch",
                "pbo_identity_mismatch",
                "pbo_rows_incomplete",
                "pbo_status_error",
                "pbo_status_pruned",
                "pbo_status_partial",
                "pbo_non_positive_equity",
                "pbo_odd_s",
                "pbo_too_few_trials",
                "pbo_zero_variance",
                "pbo_block_mismatch",
                "pbo_resource_exceeded",
                "pbo_sampler_unknown",
            },
        )

    def test_pbo_input_invalid(self) -> None:
        """Controls clause 3.5 (invalid row or argument) -> `pbo_input_invalid`.

        Values that are not finite, a negative or non-numeric m2, a count that is not an
        integer from 1, a one-interval block whose m2 is not 0, a mistyped trial id, rows that
        are not a list, S that is not an even integer from 2 (S = 0 or text), and a half that
        could hold fewer than 2 intervals (its sample deviation is undefined). (A trial id that
        occurs in more than one row is a roster difference, tested with the roster mismatch.)
        """

        def load(rows, size=2, **overrides):
            return lambda: load_table(rows, size, F4, **overrides)

        mixed = self.rows()
        mixed[0]["trial_id"] = "a"
        not_object = self.rows()
        not_object[1] = 7
        bool_id = self.rows()
        bool_id[0]["trial_id"] = True
        not_array = self.rows()
        not_array[0]["block_returns"]["count"] = 2
        short_half = self.rows()
        for row in short_half:
            row["block_returns"]["count"] = [1, 3]
            row["block_returns"]["m2"] = [0.0, 2.0]
        roster_text = self.rows()
        cases = [
            ("roster is text", load(roster_text, roster="012"), ()),
            ("roster holds a bool", load(roster_text, roster=[0, True, 2]), ()),
            ("roster mixes types", load(roster_text, roster=[0, 1, "c"]), ()),
            ("roster repeats an id", load(roster_text, roster=[0, 1, 1]), (1,)),
            ("nan sum", load(self.with_block(0, "sum", 0, float("nan"))), (0,)),
            ("infinite sum", load(self.with_block(0, "sum", 0, float("inf"))), (0,)),
            ("string sum", load(self.with_block(0, "sum", 1, "4.0")), (0,)),
            ("nan m2", load(self.with_block(1, "m2", 0, float("nan"))), (1,)),
            ("negative m2", load(self.with_block(1, "m2", 1, -1.0)), (1,)),
            ("bool count", load(self.with_block(2, "count", 0, True)), (2,)),
            ("float count", load(self.with_block(2, "count", 1, 2.0)), (2,)),
            ("zero count", load(self.with_block(2, "count", 1, 0)), (2,)),
            ("one interval with m2", load(self.with_block(0, "count", 0, 1)), (0,)),
            ("count not an array", load(not_array), (0,)),
            ("mixed id types", load(mixed), ()),
            ("row not an object", load(not_object), ()),
            ("bool trial id", load(bool_id), ()),
            ("rows not a list", load({"rows": []}), ()),
            (
                "half of one interval",
                load(short_half, return_blocks=block_object([1, 3])),
                (),
            ),
            ("S is zero", load(self.rows(), 0), ()),
            ("S is text", load(self.rows(), "2"), ()),
            ("S is a float", load(self.rows(), 2.0), ()),
            (
                "negative max_work",
                lambda: run_pbo(self.rows(), 2, F4, max_work=-1),
                (),
            ),
            (
                "bool max_work",
                lambda: run_pbo(self.rows(), 2, F4, max_work=True),
                (),
            ),
        ]
        for name, call, ids in cases:
            with self.subTest(case=name):
                self.refused("pbo_input_invalid", call, ids)

    def test_pbo_rows_incomplete(self) -> None:
        """Controls clause 3.5 (a row without a usable payload refuses; no subset PBO) ->
        `pbo_rows_incomplete`, with every offending id.

        A row with no status, with a status that is not text or not one of the trial statuses, with
        no `block_returns` payload, with a null payload, or with a payload whose status is not
        `ok` has nothing PBO can use. A failed, pruned or partial row and a payload that reports a
        non-positive equity have codes of their own (the next tests).

        Mutation: replacing `pbo_rows_incomplete` by `pbo_input_invalid` in the first
        `_RowProblem` raised for a missing payload (the `isinstance(block, dict)` branch of
        `_parse_row`) fails the "no payload" and "null payload" cases.
        """
        no_status = self.rows()
        del no_status[1]["status"]
        unknown_status = self.rows()
        unknown_status[0]["status"] = "bogus"
        text_status = self.rows()
        text_status[2]["status"] = 5
        no_payload = self.rows()
        del no_payload[1]["block_returns"]
        null_payload = self.rows()
        null_payload[1]["block_returns"] = None
        bad_payload_status = self.rows()
        bad_payload_status[2]["block_returns"]["status"] = "partial"
        two_rows = self.rows()
        del two_rows[0]["block_returns"]
        del two_rows[2]["status"]
        cases = [
            ("no status", no_status, (1,)),
            ("unknown status", unknown_status, (0,)),
            ("status is not text", text_status, (2,)),
            ("no payload", no_payload, (1,)),
            ("null payload", null_payload, (1,)),
            ("payload status", bad_payload_status, (2,)),
            ("two rows", two_rows, (0, 2)),
        ]
        for name, rows, ids in cases:
            with self.subTest(case=name):
                error = self.refused(
                    "pbo_rows_incomplete",
                    lambda rows=rows: run_pbo(rows, 2, F4),
                    ids,
                )
                self.assertIn(f"{len(ids)} of 3 rows refused", error.detail)

    def test_constraint_violation_row_is_not_refused(self) -> None:
        """Controls default D5: a row with status `constraint_violation` is part of the roster.

        The positive control of the status tests that follow: the same rows with one status
        changed to `constraint_violation` load, keep every trial and run to a result.

        Mutation: removing CONSTRAINT_VIOLATION from ROSTER_ROW_STATUSES refuses the row with
        `pbo_rows_incomplete` and this test raises.
        """
        rows = self.rows()
        rows[1]["status"] = "constraint_violation"
        table = load_table(rows, 2, F4)
        self.assertEqual(table.trial_ids, (0, 1, 2))
        self.assertEqual(run_pbo(rows, 2, F4)["N"], 3)

    def test_pbo_status_error(self) -> None:
        """Controls default D5 (an error row refuses; no subset PBO) -> `pbo_status_error`.

        Every failed-trial status of a row (`error` and the five error statuses of the trial
        vocabulary, `trial_timeout` included) has this one code and names the offending id.

        Mutation: emptying ERROR_ROW_STATUSES sends each of these statuses to
        `pbo_rows_incomplete` and every case fails.
        """
        for status in (
            "error",
            "objective_error",
            "constraint_error",
            "engine_error",
            "trial_error",
            "trial_timeout",
        ):
            with self.subTest(status=status):
                rows = self.rows()
                rows[1]["status"] = status
                error = self.refused(
                    "pbo_status_error",
                    lambda rows=rows: run_pbo(rows, 2, F4),
                    (1,),
                )
                self.assertIn("1 of 3 rows refused", error.detail)
                self.assertIn(repr(status), error.detail)

    def test_pbo_status_pruned(self) -> None:
        """Controls default D5 (a pruned row refuses; no subset PBO) -> `pbo_status_pruned`.

        Mutation: changing PRUNED_ROW_STATUS to another text sends the row to
        `pbo_rows_incomplete` and this test fails.
        """
        rows = self.rows()
        rows[0]["status"] = "pruned"
        error = self.refused("pbo_status_pruned", lambda: run_pbo(rows, 2, F4), (0,))
        self.assertIn("1 of 3 rows refused", error.detail)

    def test_pbo_status_partial(self) -> None:
        """Controls default D5 (a partial row refuses; no subset PBO) -> `pbo_status_partial`.

        Mutation: changing PARTIAL_ROW_STATUS to another text sends the row to
        `pbo_rows_incomplete` and this test fails.
        """
        rows = self.rows()
        rows[2]["status"] = "partial"
        error = self.refused("pbo_status_partial", lambda: run_pbo(rows, 2, F4), (2,))
        self.assertIn("1 of 3 rows refused", error.detail)

    def test_pbo_non_positive_equity(self) -> None:
        """Controls default D5 (a row whose equity was at or below zero refuses) ->
        `pbo_non_positive_equity`.

        An equity at or below zero skips an interval, and a block holding a skipped interval gives
        the row a payload status other than `ok` (dossier clause 3.2). This module reads the
        payload status text `non_positive_equity` as that case; any other status text that is not
        `ok` stays `pbo_rows_incomplete` (the "payload status" case above). The row status here
        is `ok`: the refusal comes from the payload alone.

        Mutation: changing NON_POSITIVE_EQUITY to another text sends the row to
        `pbo_rows_incomplete` and this test fails.
        """
        rows = self.rows()
        rows[2]["block_returns"]["status"] = "non_positive_equity"
        error = self.refused(
            "pbo_non_positive_equity", lambda: run_pbo(rows, 2, F4), (2,)
        )
        self.assertIn("1 of 3 rows refused", error.detail)

    def test_status_refusals_report_the_first_kind_whatever_the_row_order(self) -> None:
        """Controls clause 3.5 (the refusal does not depend on the row order) for the status codes.

        The kinds are reported in the order error, pruned, partial, non-positive equity (the order
        of default D5), each with only its own ids. Trial 0 is `partial`, trial 1 `pruned` and
        trial 2 `error`: the first kind present is `pbo_status_error` with trial 2; with trial 2
        healthy, `pbo_status_pruned` with trial 1; with trial 1 healthy too, `pbo_status_partial`
        with trial 0; with a non-positive-equity payload on trial 0 instead of the partial status,
        `pbo_non_positive_equity`.

        Mutation: iterating `reversed(_PRIORITY)` instead of `_PRIORITY` where `_load_parts` lists
        the refusals reports the partial row before the error row in the first step and fails
        this test.
        """
        rows = self.rows()
        rows[0]["status"] = "partial"
        rows[1]["status"] = "pruned"
        rows[2]["status"] = "error"
        steps = []
        steps.append(("pbo_status_error", (2,), copy.deepcopy(rows)))
        rows[2]["status"] = "ok"
        steps.append(("pbo_status_pruned", (1,), copy.deepcopy(rows)))
        rows[1]["status"] = "ok"
        steps.append(("pbo_status_partial", (0,), copy.deepcopy(rows)))
        rows[0]["status"] = "ok"
        rows[0]["block_returns"]["status"] = "non_positive_equity"
        steps.append(("pbo_non_positive_equity", (0,), copy.deepcopy(rows)))
        for code, ids, step in steps:
            for order in (step, list(reversed(step)), step[1:] + step[:1]):
                with self.subTest(code=code):
                    self.refused(code, lambda order=order: run_pbo(order, 2, F4), ids)

    def test_pbo_odd_s(self) -> None:
        """Controls clause 3.1 and 3.5 (S even) -> `pbo_odd_s`, before any part, row or roster
        is read: the calls below pass no roster and no declared object."""
        rows3 = [
            make_row(i, [2, 2, 2], [4.0, 6.0, 8.0], [2.0, 2.0, 2.0]) for i in range(3)
        ]
        self.refused(
            "pbo_odd_s",
            lambda: pbo_module.pbo([(None, rows3)], S=3, sampler="grid"),
            (),
        )
        self.refused(
            "pbo_odd_s",
            lambda: pbo_module.load_moment_table([(None, self.rows())], S=1),
        )
        self.refused(
            "pbo_odd_s",
            lambda: pbo_module.load_moment_table([(None, self.rows())], S=-3),
        )
        self.refused("pbo_odd_s", lambda: list(pbo_module.iter_ordered_splits(5)))
        self.refused("pbo_odd_s", lambda: pbo_module.estimate_work(7, 10))

    def test_pbo_too_few_trials(self) -> None:
        """Controls clause 3.5 (N < 2) -> `pbo_too_few_trials`.

        N is the size of the declared roster. A roster of one trial or of none refuses here; rows
        that are fewer than a roster of three are a different difference (`pbo_roster_mismatch`,
        tested below), so a partial roster is never reported as a small study.
        """
        rows = self.rows()
        self.refused(
            "pbo_too_few_trials", lambda: load_table(rows[:1], 2, F4, roster=[0])
        )
        self.refused("pbo_too_few_trials", lambda: load_table([], 2, F4, roster=[]))
        self.refused("pbo_too_few_trials", lambda: run_pbo(rows[:1], 2, F4, roster=[0]))
        self.assertEqual(len(load_table(rows[:2], 2, F4, roster=[0, 1]).trial_ids), 2)

    def test_pbo_zero_variance(self) -> None:
        """Controls clause 3.5 and default D1 (a half with zero variance refuses; no flat-trial
        imputation) -> `pbo_zero_variance`, naming the trial and the blocks.

        S = 2: trial 1's block 0 gets m2 = 0 (its two intervals are equal, both 1.0).
        S = 4, first table: trial 2 returns 3, 3, 3, 3 (every pooled M2 is 0 because d = 0).
        S = 4, second table: trial 2 returns 5, 5, 2, 9, flat only on the half {0, 1}, which
        is the first half evaluated.
        """
        flat_block = self.with_block(1, "m2", 0, 0.0)
        error = self.refused(
            "pbo_zero_variance", lambda: run_pbo(flat_block, 2, F4), (1,)
        )
        self.assertIn("[0]", error.detail)
        quad = fixture(S4)["rows"]
        constant = copy.deepcopy(quad)
        constant[2] = make_row(2, [1, 1, 1, 1], [3.0, 3.0, 3.0, 3.0], [0.0] * 4)
        self.refused("pbo_zero_variance", lambda: run_pbo(constant, 4, S4), (2,))
        partial = copy.deepcopy(quad)
        partial[2] = make_row(2, [1, 1, 1, 1], [5.0, 5.0, 2.0, 9.0], [0.0] * 4)
        error = self.refused("pbo_zero_variance", lambda: run_pbo(partial, 4, S4), (2,))
        self.assertIn("[0, 1]", error.detail)

    def test_pbo_block_mismatch(self) -> None:
        """Controls clause 3.5 (count differs from n_b; layout differs) -> `pbo_block_mismatch`.

        An array whose length is not S; a row whose count differs from the declared `n_b` (the
        object, not the first row, defines n_b, so a differing first row names only itself);
        rows that agree among themselves but not with the declared `n_b` (every row is named);
        rows whose arrays are for another S; an `n_b` or `boundary_time_ms` array of the wrong
        length.

        Mutation: deleting the comparison `if counts != declared["n_b"]:` in `_parse_row`
        (replacing it by `if False:`) makes the "differs from the declared n_b" cases return a
        result and fails them.
        """
        long_array = self.with_block(0, "sum", 0, 10.0)
        long_array[0]["block_returns"]["sum"] = [10.0, 4.0, 1.0]
        self.refused("pbo_block_mismatch", lambda: load_table(long_array, 2, F4), (0,))
        count_differs = self.with_block(2, "count", 1, 3)
        self.refused(
            "pbo_block_mismatch", lambda: load_table(count_differs, 2, F4), (2,)
        )
        first_differs = self.with_block(0, "count", 0, 3)
        error = self.refused(
            "pbo_block_mismatch", lambda: load_table(first_differs, 2, F4), (0,)
        )
        self.assertIn("count[0] is 3 where return_blocks.n_b[0] is 2", error.detail)
        self.assertIn("1 of 3 rows refused", error.detail)
        agree = self.refused(
            "pbo_block_mismatch",
            lambda: run_pbo(self.rows(), 2, F4, return_blocks=block_object([3, 3])),
            (0, 1, 2),
        )
        self.assertIn("count[0] is 2 where return_blocks.n_b[0] is 3", agree.detail)
        self.refused(
            "pbo_block_mismatch",
            lambda: run_pbo(
                self.rows(), 4, F4, return_blocks=block_object([1, 1, 1, 1])
            ),
            (0, 1, 2),
        )
        short_n_b = block_object([2, 2])
        short_n_b["n_b"] = [4]
        self.refused(
            "pbo_block_mismatch",
            lambda: run_pbo(self.rows(), 2, F4, return_blocks=short_n_b),
        )
        short_times = block_object([2, 2])
        short_times["boundary_time_ms"] = short_times["boundary_time_ms"][:2]
        self.refused(
            "pbo_block_mismatch",
            lambda: run_pbo(self.rows(), 2, F4, return_blocks=short_times),
        )

    def test_pbo_parameter_missing(self) -> None:
        """Controls the ruling that the roster and the parts, each with its declared object, are
        required inputs (clause 3.5 gives PBO the eligibility checks; nothing is defaulted) ->
        `pbo_parameter_missing`, naming what is missing.

        The roster is refused when it is not passed and when it is None; the parts when they are
        None; the declared object of a part when it is None (the name is `return_blocks`). `pbo`
        and `load_moment_table` both refuse. The same call with everything given is the positive
        control.

        Mutation: in `_declared_ids`, changing `if roster is None:` to `if roster is False:` sends a
        missing roster to `pbo_input_invalid` and fails the roster cases; the same change in
        `_check_parts` (`if parts is None:`) fails the parts case, and in `_check_return_blocks`
        (`if declared is None:`) the declared-object case.
        """
        rows = self.rows()
        context = declared(F4)
        self.assertEqual(run_pbo(rows, 2, F4)["status"], "ok")
        cases = [
            ("roster", "roster not passed", [(context["return_blocks"], rows)], {}),
            (
                "roster",
                "roster None",
                [(context["return_blocks"], rows)],
                {"roster": None},
            ),
            ("parts", "parts None", None, {"roster": context["roster"]}),
            (
                "return_blocks",
                "object None",
                [(None, rows)],
                {"roster": context["roster"]},
            ),
        ]
        for name, label, parts, extra in cases:
            with self.subTest(case=label):
                for call in (
                    lambda: pbo_module.pbo(
                        parts, S=2, sampler="grid", early_stop=None, **extra
                    ),
                    lambda: pbo_module.load_moment_table(parts, S=2, **extra),
                ):
                    error = self.refused("pbo_parameter_missing", call, ())
                    self.assertEqual(error.names, (name,))

    def test_pbo_roster_mismatch(self) -> None:
        """Controls clause 3.5 (the union of rows is exactly the declared trial set; a gap, an
        absent or an unknown part refuses) -> `pbo_roster_mismatch`, with every id that differs.

        The rows are trials 0, 1 and 2. A declared trial without a row, a row that is not
        declared, both at once, a roster that shares no id with the rows and a roster of the
        other id type all refuse and name the ids of both sides, ascending, integers before
        strings. One row of a declared roster of three is a roster mismatch, not a small study.
        The same holds across parts: the union of the rows of all the parts is what is compared
        with the roster, so a part that is absent, a part that is not declared and a gap between
        two parts refuse. The unmodified rows with the full roster are the positive control. (A
        repeated id among the rows has its own test below.)

        Mutation: in `_load_parts`, dropping `*missing` from the set that builds `roster_ids`
        makes the cases that only miss an id return a result and fails them, the part cases
        included.
        """
        rows = self.rows()
        self.assertEqual(run_pbo(rows, 2, F4)["N"], 3)
        cases = [
            (
                "a declared trial has no row",
                rows[:1] + rows[2:],
                [0, 1, 2],
                (1,),
                "no row",
            ),
            ("a row is not declared", rows, [0, 1], (2,), "not in the roster"),
            ("both", rows, [0, 1, 5], (2, 5), "no row"),
            ("no id shared", rows, [7, 8], (0, 1, 2, 7, 8), "no row"),
            (
                "ids of the other type",
                rows,
                ["a", "b", "c"],
                (0, 1, 2, "a", "b", "c"),
                "no row",
            ),
            ("one row of three", rows[:1], [0, 1, 2], (1, 2), "no row"),
        ]
        for name, given, roster, ids, phrase in cases:
            with self.subTest(case=name):
                error = self.refused(
                    "pbo_roster_mismatch",
                    lambda given=given, roster=roster: run_pbo(
                        given, 2, F4, roster=roster
                    ),
                    ids,
                )
                self.assertIn(phrase, error.detail)
        shared = declared(F4)["return_blocks"]
        part_cases = [
            ("no part at all", [], [0, 1, 2], (0, 1, 2), "no row"),
            ("an absent part", [(shared, rows[:2])], [0, 1, 2], (2,), "no row"),
            (
                "a part that is not declared",
                [(shared, rows[:2]), (shared, rows[2:])],
                [0, 1],
                (2,),
                "not in the roster",
            ),
            (
                "a gap between two parts",
                [(shared, rows[:1]), (shared, rows[2:])],
                [0, 1, 2],
                (1,),
                "no row",
            ),
        ]
        for name, parts, roster, ids, phrase in part_cases:
            with self.subTest(case=name):
                error = self.refused(
                    "pbo_roster_mismatch",
                    lambda parts=parts, roster=roster: run_parts(
                        parts, 2, F4, roster=roster
                    ),
                    ids,
                )
                self.assertIn(phrase, error.detail)

    def test_a_repeated_trial_id_is_a_roster_difference(self) -> None:
        """Controls clause 3.5 (the union of rows is exactly the declared trial set; a duplicate
        refuses) -> `pbo_roster_mismatch`, with a reason of its own: a trial id that occurs in more
        than one row.

        Trial 1 occurs twice in one part, and once in each of two parts. The call names that id
        and no other, and its detail says that the id occurs in more than one row; it counts 4
        rows supplied for 3 declared trials and 2 usable ones, because a trial with two rows is
        not usable. The refusal is the same in another row order. Trial 2 occurs twice and is not
        declared: it is one id, named once, and the detail gives both reasons.

        Mutation: replacing `seen > 1` by `seen > 2` in the computation of `repeated` in
        `_load_parts` lets the repeated ids through to the table, which refuses them as
        `pbo_input_invalid`, so the code assertion fails.
        """
        rows = self.rows()
        shared = declared(F4)["return_blocks"]
        twin = copy.deepcopy(rows[1])
        single = [rows[0], rows[1], twin, rows[2]]
        across = [(shared, rows[:2]), (shared, [twin, rows[2]])]
        cases = [
            ("twice in one part", lambda: run_pbo(single, 2, F4)),
            ("in two parts", lambda: run_parts(across, 2, F4)),
            ("rows in another order", lambda: run_pbo(list(reversed(single)), 2, F4)),
        ]
        for name, call in cases:
            with self.subTest(case=name):
                error = self.refused("pbo_roster_mismatch", call, (1,))
                self.assertIn("1 trial id(s) occur in more than one row", error.detail)
                self.assertEqual(
                    error.coverage, {"declared": 3, "supplied": 4, "usable": 2}
                )
                self.assertEqual(
                    error.refusals,
                    ({"code": "pbo_roster_mismatch", "count": 1, "ids": (1,)},),
                )
        undeclared = copy.deepcopy(rows[2])
        error = self.refused(
            "pbo_roster_mismatch",
            lambda: run_pbo(rows + [undeclared], 2, F4, roster=[0, 1]),
            (2,),
        )
        self.assertIn(
            "1 trial id(s) have a row but are not in the roster", error.detail
        )
        self.assertIn("1 trial id(s) occur in more than one row", error.detail)
        self.assertEqual(error.coverage, {"declared": 2, "supplied": 4, "usable": 2})
        self.assertEqual(
            error.refusals, ({"code": "pbo_roster_mismatch", "count": 1, "ids": (2,)},)
        )

    def test_roster_mismatch_outranks_row_statuses(self) -> None:
        """Controls clause 3.5 (the kinds are reported in a fixed order, whatever the row order).

        Trial 2 has status `error` and trial 3 is declared but has no row. The roster mismatch is
        reported first, with trial 3 only; once the roster is complete the error is reported,
        with trial 2 only.

        Mutation: iterating `reversed(_PRIORITY)` instead of `_PRIORITY` where `_load_parts` lists
        the refusals reports the error row before the roster mismatch in the first step and fails
        this test.
        """
        rows = self.rows()
        rows[2]["status"] = "error"
        for order in (rows, list(reversed(rows)), rows[1:] + rows[:1]):
            with self.subTest(order=[row["trial_id"] for row in order]):
                self.refused(
                    "pbo_roster_mismatch",
                    lambda order=order: run_pbo(order, 2, F4, roster=[0, 1, 2, 3]),
                    (3,),
                )
                self.refused(
                    "pbo_status_error", lambda order=order: run_pbo(order, 2, F4), (2,)
                )

    def test_return_blocks_object_is_checked(self) -> None:
        """Controls clauses 3.2/3.3/3.5 (the declared object of a part, which the module reads) ->
        `pbo_input_invalid` for a malformed object, `pbo_block_mismatch` for an array of the wrong
        length and `pbo_return_blocks_mismatch` for another contract, another series or another S.

        The object is {contract, series, S, K, n_b, boundary_time_ms, numeric_build_identity,
        dataset_identity} for S = 2 blocks of 2 intervals; each case changes one thing in it. An
        object that is not a mapping, lacks a key or has an unknown key, an S that is not an
        integer, a K below S, an n_b that is not an array or holds a zero, a float or a bool, a K
        that is not the sum of n_b, boundary times that are not integers or decrease, and a build
        identity or a dataset identity that is empty or not text are malformed. A half that could
        hold one interval (n_b = 1, 3, or 1, 1, or 1, 2) is malformed too: the last two differ by
        at most one, so the half-size rule alone refuses them. The unmodified object is the
        positive control.

        Mutation: replacing `if sum(n_b) != intervals:` by `if False:` in `_check_return_blocks`
        fails the "K is not the sum of n_b" case; replacing the contract comparison
        `declared["contract"] != RETURN_BLOCKS_CONTRACT` by `False` fails the "other contract"
        case; replacing `if type(dataset) is not str or not dataset:` by `if False:` fails the
        two cases of a dataset identity that is empty or not text; replacing
        `if sum(sorted(n_b)[: S // 2]) < 2:` by `if False:` fails the three half-of-one-interval
        cases.
        """
        rows = self.rows()
        self.assertEqual(
            run_pbo(rows, 2, F4, return_blocks=block_object([2, 2]))["N"], 3
        )

        def changed(**changes):
            result = block_object([2, 2])
            result.update(changes)
            return result

        def without(key):
            result = block_object([2, 2])
            del result[key]
            return result

        times = block_object([2, 2])["boundary_time_ms"]
        cases = [
            ("not a mapping", ["x"], "pbo_input_invalid", ()),
            ("a key is missing", without("K"), "pbo_input_invalid", ("K",)),
            ("an unknown key", changed(extra=1), "pbo_input_invalid", ("extra",)),
            ("S is text", changed(S="2"), "pbo_input_invalid", ()),
            ("K below S", changed(K=1), "pbo_input_invalid", ()),
            ("n_b is not an array", changed(n_b=4), "pbo_input_invalid", ()),
            ("n_b holds a zero", changed(n_b=[0, 4]), "pbo_input_invalid", ()),
            ("n_b holds a float", changed(n_b=[2.0, 2]), "pbo_input_invalid", ()),
            ("n_b holds a bool", changed(n_b=[True, 3]), "pbo_input_invalid", ()),
            ("K is not the sum of n_b", changed(K=5), "pbo_input_invalid", ()),
            (
                "times are not an array",
                changed(boundary_time_ms=0),
                "pbo_input_invalid",
                (),
            ),
            (
                "a time is a float",
                changed(boundary_time_ms=[times[0], 1.5, times[2]]),
                "pbo_input_invalid",
                (),
            ),
            (
                "times decrease",
                changed(boundary_time_ms=[times[2], times[1], times[0]]),
                "pbo_input_invalid",
                (),
            ),
            (
                "identity is empty",
                changed(numeric_build_identity=""),
                "pbo_input_invalid",
                (),
            ),
            (
                "identity is not text",
                changed(numeric_build_identity=7),
                "pbo_input_invalid",
                (),
            ),
            (
                "dataset identity is missing",
                without("dataset_identity"),
                "pbo_input_invalid",
                ("dataset_identity",),
            ),
            (
                "dataset identity is empty",
                changed(dataset_identity=""),
                "pbo_input_invalid",
                (),
            ),
            (
                "dataset identity is not text",
                changed(dataset_identity=7),
                "pbo_input_invalid",
                (),
            ),
            ("a half of one interval", block_object([1, 3]), "pbo_input_invalid", ()),
            (
                "a half of one interval, equal blocks",
                block_object([1, 1]),
                "pbo_input_invalid",
                (),
            ),
            (
                "a half of one interval, blocks that differ by one",
                block_object([1, 2]),
                "pbo_input_invalid",
                (),
            ),
            ("n_b has a wrong length", changed(n_b=[4]), "pbo_block_mismatch", ()),
            (
                "times have a wrong length",
                changed(boundary_time_ms=times[:2]),
                "pbo_block_mismatch",
                (),
            ),
            (
                "another contract",
                changed(contract="pineforge-hpo-return-blocks/v2"),
                "pbo_return_blocks_mismatch",
                (),
            ),
            (
                "another series",
                changed(series="monthly"),
                "pbo_return_blocks_mismatch",
                (),
            ),
            ("another S", block_object([1, 1, 1, 1]), "pbo_return_blocks_mismatch", ()),
        ]
        for name, given, code, names in cases:
            with self.subTest(case=name):
                error = self.refused(
                    code,
                    lambda given=given: run_pbo(rows, 2, F4, return_blocks=given),
                    (),
                )
                self.assertEqual(error.names, names)

    def test_pbo_window_mismatch(self) -> None:
        """Controls clause 3.5 (the window differs between parts) -> `pbo_window_mismatch`.

        The window is the S + 1 boundary times of the declared object. Two parts declare objects
        that agree in K, n_b and identity and differ in the window (the second object's
        boundaries are one interval later). The call refuses, naming the key `boundary_time_ms`
        and no trial, and it refuses the same way whichever part comes first and when the part
        with the other window holds no row. Rows that match their own part's object do not help:
        the parts are what disagree. The same two parts with equal objects are the positive
        control, and their result is byte for byte the result of the rows as one part.

        Mutation: deleting the `("boundary_time_ms", "pbo_window_mismatch")` entry of
        `_CONTEXT_FIELDS` lets the parts be accepted and fails every refusal case.
        """
        rows = self.rows()
        shared = block_object([2, 2])
        moved = block_object([2, 2])
        moved["boundary_time_ms"] = [
            time + INTERVAL_MS for time in moved["boundary_time_ms"]
        ]
        reference = canonical_bytes(run_pbo(self.rows(), 2, F4, with_lambdas=True))
        equal = [(shared, rows[:2]), (block_object([2, 2]), rows[2:])]
        self.assertEqual(
            canonical_bytes(run_parts(equal, 2, F4, with_lambdas=True)), reference
        )
        cases = [
            ("the moved part is second", [(shared, rows[:2]), (moved, rows[2:])]),
            ("the moved part is first", [(moved, rows[2:]), (shared, rows[:2])]),
            ("the moved part holds no row", [(shared, rows), (moved, [])]),
        ]
        for name, parts in cases:
            with self.subTest(case=name):
                error = self.refused(
                    "pbo_window_mismatch",
                    lambda parts=parts: run_parts(parts, 2, F4),
                    (),
                )
                self.assertEqual(error.names, ("boundary_time_ms",))
                self.assertIsNone(error.coverage)
                self.assertEqual(
                    error.refusals,
                    ({"code": "pbo_window_mismatch", "count": 1, "ids": ()},),
                )

    def test_pbo_dataset_mismatch(self) -> None:
        """Controls clause 3.5 (the dataset differs between parts) -> `pbo_dataset_mismatch`.

        The object names the dataset by its interval count K and by the identity that the caller
        states (tested next): the first part is over K = 4 intervals and the second over K = 6,
        and each row matches the object of its own part. Everything else of the objects differs
        too (the block layout and the window follow from K), and the refusal names every key that
        differs, in the fixed order, with one entry per code. K is compared first, so the code is
        the dataset's and not the layout's.

        Mutation: changing the code of the `"K"` entry of `_CONTEXT_FIELDS` to
        `"pbo_return_blocks_mismatch"` (or moving the `"n_b"` entry in front of the `"K"` entry,
        which compares n_b first) reports the layout and not the dataset and fails this test;
        stopping `_part_differences` after its first difference fails the `names` assertion.
        """
        rows = self.rows()
        long_row = make_row(2, [3, 3], [6.0, 9.0], [2.0, 2.0])
        parts = [(block_object([2, 2]), rows[:2]), (block_object([3, 3]), [long_row])]
        error = self.refused(
            "pbo_dataset_mismatch", lambda: run_parts(parts, 2, F4), ()
        )
        self.assertEqual(error.names, ("K", "n_b", "boundary_time_ms"))
        self.assertIn("part 0 declares 4, part 1 declares 6", error.detail)
        self.assertIsNone(error.coverage)
        self.assertEqual(
            error.refusals,
            (
                {"code": "pbo_dataset_mismatch", "count": 1, "ids": ()},
                {"code": "pbo_return_blocks_mismatch", "count": 1, "ids": ()},
                {"code": "pbo_window_mismatch", "count": 1, "ids": ()},
            ),
        )
        swapped = list(reversed(parts))
        error = self.refused(
            "pbo_dataset_mismatch", lambda: run_parts(swapped, 2, F4), ()
        )
        self.assertEqual(error.names, ("K", "n_b", "boundary_time_ms"))
        self.assertIn("part 0 declares 6, part 1 declares 4", error.detail)

    def test_pbo_dataset_identity_mismatch(self) -> None:
        """Controls clause 3.5 (the dataset differs between parts although the length and the time
        axis are the same) -> `pbo_dataset_mismatch`.

        K and the boundary times cannot tell two datasets of the same length and the same time
        axis apart, so the declared object carries a `dataset_identity` that the caller states (a
        key of this API, not of the wire). Two parts declare objects that agree in every other key
        and differ in that text: the call refuses with the dataset's code, naming the key
        `dataset_identity` and no trial, whichever part comes first and when the part of the other
        dataset holds no row. The same two parts with equal identities are the positive control,
        and their result is byte for byte the result of the rows as one part.

        Mutation: deleting the `(DATASET_IDENTITY_KEY, "pbo_dataset_mismatch")` entry of
        `_CONTEXT_FIELDS` lets the parts be accepted and fails every refusal case.
        """
        rows = self.rows()
        shared = block_object([2, 2])
        other = block_object([2, 2])
        other["dataset_identity"] = OTHER_DATASET
        reference = canonical_bytes(run_pbo(self.rows(), 2, F4, with_lambdas=True))
        equal = [(shared, rows[:2]), (block_object([2, 2]), rows[2:])]
        self.assertEqual(
            canonical_bytes(run_parts(equal, 2, F4, with_lambdas=True)), reference
        )
        cases = [
            ("the other dataset is second", [(shared, rows[:2]), (other, rows[2:])]),
            ("the other dataset is first", [(other, rows[2:]), (shared, rows[:2])]),
            ("the other dataset holds no row", [(shared, rows), (other, [])]),
        ]
        for name, parts in cases:
            with self.subTest(case=name):
                error = self.refused(
                    "pbo_dataset_mismatch",
                    lambda parts=parts: run_parts(parts, 2, F4),
                    (),
                )
                self.assertEqual(error.names, ("dataset_identity",))
                self.assertIn("return_blocks.dataset_identity differs", error.detail)
                self.assertIsNone(error.coverage)
                self.assertEqual(
                    error.refusals,
                    ({"code": "pbo_dataset_mismatch", "count": 1, "ids": ()},),
                )

    def test_pbo_identity_mismatch(self) -> None:
        """Controls clause 3.5 (the identity differs between parts) -> `pbo_identity_mismatch`.

        The identity is `numeric_build_identity`: the build that reduced the rows. Two parts
        declare objects that agree in K, n_b and the window and differ in the build: the call
        refuses, naming the key `numeric_build_identity` and no trial, whichever part comes first
        and when the part of the other build holds no row. The same two parts with equal objects
        are the positive control, and their result is byte for byte the result of the rows as one
        part.

        Mutation: deleting the `("numeric_build_identity", "pbo_identity_mismatch")` entry of
        `_CONTEXT_FIELDS` lets the parts be accepted and fails every refusal case.
        """
        rows = self.rows()
        shared = block_object([2, 2])
        other = block_object([2, 2])
        other["numeric_build_identity"] = IDENTITY + "-other"
        reference = canonical_bytes(run_pbo(self.rows(), 2, F4, with_lambdas=True))
        equal = [(shared, rows[:2]), (block_object([2, 2]), rows[2:])]
        self.assertEqual(
            canonical_bytes(run_parts(equal, 2, F4, with_lambdas=True)), reference
        )
        cases = [
            ("the other build is second", [(shared, rows[:2]), (other, rows[2:])]),
            ("the other build is first", [(other, rows[2:]), (shared, rows[:2])]),
            ("the other build holds no row", [(shared, rows), (other, [])]),
        ]
        for name, parts in cases:
            with self.subTest(case=name):
                error = self.refused(
                    "pbo_identity_mismatch",
                    lambda parts=parts: run_parts(parts, 2, F4),
                    (),
                )
                self.assertEqual(error.names, ("numeric_build_identity",))
                self.assertIsNone(error.coverage)
                self.assertEqual(
                    error.refusals,
                    ({"code": "pbo_identity_mismatch", "count": 1, "ids": ()},),
                )

    def test_pbo_return_blocks_mismatch(self) -> None:
        """Controls clause 3.5 (`return_blocks` differs between parts) ->
        `pbo_return_blocks_mismatch`.

        The first part has blocks of 2 and 3 intervals (the uneven fixture, K = 5) and the second
        blocks of 3 and 2 intervals with the same K, window and identity: only the block layout
        differs, and each row matches the object of its own part. The call refuses, naming the key
        `n_b`. Accepting it would pool block 0 of one part with block 0 of the other, which are
        not the same stretch of the series. (Another contract, series or S is checked in
        `test_return_blocks_object_is_checked`.)

        Mutation: deleting the `("n_b", "pbo_return_blocks_mismatch")` entry of
        `_CONTEXT_FIELDS` lets the parts be accepted, and a number comes back for rows of two
        layouts, so the test fails.
        """
        swapped = block_object([3, 2])
        swapped["boundary_time_ms"] = block_object([2, 3])["boundary_time_ms"]
        rows = self.uneven_rows()
        layout_row = make_row(2, [3, 2], [9.0, 6.0], [2.0, 2.0])
        parts = [(block_object([2, 3]), rows[:2]), (swapped, [layout_row])]
        error = self.refused(
            "pbo_return_blocks_mismatch", lambda: run_parts(parts, 2, UNEVEN), ()
        )
        self.assertEqual(error.names, ("n_b",))
        self.assertIsNone(error.coverage)
        self.assertEqual(
            error.refusals,
            ({"code": "pbo_return_blocks_mismatch", "count": 1, "ids": ()},),
        )
        agreeing = [(block_object([2, 3]), rows[:2]), (block_object([2, 3]), rows[2:])]
        self.assertEqual(run_parts(agreeing, 2, UNEVEN)["status"], "ok")

    def test_every_key_of_the_declared_object_is_compared_between_parts(self) -> None:
        """Controls clause 3.5 (no difference between parts may pass): each key of the declared
        object is either fixed by the check that every object passes (the contract, the series
        and S) or compared between parts, so a key that is added to `DECLARED_OBJECT_KEYS`
        without a comparison is caught here.

        One variant of the base object (blocks of 2 and 3 intervals) for each key that two
        objects that both pass the check can differ in: K (with the layout and the window that
        follow from it), n_b (same K and window), the window, the build identity and the dataset
        identity. Each pair refuses with the code of its key and names that key. The keys that
        the variants name, with the three fixed ones, are exactly `DECLARED_OBJECT_KEYS`.

        Mutation: deleting any one entry of `_CONTEXT_FIELDS` fails the variant of its key;
        adding a key to `DECLARED_OBJECT_KEYS` (and a value for it to `block_object`) without a
        variant here fails the last assertion.
        """
        rows = self.uneven_rows()
        base = block_object([2, 3])
        layout = block_object([3, 2])
        layout["boundary_time_ms"] = base["boundary_time_ms"]
        window = block_object([2, 3])
        window["boundary_time_ms"] = [
            time + INTERVAL_MS for time in base["boundary_time_ms"]
        ]
        build = block_object([2, 3])
        build["numeric_build_identity"] = IDENTITY + "-other"
        dataset = block_object([2, 3])
        dataset["dataset_identity"] = OTHER_DATASET
        variants = [
            ("K", block_object([3, 3]), "pbo_dataset_mismatch"),
            ("n_b", layout, "pbo_return_blocks_mismatch"),
            ("boundary_time_ms", window, "pbo_window_mismatch"),
            ("numeric_build_identity", build, "pbo_identity_mismatch"),
            ("dataset_identity", dataset, "pbo_dataset_mismatch"),
        ]
        for key, variant, code in variants:
            with self.subTest(key=key):
                parts = [(base, rows[:2]), (variant, rows[2:])]
                error = self.refused(
                    code, lambda parts=parts: run_parts(parts, 2, UNEVEN), ()
                )
                self.assertIn(key, error.names)
        compared = {key for key, _, _ in variants}
        self.assertEqual(
            compared | {"contract", "series", "S"}, set(pbo_module.DECLARED_OBJECT_KEYS)
        )

    def test_pbo_block_sizes_uneven(self) -> None:
        """Controls default D2 (block sizes may differ by at most one interval; a larger spread
        refuses) -> `pbo_block_sizes_uneven`.

        Blocks of 2 and 4 intervals differ by two. For S = 4, blocks of 2, 2, 3 and 4 intervals
        differ by two between the smallest and the largest although neighbours differ by at most
        one. Blocks of 2 and 3 intervals (the uneven fixture) are the positive control: a spread
        of one is computed.

        Mutation: changing `> 1` to `> 2` in the spread test of `_check_return_blocks` accepts
        both uneven layouts and fails this test.
        """
        self.assertEqual(run_pbo(self.uneven_rows(), 2, UNEVEN)["status"], "ok")
        two_four = [make_row(i, [2, 4], [10.0, 8.0], [2.0, 5.0]) for i in range(3)]
        error = self.refused(
            "pbo_block_sizes_uneven",
            lambda: run_pbo(two_four, 2, F4, return_blocks=block_object([2, 4])),
            (),
        )
        self.assertIn("from 2 to 4 intervals", error.detail)
        four_blocks = [
            make_row(i, [2, 2, 3, 4], [4.0, 6.0, 9.0, 8.0], [2.0, 2.0, 2.0, 5.0])
            for i in range(3)
        ]
        self.refused(
            "pbo_block_sizes_uneven",
            lambda: run_pbo(
                four_blocks, 4, F4, return_blocks=block_object([2, 2, 3, 4])
            ),
            (),
        )

    def test_a_row_must_match_the_object_of_its_own_part(self) -> None:
        """Controls clause 3.5 (count differs from n_b) for parts -> `pbo_block_mismatch`.

        Two parts declare the same object (blocks of 2 and 2 intervals). Trial 2 carries the
        counts [3, 3]: it does not match the object of its own part. The call names that trial and
        no other, with the position of its part, whether its part comes first or second.

        Mutation: replacing the comparison `if counts != declared["n_b"]:` in `_parse_row` by
        `if False:` accepts the row and fails this test.
        """
        rows = self.rows()
        rows[2]["block_returns"]["count"] = [3, 3]
        shared = block_object([2, 2])
        cases = [
            (
                "the row is in the second part",
                [(shared, rows[:2]), (shared, rows[2:])],
                "(part 1)",
            ),
            (
                "the row is in the first part",
                [(shared, rows[2:]), (shared, rows[:2])],
                "(part 0)",
            ),
        ]
        for name, parts, where in cases:
            with self.subTest(case=name):
                error = self.refused(
                    "pbo_block_mismatch",
                    lambda parts=parts: run_parts(parts, 2, F4),
                    (2,),
                )
                self.assertIn(
                    "count[0] is 3 where return_blocks.n_b[0] is 2", error.detail
                )
                self.assertIn(where, error.detail)
                self.assertIn("1 of 3 rows refused", error.detail)

    def test_the_parts_are_checked(self) -> None:
        """Controls clause 3.5 (invalid argument) for the parts -> `pbo_input_invalid`.

        `parts` must be a list of pairs `(declared object, rows)`. A dict, text, a table and a
        bare list of rows are not; neither is a part that is a dict, a triple, a single item or
        None; the rows of a part must be a list; ids that mix integers and strings across two
        parts are mixed ids; a malformed declared object is refused with the position of its part
        and the key. A table is not accepted: it has lost its parts. The valid call is the
        positive control.

        Mutation: deleting `or len(part) != 2` from the pair test in `_check_parts` accepts the
        triple and fails its case.
        """
        rows = self.rows()
        shared = block_object([2, 2])
        self.assertEqual(run_parts([(shared, rows)], 2, F4)["status"], "ok")
        table = load_table(self.rows(), 2, F4)
        text_id = self.rows()[2:]
        text_id[0]["trial_id"] = "c"
        cases = [
            ("parts is a dict", {"rows": rows}),
            ("parts is text", "rows"),
            ("parts is a table", table),
            ("parts is a bare list of rows", rows),
            ("a part is a dict", [{"return_blocks": shared, "rows": rows}]),
            ("a part is a triple", [(shared, rows, "extra")]),
            ("a part is a single item", [(shared,)]),
            ("a part is None", [None]),
            ("the rows of a part are not a list", [(shared, {"rows": rows})]),
            ("the ids mix types across parts", [(shared, rows[:2]), (shared, text_id)]),
        ]
        for name, parts in cases:
            with self.subTest(case=name):
                self.refused(
                    "pbo_input_invalid", lambda parts=parts: run_parts(parts, 2, F4), ()
                )
        malformed = block_object([2, 2])
        del malformed["K"]
        error = self.refused(
            "pbo_input_invalid",
            lambda: run_parts([(shared, rows[:2]), (malformed, rows[2:])], 2, F4),
            (),
        )
        self.assertEqual(error.names, ("K",))
        self.assertIn("part 1:", error.detail)

    def test_moment_table_is_validated_when_built(self) -> None:
        """Controls clause 3.5 for a table built by hand: `MomentTable` refuses what the loader
        would refuse, with a `PboError` and not with a bare error.

        The valid fields are the positive control and give the digest of the loader's table. An
        empty table or a table of one trial is `pbo_too_few_trials` (it used to reach a bare
        `max()` of nothing); ids that are not unique and ascending, mixed or mistyped; arrays of
        the wrong length; counts that are not integers or differ from the declared `n_b`; a
        non-finite sum or a negative `m2` (with the trial id); an odd or mistyped S; a declared
        object that is not text, not JSON, not an object, for another S or with a half of one
        interval all refuse.

        Mutation: changing `if len(ids) < 2:` to `if len(ids) < 0:` in `_table_ids` lets the empty
        table through and fails the "no trials" and "one trial" cases.
        """
        fields = self.table_fields()
        valid = pbo_module.MomentTable(**fields)
        self.assertEqual(
            valid.input_sha256, load_table(self.rows(), 2, F4).input_sha256
        )
        sums = fields["sums"]
        m2s = fields["m2"]
        cases = [
            ("no trials", dict(trial_ids=(), sums=(), m2=()), "pbo_too_few_trials", ()),
            (
                "one trial",
                dict(trial_ids=(0,), sums=sums[:1], m2=m2s[:1]),
                "pbo_too_few_trials",
                (),
            ),
            ("ids not ascending", dict(trial_ids=(1, 0, 2)), "pbo_input_invalid", ()),
            ("a repeated id", dict(trial_ids=(0, 0, 1)), "pbo_input_invalid", ()),
            ("mixed id types", dict(trial_ids=(0, 1, "c")), "pbo_input_invalid", ()),
            ("a bool id", dict(trial_ids=(True, 1, 2)), "pbo_input_invalid", ()),
            ("ids not an array", dict(trial_ids=7), "pbo_input_invalid", ()),
            (
                "fewer sum rows than trials",
                dict(sums=sums[:2]),
                "pbo_block_mismatch",
                (),
            ),
            (
                "a short m2 row",
                dict(m2=(m2s[0][:1],) + m2s[1:]),
                "pbo_block_mismatch",
                (),
            ),
            ("counts of the wrong length", dict(counts=(2,)), "pbo_block_mismatch", ()),
            ("counts differ from n_b", dict(counts=(3, 2)), "pbo_block_mismatch", ()),
            ("a float count", dict(counts=(2.0, 2)), "pbo_input_invalid", ()),
            (
                "a NaN sum",
                dict(sums=((float("nan"), 4.0),) + sums[1:]),
                "pbo_input_invalid",
                (0,),
            ),
            (
                "a negative m2",
                dict(m2=m2s[:1] + ((2.0, -1.0),) + m2s[2:]),
                "pbo_input_invalid",
                (1,),
            ),
            ("S is odd", dict(S=3), "pbo_odd_s", ()),
            ("S is text", dict(S="2"), "pbo_input_invalid", ()),
            (
                "object is not text",
                dict(return_blocks_json=None),
                "pbo_input_invalid",
                (),
            ),
            (
                "object is not JSON",
                dict(return_blocks_json="{"),
                "pbo_input_invalid",
                (),
            ),
            (
                "object is not an object",
                dict(return_blocks_json="[1]"),
                "pbo_input_invalid",
                (),
            ),
            (
                "object is for another S",
                dict(return_blocks_json=json.dumps(block_object([1, 1, 1, 1]))),
                "pbo_return_blocks_mismatch",
                (),
            ),
            (
                "a half of one interval",
                dict(
                    counts=(1, 3), return_blocks_json=json.dumps(block_object([1, 3]))
                ),
                "pbo_input_invalid",
                (),
            ),
        ]
        for name, changes, code, ids in cases:
            with self.subTest(case=name):
                self.refused(
                    code,
                    lambda changes=changes: pbo_module.MomentTable(
                        **{**fields, **changes}
                    ),
                    ids,
                )

    def test_moment_table_digest_is_computed_from_its_content(self) -> None:
        """Controls clause 3.6 (SHA-256 of the canonical input) for a table built by hand.

        The digest is not a constructor argument, so a table cannot carry a digest that
        disagrees with its moments: it is computed from the content, so the same moments give the
        loader's digest and other moments give another. The result of `pbo` carries that digest,
        computed from the rows it loaded itself: `pbo` takes no table, so there is no digest in
        its input to forge.

        Mutation: giving `input_sha256` an `init` argument again (deleting `init=False` from its
        `field`) makes the constructor demand a digest and fails this test.
        """
        fields = self.table_fields()
        table = pbo_module.MomentTable(**fields)
        with self.assertRaises(TypeError):
            pbo_module.MomentTable(**fields, input_sha256="0" * 64)
        expected = load_table(self.rows(), 2, F4).input_sha256
        self.assertEqual(table.input_sha256, expected)
        changed = dict(fields, sums=((10.5, 4.0),) + fields["sums"][1:])
        self.assertNotEqual(pbo_module.MomentTable(**changed).input_sha256, expected)
        self.assertEqual(run_pbo(self.rows(), 2, F4)["input_sha256"], expected)

    def test_pbo_resource_exceeded(self) -> None:
        """Controls clause 3.7 (admission against a caller-supplied max_work only).

        S = 4, N = 4 has n_splits 6 and work 24 + 48 = 72. max_work 72 runs, 71 and 0 refuse.
        With no max_work nothing is capped. The figure is checked before any split is
        evaluated: rows that hold a flat trial refuse `pbo_resource_exceeded` at
        max_work 0 and only `pbo_zero_variance` once the budget admits it.
        """
        rows = fixture(S4)["rows"]
        self.assertEqual(run_pbo(rows, 4, S4, max_work=72)["work_estimate"], 72)
        self.assertEqual(run_pbo(rows, 4, S4)["n_splits"], 6)
        error = self.refused(
            "pbo_resource_exceeded", lambda: run_pbo(rows, 4, S4, max_work=71), ()
        )
        self.assertIn("72", error.detail)
        self.refused("pbo_resource_exceeded", lambda: run_pbo(rows, 4, S4, max_work=0))
        flat = copy.deepcopy(rows)
        flat[2] = make_row(2, [1, 1, 1, 1], [3.0, 3.0, 3.0, 3.0], [0.0] * 4)
        self.refused("pbo_resource_exceeded", lambda: run_pbo(flat, 4, S4, max_work=0))
        self.refused("pbo_zero_variance", lambda: run_pbo(flat, 4, S4, max_work=72))

    def test_refusals_after_the_rows_were_examined_carry_the_coverage(self) -> None:
        """Controls clause 3.5/3.6 (a refusal names the coverage) for the refusals that are raised
        after every row was accepted: a work figure above `max_work`, a trial with zero variance
        over a half, and a pooled value that is not finite.

        The page says that `coverage` is given when the rows were examined. These three are
        raised from the admission figure or the split loop, after the rows matched the roster, so
        each carries {declared, supplied, usable}, all equal to N (4 for the S = 4 fixture), and
        so does the result of a call that is not refused. A refusal raised before the rows are
        examined has none (tested with the other coverage cases). The third case gives trial 0 two
        block sums of 1e308, whose sum over the first half overflows to infinity.

        Mutation: deleting `coverage=coverage` from the `pbo_resource_exceeded` raise in `pbo`
        fails the first assertion; deleting it from the `pbo_zero_variance` raise in
        `_half_statistics` fails the second; deleting it from the re-raise in the `except` branch
        of `_half_statistics` fails the third.
        """
        rows = fixture(S4)["rows"]
        full = {"declared": 4, "supplied": 4, "usable": 4}
        error = self.refused(
            "pbo_resource_exceeded", lambda: run_pbo(rows, 4, S4, max_work=0), ()
        )
        self.assertEqual(error.coverage, full)
        flat = copy.deepcopy(rows)
        flat[2] = make_row(2, [1, 1, 1, 1], [3.0, 3.0, 3.0, 3.0], [0.0] * 4)
        error = self.refused("pbo_zero_variance", lambda: run_pbo(flat, 4, S4), (2,))
        self.assertEqual(error.coverage, full)
        huge = copy.deepcopy(rows)
        huge[0]["block_returns"]["sum"] = [1e308, 1e308, 7.0, 3.0]
        error = self.refused("pbo_input_invalid", lambda: run_pbo(huge, 4, S4), (0,))
        self.assertIn("not finite", error.detail)
        self.assertEqual(error.coverage, full)
        self.assertEqual(error.document()["coverage"], full)
        self.assertEqual(run_pbo(rows, 4, S4)["coverage"], full)

    def test_pbo_sampler_unknown(self) -> None:
        """Controls default D6 (an unknown or missing sampler refuses) -> `pbo_sampler_unknown`.

        A missing sampler is an unknown one: the roster label depends on it, so there is no
        silent default. `early_stop` is not a sampler name: the early stop is its own input.

        Mutation: putting "early_stop" back into DIAGNOSTIC_SAMPLERS accepts that name and fails
        its subcase; making `roster_label` return a label for `None` fails the `None` subcase.
        """
        rows = self.rows()
        for sampler in ("bogus", "GRID", "", None, 5, ["grid"], "early_stop"):
            with self.subTest(sampler=sampler):
                self.refused(
                    "pbo_sampler_unknown",
                    lambda sampler=sampler: run_pbo(rows, 2, F4, sampler=sampler),
                    (),
                )
        self.assertEqual(run_pbo(rows, 2, F4)["status"], "ok")

    def test_pbo_early_stop_is_required_and_checked(self) -> None:
        """Controls default D6 (the early stop is its own input; no silent default) ->
        `pbo_parameter_missing` when it is not passed, `pbo_input_invalid` when it is neither None,
        a bool nor an object.

        None is a value (the result has no early stop), so only leaving the argument out is
        missing. Zero, one, a number, text and a list are not an early-stop object. The valid
        calls are the positive controls.

        Mutation: changing `if early_stop is _UNSET:` to `if False:` in `_check_early_stop` sends
        the missing argument to `pbo_input_invalid` and fails the first assertion.
        """
        rows = self.rows()
        context = declared(F4)
        parts = [(context["return_blocks"], rows)]
        arguments = {"S": 2, "sampler": "grid", "roster": context["roster"]}
        error = self.refused(
            "pbo_parameter_missing", lambda: pbo_module.pbo(parts, **arguments), ()
        )
        self.assertEqual(error.names, ("early_stop",))
        for value in (0, 1, 2.5, "yes", []):
            with self.subTest(early_stop=value):
                self.refused(
                    "pbo_input_invalid",
                    lambda value=value: run_pbo(rows, 2, F4, early_stop=value),
                    (),
                )
        self.assertEqual(run_pbo(rows, 2, F4, early_stop=None)["status"], "ok")
        stop = {"patience_trials": 1}
        self.assertEqual(run_pbo(rows, 2, F4, early_stop=stop)["status"], "ok")

    def test_roster_label_requires_the_early_stop(self) -> None:
        """Controls default D6 (the early stop is its own input; no silent default) for the public
        `roster_label`, as for `pbo` -> `pbo_parameter_missing`, naming `early_stop`, when it is
        not passed; `pbo_input_invalid` when it is neither None, a bool nor an object.

        None is a value (the study had no early stop). A study with an early stop that was labelled
        without saying so would come back `paper_conformant`: that call is the one refused. The
        valid calls are the positive controls.

        Mutation: changing the default `early_stop: Any = _UNSET` of `roster_label` to
        `early_stop: Any = None` lets `roster_label("grid")` return `paper_conformant` and fails
        the first assertion.
        """
        error = self.refused(
            "pbo_parameter_missing", lambda: pbo_module.roster_label("grid"), ()
        )
        self.assertEqual(error.names, ("early_stop",))
        self.refused(
            "pbo_parameter_missing", lambda: pbo_module.roster_label("tpe"), ()
        )
        self.refused(
            "pbo_input_invalid",
            lambda: pbo_module.roster_label("grid", early_stop="yes"),
            (),
        )
        self.assertEqual(
            pbo_module.roster_label("grid", early_stop=None), "paper_conformant"
        )
        self.assertEqual(
            pbo_module.roster_label("grid", early_stop={"patience_trials": 1}),
            "diagnostic_adaptive_roster",
        )

    def test_refusal_names_coverage_and_every_reason(self) -> None:
        """Controls clause 3.5/3.6 (a refusal names the coverage and, per reason, the count and
        the offending ids).

        Rows: trial 0 has status `error`, trial 1 `pruned`, trial 2 is healthy, and trial 4 is
        healthy but not declared; the roster is 0, 1, 2 and 3. The raised code is the first kind
        in the fixed order, `pbo_roster_mismatch`, with the ids 3 (declared, no row) and 4 (a row,
        not declared). `refusals` lists the three kinds found in that order: the roster (count 2),
        the error (trial 0) and the pruned row (trial 1). Coverage: 4 declared, 4 rows supplied
        and 1 usable declared trial (trial 2; trial 4 is healthy but not declared). The row
        order does not change any of it. `document()` is JSON-ready and holds no number of the
        analysis.

        Mutation: replacing `item in declared_set` by `True` in the `usable` count of
        `_load_parts` also counts trial 4 and fails the coverage assertion; replacing
        `"count": len(failures[code])` by `"count": 1` fails the roster count.
        """
        base = self.rows()
        undeclared = copy.deepcopy(base[2])
        undeclared["trial_id"] = 4
        rows = [base[0], base[1], base[2], undeclared]
        rows[0]["status"] = "error"
        rows[1]["status"] = "pruned"
        error = None
        for order in (rows, list(reversed(rows)), rows[2:] + rows[:2]):
            with self.subTest(order=[row["trial_id"] for row in order]):
                error = self.refused(
                    "pbo_roster_mismatch",
                    lambda order=order: run_pbo(order, 2, F4, roster=[0, 1, 2, 3]),
                    (3, 4),
                )
                self.assertEqual(
                    error.coverage, {"declared": 4, "supplied": 4, "usable": 1}
                )
                self.assertEqual(
                    error.refusals,
                    (
                        {"code": "pbo_roster_mismatch", "count": 2, "ids": (3, 4)},
                        {"code": "pbo_status_error", "count": 1, "ids": (0,)},
                        {"code": "pbo_status_pruned", "count": 1, "ids": (1,)},
                    ),
                )
        document = error.document()
        self.assertEqual(document["status"], "refused")
        self.assertEqual(document["code"], "pbo_roster_mismatch")
        self.assertEqual(document["ids"], [3, 4])
        self.assertEqual(
            document["coverage"], {"declared": 4, "supplied": 4, "usable": 1}
        )
        self.assertEqual(
            document["refusals"][1],
            {"code": "pbo_status_error", "count": 1, "ids": [0]},
        )
        for absent in ("pbo", "n_lt0", "n_gt0", "rate_le0", "lambdas"):
            self.assertNotIn(absent, document)
        json.dumps(document, allow_nan=False)

    def test_refusal_coverage_for_rows_parts_and_other_refusals(self) -> None:
        """Controls clause 3.5/3.6: the coverage of the other refusals.

        One error row among three declared trials: 3 declared, 3 supplied, 2 usable, and one
        reason. Three rows called with the roster [0, 1, 2, 3] cover 3 of 4 declared trials (3
        supplied, 3 usable). A difference between parts is found before any row is read, so it
        has no coverage, and it names one reason without ids. A refusal that is not found among
        rows (here an unknown sampler) has no coverage either and the one reason of its own code,
        counted as 1 because it has no ids.

        Mutation: replacing `"supplied": len(entries)` by `"supplied": len(declared_ids)` in the
        coverage of `_load_parts` reports 4 supplied rows for the second case and fails its
        assertion.
        """
        rows = self.rows()
        rows[0]["status"] = "error"
        error = self.refused("pbo_status_error", lambda: run_pbo(rows, 2, F4), (0,))
        self.assertEqual(error.coverage, {"declared": 3, "supplied": 3, "usable": 2})
        self.assertEqual(
            error.refusals, ({"code": "pbo_status_error", "count": 1, "ids": (0,)},)
        )
        error = self.refused(
            "pbo_roster_mismatch",
            lambda: run_pbo(self.rows(), 2, F4, roster=[0, 1, 2, 3]),
            (3,),
        )
        self.assertEqual(error.coverage, {"declared": 4, "supplied": 3, "usable": 3})
        self.assertEqual(
            error.refusals, ({"code": "pbo_roster_mismatch", "count": 1, "ids": (3,)},)
        )
        moved = block_object([2, 2])
        moved["boundary_time_ms"] = [
            time + INTERVAL_MS for time in moved["boundary_time_ms"]
        ]
        parts = [(block_object([2, 2]), self.rows()[:2]), (moved, self.rows()[2:])]
        error = self.refused("pbo_window_mismatch", lambda: run_parts(parts, 2, F4), ())
        self.assertIsNone(error.coverage)
        self.assertEqual(
            error.refusals, ({"code": "pbo_window_mismatch", "count": 1, "ids": ()},)
        )
        error = self.refused(
            "pbo_sampler_unknown",
            lambda: run_pbo(self.rows(), 2, F4, sampler="bogus"),
            (),
        )
        self.assertIsNone(error.coverage)
        self.assertEqual(
            error.refusals, ({"code": "pbo_sampler_unknown", "count": 1, "ids": ()},)
        )

    def test_refusal_does_not_depend_on_row_order(self) -> None:
        """Controls clause 3.5 (refusal names coverage per reason and the offending ids).

        Trial 0 has a NaN sum (invalid input) and trial 2 an error status (`pbo_status_error`).
        The first kind in the fixed priority order, invalid input, is reported with only its own
        ids, whatever the row order.
        """
        rows = self.with_block(0, "sum", 0, float("nan"))
        rows[2]["status"] = "error"
        for order in (rows, list(reversed(rows)), rows[1:] + rows[:1]):
            error = self.refused(
                "pbo_input_invalid", lambda order=order: load_table(order, 2, F4), (0,)
            )
            self.assertIn("1 of 3 rows refused", error.detail)


if __name__ == "__main__":
    unittest.main()
