"""Tests of the post hoc Pareto front (``pineforge_hpo.pareto``).

Drafts cited: ``methods-pareto-contract.proposed.md`` (P-draft: clause 3.3 is the
eligibility table, 3.4 dominance and front, 3.5 the result, 3.7 the refusals, 3.8 the
resource rule, 3.9 what is not claimed; 4.1 holds the worked fixtures PF1-PF4) and its
pin (defaults P1-P12).

The expected values of PF1-PF4 are the draft's. Each was re-derived by hand before it
was relied on, and the derivation is written in the docstring of its test; all four
reproduced the draft's numbers exactly. Nothing in this file was executed by its
author. The one value that cannot be derived by hand is a SHA-256: ``input_sha256`` is
recomputed here from the recipe of clause 3.5 (never copied from a run) and compared.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
import pickle
import random
import unittest

from pineforge_hpo import pareto
from pineforge_hpo.pareto import (
    ALGORITHM,
    CODES,
    CONTRACT,
    DIRECTIONS,
    INTERPRETATION,
    ParetoError,
    pareto_front,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pareto"

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
RESULT_KEYS = {
    "contract",
    "status",
    "axes",
    "rows_read",
    "rows_eligible",
    "rows_excluded",
    "excluded",
    "pareto_front",
    "n_front",
    "n_distinct_vectors_on_front",
    "algorithm",
    "input_sha256",
    "interpretation",
}
COUNTERS = {
    "input_bars_processed": 1000,
    "script_bars_processed": 1000,
    "input_tf_seconds": 3600,
    "script_tf_seconds": 3600,
}

# Clause 3.9 (a)-(g) of the draft, typed again here so that a drift in the module shows.
FIXED_ITEMS = (
    "(a) No Pareto sampler and no multi-objective search (P-F8).",
    "(b) The front is over the trials that a scalar-objective study chose to evaluate: "
    "TPE learns from one value and abandons constraint-violating trials "
    "(study-spec.md:765-767, 790-794), so the sampler never searched the other axes. "
    "The front is the front of the evaluated rows, not the true front, and its extent "
    "depends on the sampler, the seed and the budget.",
    "(c) A row excluded for a missing value, or for violating a constraint, may belong "
    "to the true front.",
    "(d) In-sample: no holdout (study-spec.md:376-377).",
    "(e) No claim of spread, diversity, optimality or hypervolume; equal vectors are "
    "not merged.",
    "(f) Directions are the caller's; the module does not know a metric's sign "
    "convention (P-F5).",
    "(g) Rows of one lineage may come from different scoring contexts that the space "
    "hash cannot detect (P-F13); the clause 2(e) checks are necessary, not sufficient.",
)
FIXED_TEXT = " ".join(FIXED_ITEMS)

PF1_EXPECTED = {
    "contract": "pineforge-hpo-pareto-front/v1",
    "status": "ok",
    "axes": AXES,
    "rows_read": 10,
    "rows_eligible": 7,
    "rows_excluded": 3,
    "excluded": {
        "metric_unavailable": {"count": 1, "ids": [8], "axes": [DRAWDOWN]},
        "status_constraint_violation": {"count": 1, "ids": [6]},
        "status_engine_error": {"count": 1, "ids": [7]},
    },
    "pareto_front": [0, 1, 2, 4, 9],
    "n_front": 5,
    "n_distinct_vectors_on_front": 4,
    "algorithm": "exact_all_pairs_v1",
    "interpretation": FIXED_TEXT,
}
PF1_COUNTS = {0: 0, 1: 0, 2: 0, 3: 1, 4: 0, 5: 1, 9: 0}


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_document(name):
    return json.loads(fixture_text(name))


def fixture_rows(name):
    return [json.loads(line) for line in fixture_text(name).split("\n") if line.strip()]


def make_row(trial_id, status="ok", metrics=None, **extra):
    """A valid row; ``metrics`` None leaves the member out. The objective is inert."""
    scored = status in ("ok", "constraint_violation")
    row = {
        "trial_id": trial_id,
        "status": status,
        "feasible": status == "ok",
        "objective": 1.0 if scored else None,
    }
    if metrics is not None:
        row["metrics"] = metrics
    row.update(extra)
    return row


def vector_rows(vectors, names):
    return [
        make_row(index, "ok", dict(zip(names, vector)))
        for index, vector in enumerate(vectors)
    ]


def named_axes(*directions):
    return [
        {"name": "abc"[index], "direction": direction}
        for index, direction in enumerate(directions)
    ]


def reference_digest(rows, names):
    """The input_sha256 of clause 3.5, recomputed from raw rows.

    Per row: SHA-256 of the compact, key-sorted JSON of [id, status, {axis: float or
    null}]; then SHA-256 of the sorted raw digests, joined.
    """
    digests = []
    for raw in rows:
        metrics = raw.get("metrics") or {}
        cells = {}
        for name in names:
            value = metrics.get(name)
            cells[name] = None if value is None else float(value)
        text = json.dumps(
            [raw["trial_id"], raw["status"], cells],
            sort_keys=True,
            separators=(",", ":"),
        )
        digests.append(hashlib.sha256(text.encode("utf-8")).digest())
    return hashlib.sha256(b"".join(sorted(digests))).hexdigest()


def brute_force(rows, axes):
    """An independent oracle: plain scans over raw rows, no sign flips, no pairing.

    Returns (unknown axis names, front ids, domination counts by id, number of distinct
    front vectors, exclusions as {reason: {count, ids[, axes]}}).
    """
    names = [axis["name"] for axis in axes]
    recorded = {name for raw in rows for name in raw.get("metrics", {})}
    unknown = [name for name in names if name not in recorded]
    pool = {}
    reasons = {}
    involved = {"metric_not_recorded": set(), "metric_unavailable": set()}
    for raw in rows:
        trial_id = raw["trial_id"]
        if raw["status"] != "ok":
            reasons.setdefault("status_" + raw["status"], []).append(trial_id)
            continue
        metrics = raw.get("metrics", {})
        missing = [name for name in names if name not in metrics]
        nulls = [name for name in names if name in metrics and metrics[name] is None]
        if missing:
            reasons.setdefault("metric_not_recorded", []).append(trial_id)
            involved["metric_not_recorded"].update(missing)
        elif nulls:
            reasons.setdefault("metric_unavailable", []).append(trial_id)
            involved["metric_unavailable"].update(nulls)
        else:
            pool[trial_id] = [metrics[name] for name in names]

    def beats(left, right):
        no_worse = True
        better = False
        for one, other, axis in zip(left, right, axes):
            if axis["direction"] == "maximize":
                no_worse = no_worse and one >= other
                better = better or one > other
            else:
                no_worse = no_worse and one <= other
                better = better or one < other
        return no_worse and better

    counts = {
        trial_id: sum(
            1 for other in pool if other != trial_id and beats(pool[other], pool[trial_id])
        )
        for trial_id in sorted(pool)
    }
    front = [trial_id for trial_id, count in counts.items() if count == 0]
    distinct = len({tuple(pool[trial_id]) for trial_id in front})
    summary = {}
    for reason, ids in reasons.items():
        entry = {"count": len(ids), "ids": sorted(ids)}
        if reason in involved:
            entry["axes"] = [name for name in names if name in involved[reason]]
        summary[reason] = entry
    return unknown, front, counts, distinct, summary


class ParetoTestCase(unittest.TestCase):
    def refusal(self, source, axes, **options):
        """Call pareto_front and return the ParetoError it must raise."""
        with self.assertRaises(ParetoError) as caught:
            pareto_front(source, axes, **options)
        return caught.exception

    def assertRefused(self, error, code, ids=(), names=()):
        """The code, offenders and refused document of one refusal (clause 3.7)."""
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
        self.assertNotIn("pareto_front", document)


class ConstantsTests(ParetoTestCase):
    def test_contract_words(self):
        """Rule: contract id, algorithm name and direction words (draft 3.5, 3.1)."""
        self.assertEqual(CONTRACT, "pineforge-hpo-pareto-front/v1")
        self.assertEqual(ALGORITHM, "exact_all_pairs_v1")
        self.assertEqual(DIRECTIONS, ("maximize", "minimize"))

    def test_the_eight_refusal_codes(self):
        """Rule: eight module-local codes, none a catalog code (draft 3.7; pin P11)."""
        self.assertEqual(
            CODES,
            (
                "pareto_input_invalid",
                "pareto_rows_incomplete",
                "pareto_context_mismatch",
                "pareto_parameter_missing",
                "pareto_parameter_invalid",
                "pareto_axis_unknown",
                "pareto_no_eligible_rows",
                "pareto_resource_exceeded",
            ),
        )
        self.assertEqual(len(set(CODES)), 8)
        self.assertTrue(all(code.startswith("pareto_") for code in CODES))

    def test_interpretation_is_the_fixed_text_of_clause_3_9(self):
        """Rule: the interpretation is the fixed words (a)-(g) of clause 3.9 (pin 3.4)."""
        self.assertEqual(INTERPRETATION, FIXED_TEXT)
        self.assertEqual(len(FIXED_ITEMS), 7)
        positions = []
        for letter, item in zip("abcdefg", FIXED_ITEMS):
            self.assertTrue(item.startswith(f"({letter}) "), letter)
            self.assertIn(item, INTERPRETATION)
            positions.append(INTERPRETATION.index(item))
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(positions[0], 0)

    def test_module_imports_only_the_standard_library_and_the_reader(self):
        """Rule: stdlib only (pyproject.toml:28); the shared reader is the one local import."""
        tree = ast.parse(Path(pareto.__file__).read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add("." * node.level + (node.module or ""))
        self.assertEqual(
            found, {"__future__", "hashlib", "json", "math", "typing", "._rows"}
        )


class ParetoErrorTests(ParetoTestCase):
    def test_fields_are_kept_and_ids_are_ascending(self):
        """Positive control: the four fields come back as given, ids sorted."""
        error = ParetoError("pareto_input_invalid", "one sentence", [5, 2, 9], ["b", "a"])
        self.assertEqual(error.code, "pareto_input_invalid")
        self.assertEqual(error.detail, "one sentence")
        self.assertEqual(error.ids, (2, 5, 9))
        self.assertEqual(error.names, ("b", "a"))
        self.assertEqual(str(error), "pareto_input_invalid: one sentence")
        empty = ParetoError("pareto_no_eligible_rows", "nothing left")
        self.assertEqual((empty.ids, empty.names), ((), ()))

    def test_unknown_code_is_a_programming_error(self):
        """Rule: only the eight codes of clause 3.7 can be raised."""
        with self.assertRaises(ValueError):
            ParetoError("pareto_made_up", "a sentence")
        with self.assertRaises(ValueError):
            ParetoError("hpo_made_up", "a sentence")

    def test_error_survives_pickling(self):
        """Rule: the error can cross a process boundary with its fields intact."""
        error = ParetoError("pareto_axis_unknown", "missing", [3, 1], ["m"])
        # Round trip of an object built here; no untrusted bytes are loaded.
        again = pickle.loads(pickle.dumps(error))
        self.assertIsInstance(again, ParetoError)
        self.assertEqual(
            (again.code, again.detail, again.ids, again.names),
            ("pareto_axis_unknown", "missing", (1, 3), ("m",)),
        )

    def test_refused_document_has_no_front(self):
        """Rule: a refusal is status refused with code and detail, no front (draft 3.5)."""
        error = ParetoError("pareto_input_invalid", "a detail", [4, 2], ["m"])
        self.assertEqual(
            error.document(),
            {
                "contract": "pineforge-hpo-pareto-front/v1",
                "status": "refused",
                "refusal": {
                    "code": "pareto_input_invalid",
                    "detail": "a detail",
                    "ids": [2, 4],
                    "names": ["m"],
                },
            },
        )


class WorkedFixtureTests(ParetoTestCase):
    def test_pf1_infeasible_error_missing_and_tie(self):
        """PF1 (draft 4.1), axes profit maximize and drawdown minimize.

        Hand derivation. Eligible (status ok, both axes numbers): ids 0 (10,5), 1 (12,8),
        2 (8,3), 3 (9,6), 4 (12,8), 5 (7,3), 9 (6,2). Excluded: 6 constraint_violation,
        7 engine_error, 8 ok with a null drawdown. Row 6 (20,1) would dominate all seven
        eligible rows, which is why it must not compete. Dominations among eligible
        rows: 0 dominates 3 (10>=9, 5<=6, strict in both); 2 dominates 5 (8>=7, 3<=3,
        strict in profit); every other pair is incomparable or equal (1 and 4 are equal).
        Front [0, 1, 2, 4, 9], 5 rows, 4 distinct vectors ((10,5), (12,8), (8,3), (6,2)).
        """
        result = pareto_front(FIXTURES / "pf1.json", AXES)
        self.assertEqual(set(result), RESULT_KEYS)
        digest = result.pop("input_sha256")
        self.assertEqual(result, PF1_EXPECTED)
        rows = fixture_document("pf1.json")["trials"]
        self.assertEqual(digest, reference_digest(rows, [PROFIT, DRAWDOWN]))
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, digest.lower())

    def test_pf1_with_domination_counts(self):
        """PF1 counts: 3 is dominated by 0 only, 5 by 2 only, the other five by nobody."""
        result = pareto_front(FIXTURES / "pf1.json", AXES, with_domination_counts=True)
        self.assertEqual(set(result), RESULT_KEYS | {"domination_counts"})
        self.assertEqual(result["domination_counts"], PF1_COUNTS)
        self.assertEqual(list(result["domination_counts"]), [0, 1, 2, 3, 4, 5, 9])
        result.pop("domination_counts")
        result.pop("input_sha256")
        self.assertEqual(result, PF1_EXPECTED)

    def test_pf2_three_axes_with_ties(self):
        """PF2 (draft 4.1): profit max, profit factor max, drawdown min; all rows ok.

        Hand derivation, rows (A, B, C): 0 (3,3,5), 1 (3,3,4), 2 (4,1,9), 3 (1,4,2),
        4 (3,3,4), 5 (2,2,6). Rows 1 and 4 each dominate 0 (C 4<=5, equal elsewhere) and
        5; row 0 dominates 5 (3>=2, 3>=2, 5<=6); 2 wins A and 3 wins B and C, so nobody
        dominates them. Dominators: row 0 by {1, 4}, row 5 by {0, 1, 4}. Front
        [1, 2, 3, 4]; distinct front vectors (3,3,4), (4,1,9), (1,4,2).
        """
        result = pareto_front(FIXTURES / "pf2.json", AXES3, with_domination_counts=True)
        self.assertEqual(result["pareto_front"], [1, 2, 3, 4])
        self.assertEqual(result["n_front"], 4)
        self.assertEqual(result["n_distinct_vectors_on_front"], 3)
        self.assertEqual(result["rows_read"], 6)
        self.assertEqual(result["rows_eligible"], 6)
        self.assertEqual(result["rows_excluded"], 0)
        self.assertEqual(result["excluded"], {})
        self.assertEqual(result["domination_counts"], {0: 2, 1: 0, 2: 0, 3: 0, 4: 0, 5: 3})
        self.assertEqual(result["axes"], AXES3)
        rows = fixture_document("pf2.json")["trials"]
        self.assertEqual(
            result["input_sha256"], reference_digest(rows, [PROFIT, FACTOR, DRAWDOWN])
        )

    def test_pf3_statuses_absent_metrics_and_a_single_member_front(self):
        """PF3 (draft 4.1): axes of PF1.

        Hand derivation. 0 ok (1,2) and 3 ok (2,1) are eligible. Excluded: 1 ok with no
        metrics object and 6 ok with no drawdown key (both metric_not_recorded; row 1
        lacks both axes, row 6 the second, so both axes are named), 2 pruned, 4
        objective_error, 5 partial (status reasons). Row 3 dominates row 0 (2>=1, 1<=2,
        strict in both). Front [3], one distinct vector. The excluded pruned row
        (100, 0.5) would have dominated both, which is why prefix values must not
        compete. The draft's "partial, metrics null" is written as an object of null
        values, because the shared reader refuses a null metrics member.
        """
        result = pareto_front(FIXTURES / "pf3.json", AXES, with_domination_counts=True)
        self.assertEqual(result["rows_read"], 7)
        self.assertEqual(result["rows_eligible"], 2)
        self.assertEqual(result["rows_excluded"], 5)
        self.assertEqual(
            result["excluded"],
            {
                "metric_not_recorded": {
                    "count": 2,
                    "ids": [1, 6],
                    "axes": [PROFIT, DRAWDOWN],
                },
                "status_objective_error": {"count": 1, "ids": [4]},
                "status_partial": {"count": 1, "ids": [5]},
                "status_pruned": {"count": 1, "ids": [2]},
            },
        )
        self.assertEqual(result["pareto_front"], [3])
        self.assertEqual(result["n_front"], 1)
        self.assertEqual(result["n_distinct_vectors_on_front"], 1)
        self.assertEqual(result["domination_counts"], {0: 1, 3: 0})
        rows = fixture_document("pf3.json")["trials"]
        self.assertEqual(
            result["input_sha256"], reference_digest(rows, [PROFIT, DRAWDOWN])
        )

    def test_pf4_invariance_rows_reversed_and_axes_reversed(self):
        """PF4 (draft 4.1): PF1 with rows reversed and the axes listed the other way.

        The front, counts, exclusions and digest are those of PF1; only the echoed axes
        differ. (PF1 names one axis in its exclusions, so their axes lists agree too.)
        """
        base = pareto_front(FIXTURES / "pf1.json", AXES, with_domination_counts=True)
        document = fixture_document("pf1.json")
        document["trials"].reverse()
        reversed_axes = list(reversed(AXES))
        moved = pareto_front(document, reversed_axes, with_domination_counts=True)
        self.assertEqual(moved["axes"], reversed_axes)
        self.assertNotEqual(moved["axes"], base["axes"])
        self.assertEqual(moved["pareto_front"], [0, 1, 2, 4, 9])
        self.assertEqual(moved["domination_counts"], PF1_COUNTS)
        self.assertEqual(moved["excluded"], PF1_EXPECTED["excluded"])
        moved.pop("axes")
        base.pop("axes")
        self.assertEqual(moved, base)

    def test_pf1_files_are_pf1_with_one_change(self):
        """The NaN and overflow fixtures are the JSONL of PF1 with row 0's profit changed."""
        plain = fixture_text("pf1.jsonl")
        old = '"metrics.all.net_profit_pct": 10.0,'
        self.assertEqual(plain.count(old), 1)
        self.assertEqual(
            fixture_text("pf1_nan.jsonl"),
            plain.replace(old, '"metrics.all.net_profit_pct": NaN,', 1),
        )
        self.assertEqual(
            fixture_text("pf1_overflow.jsonl"),
            plain.replace(old, '"metrics.all.net_profit_pct": 1e999,', 1),
        )
        self.assertEqual(fixture_rows("pf1.jsonl"), fixture_document("pf1.json")["trials"])


class DominanceTests(ParetoTestCase):
    def test_strict_improvement_needed_and_ties_stay(self):
        """Rule: dominance needs one strict axis (draft 3.4); equal vectors both stay.

        Axes a, b maximize. A (1,1), B (1,2), C (2,1), D (1,1). B and C each dominate A
        and D (no worse, strict on one axis); B and C are incomparable; A and D are equal.
        Dominators: A {B, C}, D {B, C}.
        """
        rows = vector_rows([(1, 1), (1, 2), (2, 1), (1, 1)], "ab")
        result = pareto_front(
            rows, named_axes("maximize", "maximize"), with_domination_counts=True
        )
        self.assertEqual(result["pareto_front"], [1, 2])
        self.assertEqual(result["domination_counts"], {0: 2, 1: 0, 2: 0, 3: 2})
        self.assertEqual(result["n_distinct_vectors_on_front"], 2)

    def test_equal_vectors_do_not_dominate_each_other(self):
        """Rule: two equal vectors both stay on the front, counted once as a vector."""
        rows = vector_rows([(5, 5), (5, 5)], "ab")
        result = pareto_front(
            rows, named_axes("maximize", "maximize"), with_domination_counts=True
        )
        self.assertEqual(result["pareto_front"], [0, 1])
        self.assertEqual(result["n_front"], 2)
        self.assertEqual(result["n_distinct_vectors_on_front"], 1)
        self.assertEqual(result["domination_counts"], {0: 0, 1: 0})

    def test_minimize_on_every_axis(self):
        """Rule: smaller is better under minimize. (1,2), (2,1), (2,2): both of the first
        two dominate (2,2) (no larger, strictly smaller on one axis) and are incomparable."""
        rows = vector_rows([(1, 2), (2, 1), (2, 2)], "ab")
        result = pareto_front(
            rows, named_axes("minimize", "minimize"), with_domination_counts=True
        )
        self.assertEqual(result["pareto_front"], [0, 1])
        self.assertEqual(result["domination_counts"], {0: 0, 1: 0, 2: 2})

    def test_mixed_directions(self):
        """Rule: each axis has its own direction. Axes a maximize, b minimize; rows
        0 (1,1), 1 (2,1), 2 (1,2). Row 1 dominates 0 (2>=1, 1<=1, strict in a) and 2;
        row 0 dominates 2 (1>=1, 1<=2, strict in b). Dominators: 0 {1}, 2 {0, 1}."""
        rows = vector_rows([(1, 1), (2, 1), (1, 2)], "ab")
        result = pareto_front(
            rows, named_axes("maximize", "minimize"), with_domination_counts=True
        )
        self.assertEqual(result["pareto_front"], [1])
        self.assertEqual(result["domination_counts"], {0: 1, 1: 0, 2: 2})

    def test_negative_zero_equals_positive_zero(self):
        """Rule: -0.0 equals +0.0 (pin P5). Rows 0 (-0.0, 1.0) and 1 (0.0, 1.0) are equal,
        so neither dominates the other; row 2 (0.0, 2.0) dominates both (0.0 >= -0.0)."""
        axes = named_axes("maximize", "maximize")
        rows = vector_rows([(-0.0, 1.0), (0.0, 1.0), (0.0, 2.0)], "ab")
        result = pareto_front(rows, axes, with_domination_counts=True)
        self.assertEqual(result["pareto_front"], [2])
        self.assertEqual(result["domination_counts"], {0: 1, 1: 1, 2: 0})
        result = pareto_front(rows[:2], axes, with_domination_counts=True)
        self.assertEqual(result["pareto_front"], [0, 1])
        self.assertEqual(result["n_distinct_vectors_on_front"], 1)
        self.assertEqual(result["domination_counts"], {0: 0, 1: 0})
        rows = vector_rows([(0.0, -0.0), (0.0, 0.0)], "ab")
        result = pareto_front(rows, named_axes("minimize", "minimize"))
        self.assertEqual(result["pareto_front"], [0, 1])
        self.assertEqual(result["n_distinct_vectors_on_front"], 1)

    def test_comparison_is_exact_with_no_tolerance(self):
        """Rule: exact binary64, no tolerance (pin P5). Adjacent doubles differ, so
        (1.0 + 2**-52, 0.0) dominates (1.0, 0.0); so does (1e-300, 0.0) over (0.0, 0.0)."""
        axes = named_axes("maximize", "maximize")
        rows = vector_rows([(1.0, 0.0), (1.0 + 2.0**-52, 0.0)], "ab")
        result = pareto_front(rows, axes, with_domination_counts=True)
        self.assertEqual(result["pareto_front"], [1])
        self.assertEqual(result["domination_counts"], {0: 1, 1: 0})
        rows = vector_rows([(0.0, 0.0), (1e-300, 0.0)], "ab")
        self.assertEqual(pareto_front(rows, axes)["pareto_front"], [1])

    def test_integers_and_floats_compare_by_value(self):
        """Rule: a value is converted with float() (draft 3.2(b)): 3 equals 3.0, and the
        integers 2**53 and 2**53 + 1 both convert to 9007199254740992.0, so they are equal."""
        axes = named_axes("maximize", "maximize")
        result = pareto_front(vector_rows([(3, 3), (3.0, 3.0)], "ab"), axes)
        self.assertEqual(result["pareto_front"], [0, 1])
        self.assertEqual(result["n_distinct_vectors_on_front"], 1)
        result = pareto_front(vector_rows([(2**53, 0), (2**53 + 1, 0)], "ab"), axes)
        self.assertEqual(result["pareto_front"], [0, 1])
        self.assertEqual(result["n_distinct_vectors_on_front"], 1)

    def test_one_eligible_row_is_its_own_front(self):
        """Positive control: a single eligible row is the front, with count 0."""
        rows = [make_row(7, "ok", {PROFIT: 1.0, DRAWDOWN: 2.0}), make_row(9, "pruned", {})]
        result = pareto_front(rows, AXES, with_domination_counts=True)
        self.assertEqual(result["pareto_front"], [7])
        self.assertEqual(result["n_front"], 1)
        self.assertEqual(result["n_distinct_vectors_on_front"], 1)
        self.assertEqual(result["domination_counts"], {7: 0})
        self.assertEqual(result["excluded"], {"status_pruned": {"count": 1, "ids": [9]}})

    def test_duplicate_parameter_vectors_keep_every_row(self):
        """Rule: rows with equal parameters and equal metrics all stay (pin P6)."""
        rows = [
            make_row(index, "ok", {PROFIT: 5.0, DRAWDOWN: 1.0}, parameters={"x": 1})
            for index in (3, 8, 12)
        ]
        result = pareto_front(rows, AXES)
        self.assertEqual(result["pareto_front"], [3, 8, 12])
        self.assertEqual(result["n_front"], 3)
        self.assertEqual(result["n_distinct_vectors_on_front"], 1)

    def test_domination_counts_are_ordered_by_numeric_id(self):
        """Rule: the counts object runs over eligible ids ascending (draft 3.5)."""
        rows = [
            make_row(100, "ok", {PROFIT: 1.0, DRAWDOWN: 1.0}),
            make_row(2, "ok", {PROFIT: 2.0, DRAWDOWN: 1.0}),
            make_row(10, "ok", {PROFIT: 3.0, DRAWDOWN: 1.0}),
        ]
        result = pareto_front(rows, AXES, with_domination_counts=True)
        self.assertEqual(list(result["domination_counts"]), [2, 10, 100])
        self.assertEqual(result["domination_counts"], {2: 1, 10: 0, 100: 2})
        self.assertEqual(result["pareto_front"], [10])


class EligibilityTests(ParetoTestCase):
    def test_every_non_ok_status_is_excluded_with_its_own_reason(self):
        """Rule: only ok rows compete; the eight other statuses give status_<status>."""
        statuses = (
            "constraint_violation",
            "objective_error",
            "constraint_error",
            "engine_error",
            "trial_error",
            "trial_timeout",
            "pruned",
            "partial",
        )
        good = {PROFIT: 1.0, DRAWDOWN: 1.0}
        better = {PROFIT: 9.0, DRAWDOWN: 0.5}
        rows = [make_row(0, "ok", good)]
        rows += [make_row(index + 1, status, better) for index, status in enumerate(statuses)]
        result = pareto_front(rows, AXES)
        self.assertEqual(result["pareto_front"], [0])
        self.assertEqual(result["rows_eligible"], 1)
        self.assertEqual(result["rows_excluded"], 8)
        self.assertEqual(
            result["excluded"],
            {
                f"status_{status}": {"count": 1, "ids": [index + 1]}
                for index, status in enumerate(statuses)
            },
        )

    def test_absent_and_null_values_are_excluded_not_refused(self):
        """Rule: absent metrics, absent key (not recorded) and null (unavailable) exclude."""
        rows = [
            make_row(0, "ok", {PROFIT: 1.0, DRAWDOWN: 1.0}),
            make_row(1, "ok"),
            make_row(2, "ok", {PROFIT: 5.0}),
            make_row(3, "ok", {PROFIT: None, DRAWDOWN: 1.0}),
            make_row(4, "ok", {PROFIT: 5.0, DRAWDOWN: None}),
            make_row(5, "ok", {PROFIT: None, DRAWDOWN: None}),
            make_row(6, "ok", {DRAWDOWN: None}),
        ]
        result = pareto_front(rows, AXES)
        self.assertEqual(result["pareto_front"], [0])
        self.assertEqual(
            result["excluded"],
            {
                "metric_not_recorded": {
                    "count": 3,
                    "ids": [1, 2, 6],
                    "axes": [PROFIT, DRAWDOWN],
                },
                "metric_unavailable": {
                    "count": 3,
                    "ids": [3, 4, 5],
                    "axes": [PROFIT, DRAWDOWN],
                },
            },
        )

    def test_not_recorded_comes_before_unavailable_for_one_row(self):
        """Rule: reasons apply in table order. A row lacking one axis and null on the other
        is metric_not_recorded and names only the absent axis."""
        rows = [
            make_row(0, "ok", {PROFIT: 1.0, DRAWDOWN: 1.0}),
            make_row(1, "ok", {DRAWDOWN: None}),
        ]
        result = pareto_front(rows, AXES)
        self.assertEqual(
            result["excluded"],
            {"metric_not_recorded": {"count": 1, "ids": [1], "axes": [PROFIT]}},
        )

    def test_extra_recorded_metrics_are_ignored(self):
        """Rule: only the requested axes are read; other recorded metrics change nothing."""
        document = fixture_document("pf1.json")
        expected = pareto_front(document, AXES)
        for row in document["trials"]:
            row["metrics"][FACTOR] = 1.5
            row["metrics"]["other.thing"] = "text"
        self.assertEqual(pareto_front(document, AXES), expected)

    def test_a_non_ok_row_with_odd_value_types_is_only_excluded(self):
        """Rule: only an ok row's axis value is type-checked (pin P4); a pruned row may hold
        a string or an array there and is excluded by its status."""
        rows = [
            make_row(0, "ok", {PROFIT: 1.0, DRAWDOWN: 1.0}),
            make_row(1, "pruned", {PROFIT: "x", DRAWDOWN: [1]}),
        ]
        result = pareto_front(rows, AXES)
        self.assertEqual(result["pareto_front"], [0])
        self.assertEqual(result["excluded"], {"status_pruned": {"count": 1, "ids": [1]}})


class ParameterRefusalTests(ParetoTestCase):
    def setUp(self):
        self.source = FIXTURES / "pf1.json"

    def test_control_the_unchanged_call_succeeds(self):
        """Forced-failure control: the call the refusals below start from is accepted."""
        self.assertEqual(pareto_front(self.source, AXES)["status"], "ok")

    def test_axes_omitted_is_missing(self):
        """Refusal fixture: axes omitted (draft 4.1) is pareto_parameter_missing."""
        for axes in (None, [], ()):
            with self.subTest(axes=axes):
                error = self.refusal(self.source, axes)
                self.assertRefused(error, "pareto_parameter_missing")

    def test_direction_omitted_is_missing(self):
        """Refusal fixture: a direction omitted is pareto_parameter_missing, no default."""
        axes = [AXES[0], {"name": DRAWDOWN}]
        self.assertRefused(
            self.refusal(self.source, axes), "pareto_parameter_missing", names=(DRAWDOWN,)
        )
        axes = [AXES[0], {"name": DRAWDOWN, "direction": None}]
        self.assertRefused(
            self.refusal(self.source, axes), "pareto_parameter_missing", names=(DRAWDOWN,)
        )

    def test_name_omitted_is_missing(self):
        """Rule: an axis without a name is named by its position."""
        axes = [AXES[0], {"direction": "minimize"}]
        self.assertRefused(
            self.refusal(self.source, axes), "pareto_parameter_missing", names=("axes[1]",)
        )

    def test_missing_wins_over_invalid(self):
        """Rule: missing is judged before invalid, whatever the order of the axes."""
        bad_word = {"name": PROFIT, "direction": "up"}
        no_direction = {"name": DRAWDOWN}
        for axes in ([bad_word, no_direction], [no_direction, bad_word]):
            error = self.refusal(self.source, axes)
            self.assertRefused(error, "pareto_parameter_missing", names=(DRAWDOWN,))

    def test_one_axis_is_invalid(self):
        """Refusal fixture: one axis only is pareto_parameter_invalid."""
        error = self.refusal(self.source, [AXES[0]])
        self.assertRefused(error, "pareto_parameter_invalid")

    def test_repeated_axis_is_invalid(self):
        """Refusal fixture: a repeated axis name, even with another direction."""
        for second in ("maximize", "minimize"):
            axes = [AXES[0], {"name": PROFIT, "direction": second}]
            with self.subTest(second=second):
                error = self.refusal(self.source, axes)
                self.assertRefused(error, "pareto_parameter_invalid", names=(PROFIT,))

    def test_direction_words_are_exact(self):
        """Refusal fixture: direction max (and every other word) is invalid, no inference."""
        for word in ("max", "min", "MAXIMIZE", "Maximize", "maximise", "", 1, True, ["maximize"]):
            axes = [AXES[0], {"name": DRAWDOWN, "direction": word}]
            with self.subTest(word=word):
                error = self.refusal(self.source, axes)
                self.assertRefused(error, "pareto_parameter_invalid", names=(DRAWDOWN,))

    def test_malformed_axes_are_invalid(self):
        """Rule: axes is a list of objects with a string name and nothing else."""
        cases = (
            {"name": PROFIT, "direction": "maximize"},
            "abc",
            5,
            ["a", "b"],
            [AXES[0], ["name", "direction"]],
            [AXES[0], {"name": 5, "direction": "minimize"}],
        )
        for axes in cases:
            with self.subTest(axes=axes):
                error = self.refusal(self.source, axes)
                self.assertRefused(error, "pareto_parameter_invalid")
        extra = [AXES[0], {"name": DRAWDOWN, "direction": "minimize", "weight": 2}]
        self.assertRefused(
            self.refusal(self.source, extra), "pareto_parameter_invalid", names=("weight",)
        )

    def test_axes_may_be_a_tuple(self):
        """Positive control: a tuple of axes is read like a list."""
        self.assertEqual(
            pareto_front(self.source, tuple(AXES))["pareto_front"], [0, 1, 2, 4, 9]
        )

    def test_domination_counts_flag_must_be_a_bool(self):
        """Rule: with_domination_counts is true or false."""
        for flag in (1, 0, "yes", None, [True]):
            with self.subTest(flag=flag):
                error = self.refusal(self.source, AXES, with_domination_counts=flag)
                self.assertRefused(error, "pareto_parameter_invalid")

    def test_max_work_must_be_a_positive_integer_or_none(self):
        """Rule: max_work invalid is pareto_parameter_invalid (draft 3.7)."""
        for limit in (0, -1, True, False, 1.5, 31.0, "31", [31]):
            with self.subTest(limit=limit):
                error = self.refusal(self.source, AXES, max_work=limit)
                self.assertRefused(error, "pareto_parameter_invalid")
        self.assertEqual(pareto_front(self.source, AXES, max_work=None)["status"], "ok")


class SourceRefusalTests(ParetoTestCase):
    def test_best_k_result_is_incomplete(self):
        """Refusal fixture: a result written with trials_out best-k (draft 4.1)."""
        document = fixture_document("pf1.json")
        self.assertEqual(pareto_front(document, AXES)["status"], "ok")
        document["trials_out"] = "best-k"
        self.assertRefused(self.refusal(document, AXES), "pareto_rows_incomplete")

    def test_counts_that_disagree_with_the_rows_are_incomplete(self):
        """Rule: trials_completed or warm_start.trials disagreeing with the rows (draft 3.7)."""
        document = fixture_document("pf1.json")
        document["trials_completed"] = 9
        self.assertRefused(self.refusal(document, AXES), "pareto_rows_incomplete")
        document = fixture_document("pf1.json")
        document["warm_start"] = {"trials": 2}
        self.assertRefused(self.refusal(document, AXES), "pareto_rows_incomplete")

    def test_missing_file_and_wrong_source_types_are_invalid_input(self):
        """Rule: a source that cannot be read is pareto_input_invalid, never a stray error."""
        self.assertEqual(
            self.refusal(FIXTURES / "missing.json", AXES).code, "pareto_input_invalid"
        )
        for source in (5, None, ("a",), 1.5):
            with self.subTest(source=source):
                self.assertEqual(self.refusal(source, AXES).code, "pareto_input_invalid")

    def test_duplicate_trial_id_is_invalid_input(self):
        """Refusal fixture: two rows with id 3 (draft 4.1)."""
        document = fixture_document("pf1.json")
        document["trials"][9]["trial_id"] = 3
        self.assertRefused(self.refusal(document, AXES), "pareto_input_invalid", ids=(3,))

    def test_malformed_rows_are_invalid_input(self):
        """Rule: unknown status, feasible disagreeing with status, a null metrics member."""
        document = fixture_document("pf1.json")
        document["trials"][2]["feasible"] = False
        self.assertRefused(self.refusal(document, AXES), "pareto_input_invalid", ids=(2,))
        document = fixture_document("pf1.json")
        document["trials"][5]["status"] = "weird"
        self.assertRefused(self.refusal(document, AXES), "pareto_input_invalid", ids=(5,))
        document = fixture_document("pf1.json")
        document["trials"][1]["metrics"] = None
        self.assertRefused(self.refusal(document, AXES), "pareto_input_invalid", ids=(1,))

    def test_nan_token_in_a_file_is_invalid_input(self):
        """Refusal fixture: the token NaN as row 0's profit (draft 4.1; pin P4)."""
        error = self.refusal(FIXTURES / "pf1_nan.jsonl", AXES)
        self.assertEqual(error.code, "pareto_input_invalid")
        self.assertIn("line 1", error.detail)
        self.assertIn("NaN", error.detail)
        self.assertEqual(pareto_front(FIXTURES / "pf1.jsonl", AXES)["status"], "ok")

    def test_overflowing_number_in_a_file_is_invalid_input(self):
        """Refusal fixture: the number 1e999 as row 0's profit (draft 4.1; pin P4)."""
        error = self.refusal(FIXTURES / "pf1_overflow.jsonl", AXES)
        self.assertEqual(error.code, "pareto_input_invalid")
        self.assertIn("line 1", error.detail)
        self.assertIn("overflows", error.detail)


class ValueRefusalTests(ParetoTestCase):
    def test_ok_row_axis_value_of_another_type_is_invalid_input(self):
        """Refusal fixture: row 0's profit true, "10.0", an array, an object, NaN, an
        infinity or an integer beyond binary64 (draft 4.1; pin P4)."""
        values = (
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
        for value in values:
            document = fixture_document("pf1.json")
            document["trials"][0]["metrics"][PROFIT] = value
            with self.subTest(value=repr(value)):
                error = self.refusal(document, AXES)
                self.assertRefused(error, "pareto_input_invalid", ids=(0,), names=(PROFIT,))

    def test_every_offending_row_and_axis_is_named(self):
        """Rule: ids ascending, axis names in request order, over all offending rows."""
        document = fixture_document("pf1.json")
        document["trials"][4]["metrics"][DRAWDOWN] = "8.0"
        document["trials"][0]["metrics"][PROFIT] = True
        error = self.refusal(document, AXES)
        self.assertRefused(error, "pareto_input_invalid", ids=(0, 4), names=(PROFIT, DRAWDOWN))

    def test_a_bad_value_is_refused_even_when_another_axis_is_absent(self):
        """Rule: a bad value on an ok row refuses the call; it is not hidden by an
        exclusion for another axis."""
        rows = [
            make_row(0, "ok", {PROFIT: 1.0, DRAWDOWN: 1.0}),
            make_row(1, "ok", {PROFIT: "x"}),
        ]
        error = self.refusal(rows, AXES)
        self.assertRefused(error, "pareto_input_invalid", ids=(1,), names=(PROFIT,))

    def test_non_finite_number_in_a_non_ok_row_is_refused_too(self):
        """Choice: a non-finite number in an axis cell refuses wherever it is, as the NaN
        token does when read from a file."""
        rows = [
            make_row(0, "ok", {PROFIT: 1.0, DRAWDOWN: 1.0}),
            make_row(1, "pruned", {PROFIT: float("nan"), DRAWDOWN: 1.0}),
        ]
        error = self.refusal(rows, AXES)
        self.assertRefused(error, "pareto_input_invalid", ids=(1,), names=(PROFIT,))

    def test_a_cell_that_json_cannot_hold_is_refused(self):
        """Rule: the digest must be writable; a set in an excluded row's axis cell refuses."""
        rows = [
            make_row(0, "ok", {PROFIT: 1.0, DRAWDOWN: 1.0}),
            make_row(1, "pruned", {PROFIT: {1, 2}, DRAWDOWN: 1.0}),
        ]
        error = self.refusal(rows, AXES)
        self.assertRefused(error, "pareto_input_invalid", ids=(1,))

    def test_big_but_finite_numbers_are_accepted(self):
        """Positive control: 1e308 and a huge but representable integer are numbers."""
        rows = [
            make_row(0, "ok", {PROFIT: 1e308, DRAWDOWN: 1.0}),
            make_row(1, "ok", {PROFIT: 10**300, DRAWDOWN: 1.0}),
        ]
        result = pareto_front(rows, AXES)
        self.assertEqual(result["pareto_front"], [0])


class AxisAndEligibilityRefusalTests(ParetoTestCase):
    def test_misspelled_axis_is_unknown(self):
        """Refusal fixture: an axis spelled metrics.all.net_profit_pc (draft 4.1)."""
        typo = "metrics.all.net_profit_pc"
        axes = [{"name": typo, "direction": "maximize"}, AXES[1]]
        error = self.refusal(FIXTURES / "pf1.json", axes)
        self.assertRefused(error, "pareto_axis_unknown", names=(typo,))
        both = [axes[0], {"name": "nope", "direction": "minimize"}]
        error = self.refusal(FIXTURES / "pf1.json", both)
        self.assertRefused(error, "pareto_axis_unknown", names=(typo, "nope"))

    def test_an_axis_recorded_only_by_an_excluded_row_is_known(self):
        """Rule: unknown means recorded in no row at all. Here the drawdown is recorded by
        a pruned row only, so the call is not axis_unknown but has no eligible row."""
        rows = [
            make_row(0, "ok", {PROFIT: 1.0}),
            make_row(1, "pruned", {PROFIT: 2.0, DRAWDOWN: 1.0}),
        ]
        self.assertRefused(self.refusal(rows, AXES), "pareto_no_eligible_rows")

    def test_every_row_engine_error_has_no_eligible_rows(self):
        """Refusal fixture: every row engine_error (draft 4.1)."""
        document = fixture_document("pf1.json")
        for row in document["trials"]:
            row["status"] = "engine_error"
            row["feasible"] = False
            row["objective"] = None
        error = self.refusal(document, AXES)
        self.assertRefused(error, "pareto_no_eligible_rows")
        self.assertIn("status_engine_error 10", error.detail)

    def test_every_value_unavailable_has_no_eligible_rows(self):
        """Rule: no row remains after exclusions, here all for null values."""
        rows = [make_row(index, "ok", {PROFIT: None, DRAWDOWN: 1.0}) for index in range(3)]
        self.assertRefused(self.refusal(rows, AXES), "pareto_no_eligible_rows")

    def test_a_source_without_rows_has_no_eligible_rows(self):
        """Choice: an empty source is no_eligible_rows, not axis_unknown."""
        for source in ([], {"trials": []}):
            with self.subTest(source=source):
                error = self.refusal(source, AXES)
                self.assertRefused(error, "pareto_no_eligible_rows")
                self.assertIn("no rows", error.detail)


class ContextRefusalTests(ParetoTestCase):
    def test_space_hash_disagreement(self):
        """Refusal fixture: row 4 with another space_hash (draft 4.1). Hand check of the
        reader's rule: the reference is the lowest id, row 0; only row 4 differs."""
        document = fixture_document("pf1.json")
        for row in document["trials"]:
            row["space_hash"] = "a" * 64
        self.assertEqual(pareto_front(document, AXES)["status"], "ok")
        document["trials"][4]["space_hash"] = "b" * 64
        error = self.refusal(document, AXES)
        self.assertRefused(error, "pareto_context_mismatch", ids=(0, 4), names=("space_hash",))

    def test_backtest_counter_disagreement_among_ok_rows(self):
        """Rule: ok rows must agree on the four backtest counters (draft 3.2(e))."""
        document = fixture_document("pf1.json")
        for row in document["trials"]:
            row["backtest"] = dict(COUNTERS)
        document["trials"][7]["backtest"] = dict.fromkeys(COUNTERS, 0)
        self.assertEqual(pareto_front(document, AXES)["status"], "ok")
        document["trials"][3]["backtest"]["input_bars_processed"] = 999
        error = self.refusal(document, AXES)
        self.assertRefused(
            error, "pareto_context_mismatch", ids=(0, 3), names=("input_bars_processed",)
        )

    def test_spaces_are_not_read_or_compared(self):
        """Rule: spaces are not part of this contract; none is required and recorded ones
        may disagree without a refusal."""
        document = fixture_document("pf1.json")
        expected = pareto_front(document, AXES)
        document["space"] = {"parameters": {}}
        document["trials"][0]["space"] = {"parameters": {"x": {"kind": "weird"}}}
        document["trials"][1]["space"] = {"parameters": {}}
        self.assertEqual(pareto_front(document, AXES), expected)


class ResourceTests(ParetoTestCase):
    def test_admission_figure_boundaries(self):
        """Rule: figure = rows read + N(N-1)/2 over N eligible rows; refuse only above a
        supplied max_work (draft 3.8). PF1: 10 + 21 = 31. PF2: 6 + 15 = 21. PF3: 7 + 1 = 8."""
        cases = (
            ("pf1.json", AXES, 31),
            ("pf2.json", AXES3, 21),
            ("pf3.json", AXES, 8),
        )
        for name, axes, figure in cases:
            with self.subTest(name=name):
                source = FIXTURES / name
                self.assertEqual(pareto_front(source, axes, max_work=figure)["status"], "ok")
                self.assertEqual(
                    pareto_front(source, axes, max_work=figure + 1000)["status"], "ok"
                )
                error = self.refusal(source, axes, max_work=figure - 1)
                self.assertRefused(error, "pareto_resource_exceeded")
                self.assertIn(f"figure {figure} ", error.detail)

    def test_max_work_one_refuses_pf1(self):
        """Refusal fixture: max_work 1 (draft 4.1)."""
        error = self.refusal(FIXTURES / "pf1.json", AXES, max_work=1)
        self.assertRefused(error, "pareto_resource_exceeded")
        self.assertIn("max_work 1", error.detail)

    def test_no_built_in_cap(self):
        """Rule: without max_work nothing is refused for size; 300 pairwise incomparable
        rows (44,850 pairs) are accepted and all stay on the front."""
        rows = [
            make_row(index, "ok", {PROFIT: float(index), DRAWDOWN: float(index)})
            for index in range(300)
        ]
        result = pareto_front(rows, AXES)
        self.assertEqual(result["n_front"], 300)

    def test_figure_counts_every_row_read_but_pairs_only_eligible_ones(self):
        """Rule: rows read include the excluded rows; the pairs are over eligible rows."""
        rows = [make_row(0, "ok", {PROFIT: 1.0, DRAWDOWN: 1.0})]
        rows += [make_row(index, "pruned", {}) for index in range(1, 5)]
        self.assertEqual(pareto_front(rows, AXES, max_work=5)["status"], "ok")
        self.assertRefused(self.refusal(rows, AXES, max_work=4), "pareto_resource_exceeded")


class CheckOrderTests(ParetoTestCase):
    def test_parameters_are_judged_before_the_source(self):
        """Rule: the axes are checked before the source is read."""
        error = self.refusal(FIXTURES / "missing.json", [AXES[0]])
        self.assertEqual(error.code, "pareto_parameter_invalid")

    def test_incomplete_rows_come_before_unknown_axes(self):
        """Rule: rows_incomplete is raised before an axis name is looked up."""
        document = fixture_document("pf1.json")
        document["trials_out"] = "best-k"
        axes = [{"name": "nope", "direction": "maximize"}, AXES[1]]
        self.assertEqual(self.refusal(document, axes).code, "pareto_rows_incomplete")

    def test_context_comes_before_unknown_axes(self):
        """Rule: a context mismatch is raised before an axis name is looked up."""
        document = fixture_document("pf1.json")
        for row in document["trials"]:
            row["space_hash"] = "a" * 64
        document["trials"][4]["space_hash"] = "b" * 64
        axes = [{"name": "nope", "direction": "maximize"}, AXES[1]]
        self.assertEqual(self.refusal(document, axes).code, "pareto_context_mismatch")

    def test_unknown_axes_come_before_no_eligible_rows(self):
        """Rule: an axis recorded nowhere is named even when no row would be eligible."""
        rows = [make_row(0, "pruned", {PROFIT: 1.0}), make_row(1, "pruned", {PROFIT: 2.0})]
        self.assertEqual(self.refusal(rows, AXES).code, "pareto_axis_unknown")

    def test_invalid_values_come_before_the_resource_check(self):
        """Rule: input defects are refused before the admission figure is looked at."""
        error = self.refusal(FIXTURES / "pf1_nan.jsonl", AXES, max_work=1)
        self.assertEqual(error.code, "pareto_input_invalid")
        document = fixture_document("pf1.json")
        document["trials"][0]["metrics"][PROFIT] = "10.0"
        self.assertEqual(self.refusal(document, AXES, max_work=1).code, "pareto_input_invalid")

    def test_no_eligible_rows_come_before_the_resource_check(self):
        """Rule: with nothing eligible the answer is no_eligible_rows, not a size refusal."""
        rows = [make_row(0, "pruned", {PROFIT: 1.0, DRAWDOWN: 1.0})]
        self.assertEqual(self.refusal(rows, AXES, max_work=1).code, "pareto_no_eligible_rows")


class InvarianceTests(ParetoTestCase):
    def test_every_source_form_gives_the_same_document(self):
        """Rule: a path, a path as text, a parsed result, a parsed row list, a JSONL path
        and parsed JSONL rows read alike (draft 3.6)."""
        path = FIXTURES / "pf1.json"
        document = fixture_document("pf1.json")
        expected = pareto_front(document, AXES, with_domination_counts=True)
        sources = (
            path,
            str(path),
            document["trials"],
            FIXTURES / "pf1.jsonl",
            fixture_rows("pf1.jsonl"),
        )
        for source in sources:
            with self.subTest(source=type(source).__name__):
                self.assertEqual(
                    pareto_front(source, AXES, with_domination_counts=True), expected
                )

    def test_shuffled_rows_and_repeats_give_the_same_document(self):
        """Rule: row order and repeats change nothing, digest included (draft 3.6)."""
        document = fixture_document("pf1.json")
        expected = pareto_front(document, AXES, with_domination_counts=True)
        rng = random.Random(1010)
        for _ in range(8):
            shuffled = list(document["trials"])
            rng.shuffle(shuffled)
            self.assertEqual(
                pareto_front(shuffled, AXES, with_domination_counts=True), expected
            )
        self.assertEqual(
            pareto_front(document, AXES, with_domination_counts=True), expected
        )

    def test_reversed_axes_change_only_the_echo_for_every_fixture(self):
        """Rule: axis order changes the echoed axes (and an exclusion's axes list), not the
        front, counts or digest (draft 3.6)."""
        for name, axes in (("pf1.json", AXES), ("pf2.json", AXES3), ("pf3.json", AXES)):
            with self.subTest(name=name):
                forward = pareto_front(FIXTURES / name, axes, with_domination_counts=True)
                backward = pareto_front(
                    FIXTURES / name, list(reversed(axes)), with_domination_counts=True
                )
                for key in ("pareto_front", "domination_counts", "input_sha256", "n_front"):
                    self.assertEqual(forward[key], backward[key], key)
                self.assertEqual(backward["axes"], list(reversed(axes)))

    def test_digest_follows_the_input(self):
        """Rule: the digest changes with a value or a status, not with row order."""
        document = fixture_document("pf1.json")
        base = pareto_front(document, AXES)["input_sha256"]
        changed = copy.deepcopy(document)
        changed["trials"][9]["metrics"][PROFIT] = 6.5
        self.assertNotEqual(pareto_front(changed, AXES)["input_sha256"], base)
        changed = copy.deepcopy(document)
        changed["trials"][6]["status"] = "ok"
        changed["trials"][6]["feasible"] = True
        self.assertNotEqual(pareto_front(changed, AXES)["input_sha256"], base)
        changed = copy.deepcopy(document)
        changed["trials"].reverse()
        self.assertEqual(pareto_front(changed, AXES)["input_sha256"], base)

    def test_inputs_are_not_changed_and_the_result_owns_its_data(self):
        """Rule: no side effect on the caller's rows or axes; the result shares nothing."""
        document = fixture_document("pf1.json")
        axes = copy.deepcopy(AXES)
        before = (copy.deepcopy(document), copy.deepcopy(axes))
        result = pareto_front(document, axes, with_domination_counts=True)
        self.assertEqual((document, axes), before)
        result["axes"][0]["name"] = "changed"
        result["excluded"]["status_engine_error"]["ids"].append(99)
        self.assertEqual(axes, before[1])
        self.assertEqual(document, before[0])

    def test_result_is_json_with_numeric_ids(self):
        """Rule: the document is plain JSON data; counts keys become strings only there."""
        result = pareto_front(FIXTURES / "pf1.json", AXES, with_domination_counts=True)
        again = json.loads(json.dumps(result, sort_keys=True))
        counts = again.pop("domination_counts")
        self.assertEqual(
            {int(key): value for key, value in counts.items()}, result.pop("domination_counts")
        )
        self.assertEqual(again, result)


class BruteForceCrossCheckTests(ParetoTestCase):
    def test_seeded_random_cases_agree_with_the_brute_force(self):
        """Seeded cases on tiny integer vectors (values 0 to 2, so many ties and
        duplicates, some written as floats or -0.0) over 2, 3 and 4 axes with random
        directions, random statuses, absent and null values; the module's front, counts,
        distinct-vector count and exclusions equal an independent O(N^2) scan, and a
        shuffle of the rows changes nothing. Every tenth case drops one axis from every
        row and every thirteenth turns every row pruned, so the refusals are exercised
        deterministically."""
        rng = random.Random(20261010)
        statuses = ["ok"] * 7 + ["pruned", "constraint_violation", "engine_error"]
        outcomes = {"front": 0, "axis_unknown": 0, "no_eligible_rows": 0}
        for case in range(400):
            axes = [
                {"name": f"m{index}", "direction": rng.choice(DIRECTIONS)}
                for index in range(rng.choice((2, 3, 4)))
            ]
            rows = []
            for trial_id in rng.sample(range(100), rng.randint(1, 14)):
                metrics = {}
                for axis in axes:
                    draw = rng.random()
                    if draw < 0.08:
                        continue
                    if draw < 0.16:
                        metrics[axis["name"]] = None
                        continue
                    value = rng.randint(0, 2)
                    metrics[axis["name"]] = rng.choice((value, float(value)))
                    if value == 0 and rng.random() < 0.3:
                        metrics[axis["name"]] = -0.0
                rows.append(
                    make_row(
                        trial_id,
                        rng.choice(statuses),
                        metrics if rng.random() < 0.95 else None,
                    )
                )
            if case % 10 == 3:
                gone = rng.choice(axes)["name"]
                for raw in rows:
                    raw.get("metrics", {}).pop(gone, None)
            if case % 13 == 5:
                for raw in rows:
                    raw.update(status="pruned", feasible=False, objective=None)
            unknown, front, counts, distinct, excluded = brute_force(rows, axes)
            with self.subTest(case=case):
                if unknown:
                    error = self.refusal(rows, axes)
                    self.assertRefused(error, "pareto_axis_unknown", names=unknown)
                    outcomes["axis_unknown"] += 1
                    continue
                if not counts:
                    error = self.refusal(rows, axes)
                    self.assertRefused(error, "pareto_no_eligible_rows")
                    outcomes["no_eligible_rows"] += 1
                    continue
                result = pareto_front(rows, axes, with_domination_counts=True)
                self.assertEqual(result["pareto_front"], front)
                self.assertEqual(result["domination_counts"], counts)
                self.assertEqual(result["n_front"], len(front))
                self.assertEqual(result["n_distinct_vectors_on_front"], distinct)
                self.assertEqual(result["rows_read"], len(rows))
                self.assertEqual(result["rows_eligible"], len(counts))
                self.assertEqual(result["rows_excluded"], len(rows) - len(counts))
                self.assertEqual(result["excluded"], excluded)
                shuffled = list(rows)
                rng.shuffle(shuffled)
                self.assertEqual(
                    pareto_front(shuffled, axes, with_domination_counts=True), result
                )
                outcomes["front"] += 1
        self.assertGreater(outcomes["front"], 100)
        self.assertGreater(outcomes["axis_unknown"], 0)
        self.assertGreater(outcomes["no_eligible_rows"], 0)


if __name__ == "__main__":
    unittest.main()
