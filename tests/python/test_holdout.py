"""Tests of the post hoc holdout comparison (``pineforge_hpo.holdout``).

Each test's docstring derives its expected values from the rows and explains the
behaviour it checks. A SHA-256 cannot be derived by hand: ``input_sha256`` is rebuilt
independently from the digest recipe in docs/holdout.md and compared.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
import random
import unittest

from pineforge_hpo import holdout
from pineforge_hpo.holdout import (
    ALGORITHM,
    CODES,
    CONTRACT,
    DIRECTIONS,
    INTERPRETATION,
    HoldoutError,
    holdout_compare,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "holdout"

S1 = {
    "parameters": {
        "arm": {"kind": "categorical", "choices": ["A", "B", "C", "D", "E", "F"]},
    },
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
KEYS = {arm: '{"arm":["string","' + arm + '"]}' for arm in "ABCDEF"}

# The interpretation from docs/holdout.md, typed here to catch wording drift.
FIXED_ITEMS = (
    "(a) The module splits no data and runs no trial: it reads two sets of rows that "
    "two finished studies already wrote, and joins them by the exact parameter vector. "
    "It cannot know what data either study used, so a value recorded by the second "
    "study is only as held-out as the caller made it.",
    "(b) A study is a holdout only when the caller ran it on data set aside before the "
    "selection rows were read; the module cannot check that, and reading the best "
    "selection rows while the second study's values are in view contaminates it.",
    "(c) Equality of parameter vectors is exact: no tolerance and no lattice snapping, "
    "so a near-equal vector is a different vector and is reported unmatched, never "
    "merged.",
    "(d) No significance test, no threshold and no verdict: a few vectors give a noisy "
    "rank correlation, and the module states no probability, interval or error bar.",
    "(e) The selection side is the best of many trials, so its values are optimistic; "
    "every number is in-sample for the selection source and only as held-out as the "
    "caller made it for the other.",
    "(f) v1 analyses finished studies; native in-loop walk-forward comes later.",
    "(g) The scoring-context check is necessary, not sufficient, and it runs on each "
    "source on its own, never between the two studies: a space hash names the "
    "parameter space and the objective, not the data, so an equal hash says nothing "
    "about the data, and this module requires the two studies to share that space, "
    "so their hashes are normally equal and can differ only through a space-hash "
    "version change.",
    "(h) When a source records no space, its rows are keyed with the space the other "
    "source records or the caller passes, so the module cannot tell whether the second "
    "study ran in that space.",
    "(i) A complete result may carry the ancestor rows of a study it continued from: "
    "the module counts them for each source, reports the counts, and refuses a "
    "holdout source whose ancestor rows repeat a trial id of the selection source, "
    "because such a source may be reporting the selection study's own rows; an "
    "overlap that the trial ids do not show cannot be detected, and an id that "
    "repeats by a coincidence of numbering is refused like a real one.",
    "(j) No claim is made about the data of either source, about the run that produced "
    "it, or about any strategy.",
)
FIXED_TEXT = " ".join(FIXED_ITEMS)

FOLLOWED_KEYS = {
    "selection_trial_id",
    "vector_key",
    "selection_value",
    "selection_rank",
    "holdout_trial_id",
    "holdout_value",
    "holdout_rank",
    "difference",
    "improved",
}
UNMATCHED_KEYS = {
    "selection_trial_id",
    "vector_key",
    "selection_value",
    "selection_rank",
    "reason",
    "holdout_ids",
}
UNMATCHED_ABSENT = (
    "holdout_trial_id",
    "holdout_value",
    "holdout_rank",
    "difference",
)


def make_row(trial_id, status="ok", objective=0.0, arm="A", metrics=None, **extra):
    """A valid row; None for ``arm`` or ``metrics`` leaves that field out."""
    row = {
        "trial_id": trial_id,
        "status": status,
        "feasible": status == "ok",
        "objective": objective,
    }
    if arm is not None:
        row["parameters"] = {"arm": arm}
    if metrics is not None:
        row["metrics"] = metrics
    row.update(extra)
    return row


def result_of(ancestors, trials):
    return {"warm_start_trials": list(ancestors), "trials": list(trials)}


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def non_blank_lines(text):
    return [line for line in text.split("\n") if line.strip()]


def main_selection():
    """Selection rows with tied objectives and an engine_error row last."""
    return [
        make_row(0, "ok", 10.0, "C"),
        make_row(1, "ok", 12.0, "B"),
        make_row(2, "ok", 9.0, "A"),
        make_row(3, "ok", 11.0, "E"),
        make_row(4, "ok", 12.0, "D"),
        make_row(5, "engine_error", None, "A"),
    ]


def main_holdout():
    """Holdout rows with a tie, a holdout-only arm and an ineligible arm D."""
    return [
        make_row(11, "ok", 6.0, "F"),
        make_row(8, "ok", 4.0, "B"),
        make_row(7, "ok", 5.0, "A"),
        make_row(10, "constraint_violation", 99.0, "D"),
        make_row(9, "ok", 4.0, "C"),
    ]


def main_call(**options):
    options.setdefault("metric", "objective")
    options.setdefault("direction", "maximize")
    options.setdefault("k", 5)
    options.setdefault("space", S1)
    return holdout_compare(main_selection(), main_holdout(), **options)


def digest_value(raw, metric):
    """The value one row contributes to the digest."""
    if metric == "objective":
        value = raw["objective"]
        return None if value is None else float(value)
    metrics = raw.get("metrics")
    if not isinstance(metrics, dict) or metric not in metrics:
        return None
    cell = metrics[metric]
    if cell is None:
        return None
    if type(cell) in (int, float):
        return float(cell)
    return cell


def raw_digest(rows_by_source, metric):
    """The ``input_sha256`` rebuilt from the raw rows of both sources.

    Per row: SHA-256 of the compact, key-sorted, ASCII JSON of
    ``[source_index, trial_id, status, value, vector_key]``; then SHA-256 of the sorted
    per-row digests, joined.
    """
    parts = []
    for index, rows in enumerate(rows_by_source):
        for raw in rows:
            key = KEYS[raw["parameters"]["arm"]]
            entry = [
                index,
                raw["trial_id"],
                raw["status"],
                digest_value(raw, metric),
                key,
            ]
            text = json.dumps(
                entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            parts.append(hashlib.sha256(text.encode("ascii")).digest())
    return hashlib.sha256(b"".join(sorted(parts))).hexdigest()


class HoldoutTestCase(unittest.TestCase):
    def refusal(self, selection, holdout_source, **options):
        """Call holdout_compare and return the HoldoutError it must raise."""
        with self.assertRaises(HoldoutError) as caught:
            holdout_compare(selection, holdout_source, **options)
        return caught.exception

    def assertRefused(self, error, code, ids=(), names=()):
        """The code, offenders and refused document of one refusal."""
        self.assertEqual(error.code, code)
        self.assertEqual(error.ids, tuple(ids))
        self.assertEqual(error.names, tuple(names))
        document = error.document()
        self.assertEqual(set(document), {"contract", "status", "refusal"})
        self.assertEqual(document["contract"], CONTRACT)
        self.assertEqual(document["status"], "refused")
        self.assertEqual(document["refusal"]["code"], code)
        self.assertEqual(document["refusal"]["ids"], list(ids))
        self.assertEqual(document["refusal"]["names"], list(names))
        self.assertNotIn("followed", document)

    def assertSource(self, source, **counts):
        """The counts of one source's object."""
        for field, expected in counts.items():
            self.assertEqual(source[field], expected, field)


class ConstantsTests(HoldoutTestCase):
    def test_contract_words(self):
        """Catch changes to the public format id, algorithm name or direction words."""
        self.assertEqual(CONTRACT, "pineforge-hpo-holdout/v1")
        self.assertEqual(ALGORITHM, "exact_join_midrank_v1")
        self.assertEqual(DIRECTIONS, ("maximize", "minimize"))

    def test_the_thirteen_refusal_codes(self):
        """Catch missing codes, reordered codes or shared-catalog refusal codes."""
        self.assertEqual(
            CODES,
            (
                "holdout_input_invalid",
                "holdout_rows_incomplete",
                "holdout_space_missing",
                "holdout_space_mismatch",
                "holdout_context_mismatch",
                "holdout_ancestor_rows",
                "holdout_parameter_missing",
                "holdout_parameter_invalid",
                "holdout_metric_unknown",
                "holdout_value_invalid",
                "holdout_no_eligible_rows",
                "holdout_duplicate_vector",
                "holdout_resource_exceeded",
            ),
        )
        self.assertEqual(len(set(CODES)), 13)
        self.assertTrue(all(code.startswith("holdout_") for code in CODES))

    def test_interpretation_is_the_fixed_public_text(self):
        """Catch wording drift or reordered clauses in the public interpretation."""
        self.assertEqual(INTERPRETATION, FIXED_TEXT)
        self.assertEqual(len(FIXED_ITEMS), 10)
        positions = []
        for letter, item in zip("abcdefghij", FIXED_ITEMS):
            self.assertTrue(item.startswith(f"({letter}) "), letter)
            self.assertIn(item, INTERPRETATION)
            positions.append(INTERPRETATION.index(item))
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(positions[0], 0)

    def test_module_imports_only_the_standard_library_and_the_reader(self):
        """Catch imports beyond the standard library, reader and StudySpec."""
        tree = ast.parse(Path(holdout.__file__).read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add("." * node.level + (node.module or ""))
        self.assertEqual(
            found,
            {
                "__future__",
                "hashlib",
                "json",
                "math",
                "os",
                "fractions",
                "typing",
                "._rows",
                ".study_spec",
            },
        )


class HoldoutErrorTests(HoldoutTestCase):
    def test_fields_are_kept_and_ids_are_ascending(self):
        """Catch lost error fields or id pairs that are not sorted ascending."""
        error = HoldoutError(
            "holdout_value_invalid", "one sentence", [(1, 3), (0, 9)], ["b", "a"]
        )
        self.assertEqual(error.code, "holdout_value_invalid")
        self.assertEqual(error.detail, "one sentence")
        self.assertEqual(error.ids, ((0, 9), (1, 3)))
        self.assertEqual(error.names, ("b", "a"))
        self.assertEqual(str(error), "holdout_value_invalid: one sentence")
        empty = HoldoutError("holdout_no_eligible_rows", "nothing left")
        self.assertEqual((empty.ids, empty.names), ((), ()))

    def test_unknown_code_is_a_programming_error(self):
        """Catch acceptance of codes outside the thirteen public refusal codes."""
        with self.assertRaises(ValueError):
            HoldoutError("holdout_made_up", "a sentence")
        with self.assertRaises(ValueError):
            HoldoutError("pareto_input_invalid", "a sentence")


class LadderTests(HoldoutTestCase):
    def test_eligibility_ladder_and_the_per_source_counts(self):
        """Catch wrong source counts, exclusion reasons or ineligible-vector poisoning.

        Hand derivation. Selection: six rows, ids 0-4 are ``ok`` and eligible (no two
        eligible rows share a vector), row 5 is excluded as ``status_engine_error``, so
        rows 6, rows_eligible 5, vectors_eligible 5, rows_excluded 1. Row 5 shares the
        vector of arm A and refuses nothing (only eligible rows count); arm A's
        representative is trial id 2. Holdout: five rows, id 10 is excluded as
        ``status_constraint_violation``, so rows 5, rows_eligible 4, vectors_eligible 4,
        rows_excluded 1. Neither source is a result, so both ancestor counts are 0.
        """
        document = main_call()
        self.assertSource(
            document["selection"],
            index=0,
            rows=6,
            ancestor_rows=0,
            rows_eligible=5,
            vectors_eligible=5,
            rows_excluded=1,
            excluded={"status_engine_error": {"count": 1, "ids": [5]}},
            space_hash=None,
        )
        self.assertSource(
            document["holdout"],
            index=1,
            rows=5,
            ancestor_rows=0,
            rows_eligible=4,
            vectors_eligible=4,
            rows_excluded=1,
            excluded={"status_constraint_violation": {"count": 1, "ids": [10]}},
            space_hash=None,
        )
        self.assertEqual(document["n_sel"], 5)
        self.assertEqual(document["n_hol"], 4)

    def test_selection_ranking_the_tie_rule_and_k_clipping(self):
        """12.0 ties for arms B (1) and D (4) break by trial id, so B is
        rank 1 and D rank 2; then E 3, C 4, A 5. At k=5 the followed order is ids
        1, 4, 3, 0, 2; at k=7 it is clipped to the same five with no refusal; at k=3 the
        pool is unchanged (built from every eligible selection vector, of any rank)
        while only arm B is matched, so the better-half denominator is the matched
        count 1: the pool is A (5.0), B (4.0) and C (4.0), M = 3, B and C share the
        mid-rank (2 + 3)/2 = 5/2, the cut is (3 + 1)/2 = 2, and 5/2 > 2 leaves B out, so
        the numerator is 0 and the value 0.0.
        Catch wrong tie ordering, refusing large k or building the pool from top k.
        """
        document = main_call()
        self.assertEqual(document["k_followed"], 5)
        self.assertEqual(
            [
                (entry["selection_trial_id"], entry["selection_rank"])
                for entry in document["followed"]
            ],
            [(1, 1), (0, 4), (2, 5)],
        )
        self.assertEqual(
            [entry["selection_trial_id"] for entry in document["unmatched"]], [4, 3]
        )
        clipped = main_call(k=7)
        self.assertEqual(clipped["k_followed"], 5)
        self.assertEqual(clipped["k"], 7)
        self.assertEqual(
            [entry["selection_trial_id"] for entry in clipped["followed"]], [1, 0, 2]
        )
        three = main_call(k=3)
        self.assertEqual(three["k_followed"], 3)
        self.assertEqual(
            [entry["selection_trial_id"] for entry in three["followed"]], [1]
        )
        self.assertEqual(
            [entry["selection_trial_id"] for entry in three["unmatched"]], [4, 3]
        )
        self.assertEqual(three["pool"]["size"], 3)
        self.assertEqual(three["n_matched"], 1)
        self.assertEqual(three["followed"][0]["holdout_rank"], [5, 2])
        self.assertEqual(
            three["statistics"]["better_half"],
            {"numerator": 0, "denominator": 1, "value": 0.0},
        )
        self.assertEqual(
            three["statistics"]["spearman_terms"],
            {"xy": [0, 1], "xx": [0, 1], "yy": [0, 1]},
        )
        self.assertEqual(
            three["statistics"]["spearman_ranks"], {"x": [[1, 1]], "y": [[1, 1]]}
        )
        self.assertIsNone(three["statistics"]["spearman"])
        self.assertEqual(
            three["statistics"]["spearman_undefined"], "fewer_than_three_matched"
        )

    def test_the_join_matched_and_unmatched_never_zero(self):
        """Arm F (id 11, 6.0) matches no eligible selection vector and is
        reported only in ``pool.holdout_only``; arm D's vector is held by ineligible
        row 10, so D is unmatched with ``holdout_row_not_eligible`` and its ids;
        no holdout row holds arm E, so E is ``absent_in_holdout`` with no ids. An
        unmatched entry carries no holdout key at all, never a zero.
        Catch dropped unmatched vectors, swapped reasons or fabricated holdout values.
        """
        document = main_call()
        unmatched_ranks = [
            (entry["selection_rank"], entry["selection_trial_id"])
            for entry in document["unmatched"]
        ]
        followed_ranks = [
            (entry["selection_rank"], entry["selection_trial_id"])
            for entry in document["followed"]
        ]
        self.assertEqual(unmatched_ranks, [(2, 4), (3, 3)])
        self.assertEqual(followed_ranks, [(1, 1), (4, 0), (5, 2)])
        self.assertEqual(
            document["unmatched"][0],
            {
                "selection_trial_id": 4,
                "vector_key": KEYS["D"],
                "selection_value": 12.0,
                "selection_rank": 2,
                "reason": "holdout_row_not_eligible",
                "holdout_ids": [10],
            },
        )
        self.assertEqual(document["unmatched"][1]["reason"], "absent_in_holdout")
        self.assertEqual(document["unmatched"][1]["holdout_ids"], [])
        self.assertEqual(document["unmatched"][1]["vector_key"], KEYS["E"])
        for entry in document["unmatched"]:
            self.assertEqual(set(entry), UNMATCHED_KEYS)
            for key in UNMATCHED_ABSENT:
                self.assertNotIn(key, entry)
        for entry in document["followed"]:
            self.assertEqual(set(entry), FOLLOWED_KEYS)
        self.assertEqual(document["n_matched"], 3)
        self.assertEqual(len(document["followed"]) + len(document["unmatched"]), 5)
        self.assertEqual(document["pool"]["holdout_only"], {"count": 1, "ids": [11]})

    def test_the_pool_and_the_mid_rank_tie_rule(self):
        """The pool is A (7, 5.0), B (8, 4.0) and C (9, 4.0), M = 3; A is
        at position 1 and B and C occupy positions 2 and 3, so both get the mid-rank
        (2 + 3)/2 = 5/2, reported as the pair [5, 2]; the cut is (3 + 1)/2 = 2, reported
        as [2, 1], so only A (mid-rank 1) is in the better half: 1 of the 3 matched
        vectors, the binary64 of 1/3.
        Catch distinct ranks for tied values or using the first position as a mid-rank.
        """
        document = main_call()
        self.assertEqual(
            document["pool"],
            {
                "size": 3,
                "median_rank_cut": [2, 1],
                "holdout_only": {"count": 1, "ids": [11]},
            },
        )
        ranks = {
            entry["vector_key"]: entry["holdout_rank"] for entry in document["followed"]
        }
        self.assertEqual(ranks[KEYS["A"]], [1, 1])
        self.assertEqual(ranks[KEYS["B"]], [5, 2])
        self.assertEqual(ranks[KEYS["C"]], [5, 2])
        self.assertEqual(
            document["statistics"]["better_half"],
            {"numerator": 1, "denominator": 3, "value": 0.3333333333333333},
        )

    def test_the_exact_spearman_correlation(self):
        """At k = 5 the matched vectors are B (rank 1, 12.0, 4.0),
        C (rank 4, 10.0, 4.0) and A (rank 5, 9.0, 5.0).

        Hand derivation: x = (1, 2, 3); the holdout values 4.0, 4.0, 5.0 give the tie at
        positions 2 and 3 the mid-rank 5/2, so y = (5/2, 5/2, 1).
        SUM(x) = 6, SUM(y) = 6, SUM(x*y) = 21/2, SUM(x^2) = 14,
        SUM(y^2) = 27/2, n = 3, so Sxy = 63/2 - 36 =
        -9/2, Sxx = 42 - 36 = 6 and Syy = 81/2 - 36 = 9/2; rho^2 = (81/4)/27 = 3/4 and
        the signed binary64 of sqrt(3/4) is -0.8660254037844386.
        Catch correlation from all selection ranks, tie-breaking or a dropped sign.
        """
        document = main_call()
        statistics = document["statistics"]
        self.assertEqual(
            statistics["spearman_terms"],
            {"xy": [-9, 2], "xx": [6, 1], "yy": [9, 2]},
        )
        self.assertEqual(
            statistics["spearman_ranks"],
            {"x": [[1, 1], [2, 1], [3, 1]], "y": [[5, 2], [5, 2], [1, 1]]},
        )
        self.assertIsNone(statistics["spearman_undefined"])
        self.assertEqual(statistics["spearman"], -0.8660254037844386)
        self.assertEqual(
            [entry["difference"] for entry in document["followed"]],
            [-8.0, -6.0, -4.0],
        )
        self.assertEqual(
            [entry["improved"] for entry in document["followed"]], [False, False, False]
        )
        self.assertEqual(
            document["admission"],
            {
                "rows_read": 11,
                "selection_pairs": 10,
                "holdout_pairs": 6,
                "work": 27,
                "max_work": None,
            },
        )

    def test_whole_document(self):
        """Catch missing or changed result fields at k = 5 using the derived values."""
        document = main_call()
        self.assertEqual(
            set(document),
            {
                "contract",
                "status",
                "metric",
                "direction",
                "k",
                "k_followed",
                "n_sel",
                "n_hol",
                "selection",
                "holdout",
                "followed",
                "unmatched",
                "n_matched",
                "pool",
                "statistics",
                "algorithm",
                "input_sha256",
                "admission",
                "interpretation",
            },
        )
        document = dict(document)
        digest = document.pop("input_sha256")
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, digest.lower())
        self.assertEqual(
            document,
            {
                "contract": CONTRACT,
                "status": "ok",
                "metric": "objective",
                "direction": "maximize",
                "k": 5,
                "k_followed": 5,
                "n_sel": 5,
                "n_hol": 4,
                "selection": {
                    "index": 0,
                    "rows": 6,
                    "ancestor_rows": 0,
                    "rows_eligible": 5,
                    "vectors_eligible": 5,
                    "rows_excluded": 1,
                    "excluded": {"status_engine_error": {"count": 1, "ids": [5]}},
                    "space_hash": None,
                },
                "holdout": {
                    "index": 1,
                    "rows": 5,
                    "ancestor_rows": 0,
                    "rows_eligible": 4,
                    "vectors_eligible": 4,
                    "rows_excluded": 1,
                    "excluded": {
                        "status_constraint_violation": {"count": 1, "ids": [10]},
                    },
                    "space_hash": None,
                },
                "followed": [
                    {
                        "selection_trial_id": 1,
                        "vector_key": KEYS["B"],
                        "selection_value": 12.0,
                        "selection_rank": 1,
                        "holdout_trial_id": 8,
                        "holdout_value": 4.0,
                        "holdout_rank": [5, 2],
                        "difference": -8.0,
                        "improved": False,
                    },
                    {
                        "selection_trial_id": 0,
                        "vector_key": KEYS["C"],
                        "selection_value": 10.0,
                        "selection_rank": 4,
                        "holdout_trial_id": 9,
                        "holdout_value": 4.0,
                        "holdout_rank": [5, 2],
                        "difference": -6.0,
                        "improved": False,
                    },
                    {
                        "selection_trial_id": 2,
                        "vector_key": KEYS["A"],
                        "selection_value": 9.0,
                        "selection_rank": 5,
                        "holdout_trial_id": 7,
                        "holdout_value": 5.0,
                        "holdout_rank": [1, 1],
                        "difference": -4.0,
                        "improved": False,
                    },
                ],
                "unmatched": [
                    {
                        "selection_trial_id": 4,
                        "vector_key": KEYS["D"],
                        "selection_value": 12.0,
                        "selection_rank": 2,
                        "reason": "holdout_row_not_eligible",
                        "holdout_ids": [10],
                    },
                    {
                        "selection_trial_id": 3,
                        "vector_key": KEYS["E"],
                        "selection_value": 11.0,
                        "selection_rank": 3,
                        "reason": "absent_in_holdout",
                        "holdout_ids": [],
                    },
                ],
                "n_matched": 3,
                "pool": {
                    "size": 3,
                    "median_rank_cut": [2, 1],
                    "holdout_only": {"count": 1, "ids": [11]},
                },
                "statistics": {
                    "spearman": -0.8660254037844386,
                    "spearman_terms": {"xy": [-9, 2], "xx": [6, 1], "yy": [9, 2]},
                    "spearman_ranks": {
                        "x": [[1, 1], [2, 1], [3, 1]],
                        "y": [[5, 2], [5, 2], [1, 1]],
                    },
                    "spearman_undefined": None,
                    "better_half": {
                        "numerator": 1,
                        "denominator": 3,
                        "value": 0.3333333333333333,
                    },
                },
                "algorithm": ALGORITHM,
                "admission": {
                    "rows_read": 11,
                    "selection_pairs": 10,
                    "holdout_pairs": 6,
                    "work": 27,
                    "max_work": None,
                },
                "interpretation": INTERPRETATION,
            },
        )

    def test_exclusion_reasons_for_a_named_metric(self):
        """With metric m, k = 1, row 0 records m and is eligible; row 1 records
        no key (metric_not_recorded), row 2 records a null cell (metric_unavailable) and
        row 3 has no metrics object (metric_not_recorded), so the selection source
        has rows 4, eligible 1, excluded 3 with counts 2 and 1. Pool = {A}, M = 1,
        cut (1 + 1)/2 = 1, A's mid-rank 1 is inside, and one matched vector gives
        better_half 1 of 1.
        Catch merging null and absent metrics or failing to exclude missing metrics.
        """
        selection = [
            make_row(0, "ok", 1.0, "A", metrics={"m": 1.0}),
            make_row(1, "ok", 1.0, "B", metrics={}),
            make_row(2, "ok", 1.0, "C", metrics={"m": None}),
            make_row(3, "ok", 1.0, "D"),
        ]
        holdout_rows = [make_row(7, "ok", 1.0, "A", metrics={"m": 2.0})]
        document = holdout_compare(
            selection,
            holdout_rows,
            metric="m",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertSource(
            document["selection"],
            index=0,
            rows=4,
            rows_eligible=1,
            vectors_eligible=1,
            rows_excluded=3,
            excluded={
                "metric_not_recorded": {"count": 2, "ids": [1, 3]},
                "metric_unavailable": {"count": 1, "ids": [2]},
            },
        )
        self.assertSource(
            document["holdout"],
            index=1,
            rows=1,
            rows_eligible=1,
            vectors_eligible=1,
            rows_excluded=0,
            excluded={},
        )
        self.assertEqual(document["k_followed"], 1)
        self.assertEqual(document["n_matched"], 1)
        self.assertEqual(document["followed"][0]["selection_trial_id"], 0)
        self.assertEqual(document["followed"][0]["holdout_trial_id"], 7)
        self.assertEqual(document["followed"][0]["difference"], 1.0)
        self.assertEqual(document["followed"][0]["improved"], True)
        self.assertEqual(document["followed"][0]["holdout_rank"], [1, 1])
        self.assertEqual(
            document["pool"],
            {
                "size": 1,
                "median_rank_cut": [1, 1],
                "holdout_only": {"count": 0, "ids": []},
            },
        )
        self.assertEqual(
            document["statistics"]["better_half"],
            {"numerator": 1, "denominator": 1, "value": 1.0},
        )
        self.assertIsNone(document["statistics"]["spearman"])
        self.assertEqual(
            document["statistics"]["spearman_undefined"], "fewer_than_three_matched"
        )
        self.assertEqual(
            document["admission"],
            {
                "rows_read": 5,
                "selection_pairs": 0,
                "holdout_pairs": 0,
                "work": 5,
                "max_work": None,
            },
        )

    def test_nothing_matched_and_the_document_that_must_still_be_complete(self):
        """An eligible selection row and an empty holdout still give a complete result.

        Hand derivation: k_followed 1, the single followed vector is unmatched
        (``absent_in_holdout``), n_matched 0, pool empty with the cut (0 + 1)/2 = 1
        reported as [1, 2], and the statistics are null: no matched vectors, so the
        terms and both rank vectors are null; the better half is 0 over 0, value null.
        The admission figure is 1 row read and nothing else.
        Catch refusing an empty holdout or substituting zero for undefined statistics.
        """
        document = holdout_compare(
            [make_row(0, "ok", 3.0, "A")],
            [],
            metric="objective",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertEqual(document["n_sel"], 1)
        self.assertEqual(document["k_followed"], 1)
        self.assertEqual(document["n_hol"], 0)
        self.assertEqual(document["n_matched"], 0)
        self.assertEqual(document["followed"], [])
        self.assertSource(
            document["holdout"],
            index=1,
            rows=0,
            ancestor_rows=0,
            rows_eligible=0,
            vectors_eligible=0,
            rows_excluded=0,
            excluded={},
            space_hash=None,
        )
        self.assertEqual(
            document["unmatched"],
            [
                {
                    "selection_trial_id": 0,
                    "vector_key": KEYS["A"],
                    "selection_value": 3.0,
                    "selection_rank": 1,
                    "reason": "absent_in_holdout",
                    "holdout_ids": [],
                },
            ],
        )
        self.assertEqual(
            document["pool"],
            {
                "size": 0,
                "median_rank_cut": [1, 2],
                "holdout_only": {"count": 0, "ids": []},
            },
        )
        self.assertEqual(
            document["statistics"],
            {
                "spearman": None,
                "spearman_terms": {"xy": None, "xx": None, "yy": None},
                "spearman_ranks": {"x": None, "y": None},
                "spearman_undefined": "no_matched_vectors",
                "better_half": {
                    "numerator": 0,
                    "denominator": 0,
                    "value": None,
                },
            },
        )
        self.assertEqual(
            document["admission"],
            {
                "rows_read": 1,
                "selection_pairs": 0,
                "holdout_pairs": 0,
                "work": 1,
                "max_work": None,
            },
        )


class StatisticsTests(HoldoutTestCase):
    def test_the_correlation_that_is_null_although_the_ranks_are_perfect(self):
        """With metric m, minimize, k = 2, the selection vectors A (1.0) and
        B (2.0) rank 1 and 2, the pool is {A, B} with B (0.5) at position 1 and A (3.0)
        at position 2, so holdout_rank is [2, 1] for A and [1, 1] for B. The difference
        and ``improved`` point in opposite ways: A's +2.0 (3.0 - 1.0) is worse and B's
        -1.5 (0.5 - 2.0) is the improvement. The cut is (2 + 1)/2 = 3/2, reported as
        [3, 2], so only B is in the better half: 1 of 2. x = (1, 2) and y = (2, 1) give
        Sxy = 2*4 - 9 = -1, Sxx = 1 and Syy = 1, and n = 2 < 3 leaves the correlation
        null with its exact terms and both rank vectors reported.
        Catch publishing a correlation below three matches or ignoring minimize.
        """
        selection = [
            make_row(0, "ok", 1.0, "A", metrics={"m": 1.0}),
            make_row(1, "ok", 2.0, "B", metrics={"m": 2.0}),
        ]
        holdout_rows = [
            make_row(5, "ok", 9.0, "A", metrics={"m": 3.0}),
            make_row(6, "ok", 9.0, "B", metrics={"m": 0.5}),
        ]
        document = holdout_compare(
            selection, holdout_rows, metric="m", direction="minimize", k=2, space=S2
        )
        self.assertEqual(document["direction"], "minimize")
        self.assertEqual(document["n_sel"], 2)
        self.assertEqual(document["n_hol"], 2)
        self.assertEqual(document["k_followed"], 2)
        entries = [
            (
                entry["selection_trial_id"],
                entry["selection_rank"],
                entry["holdout_trial_id"],
                entry["holdout_value"],
                entry["holdout_rank"],
                entry["difference"],
                entry["improved"],
            )
            for entry in document["followed"]
        ]
        self.assertEqual(
            entries,
            [
                (0, 1, 5, 3.0, [2, 1], 2.0, False),
                (1, 2, 6, 0.5, [1, 1], -1.5, True),
            ],
        )
        self.assertEqual(
            document["pool"],
            {
                "size": 2,
                "median_rank_cut": [3, 2],
                "holdout_only": {"count": 0, "ids": []},
            },
        )
        self.assertEqual(
            document["statistics"],
            {
                "spearman": None,
                "spearman_terms": {"xy": [-1, 1], "xx": [1, 1], "yy": [1, 1]},
                "spearman_ranks": {"x": [[1, 1], [2, 1]], "y": [[2, 1], [1, 1]]},
                "spearman_undefined": "fewer_than_three_matched",
                "better_half": {"numerator": 1, "denominator": 2, "value": 0.5},
            },
        )

    def test_the_better_half_cut_with_an_even_pool_and_a_straddling_tie(self):
        """At k = 4 the pool is all four arms, M = 4; A (5.0) is at position
        1, B and C (4.0) at positions 2 and 3 with mid-rank 5/2, D (1.0) at position 4.
        The cut is (4 + 1)/2 = 5/2, so A, B and C are in and D is out: 3 of 4. The
        correlation's ranks are x = (1, 2, 3, 4) and y = (1, 5/2, 5/2, 4), giving
        SUM(x*y) = 59/2, SUM(y^2) = 59/2, Sxy = 18, Sxx = 20 and Syy = 18, so
        rho = 3/sqrt(10) = 0.9486832980505138.
        Catch a strict median cut, a cut of M/2 or splitting a tie by trial id.
        """
        selection = [
            make_row(0, "ok", 4.0, "A"),
            make_row(1, "ok", 3.0, "B"),
            make_row(2, "ok", 2.0, "C"),
            make_row(3, "ok", 1.0, "D"),
        ]
        holdout_rows = [
            make_row(7, "ok", 5.0, "A"),
            make_row(8, "ok", 4.0, "B"),
            make_row(9, "ok", 4.0, "C"),
            make_row(10, "ok", 1.0, "D"),
        ]
        document = holdout_compare(
            selection,
            holdout_rows,
            metric="objective",
            direction="maximize",
            k=4,
            space=S1,
        )
        self.assertEqual(document["k_followed"], 4)
        self.assertEqual(
            [entry["holdout_rank"] for entry in document["followed"]],
            [[1, 1], [5, 2], [5, 2], [4, 1]],
        )
        self.assertEqual(
            [entry["difference"] for entry in document["followed"]],
            [1.0, 1.0, 2.0, 0.0],
        )
        self.assertEqual(
            [entry["improved"] for entry in document["followed"]],
            [True, True, True, False],
        )
        self.assertEqual(document["pool"]["median_rank_cut"], [5, 2])
        self.assertEqual(
            document["statistics"]["better_half"],
            {"numerator": 3, "denominator": 4, "value": 0.75},
        )
        self.assertEqual(
            document["statistics"]["spearman_terms"],
            {"xy": [18, 1], "xx": [20, 1], "yy": [18, 1]},
        )
        self.assertEqual(
            document["statistics"]["spearman_ranks"],
            {
                "x": [[1, 1], [2, 1], [3, 1], [4, 1]],
                "y": [[1, 1], [5, 2], [5, 2], [4, 1]],
            },
        )
        self.assertEqual(document["statistics"]["spearman"], 0.9486832980505138)

    def test_the_correlations_ranks_are_the_matched_sets_not_the_pools(self):
        """At k = 3 the pool holds four arms (D is eligible in the holdout
        source although D is not followed), while only A, B and C are matched.

        Hand derivation: pool ranks are C [1, 1], B [2, 1], D [3, 1], A [4, 1],
        cut [5, 2]. The correlation ranks the MATCHED set: x = (1, 2, 3) and
        y = (3, 2, 1) for A, B, C. SUM(x*y) = 10, SUM(x^2) = SUM(y^2) = 14,
        Sxy = 30 - 36 = -6, Sxx = Syy = 6: correlation -1.0. Against cut 5/2 only
        B and C are in, so the better half is 2 of 3.
        Catch pool ranks entering the correlation or a pool based only on top k.
        """
        selection = [
            make_row(0, "ok", 4.0, "A"),
            make_row(1, "ok", 3.0, "B"),
            make_row(2, "ok", 2.0, "C"),
            make_row(3, "ok", 1.0, "D"),
        ]
        holdout_rows = [
            make_row(7, "ok", 1.0, "A"),
            make_row(8, "ok", 3.0, "B"),
            make_row(9, "ok", 4.0, "C"),
            make_row(10, "ok", 2.0, "D"),
        ]
        document = holdout_compare(
            selection,
            holdout_rows,
            metric="objective",
            direction="maximize",
            k=3,
            space=S1,
        )
        self.assertEqual(document["k_followed"], 3)
        self.assertEqual(document["unmatched"], [])
        self.assertEqual(document["n_matched"], 3)
        self.assertEqual(
            document["pool"],
            {
                "size": 4,
                "median_rank_cut": [5, 2],
                "holdout_only": {"count": 0, "ids": []},
            },
        )
        self.assertEqual(
            [entry["holdout_rank"] for entry in document["followed"]],
            [[4, 1], [2, 1], [1, 1]],
        )
        self.assertEqual(
            [entry["difference"] for entry in document["followed"]],
            [-3.0, 0.0, 2.0],
        )
        self.assertEqual(
            [entry["improved"] for entry in document["followed"]],
            [False, False, True],
        )
        self.assertEqual(
            document["statistics"]["spearman_terms"],
            {"xy": [-6, 1], "xx": [6, 1], "yy": [6, 1]},
        )
        self.assertEqual(
            document["statistics"]["spearman_ranks"],
            {
                "x": [[1, 1], [2, 1], [3, 1]],
                "y": [[3, 1], [2, 1], [1, 1]],
            },
        )
        self.assertIsNone(document["statistics"]["spearman_undefined"])
        self.assertEqual(document["statistics"]["spearman"], -1.0)
        self.assertEqual(
            document["statistics"]["better_half"],
            {"numerator": 2, "denominator": 3, "value": 0.6666666666666666},
        )

    def test_a_constant_holdout_rank_vector(self):
        """At k = 3 the selection values 3.0, 2.0, 1.0 are distinct and all
        three holdout values are 1.0, so y = (2, 2, 2) (one tie group, positions 1 to
        3), x = (1, 2, 3). SUM(x*y) = 12, SUM(y^2) = 12, n = 3, so Sxy = 0, Sxx = 6
        and Syy = 0: the undefined table reports ``constant_holdout_ranks`` before any
        zero correlation. Every pool mid-rank is 2 and the cut is 2, so all three
        matched vectors are in the better half. Catch dividing by zero, reporting
        zero correlation or ranking tied holdout values by trial id.
        """
        selection = [
            make_row(0, "ok", 3.0, "A"),
            make_row(1, "ok", 2.0, "B"),
            make_row(2, "ok", 1.0, "C"),
        ]
        holdout_rows = [
            make_row(7, "ok", 1.0, "A"),
            make_row(8, "ok", 1.0, "B"),
            make_row(9, "ok", 1.0, "C"),
        ]
        document = holdout_compare(
            selection,
            holdout_rows,
            metric="objective",
            direction="maximize",
            k=3,
            space=S1,
        )
        self.assertEqual(
            document["statistics"],
            {
                "spearman": None,
                "spearman_terms": {"xy": [0, 1], "xx": [6, 1], "yy": [0, 1]},
                "spearman_ranks": {
                    "x": [[1, 1], [2, 1], [3, 1]],
                    "y": [[2, 1], [2, 1], [2, 1]],
                },
                "spearman_undefined": "constant_holdout_ranks",
                "better_half": {"numerator": 3, "denominator": 3, "value": 1.0},
            },
        )
        self.assertEqual(document["pool"]["median_rank_cut"], [2, 1])
        self.assertEqual(
            [entry["holdout_rank"] for entry in document["followed"]],
            [[2, 1], [2, 1], [2, 1]],
        )
        self.assertEqual(
            [entry["difference"] for entry in document["followed"]],
            [-2.0, -1.0, 0.0],
        )

    def test_the_constant_selection_rank_vector_reached_directly(self):
        """At k = 3 the three selection values are all 5.0, so the
        selection order is by trial id (A, B, C) and the correlation's x = (2, 2, 2);
        holdout values 1.0, 2.0, 3.0 give y = (3, 2, 1).
        SUM(x*y) = 12, SUM(x^2) = 12,
        n = 3, so Sxy = 0, Sxx = 0 and Syy = 6: the reason is
        ``constant_selection_ranks`` (Syy is not 0). The pool ranks C 1, B 2, A 3 with
        the cut 2, so B and C are in the better half: 2 of 3.
        Catch ranking tied selection values by trial id in the correlation.
        """
        selection = [
            make_row(0, "ok", 5.0, "A"),
            make_row(1, "ok", 5.0, "B"),
            make_row(2, "ok", 5.0, "C"),
        ]
        holdout_rows = [
            make_row(7, "ok", 1.0, "A"),
            make_row(8, "ok", 2.0, "B"),
            make_row(9, "ok", 3.0, "C"),
        ]
        document = holdout_compare(
            selection,
            holdout_rows,
            metric="objective",
            direction="maximize",
            k=3,
            space=S1,
        )
        self.assertEqual(
            [entry["selection_trial_id"] for entry in document["followed"]],
            [0, 1, 2],
        )
        self.assertEqual(
            document["statistics"],
            {
                "spearman": None,
                "spearman_terms": {"xy": [0, 1], "xx": [0, 1], "yy": [6, 1]},
                "spearman_ranks": {
                    "x": [[2, 1], [2, 1], [2, 1]],
                    "y": [[3, 1], [2, 1], [1, 1]],
                },
                "spearman_undefined": "constant_selection_ranks",
                "better_half": {
                    "numerator": 2,
                    "denominator": 3,
                    "value": 0.6666666666666666,
                },
            },
        )
        self.assertEqual(
            [entry["holdout_rank"] for entry in document["followed"]],
            [[3, 1], [2, 1], [1, 1]],
        )


class DigestTests(HoldoutTestCase):
    def test_the_digest_and_determinism(self):
        """The digest is independently rebuilt from the recipe in docs/holdout.md
        and must equal the module's; with the rows of both sources reordered the
        whole document is unchanged, digest included, because the digests are sorted and
        every list of the document is ordered by an explicit rule. A SHA-256 cannot be
        derived by hand, so this test compares two constructions of the same recipe; it
        cannot fail on the arithmetic of the statistics.
        """
        selection = main_selection()
        holdout_rows = main_holdout()
        options = {
            "metric": "objective",
            "direction": "maximize",
            "k": 5,
            "space": S1,
        }
        first = holdout_compare(selection, holdout_rows, **options)
        self.assertEqual(
            first["input_sha256"], raw_digest([selection, holdout_rows], "objective")
        )
        rng = random.Random(20261012)
        for _ in range(8):
            shuffled_selection = list(selection)
            shuffled_holdout = list(holdout_rows)
            rng.shuffle(shuffled_selection)
            rng.shuffle(shuffled_holdout)
            again = holdout_compare(shuffled_selection, shuffled_holdout, **options)
            self.assertEqual(again, first)
        self.assertEqual(holdout_compare(selection, holdout_rows, **options), first)

    def test_the_digest_follows_the_rows(self):
        """The digest covers every row's metric value, ignoring unrelated metadata.

        Changing B's holdout objective from 4.0 to 4.5 changes a per-row digest.
        Adding a ``visible`` field to F's row changes none of the five digest fields,
        so the digest stays equal when all original rows are retained. This catches
        a digest that ignores values or includes unrelated raw fields. Both sources
        also record the same values under m and m2; changing only the requested name
        must leave the digest equal, catching a digest that includes the metric name.
        """
        document = main_call()
        changed = copy.deepcopy(main_holdout())
        changed[1]["objective"] = 4.5
        self.assertNotEqual(
            holdout_compare(
                main_selection(),
                changed,
                metric="objective",
                direction="maximize",
                k=5,
                space=S1,
            )["input_sha256"],
            document["input_sha256"],
        )
        annotated = copy.deepcopy(main_holdout())
        annotated[0]["visible"] = True
        self.assertEqual(
            holdout_compare(
                main_selection(),
                annotated,
                metric="objective",
                direction="maximize",
                k=5,
                space=S1,
            )["input_sha256"],
            document["input_sha256"],
        )
        selection = [make_row(0, "ok", 3.0, "A", metrics={"m": 1.0, "m2": 1.0})]
        holdout_rows = [make_row(7, "ok", 2.0, "A", metrics={"m": 2.0, "m2": 2.0})]
        options = {"direction": "maximize", "k": 1, "space": S1}
        first_name = holdout_compare(selection, holdout_rows, metric="m", **options)
        second_name = holdout_compare(selection, holdout_rows, metric="m2", **options)
        self.assertEqual(first_name["input_sha256"], second_name["input_sha256"])


class DirectionTests(HoldoutTestCase):
    def test_direction_taken_from_a_recorded_space(self):
        """Two complete results each recording S1 and no result-level
        direction, one eligible row each. Both ``Source.direction`` values are maximize
        and equal, so ``direction=None`` runs with maximize: 2.0 - 3.0 = -1.0 and
        ``improved`` is False, and one matched vector leaves the correlation null.
        """
        selection = {"trials": [make_row(0, "ok", 3.0, "A")], "space": S1}
        holdout_result = {"trials": [make_row(1, "ok", 2.0, "A")], "space": S1}
        document = holdout_compare(
            selection, holdout_result, metric="objective", direction=None, k=1
        )
        self.assertEqual(document["direction"], "maximize")
        self.assertEqual(document["followed"][0]["difference"], -1.0)
        self.assertEqual(document["followed"][0]["improved"], False)
        self.assertEqual(document["n_matched"], 1)
        self.assertIsNone(document["statistics"]["spearman"])
        self.assertEqual(
            document["statistics"]["spearman_undefined"], "fewer_than_three_matched"
        )
        self.assertEqual(
            document["statistics"]["better_half"],
            {"numerator": 1, "denominator": 1, "value": 1.0},
        )

    def test_a_recorded_space_that_differs_refuses_before_the_direction_rule(self):
        """With one result's space recording ``minimize``, the space mismatch
        fires before the missing-direction check. Catch swapping those checks.
        """
        other = copy.deepcopy(S1)
        other["objective"]["direction"] = "minimize"
        error = self.refusal(
            {"trials": [make_row(0, "ok", 3.0, "A")], "space": S1},
            {"trials": [make_row(1, "ok", 2.0, "A")], "space": other},
            metric="objective",
            direction=None,
            k=1,
        )
        self.assertRefused(error, "holdout_space_mismatch")

    def test_no_recorded_direction_anywhere_is_missing(self):
        """Two bare rows lists record no direction, so ``direction=None`` refuses.
        Catch inferring a recorded direction from the caller's passed space.
        """
        error = self.refusal(
            [make_row(0, "ok", 3.0, "A")],
            [make_row(1, "ok", 2.0, "A")],
            metric="objective",
            direction=None,
            k=1,
            space=S1,
        )
        self.assertRefused(error, "holdout_parameter_missing", names=("direction",))

    def test_two_recorded_directions_that_disagree_are_missing(self):
        """Two results record no space and disagree on their result-level directions.
        The passed space supplies keys; catch taking either recorded direction when
        the caller passes None instead of refusing their disagreement.
        """
        error = self.refusal(
            {"trials": [make_row(0, "ok", 3.0, "A")], "direction": "maximize"},
            {"trials": [make_row(1, "ok", 2.0, "A")], "direction": "minimize"},
            metric="objective",
            direction=None,
            k=1,
            space=S1,
        )
        self.assertRefused(error, "holdout_parameter_missing", names=("direction",))

    def test_a_caller_direction_overrides_a_recorded_one(self):
        """Catch replacing the caller's direction with the recorded direction."""
        selection = {"trials": [make_row(0, "ok", 3.0, "A")], "space": S1}
        holdout_result = {"trials": [make_row(1, "ok", 2.0, "A")], "space": S1}
        document = holdout_compare(
            selection, holdout_result, metric="objective", direction="minimize", k=1
        )
        self.assertEqual(document["direction"], "minimize")
        self.assertEqual(document["followed"][0]["difference"], -1.0)
        self.assertEqual(document["followed"][0]["improved"], True)

    def test_direction_none_with_a_named_metric_is_missing(self):
        """Catch inferring a named metric's direction instead of refusing None."""
        error = self.refusal(
            [make_row(0, "ok", 1.0, "A", metrics={"m": 1.0})],
            [make_row(1, "ok", 1.0, "A", metrics={"m": 2.0})],
            metric="m",
            direction=None,
            k=1,
            space=S1,
        )
        self.assertRefused(error, "holdout_parameter_missing", names=("direction",))


class DuplicateVectorTests(HoldoutTestCase):
    def test_repeated_vectors_agree_collapse_and_disagree_refuse(self):
        """At k = 2, agreeing repeated vectors collapse and disagreeing ones refuse.

        (a) Two eligible selection rows hold arm A with the same value 5.0, so they are
        one vector: rows_eligible 2, vectors_eligible 1, k_followed 1 (clipped) and the
        representative is trial id 0. Holdout vector A (id 7, 6.0) is the pool, M = 1,
        cut [1, 1], difference +1.0, improved True, better_half 1 of 1; with n = 1 the
        terms are the three zero pairs and the reason is ``fewer_than_three_matched``,
        which the undefined table takes before the constant-rank branches.
        (b) The same rows with 5.0 and 6.0 disagree, so the call refuses with the ids of
        every eligible row of the vector and the key in the detail.
        Catch refusing agreeing repeats, collapsing conflicts or choosing a higher id.
        """
        selection = [
            make_row(0, "ok", 5.0, "A"),
            make_row(1, "ok", 5.0, "A"),
        ]
        holdout_rows = [make_row(7, "ok", 6.0, "A")]
        document = holdout_compare(
            selection,
            holdout_rows,
            metric="objective",
            direction="maximize",
            k=2,
            space=S1,
        )
        self.assertSource(
            document["selection"],
            rows=2,
            rows_eligible=2,
            vectors_eligible=1,
            rows_excluded=0,
        )
        self.assertEqual(document["k_followed"], 1)
        self.assertEqual(document["n_matched"], 1)
        self.assertEqual(document["followed"][0]["selection_trial_id"], 0)
        self.assertEqual(document["followed"][0]["selection_value"], 5.0)
        self.assertEqual(document["followed"][0]["holdout_trial_id"], 7)
        self.assertEqual(document["followed"][0]["difference"], 1.0)
        self.assertEqual(document["followed"][0]["improved"], True)
        self.assertEqual(document["followed"][0]["holdout_rank"], [1, 1])
        self.assertEqual(
            document["pool"],
            {
                "size": 1,
                "median_rank_cut": [1, 1],
                "holdout_only": {"count": 0, "ids": []},
            },
        )
        self.assertEqual(
            document["statistics"]["better_half"],
            {"numerator": 1, "denominator": 1, "value": 1.0},
        )
        self.assertEqual(
            document["statistics"]["spearman_terms"],
            {"xy": [0, 1], "xx": [0, 1], "yy": [0, 1]},
        )
        self.assertEqual(
            document["statistics"]["spearman_ranks"], {"x": [[1, 1]], "y": [[1, 1]]}
        )
        self.assertIsNone(document["statistics"]["spearman"])
        self.assertEqual(
            document["statistics"]["spearman_undefined"], "fewer_than_three_matched"
        )
        error = self.refusal(
            [make_row(0, "ok", 5.0, "A"), make_row(1, "ok", 6.0, "A")],
            holdout_rows,
            metric="objective",
            direction="maximize",
            k=2,
            space=S1,
        )
        self.assertRefused(error, "holdout_duplicate_vector", ids=((0, 0), (0, 1)))
        self.assertIn(KEYS["A"], error.detail)

    def test_a_repeated_vector_with_an_ineligible_row_does_not_refuse(self):
        """Only eligible rows count toward repeated-vector conflicts.

        Selection row 0 is eligible at 5.0; row 1 shares A but is pruned, so it is
        excluded as ``status_pruned``. Holdout row 7 matches A at 6.0, giving one
        matched vector represented by selection id 0, difference 1.0. This catches
        an ineligible row poisoning the eligible vector or suppressing its join.
        """
        document = holdout_compare(
            [make_row(0, "ok", 5.0, "A"), make_row(1, "pruned", None, "A")],
            [make_row(7, "ok", 6.0, "A")],
            metric="objective",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertEqual(
            document["selection"]["excluded"],
            {"status_pruned": {"count": 1, "ids": [1]}},
        )
        self.assertEqual(document["selection"]["rows_eligible"], 1)
        self.assertEqual(document["selection"]["vectors_eligible"], 1)
        self.assertEqual(document["n_matched"], 1)
        self.assertEqual(document["followed"][0]["selection_trial_id"], 0)
        self.assertEqual(document["followed"][0]["holdout_trial_id"], 7)
        self.assertEqual(document["followed"][0]["difference"], 1.0)

    def test_the_smallest_conflicting_key_is_named(self):
        """Catch naming any conflicting key other than the smallest one."""
        error = self.refusal(
            [
                make_row(0, "ok", 5.0, "C"),
                make_row(1, "ok", 6.0, "C"),
                make_row(2, "ok", 1.0, "A"),
                make_row(3, "ok", 2.0, "A"),
            ],
            [],
            metric="objective",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertRefused(
            error, "holdout_duplicate_vector", ids=((0, 0), (0, 1), (0, 2), (0, 3))
        )
        self.assertIn(KEYS["A"], error.detail)
        self.assertNotIn(KEYS["C"], error.detail)

    def test_duplicate_refusals_include_all_groups_of_the_first_conflicting_source(
        self,
    ):
        """The repeated-vector stage checks the selection source first.

        A has values 5, 6, 5 and C has 1, 2, so every eligible row of both groups
        belongs to the selection refusal, including A's two agreeing rows. Pruned
        A is excluded. A conflicting holdout group must not add its ids. With only
        one eligible selection row, the holdout conflict is reached and names all
        three of its rows. This catches partial groups, cross-source aggregation,
        and a skipped holdout check.
        """
        selection = [
            make_row(8, "ok", 5.0, "A"),
            make_row(2, "ok", 6.0, "A"),
            make_row(5, "ok", 5.0, "A"),
            make_row(4, "ok", 1.0, "C"),
            make_row(1, "ok", 2.0, "C"),
            make_row(0, "pruned", None, "A"),
        ]
        holdout_rows = [
            make_row(7, "ok", 3.0, "A"),
            make_row(9, "ok", 4.0, "A"),
            make_row(6, "ok", 3.0, "A"),
        ]
        options = {"metric": "objective", "direction": "maximize", "k": 1, "space": S1}
        error = self.refusal(selection, holdout_rows, **options)
        self.assertRefused(
            error,
            "holdout_duplicate_vector",
            ids=((0, 1), (0, 2), (0, 4), (0, 5), (0, 8)),
        )
        self.assertIn(KEYS["A"], error.detail)
        self.assertNotIn(KEYS["C"], error.detail)
        error = self.refusal([selection[0]], holdout_rows, **options)
        self.assertRefused(
            error, "holdout_duplicate_vector", ids=((1, 6), (1, 7), (1, 9))
        )
        self.assertIn(KEYS["A"], error.detail)


class AncestorTests(HoldoutTestCase):
    def test_the_ancestor_counts_and_their_refusal(self):
        """Ancestor counts come from the result value, so a dictionary and its file
        count alike. Catch missing ancestors, mistaken file dispatch or refusing
        ancestors whose ids do not overlap selection rows.

        (a) the holdout result carries one ancestor row (id 7) which appears in no
        selection row, so nothing refuses: selection.ancestor_rows 0,
        holdout.ancestor_rows 1; selection vector A matches holdout row 20, difference
        2.0 - 3.0 = -1.0. (b) The same result as one compact JSON line gives the same
        document, digest included. (d) a two-line rows file is a rows source, so both
        counts are 0 and the rest of the document equals (a)'s, digest included. (e) the
        same result written with an indent is still one JSON document, because the first
        line ``{`` is not one value by syntax alone. (c) The holdout result instead
        repeats the selection's ancestor trial id 0, so the call refuses on the overlap.
        """
        holdout_result = result_of(
            [make_row(7, "ok", 1.0, "B")], [make_row(20, "ok", 2.0, "A")]
        )
        selection = [make_row(0, "ok", 3.0, "A")]
        options = {"metric": "objective", "direction": "maximize", "k": 1, "space": S1}
        base = holdout_compare(selection, holdout_result, **options)
        self.assertEqual(base["selection"]["ancestor_rows"], 0)
        self.assertEqual(base["holdout"]["ancestor_rows"], 1)
        self.assertEqual(base["followed"][0]["difference"], -1.0)
        self.assertEqual(base["followed"][0]["improved"], False)
        self.assertEqual(base["holdout"]["rows"], 2)
        self.assertEqual(base["pool"]["holdout_only"], {"count": 1, "ids": [7]})
        compact = FIXTURES / "ancestor_result_compact.json"
        self.assertEqual(json.loads(fixture_text(compact.name)), holdout_result)
        self.assertEqual(len(non_blank_lines(fixture_text(compact.name))), 1)
        as_path = holdout_compare(selection, compact, **options)
        self.assertEqual(as_path, base)
        indented = FIXTURES / "ancestor_result_indented.json"
        self.assertEqual(json.loads(fixture_text(indented.name)), holdout_result)
        self.assertGreater(len(fixture_text(indented.name).split("\n")), 2)
        self.assertEqual(holdout_compare(selection, indented, **options), base)
        rows_file = FIXTURES / "ancestor_rows.jsonl"
        text = fixture_text(rows_file.name)
        self.assertEqual(
            [json.loads(line) for line in non_blank_lines(text)],
            holdout_result["warm_start_trials"] + holdout_result["trials"],
        )
        from_rows_file = holdout_compare(selection, rows_file, **options)
        self.assertEqual(from_rows_file["selection"]["ancestor_rows"], 0)
        self.assertEqual(from_rows_file["holdout"]["ancestor_rows"], 0)
        adjusted = copy.deepcopy(from_rows_file)
        adjusted["holdout"]["ancestor_rows"] = 1
        self.assertEqual(adjusted, base)
        overlapped = result_of(
            [make_row(0, "ok", 9.0, "A")], [make_row(20, "ok", 2.0, "B")]
        )
        contested = result_of(
            [make_row(0, "ok", 9.0, "A")], [make_row(30, "ok", 3.0, "B")]
        )
        error = self.refusal(contested, overlapped, **options)
        self.assertRefused(error, "holdout_ancestor_rows", ids=((1, 0),))
        self.assertIn("selection ancestor rows 1", error.detail)
        self.assertIn("holdout ancestor rows 1", error.detail)
        indented_pair = FIXTURES / "overlapping_ancestor_result_indented.json"
        self.assertEqual(json.loads(fixture_text(indented_pair.name)), overlapped)
        self.assertRefused(
            self.refusal(contested, indented_pair, **options),
            "holdout_ancestor_rows",
            ids=((1, 0),),
        )

    def test_an_overlap_that_is_not_there_refuses_nothing(self):
        """Catch refusing a holdout ancestor whose id appears in no selection row.

        Its ancestor count is reported and the eligible selection vector is matched.
        """
        holdout_result = result_of(
            [make_row(7, "ok", 1.0, "B")], [make_row(20, "ok", 2.0, "A")]
        )
        document = holdout_compare(
            [make_row(0, "ok", 3.0, "A")],
            holdout_result,
            metric="objective",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertEqual(document["holdout"]["ancestor_rows"], 1)
        self.assertEqual(document["n_matched"], 1)


class RefusalTests(HoldoutTestCase):
    def test_non_string_metric_before_missing_direction(self):
        """A missing direction applies only when metric is a named string.

        With metric=7 and k=1, neither metric nor k is missing, and the named-string
        condition for direction=None is false. The next check rejects the non-string
        metric: holdout_parameter_invalid, empty ids, names ("metric",).
        Catch an implementation that reports holdout_parameter_missing for direction
        before rejecting a non-string metric.
        """
        error = self.refusal(
            main_selection(),
            main_holdout(),
            metric=7,
            direction=None,
            k=1,
            space=S1,
        )
        self.assertRefused(error, "holdout_parameter_invalid", names=("metric",))

    def test_parameter_refusals(self):
        """Invalid arguments name the argument responsible for each refusal.

        None gives ``holdout_parameter_missing``; invalid k, direction, metric,
        max_work and space give ``holdout_parameter_invalid`` with empty ids and
        that argument's name. These single-defect cases catch missing names or wrong
        codes. The two-defect case metric=7, k=None checks missing k is refused before
        invalid metric; checking invalid arguments first changes the code and name.
        """
        selection = main_selection()
        holdout_rows = main_holdout()
        base = {"metric": "objective", "direction": "maximize", "k": 5, "space": S1}
        for options, names in (
            ({"metric": "objective", "direction": "maximize", "k": None}, ("k",)),
            ({"metric": None, "direction": "maximize", "k": 5}, ("metric",)),
            ({"metric": "m", "direction": None, "k": 5}, ("direction",)),
        ):
            with self.subTest(names=names):
                options["space"] = S1
                error = self.refusal(selection, holdout_rows, **options)
                self.assertRefused(error, "holdout_parameter_missing", names=names)
        error = self.refusal(
            [make_row(0, "ok", 3.0, "A")],
            [make_row(1, "ok", 2.0, "A")],
            metric="objective",
            direction=None,
            k=1,
            space=S1,
        )
        self.assertRefused(error, "holdout_parameter_missing", names=("direction",))
        error = self.refusal(
            selection, holdout_rows, metric=7, direction="maximize", k=None, space=S1
        )
        self.assertRefused(error, "holdout_parameter_missing", names=("k",))
        for options, names in (
            ({"metric": "objective", "direction": "maximize", "k": 0}, ("k",)),
            ({"metric": "objective", "direction": "maximize", "k": True}, ("k",)),
            ({"metric": "objective", "direction": "maximize", "k": 1.0}, ("k",)),
            ({"metric": "objective", "direction": "up", "k": 1}, ("direction",)),
            ({"metric": 7, "direction": "maximize", "k": 1}, ("metric",)),
            (
                {"metric": "objective", "direction": "maximize", "k": 1, "max_work": 0},
                ("max_work",),
            ),
            (
                {"metric": "objective", "direction": "maximize", "k": 1, "space": 42},
                ("space",),
            ),
        ):
            with self.subTest(options=options):
                options.setdefault("space", S1)
                error = self.refusal(selection, holdout_rows, **options)
                self.assertRefused(error, "holdout_parameter_invalid", names=names)
        accepted = holdout_compare(selection, holdout_rows, **base)
        self.assertEqual(accepted["status"], "ok")

    def test_source_and_space_refusals(self):
        """Catch wrong source and space refusals and their offending names and ids.

        Cases cover an unreadable source, mismatched parameter names (the symmetric
        difference, ascending), a malformed passed space used when nothing records
        one, an incomplete best-k result, missing space and differing recorded spaces.
        """
        selection = main_selection()
        holdout_rows = main_holdout()
        base = {"metric": "objective", "direction": "maximize", "k": 1, "space": S1}
        self.assertRefused(
            self.refusal(42, holdout_rows, **base), "holdout_input_invalid"
        )
        space_a = {
            "parameters": {"a": {"kind": "boolean"}},
            "objective": dict(S1["objective"]),
        }
        two_rows = [
            {
                "trial_id": 0,
                "status": "ok",
                "feasible": True,
                "objective": 1.0,
                "parameters": {"a": True},
            },
            {
                "trial_id": 1,
                "status": "ok",
                "feasible": True,
                "objective": 1.0,
                "parameters": {"b": 1},
            },
        ]
        error = self.refusal(
            two_rows,
            holdout_rows,
            metric="objective",
            direction="maximize",
            k=1,
            space=space_a,
        )
        self.assertRefused(
            error, "holdout_input_invalid", ids=((0, 1),), names=("a", "b")
        )
        error = self.refusal(
            selection,
            holdout_rows,
            metric="objective",
            direction="maximize",
            k=1,
            space={"parameters": "x"},
        )
        self.assertRefused(error, "holdout_input_invalid")
        recording = [dict(row, space=S1) for row in selection]
        self.assertEqual(
            holdout_compare(
                recording,
                holdout_rows,
                metric="objective",
                direction="maximize",
                k=1,
                space={"parameters": "x"},
            )["status"],
            "ok",
        )
        self.assertRefused(
            self.refusal(
                {"trials_out": "best", "trials": []},
                [],
                metric="objective",
                direction="maximize",
                k=1,
            ),
            "holdout_rows_incomplete",
        )
        self.assertRefused(
            self.refusal(
                [make_row(0, "ok", 3.0, "A")],
                [make_row(1, "ok", 2.0, "A")],
                metric="objective",
                direction="maximize",
                k=1,
            ),
            "holdout_space_missing",
        )
        other = copy.deepcopy(S2)
        self.assertRefused(
            self.refusal(
                [dict(make_row(0, "ok", 3.0, "A"), space=S1)],
                [dict(make_row(1, "ok", 2.0, "A"), space=other)],
                metric="objective",
                direction="maximize",
                k=1,
            ),
            "holdout_space_mismatch",
            ids=((1, 1),),
            names=("arm",),
        )
        objective_only = copy.deepcopy(S1)
        objective_only["objective"]["expression"] = "metrics.all.profit_factor"
        self.assertRefused(
            self.refusal(
                [dict(make_row(0, "ok", 3.0, "A"), space=S1)],
                [dict(make_row(1, "ok", 2.0, "A"), space=objective_only)],
                metric="objective",
                direction="maximize",
                k=1,
            ),
            "holdout_space_mismatch",
            ids=((1, 1),),
        )

    def test_context_metric_and_value_refusals(self):
        """Catch wrong context, metric, value and eligibility refusals.

        Cases cover differing counters within one source and permit differing
        counters between sources, unknown names in either source, non-number ok
        cells, non-finite cells and no eligible selection rows with reason counts.
        """
        first = dict.fromkeys(
            (
                "input_bars_processed",
                "script_bars_processed",
                "input_tf_seconds",
                "script_tf_seconds",
            ),
            100,
        )
        second = dict(first, input_bars_processed=200)
        rows = [
            make_row(0, "ok", 3.0, "A", metrics={"m": 1.0}, backtest=first),
            make_row(1, "ok", 2.0, "B", metrics={"m": 2.0}, backtest=second),
        ]
        error = self.refusal(rows, [], metric="m", direction="maximize", k=1, space=S1)
        self.assertRefused(
            error,
            "holdout_context_mismatch",
            ids=((0, 0), (0, 1)),
            names=("input_bars_processed",),
        )
        split = holdout_compare(
            [rows[0]],
            [dict(rows[1], parameters={"arm": "A"})],
            metric="m",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertEqual(split["status"], "ok")
        error = self.refusal(
            [],
            [make_row(7, "ok", 1.0, "A", metrics={"other": 1.0})],
            metric="nope",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertRefused(error, "holdout_metric_unknown", names=("nope",))
        error = self.refusal(
            [make_row(0, "ok", 1.0, "A", metrics={"other": 1.0})],
            [make_row(7, "ok", 1.0, "A", metrics={})],
            metric="nope",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertRefused(error, "holdout_metric_unknown", names=("nope",))
        error = self.refusal(
            [make_row(0, "ok", 1.0, "A", metrics={"m": 1.0})],
            [make_row(7, "ok", 1.0, "A", metrics={"m": 2.0})],
            metric="nope",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertRefused(error, "holdout_metric_unknown", names=("nope",))
        error = self.refusal(
            [make_row(0, "ok", 1.0, "A", metrics={"m": 1.0})],
            [make_row(7, "ok", 1.0, "A", metrics={"other": 1.0})],
            metric="m",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertRefused(error, "holdout_metric_unknown", names=("m",))
        error = self.refusal(
            [make_row(0, "ok", 1.0, "A", metrics={"m": "x"})],
            [make_row(7, "ok", 1.0, "A", metrics={"m": 2.0})],
            metric="m",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertRefused(error, "holdout_value_invalid", ids=((0, 0),))
        error = self.refusal(
            [make_row(0, "pruned", None, "A", metrics={"m": float("nan")})],
            [make_row(7, "ok", 1.0, "A", metrics={"m": 2.0})],
            metric="m",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertRefused(error, "holdout_value_invalid", ids=((0, 0),))
        unchanged = holdout_compare(
            [
                make_row(0, "pruned", None, "A", metrics={"m": "x"}),
                make_row(1, "ok", 1.0, "B", metrics={"m": 1.0}),
            ],
            [],
            metric="m",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertEqual(unchanged["status"], "ok")
        self.assertEqual(unchanged["selection"]["rows_eligible"], 1)
        self.assertEqual(
            unchanged["selection"]["excluded"],
            {"status_pruned": {"count": 1, "ids": [0]}},
        )
        error = self.refusal(
            [make_row(0, "pruned", None, "A")],
            [],
            metric="objective",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertRefused(error, "holdout_no_eligible_rows")
        self.assertIn("status_pruned 1", error.detail)

    def test_duplicate_and_resource_refusals(self):
        """Catch partial conflict ids or an incorrect resource boundary.

        Every eligible row of the conflicting vector is named. Eleven rows and
        10 + 6 vector pairs give work 27, refused above 26 and admitted at 27.
        """
        self.assertRefused(
            self.refusal(
                [make_row(0, "ok", 5.0, "A"), make_row(1, "ok", 6.0, "A")],
                [],
                metric="objective",
                direction="maximize",
                k=1,
                space=S1,
            ),
            "holdout_duplicate_vector",
            ids=((0, 0), (0, 1)),
        )
        selection = main_selection()
        holdout_rows = main_holdout()
        options = {"metric": "objective", "direction": "maximize", "k": 5, "space": S1}
        error = self.refusal(selection, holdout_rows, max_work=26, **options)
        self.assertRefused(error, "holdout_resource_exceeded")
        self.assertIn("figure 27", error.detail)
        document = holdout_compare(selection, holdout_rows, max_work=27, **options)
        self.assertEqual(document["status"], "ok")
        self.assertEqual(document["admission"]["max_work"], 27)

    def test_cells_that_cannot_be_written_and_a_difference_that_overflows(self):
        """Catch unwritable cells or overflowing differences escaping as exceptions.

        (a) A pruned row's cell ``[nan]`` cannot be written with allow_nan=False, so
        value checking refuses for its row before digest construction. (b) The integer
        2**1024 makes float() raise OverflowError. (c) A set is not JSON. (d) The
        difference 1e308 - (-1e308) exceeds the largest finite binary64 (about
        1.7976931348623157e308), so subtraction gives +inf and refuses for both rows.
        """
        selection_m = [make_row(0, "ok", 1.0, "B", metrics={"m": 1.0})]
        holdout_m = [make_row(7, "ok", 2.0, "B", metrics={"m": 2.0})]
        cases = (
            (
                [
                    make_row(0, "pruned", None, "A", metrics={"m": [float("nan")]}),
                    make_row(1, "ok", 1.0, "B", metrics={"m": 1.0}),
                ],
                holdout_m,
            ),
            (
                [make_row(0, "ok", 1.0, "A", metrics={"m": 2**1024})],
                [make_row(7, "ok", 2.0, "A", metrics={"m": 2.0})],
            ),
            (
                [
                    make_row(0, "pruned", None, "A", metrics={"m": {1, 2}}),
                    make_row(1, "ok", 1.0, "B", metrics={"m": 1.0}),
                ],
                holdout_m,
            ),
        )
        for selection, holdout_rows in cases:
            with self.subTest(selection=selection):
                error = self.refusal(
                    selection,
                    holdout_rows,
                    metric="m",
                    direction="maximize",
                    k=1,
                    space=S1,
                )
                self.assertRefused(error, "holdout_value_invalid", ids=((0, 0),))
        overflow = self.refusal(
            [make_row(0, "ok", -1e308, "A")],
            [make_row(7, "ok", 1e308, "A")],
            metric="objective",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertRefused(overflow, "holdout_value_invalid", ids=((0, 0), (1, 7)))
        self.assertEqual(
            holdout_compare(
                selection_m,
                holdout_m,
                metric="m",
                direction="maximize",
                k=1,
                space=S1,
            )["status"],
            "ok",
        )

    def test_mixed_type_cell_keys_refuse_before_digest_construction(self):
        """Catch checking JSON writability without the digest's key sorting.

        The pruned row's m cell has keys 1 and "b". Unsorted JSON can write it,
        but sorting those keys raises TypeError. Canonical value checking must
        refuse holdout_value_invalid for row (0, 0) before the digest can crash.
        An eligible row keeps this case independent of the no-eligible-row refusal.
        """
        error = self.refusal(
            [
                make_row(0, "pruned", None, "A", metrics={"m": {1: "a", "b": 2}}),
                make_row(1, "ok", 1.0, "B", metrics={"m": 1.0}),
            ],
            [make_row(7, "ok", 2.0, "B", metrics={"m": 2.0})],
            metric="m",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertRefused(error, "holdout_value_invalid", ids=((0, 0),))

    def test_the_order_of_adjacent_steps_with_two_defects(self):
        """Catch reversed precedence with three pairs of simultaneous defects.

        (a) Holdout ok rows disagree on ``input_bars_processed`` and ancestor id 0
        repeats a selection id: context checking wins over the ancestor check.
        (b) Holdout ancestor id 0 repeats a selection id and another row cannot be
        keyed: the ancestor check wins over key checking. (c) A selection row cannot
        be keyed and another has a non-finite metric: key checking wins over value
        checking. Swapping either check in a pair changes that pair's refusal code.
        """
        first = dict.fromkeys(
            (
                "input_bars_processed",
                "script_bars_processed",
                "input_tf_seconds",
                "script_tf_seconds",
            ),
            100,
        )
        second = dict(first, input_bars_processed=200)
        selection = result_of(
            [make_row(0, "ok", 1.0, "A")], [make_row(30, "ok", 3.0, "B")]
        )
        holdout_result = result_of(
            [make_row(0, "ok", 9.0, "A")],
            [
                make_row(20, "ok", 2.0, "B", backtest=first),
                make_row(21, "ok", 5.0, "C", backtest=second),
            ],
        )
        self.assertRefused(
            self.refusal(
                selection,
                holdout_result,
                metric="objective",
                direction="maximize",
                k=1,
                space=S1,
            ),
            "holdout_context_mismatch",
            ids=((1, 20), (1, 21)),
            names=("input_bars_processed",),
        )
        bad_parameters = [
            {
                "trial_id": 20,
                "status": "ok",
                "feasible": True,
                "objective": 2.0,
                "parameters": {"b": 1.0},
            },
        ]
        self.assertRefused(
            self.refusal(
                [make_row(0, "ok", 1.0, "A")],
                result_of([make_row(0, "ok", 9.0, "A")], bad_parameters),
                metric="objective",
                direction="maximize",
                k=1,
                space=S1,
            ),
            "holdout_ancestor_rows",
            ids=((1, 0),),
        )
        self.assertRefused(
            self.refusal(
                [
                    make_row(0, "ok", 1.0, "A", metrics={"m": float("nan")}),
                    {
                        "trial_id": 1,
                        "status": "ok",
                        "feasible": True,
                        "objective": 2.0,
                        "parameters": {"b": 1.0},
                        "metrics": {"m": 2.0},
                    },
                ],
                [make_row(7, "ok", 3.0, "A", metrics={"m": 3.0})],
                metric="m",
                direction="maximize",
                k=1,
                space=S1,
            ),
            "holdout_input_invalid",
            ids=((0, 1),),
            names=("arm", "b"),
        )

    def test_check_order_parameters_before_the_source(self):
        """Catch reading an invalid source before rejecting an invalid argument."""
        error = self.refusal(42, 42, metric="objective", direction="maximize", k=0)
        self.assertEqual(error.code, "holdout_parameter_invalid")

    def test_check_order_the_space_before_the_direction_rule(self):
        """Catch checking a missing direction before a conflicting recorded space."""
        other = copy.deepcopy(S2)
        error = self.refusal(
            [dict(make_row(0, "ok", 1.0, "A"), space=S1)],
            [dict(make_row(1, "ok", 1.0, "A"), space=other)],
            metric="objective",
            direction=None,
            k=1,
        )
        self.assertEqual(error.code, "holdout_space_mismatch")

    def test_check_order_the_metric_name_before_the_values(self):
        """Catch checking values before metric-name availability in both sources.

        Selection records m with a string cell; holdout does not record m, so the
        unknown-name refusal wins over the invalid selection value.
        """
        error = self.refusal(
            [make_row(0, "ok", 1.0, "A", metrics={"m": "x"})],
            [make_row(1, "ok", 1.0, "A", metrics={"other": 1.0})],
            metric="m",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertEqual(error.code, "holdout_metric_unknown")

    def test_check_order_no_eligible_rows_before_the_resource_check(self):
        """Catch swapping the no-eligible-selection check and resource check.

        Two pruned selection rows give work 2 + 0 + 0 = 2, above max_work=1.
        Checking resources first would give holdout_resource_exceeded instead of
        holdout_no_eligible_rows.
        """
        error = self.refusal(
            [make_row(0, "pruned", None, "A"), make_row(1, "pruned", None, "B")],
            [],
            metric="objective",
            direction="maximize",
            k=1,
            space=S1,
            max_work=1,
        )
        self.assertEqual(error.code, "holdout_no_eligible_rows")

    def test_check_order_max_work_before_space_type(self):
        """Catch checking space's type before an invalid max_work.

        With max_work=0 and space=42, both arguments are invalid. The refusal must
        name max_work; the swapped order names space with the same refusal code.
        """
        error = self.refusal(
            [],
            [],
            metric="objective",
            direction="maximize",
            k=1,
            space=42,
            max_work=0,
        )
        self.assertRefused(error, "holdout_parameter_invalid", names=("max_work",))

    def test_an_empty_or_ineligible_holdout_source_does_not_refuse(self):
        """Catch refusing an empty or wholly ineligible holdout source."""
        options = {"metric": "objective", "direction": "maximize", "k": 1, "space": S1}
        selection = [make_row(0, "ok", 3.0, "A")]
        for holdout_source in ([], [make_row(7, "pruned", None, "A")]):
            with self.subTest(holdout=holdout_source):
                document = holdout_compare(selection, holdout_source, **options)
                self.assertEqual(document["status"], "ok")
                self.assertEqual(document["n_matched"], 0)
                self.assertEqual(
                    document["unmatched"][0]["reason"],
                    (
                        "absent_in_holdout"
                        if not holdout_source
                        else "holdout_row_not_eligible"
                    ),
                )


class InvarianceTests(HoldoutTestCase):
    def test_every_source_form_gives_the_same_document(self):
        """Catch reading a parsed result differently from the same result file."""
        result = result_of(
            [make_row(7, "ok", 1.0, "B")], [make_row(20, "ok", 2.0, "A")]
        )
        options = {"metric": "objective", "direction": "maximize", "k": 1, "space": S1}
        selection = [make_row(0, "ok", 3.0, "A")]
        path = FIXTURES / "ancestor_result_compact.json"
        documents = [
            holdout_compare(selection, result, **options),
            holdout_compare(selection, path, **options),
        ]
        self.assertEqual(documents[0], documents[1])
        rows_source = result["warm_start_trials"] + result["trials"]
        plain = holdout_compare(selection, rows_source, **options)
        self.assertEqual(plain["holdout"]["ancestor_rows"], 0)

    def test_inputs_are_not_changed(self):
        """Catch modifying the caller's rows or space during comparison."""
        selection = main_selection()
        holdout_rows = main_holdout()
        space = copy.deepcopy(S1)
        before = (
            copy.deepcopy(selection),
            copy.deepcopy(holdout_rows),
            copy.deepcopy(space),
        )
        options = {
            "metric": "objective",
            "direction": "maximize",
            "k": 5,
            "space": space,
        }
        result = holdout_compare(selection, holdout_rows, **options)
        self.assertEqual((selection, holdout_rows, space), before)
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_a_source_passed_twice_without_ancestors(self):
        """Catch refusing identical sources or reporting differences for identical rows.

        Without ancestors, every difference is 0.0 and the correlation is +1.
        """
        rows = [
            make_row(0, "ok", 3.0, "A"),
            make_row(1, "ok", 2.0, "B"),
            make_row(2, "ok", 1.0, "C"),
        ]
        document = holdout_compare(
            copy.deepcopy(rows),
            copy.deepcopy(rows),
            metric="objective",
            direction="maximize",
            k=3,
            space=S1,
        )
        self.assertEqual(
            [entry["difference"] for entry in document["followed"]], [0.0, 0.0, 0.0]
        )
        self.assertEqual(
            [entry["improved"] for entry in document["followed"]], [False, False, False]
        )
        self.assertEqual(document["pool"]["size"], 3)
        self.assertEqual(document["statistics"]["spearman"], 1.0)
        self.assertEqual(
            document["statistics"]["spearman_terms"],
            {"xy": [6, 1], "xx": [6, 1], "yy": [6, 1]},
        )

    def test_a_source_passed_twice_with_ancestor_rows_refuses(self):
        """Catch accepting overlapping ancestors when both sources are identical."""
        result = result_of(
            [make_row(0, "ok", 1.0, "A")], [make_row(20, "ok", 2.0, "B")]
        )
        error = self.refusal(
            copy.deepcopy(result),
            copy.deepcopy(result),
            metric="objective",
            direction="maximize",
            k=1,
            space=S1,
        )
        self.assertRefused(error, "holdout_ancestor_rows", ids=((1, 0),))


if __name__ == "__main__":
    unittest.main()
