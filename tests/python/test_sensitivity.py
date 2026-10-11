"""Tests of the sensitivity module: pair ranking and pair maps.

Drafts cited: ``methods-neighbourhood-contract.proposed.md`` (N-draft; clause 3.6
holds the pair maps and the ranking, 4.1 the worked fixtures NF5 and NF5b) and
``methods-neighbourhood-contract.pin.md`` (choice N12).

None of this file was run when it was written. Every expected value is derived by
hand in the docstring of the test that uses it, from the fixture rows, and each
derivation was redone a second time before it was trusted.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from pineforge_hpo.neighbourhood import NeighbourhoodError, neighbourhood, prepare
from pineforge_hpo.sensitivity import (
    PairPlan,
    not_near_counts,
    pair_maps,
    plan_pairs,
    rank_pairs,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sensitivity"
NF5 = FIXTURES / "nf5_result.json"
NF5B = FIXTURES / "nf5b_result.json"
ABSOLUTE_1 = {"mode": "absolute", "value": 1.0}
OBJECTIVE = {
    "expression": "metrics.all.net_profit_pct",
    "direction": "maximize",
    "constraints": [],
}


def project(pair):
    """The cells of a pair map as (first, second, role, class, objective, gap, near, rows)."""
    first, second = pair["dimensions"]
    return [
        (
            item["values"][first],
            item["values"][second],
            item["role"],
            item["class"],
            item["objective"],
            item["gap"],
            item["near"],
            item["rows"],
        )
        for item in pair["cells"]
    ]


def diagonal_totals(pair):
    return {
        key: pair[key]
        for key in (
            "diagonal_total",
            "diagonal_evaluated",
            "diagonal_scored",
            "diagonal_near",
        )
    }


class RankPairsTests(unittest.TestCase):
    def test_nf5b_ranking_by_the_sum_of_not_near_counts(self):
        """NF5b: not-near counts Fast 1, Slow 1, Mode 0 give the three pair scores.

        (Fast, Mode) 1 + 0 = 1, (Fast, Slow) 1 + 1 = 2, (Mode, Slow) 0 + 1 = 1. The
        highest score comes first and the two ties go in ascending name order, so
        pair_k 1 is [(Fast, Slow)], 2 adds (Fast, Mode) and 3 adds (Mode, Slow). The
        dict is given in a scrambled order on purpose.
        """
        counts = {"Slow": 1, "Fast": 1, "Mode": 0}
        self.assertEqual(rank_pairs(counts, 0), [])
        self.assertEqual(rank_pairs(counts, 1), [("Fast", "Slow", 2)])
        self.assertEqual(
            rank_pairs(counts, 2), [("Fast", "Slow", 2), ("Fast", "Mode", 1)]
        )
        full = [("Fast", "Slow", 2), ("Fast", "Mode", 1), ("Mode", "Slow", 1)]
        self.assertEqual(rank_pairs(counts, 3), full)
        self.assertEqual(rank_pairs(counts, 4), full)

    def test_ties_go_by_ascending_byte_wise_names(self):
        """Rule N12: equal scores order by (first, second), upper case before lower case.

        Four names with equal counts: every pair scores 2. Byte order is B, a, b, c, so
        the six pairs come as (B, a), (B, b), (B, c), (a, b), (a, c), (b, c).
        """
        counts = {"b": 1, "a": 1, "c": 1, "B": 1}
        self.assertEqual(
            rank_pairs(counts, 6),
            [
                ("B", "a", 2),
                ("B", "b", 2),
                ("B", "c", 2),
                ("a", "b", 2),
                ("a", "c", 2),
                ("b", "c", 2),
            ],
        )

    def test_a_higher_sum_beats_the_name_order(self):
        """Rule N12: counts A 3, B 0, C 2 give (A, C) 5, (A, B) 3, (B, C) 2."""
        self.assertEqual(
            rank_pairs({"A": 3, "B": 0, "C": 2}, 3),
            [("A", "C", 5), ("A", "B", 3), ("B", "C", 2)],
        )

    def test_one_name_has_no_pair(self):
        """Rule: a single varying dimension, or none, gives no pair."""
        self.assertEqual(rank_pairs({"A": 4}, 3), [])
        self.assertEqual(rank_pairs({}, 3), [])

    def test_not_near_counts_include_infeasible_but_not_unscored(self):
        """Rule N12: scored neighbours that are not near count; the rest do not.

        A: a scored ok far one (1), a scored ok near one (0), an infeasible one (1: it
        is scored and never near), an unscorable one (0) and a missing one (0) -> 2.
        B has no neighbour -> 0.
        """

        def item(klass, near):
            return {"class": klass, "near": near}

        profile = {
            "A": [
                item("scored_ok", False),
                item("scored_ok", True),
                item("scored_infeasible", False),
                item("unscorable", False),
                item("never_evaluated", False),
            ],
            "B": [],
        }
        self.assertEqual(not_near_counts(profile), {"A": 2, "B": 0})


class NF5Tests(unittest.TestCase):
    def test_pair_map_cells_and_diagonal_totals(self):
        """NF5, band absolute 1.0, pair_k 1, derived again by hand.

        Space Fast 2..6 step 2, Slow 10..30 step 10; centre 0:0 = (4, 20), 8.0; R = 8.0
        (0:0 and 0:5 tie; the lower identity 0:0 is named). Axis gaps: Fast 2 1.0 near
        (inclusive), Fast 6 2.0, Slow 10 0.5 near, Slow 30 4.0, so each dimension has
        one not-near neighbour and the only pair (Fast, Slow) scores 2. Its cells, row
        major with Fast outer: (2,10) is a diagonal and row 0:5 (8.0, gap 0.0, near);
        (2,20) axis, 0:1; (2,30) diagonal with no row; (4,10) axis, 0:3; (4,20) the
        centre; (4,30) axis, 0:4; (6,10) diagonal, 0:7 (engine error); (6,20) axis,
        0:2; (6,30) diagonal, 0:6 (constraint violation, 9.0, gap null, not near).
        Diagonals: 4 cells, 3 with a row, 2 scored, 1 near. Only (2, 30) is missing.
        """
        result = neighbourhood([NF5], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=1)
        centre = result["centres"][0]
        self.assertEqual(len(centre["pair_maps"]), 1)
        pair = centre["pair_maps"][0]
        self.assertEqual(pair["dimensions"], ["Fast", "Slow"])
        self.assertEqual(pair["score"], 2)
        self.assertEqual(
            project(pair),
            [
                (2, 10, "diagonal", "scored_ok", 8.0, 0.0, True, [[0, 5]]),
                (2, 20, "axis", "scored_ok", 7.0, 1.0, True, [[0, 1]]),
                (2, 30, "diagonal", "never_evaluated", None, None, False, []),
                (4, 10, "axis", "scored_ok", 7.5, 0.5, True, [[0, 3]]),
                (4, 20, "centre", "scored_ok", 8.0, 0.0, True, [[0, 0]]),
                (4, 30, "axis", "scored_ok", 4.0, 4.0, False, [[0, 4]]),
                (6, 10, "diagonal", "unscorable", None, None, False, [[0, 7]]),
                (6, 20, "axis", "scored_ok", 6.0, 2.0, False, [[0, 2]]),
                (6, 30, "diagonal", "scored_infeasible", 9.0, None, False, [[0, 6]]),
            ],
        )
        self.assertEqual(
            diagonal_totals(pair),
            {
                "diagonal_total": 4,
                "diagonal_evaluated": 3,
                "diagonal_scored": 2,
                "diagonal_near": 1,
            },
        )
        self.assertEqual(result["missing_vectors"], [{"Fast": 2, "Slow": 30}])
        self.assertEqual(
            result["admission"],
            {"rows": 8, "lookups": 12, "work": 20, "max_work": None},
        )
        self.assertEqual(result["rules"]["pair_k"], 1)

    def test_axis_numbers_do_not_depend_on_pair_k(self):
        """Rule: pair maps add cells; they never change the axis counts or shares."""
        without = neighbourhood([NF5], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)
        with_maps = neighbourhood([NF5], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=1)
        for key in ("counts", "dropped", "share_near_best", "coverage", "profile"):
            self.assertEqual(with_maps["centres"][0][key], without["centres"][0][key])
        self.assertEqual(without["centres"][0]["pair_maps"], [])

    def test_roles_are_one_centre_axis_cells_and_diagonal_cells(self):
        """Rule 3.6: a 3 x 3 map has 1 centre, 4 axis and 4 diagonal cells."""
        result = neighbourhood([NF5], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=1)
        roles = [item["role"] for item in result["centres"][0]["pair_maps"][0]["cells"]]
        self.assertEqual(roles.count("centre"), 1)
        self.assertEqual(roles.count("axis"), 4)
        self.assertEqual(roles.count("diagonal"), 4)


class NF5bTests(unittest.TestCase):
    def run_nf5b(self, pair_k):
        return neighbourhood([NF5B], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=pair_k)

    def test_ranking_for_pair_k_one_two_and_three(self):
        """NF5b: pair_k 1 -> [(Fast, Slow)]; 2 -> + (Fast, Mode); 3 -> + (Mode, Slow).

        Axis gaps (R = 8.0, band 1.0): Fast 2 1.0 near, Fast 6 2.0, Mode sma 0.5 near,
        Slow 10 0.5 near, Slow 30 4.0. Not-near counts: Fast 1, Mode 0, Slow 1. Scores:
        (Fast, Mode) 1, (Fast, Slow) 2, (Mode, Slow) 1; the tie goes to the lower name
        pair. A pair_k above the number of pairs returns all three.
        """
        expected = [["Fast", "Slow"], ["Fast", "Mode"], ["Mode", "Slow"]]
        for pair_k, count in ((1, 1), (2, 2), (3, 3), (10, 3)):
            result = self.run_nf5b(pair_k)
            maps = result["centres"][0]["pair_maps"]
            self.assertEqual([pair["dimensions"] for pair in maps], expected[:count])
            self.assertEqual([pair["score"] for pair in maps], [2, 1, 1][:count])
        self.assertEqual(self.run_nf5b(0)["centres"][0]["pair_maps"], [])

    def test_the_three_maps_cell_by_cell(self):
        """NF5b with pair_k 3: the cells of each map, derived again by hand.

        The third dimension stays at the centre's value in every cell. (Fast, Slow),
        Mode ema: the four axis cells have the rows 0:1, 0:3, 0:4, 0:2 and the four
        diagonals none. (Fast, Mode), Slow 20: axis (2,ema) 0:1, (4,sma) 0:5, (6,ema)
        0:2, diagonals (2,sma) and (6,sma) without a row. (Mode, Slow), Fast 4: axis
        (ema,10) 0:3, (ema,30) 0:4, (sma,20) 0:5, diagonals (sma,10) and (sma,30)
        without a row. Mode keeps its declared order ema, sma.
        """
        maps = self.run_nf5b(3)["centres"][0]["pair_maps"]
        never = ("never_evaluated", None, None, False, [])
        self.assertEqual(
            project(maps[0]),
            [
                (2, 10, "diagonal", *never),
                (2, 20, "axis", "scored_ok", 7.0, 1.0, True, [[0, 1]]),
                (2, 30, "diagonal", *never),
                (4, 10, "axis", "scored_ok", 7.5, 0.5, True, [[0, 3]]),
                (4, 20, "centre", "scored_ok", 8.0, 0.0, True, [[0, 0]]),
                (4, 30, "axis", "scored_ok", 4.0, 4.0, False, [[0, 4]]),
                (6, 10, "diagonal", *never),
                (6, 20, "axis", "scored_ok", 6.0, 2.0, False, [[0, 2]]),
                (6, 30, "diagonal", *never),
            ],
        )
        self.assertEqual(
            project(maps[1]),
            [
                (2, "ema", "axis", "scored_ok", 7.0, 1.0, True, [[0, 1]]),
                (2, "sma", "diagonal", *never),
                (4, "ema", "centre", "scored_ok", 8.0, 0.0, True, [[0, 0]]),
                (4, "sma", "axis", "scored_ok", 7.5, 0.5, True, [[0, 5]]),
                (6, "ema", "axis", "scored_ok", 6.0, 2.0, False, [[0, 2]]),
                (6, "sma", "diagonal", *never),
            ],
        )
        self.assertEqual(
            project(maps[2]),
            [
                ("ema", 10, "axis", "scored_ok", 7.5, 0.5, True, [[0, 3]]),
                ("ema", 20, "centre", "scored_ok", 8.0, 0.0, True, [[0, 0]]),
                ("ema", 30, "axis", "scored_ok", 4.0, 4.0, False, [[0, 4]]),
                ("sma", 10, "diagonal", *never),
                ("sma", 20, "axis", "scored_ok", 7.5, 0.5, True, [[0, 5]]),
                ("sma", 30, "diagonal", *never),
            ],
        )
        self.assertEqual(
            [diagonal_totals(pair) for pair in maps],
            [
                {
                    "diagonal_total": 4,
                    "diagonal_evaluated": 0,
                    "diagonal_scored": 0,
                    "diagonal_near": 0,
                },
                {
                    "diagonal_total": 2,
                    "diagonal_evaluated": 0,
                    "diagonal_scored": 0,
                    "diagonal_near": 0,
                },
                {
                    "diagonal_total": 2,
                    "diagonal_evaluated": 0,
                    "diagonal_scored": 0,
                    "diagonal_near": 0,
                },
            ],
        )

    def test_missing_vectors_collect_the_diagonals_in_vector_order(self):
        """NF5b with pair_k 3: eight never-evaluated diagonals, ordered by (Fast, Mode, Slow).

        Axis neighbours all have rows. The diagonals: (Fast, Slow) adds Fast 2 or 6 with
        Slow 10 or 30 at Mode ema (4 vectors); (Fast, Mode) adds Fast 2 or 6 with Mode
        sma at Slow 20 (2); (Mode, Slow) adds Mode sma with Slow 10 or 30 at Fast 4 (2).
        Sorted by Fast, then Mode in declared order, then Slow.
        """
        result = self.run_nf5b(3)
        self.assertEqual(
            result["missing_vectors"],
            [
                {"Fast": 2, "Mode": "ema", "Slow": 10},
                {"Fast": 2, "Mode": "ema", "Slow": 30},
                {"Fast": 2, "Mode": "sma", "Slow": 20},
                {"Fast": 4, "Mode": "sma", "Slow": 10},
                {"Fast": 4, "Mode": "sma", "Slow": 30},
                {"Fast": 6, "Mode": "ema", "Slow": 10},
                {"Fast": 6, "Mode": "ema", "Slow": 30},
                {"Fast": 6, "Mode": "sma", "Slow": 20},
            ],
        )
        self.assertEqual(self.run_nf5b(0)["missing_vectors"], [])

    def test_plan_and_maps_through_the_public_functions(self):
        """The ranking, the spans and the lookup counts, then the maps once more.

        With pair_k 3 the axes hold the centre and its neighbours in the dimension's
        order: Fast (2, 4, 6), Slow (10, 20, 30), Mode (ema, sma). The maps span
        3 x 3, 3 x 2 and 2 x 3 cells, so 8, 5 and 5 cells are looked up besides the
        centre cell.
        """
        result = self.run_nf5b(3)
        study = prepare([NF5B])
        study.bind({"mode": "absolute", "value": 1.0}, None, None)
        values = study.row_values(study.by_id[(0, 0)])
        plans = plan_pairs(study, values, result["centres"][0]["profile"], 3)
        self.assertTrue(all(isinstance(plan, PairPlan) for plan in plans))
        self.assertEqual(
            [(plan.first, plan.second, plan.score) for plan in plans],
            [("Fast", "Slow", 2), ("Fast", "Mode", 1), ("Mode", "Slow", 1)],
        )
        self.assertEqual(
            [plan.first_values for plan in plans],
            [(2, 4, 6), (2, 4, 6), ("ema", "sma")],
        )
        self.assertEqual(
            [plan.second_values for plan in plans],
            [(10, 20, 30), ("ema", "sma"), (10, 20, 30)],
        )
        self.assertEqual([plan.lookups for plan in plans], [8, 5, 5])
        self.assertEqual(
            pair_maps(study, values, plans), result["centres"][0]["pair_maps"]
        )
        self.assertEqual(
            not_near_counts(result["centres"][0]["profile"]),
            {"Fast": 1, "Mode": 0, "Slow": 1},
        )

    def test_admission_counts_the_pair_cells(self):
        """Rule 3.9: rows 6, axis lookups 2 + 1 + 2 = 5, pair lookups 8 + 5 + 5 = 18."""
        result = self.run_nf5b(3)
        self.assertEqual(
            result["admission"],
            {"rows": 6, "lookups": 23, "work": 29, "max_work": None},
        )


class AxisOrderTests(unittest.TestCase):
    def test_categorical_axis_keeps_the_declared_order(self):
        """Rule 3.6: cells run in declared order, not alphabetical order.

        Mode is declared [wma, ema, sma] and the centre is ema, so its neighbours are
        wma and sma and the axis is (wma, ema, sma). With Fast 2..6 step 2 and centre
        4 the single pair (Fast, Mode) has 9 cells: for each Fast value in turn, Mode
        runs wma, ema, sma.
        """
        space = {
            "parameters": {
                "Fast": {
                    "kind": "integer",
                    "low": 2,
                    "high": 6,
                    "step": 2,
                    "log": False,
                },
                "Mode": {"kind": "categorical", "choices": ["wma", "ema", "sma"]},
            },
            "objective": OBJECTIVE,
        }
        trial = {
            "trial_id": 0,
            "status": "ok",
            "feasible": True,
            "objective": 1.0,
            "parameters": {"Fast": 4, "Mode": "ema"},
        }
        result = neighbourhood(
            [{"space": space, "trials": [trial]}],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=1,
        )
        pair = result["centres"][0]["pair_maps"][0]
        self.assertEqual(pair["dimensions"], ["Fast", "Mode"])
        self.assertEqual(
            [
                (item["values"]["Fast"], item["values"]["Mode"])
                for item in pair["cells"]
            ],
            [
                (2, "wma"),
                (2, "ema"),
                (2, "sma"),
                (4, "wma"),
                (4, "ema"),
                (4, "sma"),
                (6, "wma"),
                (6, "ema"),
                (6, "sma"),
            ],
        )
        roles = [item["role"] for item in pair["cells"]]
        self.assertEqual(
            roles,
            [
                "diagonal",
                "axis",
                "diagonal",
                "axis",
                "centre",
                "axis",
                "diagonal",
                "axis",
                "diagonal",
            ],
        )

    def test_pair_k_must_be_valid(self):
        """Rule 3.8: a negative or non-integer pair_k is parameter_invalid."""
        for pair_k in (-1, 1.0, True, "1"):
            with self.assertRaises(NeighbourhoodError) as caught:
                neighbourhood([NF5], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=pair_k)
            self.assertEqual(caught.exception.code, "neighbourhood_parameter_invalid")

    def test_result_with_maps_is_plain_json(self):
        """Rule 3.7: a result with pair maps survives a strict JSON round trip."""
        result = neighbourhood([NF5B], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=3)
        text = json.dumps(result, sort_keys=True, allow_nan=False)
        self.assertEqual(json.loads(text), result)
