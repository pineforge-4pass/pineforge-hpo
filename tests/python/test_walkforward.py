"""Contract tests for post hoc analysis of finished per-window studies.

Expected values are the hand-derived selections, exclusions, mid-ranks, stability
entries and admission figures of the documented behavior. Each test describes a wrong
implementation it catches.
SHA-256 is compared from literal hand-derived preimage texts, never a module run.
"""

from __future__ import annotations

from collections import UserDict
import copy
import hashlib
import json
import math
import pickle
import unittest

from pineforge_hpo.walkforward import (
    ALGORITHM,
    CODES,
    CONTRACT,
    DIRECTIONS,
    INTERPRETATION,
    WalkforwardError,
    walkforward,
)

PROFIT = "metrics.all.net_profit_pct"
PLAN = ["w1", "w2", "w3"]
SPACE = {
    "parameters": {
        "A": {"kind": "integer", "low": 1, "high": 5, "step": 1, "log": False},
        "B": {"kind": "real", "low": 0.0, "high": 1.0, "step": 0.25, "log": False},
        "C": {"kind": "boolean"},
    },
    "objective": {
        "expression": PROFIT,
        "direction": "maximize",
        "constraints": [],
    },
}

# A distinct status reason precedes missing/null reasons; ids always ascend.
D_EXCLUSIONS = [
    {"status_pruned": {"count": 1, "ids": [2]}},
    {"status_engine_error": {"count": 1, "ids": [2]}},
    {"status_constraint_violation": {"count": 1, "ids": [2]}},
]
D_SELECTIONS = [
    {"trial_id": 1, "value": 3.0, "parameters": {"A": 3, "B": 0.5, "C": False}},
    {"trial_id": 3, "value": 5.0, "parameters": {"A": 5, "B": 0.75, "C": True}},
    {"trial_id": 3, "value": 4.0, "parameters": {"A": 5, "B": 0.75, "C": True}},
]
D_DIMENSIONS = {
    "A": {
        "kind": "integer",
        "values": [3, 5, 5],
        "distinct": 2,
        "minimum": 3,
        "maximum": 5,
    },
    "B": {
        "kind": "real",
        "values": [0.5, 0.75, 0.75],
        "distinct": 2,
        "minimum": 0.5,
        "maximum": 0.75,
    },
    "C": {
        "kind": "boolean",
        "values": [False, True, True],
        "distinct": 2,
        "minimum": None,
        "maximum": None,
    },
}

# Literal compact ASCII preimages: every status is included, integer cells are floats.
D_PREIMAGES = (
    '["w1",0,"ok",1.5]',
    '["w1",1,"ok",3.0]',
    '["w1",2,"pruned",9.0]',
    '["w1",3,"ok",3.0]',
    '["w2",0,"ok",4.0]',
    '["w2",1,"ok",4.0]',
    '["w2",2,"engine_error",null]',
    '["w2",3,"ok",5.0]',
    '["w3",0,"ok",3.0]',
    '["w3",1,"ok",1.0]',
    '["w3",2,"constraint_violation",6.0]',
    '["w3",3,"ok",4.0]',
)

FIXED_TEXT = (
    "(a) This module runs no window and evaluates nothing. The studies were run by "
    "the caller's own application; this module reads rows that already exist.\n"
    "(b) No claim is made about what data any window's study used. The context checks "
    "are necessary, not sufficient: equal bar counters, one shared space and one "
    "shared space hash do not prove the same data, the same fixed inputs or the same "
    "strategy, and the windows may overlap, touch or be separated.\n"
    "(c) Each window's selected value is in-sample for that window: it is the best "
    "scored row of that window's rows.\n"
    "(d) A carry-over value is a row that the user's own study recorded, for the "
    "vector this contract's rule selects in the previous window, and nothing more. "
    "No value here is claimed to be out of sample. When the previous window has a "
    "selected vector the pair is always reported, and a target window that holds no "
    "eligible row with that vector is reported with row_found false and a reason: "
    "that is not a zero and not a drop.\n"
    "(e) The number of windows is small. The stability table is a summary of the "
    "selected values in window order; it is not a trajectory, not a robustness "
    "measure, and no threshold and no verdict is produced.\n"
    "(f) v1 analyses finished studies; native in-loop walk-forward comes later."
)


def row(trial_id, status, objective, a, b, c):
    return {
        "trial_id": trial_id,
        "status": status,
        "feasible": status == "ok",
        "objective": objective,
        "parameters": {"A": a, "B": b, "C": c},
        "space": copy.deepcopy(SPACE),
    }


def fixture_d():
    return {
        "w1": [
            row(0, "ok", 1.5, 2, 0.25, True),
            row(1, "ok", 3.0, 3, 0.5, False),
            row(2, "pruned", 9.0, 1, 0.0, True),
            row(3, "ok", 3.0, 4, 0.0, False),
        ],
        "w2": [
            row(0, "ok", 4.0, 3, 0.5, False),
            row(1, "ok", 4.0, 2, 0.25, True),
            row(2, "engine_error", None, 5, 1.0, True),
            row(3, "ok", 5.0, 5, 0.75, True),
        ],
        "w3": [
            row(0, "ok", 3.0, 3, 0.5, True),
            row(1, "ok", 1.0, 1, 0.25, False),
            row(2, "constraint_violation", 6.0, 2, 0.5, False),
            row(3, "ok", 4.0, 5, 0.75, True),
        ],
    }


def fixture_e():
    sources = fixture_d()
    cells = {"w1": {0: 2.0, 1: 3.0, 2: 9.0}, "w2": {0: None, 1: 4.0, 3: 5.0}}
    cells["w3"] = {0: 3.0, 1: 1.0, 2: 6.0}
    for label, rows in sources.items():
        for raw in rows:
            if raw["trial_id"] in cells[label]:
                raw["metrics"] = {PROFIT: cells[label][raw["trial_id"]]}
    return sources


def fixture_d_with_w3_boolean_false():
    """Fixture D with w3 id 3's C parameter set to false."""
    sources = fixture_d()
    sources["w3"][3]["parameters"]["C"] = False
    return sources


def fixture_d_with_w3_pruned():
    """Fixture D with w3 id 3 pruned."""
    sources = fixture_d()
    sources["w3"][3].update(status="pruned", feasible=False)
    return sources


def fixture_d_with_carried_duplicate(value=4.0):
    """Fixture D with w2 id 1 carrying id 0's vector and the given value."""
    sources = fixture_d()
    sources["w2"][1].update(objective=value, parameters={"A": 3, "B": 0.5, "C": False})
    return sources


def fixture_e_with_cell(label, trial_id, cell):
    """Fixture E with one recorded profit cell replaced."""
    sources = fixture_e()
    sources[label][trial_id]["metrics"][PROFIT] = cell
    return sources


def fixture_f():
    return {
        "w1": [row(0, "ok", 1.0, 1, 0.0, True)],
        "w2": [
            row(0, "engine_error", None, 1, 0.0, True),
            row(1, "pruned", 2.0, 4, 0.25, True),
        ],
    }


def fixture_f_with_pruned_source():
    """Fixture F with its sole w1 row pruned, leaving no eligible rows."""
    sources = fixture_f()
    sources["w1"][0].update(status="pruned", feasible=False)
    return sources


def fixture_m():
    sources = fixture_d()
    for raw in sources["w2"]:
        if raw["trial_id"] != 2:
            raw.update(status="pruned", feasible=False)
    return sources


def fixture_g():
    return {
        "w1": [row(0, "ok", 1.0, 1, 0.0, True)],
        "w2": [
            row(0, "ok", 2.0, 1, 0.0, True),
            row(1, "ok", 3.0, 2, 0.25, False),
            row(2, "ok", 3.5, 2, 0.25, False),
        ],
    }


def fixture_h():
    return {
        "w1": [row(0, "ok", 1.0, 1, 1, True)],
        "w2": [row(0, "ok", 2.0, 1, 1.0, True)],
        "w3": [row(0, "ok", 3.0, 1, -0.0, True)],
        "w4": [row(0, "ok", 4.0, 1, 0.0, True)],
    }


def analyse(sources=None, windows=None, **options):
    options.setdefault("metric", "objective")
    options.setdefault("direction", "maximize")
    return walkforward(
        PLAN if windows is None else windows,
        fixture_d() if sources is None else sources,
        **options,
    )


def literal_digest(texts):
    parts = [hashlib.sha256(text.encode("ascii")).digest() for text in texts]
    return hashlib.sha256(b"".join(sorted(parts))).hexdigest()


def pair(first, second, parameters, key, found=None, reason=None):
    trial_id, value, better, equal, rank = (
        (None, None, None, None, None) if found is None else found
    )
    return {
        "from": first,
        "to": second,
        "vector_key": key,
        "parameters": parameters,
        "row_found": found is not None,
        "trial_id": trial_id,
        "value": value,
        "better": better,
        "equal": equal,
        "rank_mid": rank,
        "reason": reason,
    }


KEY_3_HALF_FALSE = (
    '{"A":["integer","3"],"B":["real","3fe0000000000000"],"C":["boolean",false]}'
)
KEY_5_THREE_QUARTERS_TRUE = (
    '{"A":["integer","5"],"B":["real","3fe8000000000000"],"C":["boolean",true]}'
)


class ContractCase(unittest.TestCase):
    def refusal(self, code, sources=None, windows=None, ids=(), names=(), **options):
        with self.assertRaises(WalkforwardError) as caught:
            analyse(sources, windows, **options)
        error = caught.exception
        self.assertEqual(error.code, "walkforward_" + code)
        self.assertEqual(error.ids, tuple(ids))
        self.assertEqual(error.names, tuple(names))
        return error

    def absent_pair(self, entry, reason):
        self.assertIs(entry["row_found"], False)
        self.assertEqual(entry["reason"], reason)
        for field in ("trial_id", "value", "better", "equal", "rank_mid"):
            self.assertIsNone(entry[field])

    def a_dimension(self, report, values, distinct, minimum, maximum):
        self.assertEqual(
            report["stability"]["dimensions"]["A"],
            {
                "kind": "integer",
                "values": values,
                "distinct": distinct,
                "minimum": minimum,
                "maximum": maximum,
            },
        )


class SelectionTests(ContractCase):
    def test_best_and_lowest_id_tie(self):
        """3 ties at ids 1/3 beat 1.5; 5 beats 4; 4 beats 3/1: ids 1,3,3.

        Catches inverted maximize sign, last-row ties and eligibility of pruned 9.
        Work = 12 + 3+3+3 + 3+3 = 27.
        """
        report = analyse()
        self.assertEqual(
            [entry["selected"] for entry in report["per_window"]], D_SELECTIONS
        )
        self.assertEqual(report["work"], 27)
        self.assertTrue(
            all(not entry["no_eligible_rows"] for entry in report["per_window"])
        )

    def test_equal_objectives_choose_lower_id_when_higher_id_is_read_first(self):
        """Input ids1,0 both record 4 under minimize: id0 wins the exact tie.

        The only window has two eligible rows and no pair, so work=2+1=3.
        Catches using the first input row as the winner of an equal-value tie.
        """
        rows = [
            row(1, "ok", 4.0, 2, 0.25, True),
            row(0, "ok", 4.0, 1, 0.0, True),
        ]
        for raw in rows:
            raw["space"]["objective"]["direction"] = "minimize"
        report = analyse({"w1": rows}, ["w1"], direction="minimize")
        self.assertEqual(
            report["per_window"][0]["selected"],
            {"trial_id": 0, "value": 4.0, "parameters": {"A": 1, "B": 0.0, "C": True}},
        )
        self.assertEqual(report["work"], 3)


class ExclusionTests(ContractCase):
    def test_counts_and_status_reasons(self):
        """Each window has 4 rows, 3 ok and excluded id 2: totals 12,9,3.

        Catches competing constraint_violation 6, renamed reasons and wrong counts.
        """
        report = analyse()
        self.assertEqual(
            [entry["excluded"] for entry in report["per_window"]], D_EXCLUSIONS
        )
        for entry in report["per_window"]:
            self.assertEqual(
                (entry["rows_read"], entry["rows_eligible"], entry["rows_excluded"]),
                (4, 3, 1),
            )
        self.assertEqual(
            (report["rows_read"], report["rows_eligible"], report["rows_excluded"]),
            (12, 9, 3),
        )

    def test_exclusion_ids_ascend_after_reversal(self):
        """M's reversed pruned ids read as 3,1,0, but must be listed 0,1,3.

        Reasons sort engine_error before pruned despite id0 being pruned. Catches
        preserving file order, unsorted reason keys and counting the wrong rows.
        """
        sources = fixture_m()
        sources["w2"].reverse()
        block = analyse(sources)["per_window"][1]
        self.assertEqual(
            block["excluded"],
            {
                "status_engine_error": {"count": 1, "ids": [2]},
                "status_pruned": {"count": 3, "ids": [0, 1, 3]},
            },
        )
        self.assertEqual(
            list(block["excluded"]), ["status_engine_error", "status_pruned"]
        )


class NeverRunTests(ContractCase):
    def test_missing_final_window(self):
        """Only w1/w2 supplied: 8 + 3+3 + 3 = 17, no block for w3.

        Catches silently omitted missing labels, invented windows and wrong order.
        """
        sources = fixture_d()
        sources.pop("w3")
        report = analyse(sources)
        self.assertEqual(report["windows"], PLAN)
        self.assertEqual(report["supplied"], ["w1", "w2"])
        self.assertEqual(report["never_run"], ["w3"])
        self.assertEqual(
            [entry["window"] for entry in report["per_window"]], ["w1", "w2"]
        )
        self.assertEqual((report["rows_read"], report["work"]), (8, 17))
        for key, value in report.items():
            if key not in ("windows", "never_run"):
                self.assertNotIn('"w3"', json.dumps(value))


class MidRankTests(ContractCase):
    def test_tied_target_values(self):
        """Carried 3,.5,false matches w2 id0=4; 5 is better, two 4s equal.

        Rank = 1 + (2+1)/2 = 2.5. Catches ordinal rank, dropped ties and
        counting strictly less values as better for maximize.
        """
        self.assertEqual(
            analyse()["carry_over"][0],
            pair(
                "w1",
                "w2",
                {"A": 3, "B": 0.5, "C": False},
                KEY_3_HALF_FALSE,
                (0, 4.0, 1, 2, 2.5),
            ),
        )


class UniqueBestTests(ContractCase):
    def test_second_pair_is_unique_best(self):
        """5,.75,true matches w3 id3=4, above eligible 3/1: rank 0+(1+1)/2=1.

        Catches lookup by id/value, including constraint_violation 6 in better,
        or a false row_found while retaining a value.
        """
        self.assertEqual(
            analyse()["carry_over"][1],
            pair(
                "w2",
                "w3",
                {"A": 5, "B": 0.75, "C": True},
                KEY_5_THREE_QUARTERS_TRUE,
                (3, 4.0, 0, 1, 1.0),
            ),
        )


class NoRowTests(ContractCase):
    def test_changed_boolean_is_no_row(self):
        """Fixture D with w3 id 3 C=false has no match for carried C=true.

        Selection remains id3=4, work 12+9+6=27. Catches zero substitution,
        dropped pair, invented break and comparing only part of the vector.
        """
        sources = fixture_d_with_w3_boolean_false()
        report = analyse(sources)
        self.assertEqual(len(report["carry_over"]), 2)
        self.absent_pair(report["carry_over"][1], "no_row")
        self.assertEqual(report["chain_breaks"], [])
        self.assertEqual(
            report["per_window"][2]["selected"],
            {
                "trial_id": 3,
                "value": 4.0,
                "parameters": {"A": 5, "B": 0.75, "C": False},
            },
        )
        self.assertEqual(report["work"], 27)


class IneligibleMatchTests(ContractCase):
    def test_matching_pruned_row(self):
        """Fixture D with w3 id 3 pruned selects id0=3; id3 supplies reason.

        Counts 4,2,2; work 12+(3+3+1)+(3+2)=24. Catches reporting pruned
        value 4 or taking constraint_violation as the carrying row's reason.
        """
        sources = fixture_d_with_w3_pruned()
        report = analyse(sources)
        self.absent_pair(report["carry_over"][1], "status_pruned")
        block = report["per_window"][2]
        self.assertEqual(
            (block["rows_read"], block["rows_eligible"], block["rows_excluded"]),
            (4, 2, 2),
        )
        self.assertEqual(
            block["excluded"],
            {
                "status_constraint_violation": {"count": 1, "ids": [2]},
                "status_pruned": {"count": 1, "ids": [3]},
            },
        )
        self.assertEqual(
            block["selected"],
            {"trial_id": 0, "value": 3.0, "parameters": {"A": 3, "B": 0.5, "C": True}},
        )
        self.assertEqual(report["work"], 24)


class ChainBreakTests(ContractCase):
    def test_middle_never_run_breaks_succession(self):
        """w1/w3 supplied: no adjacent pair, A=3,5; work 8+3+3=14.

        Catches pairing across a gap, a silent break and missing skipped label.
        """
        sources = fixture_d()
        sources.pop("w2")
        report = analyse(sources)
        self.assertEqual(report["supplied"], ["w1", "w3"])
        self.assertEqual(report["never_run"], ["w2"])
        self.assertEqual(report["carry_over"], [])
        self.assertEqual(
            report["chain_breaks"], [{"from": "w1", "to": "w3", "skipped": ["w2"]}]
        )
        self.assertEqual(report["stability"]["windows"], ["w1", "w3"])
        self.a_dimension(report, [3, 5], 2, 3, 5)
        self.assertEqual((report["rows_read"], report["work"]), (8, 14))


class StabilityTests(ContractCase):
    def test_selected_values_and_dimension_order(self):
        """Winners 1,3,3 give A=3,5,5; B=.5,.75,.75; C=false,true,true.

        Each has two distinct scalars; numeric bounds are listed extremes and
        boolean bounds null. Catches averages, count=3 and invented boolean order.
        """
        report = analyse()
        self.assertEqual(report["stability"]["dimensions"], D_DIMENSIONS)
        self.assertEqual(list(report["stability"]["dimensions"]), ["A", "B", "C"])
        self.assertEqual(report["stability"]["windows"], PLAN)
        self.assertEqual(report["stability"]["windows_without_selection"], [])


class SelectionGapTests(ContractCase):
    def test_explained_stability_gaps(self):
        """Pruned w3 winner gives A=3,5,3; only w1 gives [3]; F gives w1 only;
        M gives w1/w3 A=3,5 and no w2 winner. Figures 24,7,3,18.

        Catches unexplained shorter values or never-run labels in no-selection.
        """
        pruned = fixture_d_with_w3_pruned()
        cases = [
            (pruned, PLAN, [3, 5, 3], 2, 3, 5, PLAN, [], [], 24),
            (
                {"w1": fixture_d()["w1"]},
                PLAN,
                [3],
                1,
                3,
                3,
                ["w1"],
                [],
                ["w2", "w3"],
                7,
            ),
            (fixture_f(), ["w1", "w2"], [1], 1, 1, 1, ["w1"], ["w2"], [], 3),
            (fixture_m(), PLAN, [3, 5], 2, 3, 5, ["w1", "w3"], ["w2"], [], 18),
        ]
        for (
            sources,
            plan,
            values,
            distinct,
            low,
            high,
            selected,
            empty,
            missing,
            work,
        ) in cases:
            with self.subTest(plan=plan, work=work):
                report = analyse(sources, plan)
                self.a_dimension(report, values, distinct, low, high)
                self.assertEqual(report["stability"]["windows"], selected)
                self.assertEqual(
                    report["stability"]["windows_without_selection"], empty
                )
                self.assertEqual(report["never_run"], missing)
                self.assertEqual(report["work"], work)


class EmptyWindowTests(ContractCase):
    def test_f_target_empty_pair_is_retained(self):
        """F target has engine_error id0 matching source; pruned id1 differs.

        Its 2 rows all excluded, no selection; pair reason status_engine_error,
        no break, work 3+0+0=3. Catches dropping pair or refusing empty target.
        The key-list assertion checks engine_error before pruned; first-seen and
        sorted status order coincide here.
        """
        report = analyse(fixture_f(), ["w1", "w2"])
        self.assertEqual(
            list(report["per_window"][1]["excluded"]),
            ["status_engine_error", "status_pruned"],
        )
        self.assertEqual(
            report["per_window"][1],
            {
                "window": "w2",
                "rows_read": 2,
                "rows_eligible": 0,
                "rows_excluded": 2,
                "excluded": {
                    "status_engine_error": {"count": 1, "ids": [0]},
                    "status_pruned": {"count": 1, "ids": [1]},
                },
                "selected": None,
                "no_eligible_rows": True,
            },
        )
        self.assertEqual(len(report["carry_over"]), 1)
        self.assertEqual(
            (report["carry_over"][0]["from"], report["carry_over"][0]["to"]),
            ("w1", "w2"),
        )
        self.absent_pair(report["carry_over"][0], "status_engine_error")
        self.assertEqual(report["chain_breaks"], [])
        self.assertEqual(report["work"], 3)

    def test_pruned_source_refuses_empty_everywhere(self):
        """Fixture F with its sole w1 row pruned has 3 rows, zero eligible.

        Catches successful reports over no selected row in any supplied window.
        """
        sources = fixture_f_with_pruned_source()
        self.refusal("no_eligible_rows", sources, ["w1", "w2"])

    def test_m_source_empty_is_not_a_break(self):
        """M has e=3,0,3; only w1->w2 formed, id0 pruned supplies reason.

        Counts for w2 are 4,0,4; work 12+(3+0+3)+0=18. Catches invented
        break or outgoing pair for empty source, and the wrong carrying reason.
        Sorted reasons are engine_error,pruned although id0 is pruned; catches
        retaining the first-seen order of status reason keys.
        """
        report = analyse(fixture_m())
        block = report["per_window"][1]
        self.assertEqual(
            (block["rows_read"], block["rows_eligible"], block["rows_excluded"]),
            (4, 0, 4),
        )
        self.assertIsNone(block["selected"])
        self.assertIs(block["no_eligible_rows"], True)
        self.assertEqual(
            block["excluded"],
            {
                "status_engine_error": {"count": 1, "ids": [2]},
                "status_pruned": {"count": 3, "ids": [0, 1, 3]},
            },
        )
        self.assertEqual(
            list(block["excluded"]), ["status_engine_error", "status_pruned"]
        )
        self.assertEqual(len(report["carry_over"]), 1)
        self.absent_pair(report["carry_over"][0], "status_pruned")
        self.assertEqual(report["chain_breaks"], [])
        self.assertEqual(report["work"], 18)


class SpaceMismatchTests(ContractCase):
    def test_different_integer_bound(self):
        """w2 A high 6 differs from reference w1 high 5 in four rows.

        Catches accepting cross-window mismatches or omitting w2/A from names.
        """
        sources = fixture_d()
        for raw in sources["w2"]:
            raw["space"]["parameters"]["A"]["high"] = 6
        self.refusal(
            "space_mismatch", sources, ids=[0, 1, 2, 3], names=["w1", "w2", "A"]
        )


class ContextTests(ContractCase):
    def test_inside_window_hash_difference(self):
        """w1 ids0/1 record hashes aa/bb: context_mismatch names w1,space_hash.

        The reference id0 and differing id1 are both implicated, giving (0,1).
        Catches skipping the in-window hash check or treating it as a cross-window
        space_mismatch instead of a defect found while reading w1.
        """
        sources = {"w1": fixture_d()["w1"][:2]}
        sources["w1"][0]["space_hash"] = "aa"
        sources["w1"][1]["space_hash"] = "bb"
        self.refusal(
            "context_mismatch",
            sources,
            ["w1"],
            ids=[0, 1],
            names=["w1", "space_hash"],
        )

    def test_inside_window_counter_difference(self):
        """w1 ok ids0/1 counter 100 vs101: both ids and exact counter named.

        Catches omitting per-window context checks or omitting the window label.
        """
        sources = fixture_d()
        sources["w1"][0]["backtest"] = {"input_bars_processed": 100}
        sources["w1"][1]["backtest"] = {"input_bars_processed": 101}
        self.refusal(
            "context_mismatch",
            sources,
            ids=[0, 1],
            names=["w1", "input_bars_processed"],
        )

    def test_between_window_counters_are_not_compared(self):
        """Equal counters within each window differ 100/200/300 between windows.

        Neither counters nor hashes enter digest: document remains D, work 27.
        Catches checking context once across windows or comparing timeframes there.
        """
        sources = fixture_d()
        for count, rows in zip((100, 200, 300), sources.values()):
            for raw in rows:
                if raw["status"] == "ok":
                    raw["backtest"] = {
                        "input_bars_processed": count,
                        "script_bars_processed": count,
                        "input_tf_seconds": count,
                        "script_tf_seconds": count,
                    }
        self.assertEqual(analyse(sources), analyse())
        self.assertEqual(analyse(sources)["work"], 27)


class DuplicateTests(ContractCase):
    def test_decreasing_carried_values_with_higher_id_read_first(self):
        """w2 carried id0=4.5 and id1=4.0 disagree under minimize; ids1,0 read.

        Both eligible rows carry w1's sole selected vector, so duplicate_vector
        names w2 and ids (0,1), regardless of which value is better. Sorted id0
        has 4.5; testing only whether later values are greater misses id1's 4.0.
        Catches replacing the unequal-value check with a greater-than check.
        """
        sources = {
            "w1": [row(0, "ok", 1.0, 1, 0.0, True)],
            "w2": [
                row(1, "ok", 4.0, 1, 0.0, True),
                row(0, "ok", 4.5, 1, 0.0, True),
            ],
        }
        for rows in sources.values():
            for raw in rows:
                raw["space"]["objective"]["direction"] = "minimize"
        self.refusal(
            "duplicate_vector",
            sources,
            ["w1", "w2"],
            direction="minimize",
            ids=[0, 1],
            names=["w2"],
        )

    def test_equal_duplicates_report_lowest_id(self):
        """Fixture D with w2 id 1 carrying id 0's vector has two values of 4.

        Still better=1,equal=2,rank=2.5,id0; work27. Catches refusing equal
        duplicates, last-match selection or deduplicating ties in rank.
        """
        sources = fixture_d_with_carried_duplicate()
        report = analyse(sources)
        self.assertEqual(
            report["carry_over"][0],
            pair(
                "w1",
                "w2",
                {"A": 3, "B": 0.5, "C": False},
                KEY_3_HALF_FALSE,
                (0, 4.0, 1, 2, 2.5),
            ),
        )
        self.assertEqual(report["work"], 27)

    def test_unequal_carried_duplicates_refuse(self):
        """Fixture D with w2 id 1 carrying id 0's vector at 4.5 is ambiguous.

        Catches arbitrarily choosing one value or naming ids of unrelated vectors.
        """
        sources = fixture_d_with_carried_duplicate(4.5)
        self.refusal("duplicate_vector", sources, ids=[0, 1], names=["w2"])

    def test_g_unrelated_duplicates_do_not_refuse(self):
        """G carried key matches id0=2; other key duplicates at3/3.5 are better.

        Rank2+(1+1)/2=3; target3,3,0 and work4+3+3=10. Catches a blanket
        duplicate refusal rather than checking only the carried key.
        """
        report = analyse(fixture_g(), ["w1", "w2"])
        entry = report["carry_over"][0]
        self.assertEqual(
            (
                entry["trial_id"],
                entry["value"],
                entry["better"],
                entry["equal"],
                entry["rank_mid"],
            ),
            (0, 2.0, 2, 1, 3.0),
        )
        self.assertIs(entry["row_found"], True)
        block = report["per_window"][1]
        self.assertEqual(
            (
                block["rows_read"],
                block["rows_eligible"],
                block["rows_excluded"],
                block["excluded"],
            ),
            (3, 3, 0, {}),
        )
        self.assertEqual(report["work"], 10)


class DeterminismTests(ContractCase):
    def test_rows_mapping_and_repetition(self):
        """File reversal and mapping reversal change no selected ids or entries.

        Catches order-dependent listings/digests and state retained between calls.
        """
        baseline = analyse()
        sources = fixture_d()
        reversed_rows = {label: list(reversed(rows)) for label, rows in sources.items()}
        self.assertEqual(analyse(reversed_rows), baseline)
        self.assertEqual(analyse(dict(reversed(list(sources.items())))), baseline)
        self.assertEqual(analyse(), baseline)

    def test_reversed_plan_changes_pairs_but_not_digest(self):
        """Reverse plan selects w3,w2,w1; w3->w2 id3=5 rank1; w2->w1 no row.

        Same twelve labelled digest preimages. Catches mapping order as plan or
        hashing positions, while ordinary row permutations must stay invariant.
        """
        baseline = analyse()
        report = analyse(windows=["w3", "w2", "w1"])
        self.assertNotEqual(report, baseline)
        self.assertEqual(
            [entry["window"] for entry in report["per_window"]], ["w3", "w2", "w1"]
        )
        self.assertEqual(report["supplied"], ["w3", "w2", "w1"])
        self.assertEqual(report["stability"]["windows"], ["w3", "w2", "w1"])
        self.assertEqual(
            report["carry_over"][0],
            pair(
                "w3",
                "w2",
                {"A": 5, "B": 0.75, "C": True},
                KEY_5_THREE_QUARTERS_TRUE,
                (3, 5.0, 0, 1, 1.0),
            ),
        )
        self.absent_pair(report["carry_over"][1], "no_row")
        self.assertEqual(
            (report["carry_over"][1]["from"], report["carry_over"][1]["to"]),
            ("w2", "w1"),
        )
        self.assertEqual(report["input_sha256"], baseline["input_sha256"])


class DigestTests(ContractCase):
    def test_non_ascii_label_has_ascii_escaped_preimage(self):
        """Label é1, id0, ok, objective1 gives ["\\u00e91",0,"ok",1.0].

        The escape for é is six ASCII characters: backslash,u,0,0,e,9.
        Hash this literal text, then hash the sole raw digest. Catches disabling
        ASCII escaping or encoding an unescaped label as UTF-8 in the preimage.
        """
        report = analyse({"é1": [row(0, "ok", 1.0, 1, 0.0, True)]}, ["é1"])
        self.assertEqual(
            report["input_sha256"],
            literal_digest((r'["\u00e91",0,"ok",1.0]',)),
        )

    def test_literal_preimages(self):
        """Twelve literal [label,id,status,binary64 cell] preimages give digest.

        Hash is not hand arithmetic; texts are hand-derived. Catches omitting
        labels/non-ok rows, integer text for float cells and joining unsorted bytes.
        """
        report = analyse()
        self.assertEqual(report["input_sha256"], literal_digest(D_PREIMAGES))
        sources = fixture_d()
        sources["w2"][3]["objective"] = 4.0
        texts = list(D_PREIMAGES)
        texts[7] = '["w2",3,"ok",4.0]'
        changed = analyse(sources)
        self.assertEqual(changed["input_sha256"], literal_digest(texts))
        self.assertNotEqual(changed["input_sha256"], report["input_sha256"])
        self.assertEqual(
            analyse(windows=list(reversed(PLAN)))["input_sha256"],
            report["input_sha256"],
        )


class MinimizeTests(ContractCase):
    def test_recorded_metric_minimize(self):
        """E pools {2,3},{4,5},{3,1}: minima ids0,1,1, A=2,2,1.

        w1->w2 carried id1=4 rank0+(1+1)/2=1; w2->w3 no row. B=.25
        throughout; C=true,true,false. Work12+1+1+1+2+2=19. Catches ignored
        direction, greater-is-better ranking or checking metric against objective.
        """
        report = analyse(fixture_e(), metric=PROFIT, direction="minimize")
        self.assertEqual(
            [entry["selected"] for entry in report["per_window"]],
            [
                {
                    "trial_id": 0,
                    "value": 2.0,
                    "parameters": {"A": 2, "B": 0.25, "C": True},
                },
                {
                    "trial_id": 1,
                    "value": 4.0,
                    "parameters": {"A": 2, "B": 0.25, "C": True},
                },
                {
                    "trial_id": 1,
                    "value": 1.0,
                    "parameters": {"A": 1, "B": 0.25, "C": False},
                },
            ],
        )
        entry = report["carry_over"][0]
        self.assertEqual(
            (
                entry["trial_id"],
                entry["value"],
                entry["better"],
                entry["equal"],
                entry["rank_mid"],
            ),
            (1, 4.0, 0, 1, 1.0),
        )
        self.assertIs(entry["row_found"], True)
        self.absent_pair(report["carry_over"][1], "no_row")
        self.a_dimension(report, [2, 2, 1], 2, 1, 2)
        dimensions = report["stability"]["dimensions"]
        self.assertEqual(
            dimensions["B"],
            {
                "kind": "real",
                "values": [0.25, 0.25, 0.25],
                "distinct": 1,
                "minimum": 0.25,
                "maximum": 0.25,
            },
        )
        self.assertEqual(
            dimensions["C"],
            {
                "kind": "boolean",
                "values": [True, True, False],
                "distinct": 2,
                "minimum": None,
                "maximum": None,
            },
        )
        self.assertEqual(report["work"], 19)


class RecordedMetricTests(ContractCase):
    def test_named_metric_known_across_windows_with_unrecorded_window(self):
        """E records PROFIT; two added ok rows in w4 record no metric cell.

        A name is unknown only if no supplied row records it. Both w4 rows are
        metric_not_recorded, so its counts are 2,0,2 and selected is null.
        E contributes 12 rows and 6 eligible; totals are 14,6,8. Its three
        windows each contribute one ranking pair, and target eligible counts
        are 2+2+0, so work=14+3+4=21. w4 id0 carries w3's selected vector,
        giving an absent pair with metric_not_recorded and no chain break.
        Catches refusing a known name when any individual window lacks it.
        """
        sources = fixture_e()
        sources["w4"] = [
            row(0, "ok", 8.0, 3, 0.5, True),
            row(1, "ok", 9.0, 1, 0.0, True),
        ]
        report = analyse(sources, ["w1", "w2", "w3", "w4"], metric=PROFIT)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(
            (report["rows_read"], report["rows_eligible"], report["rows_excluded"]),
            (14, 6, 8),
        )
        self.assertEqual(
            report["per_window"][3],
            {
                "window": "w4",
                "rows_read": 2,
                "rows_eligible": 0,
                "rows_excluded": 2,
                "excluded": {"metric_not_recorded": {"count": 2, "ids": [0, 1]}},
                "selected": None,
                "no_eligible_rows": True,
            },
        )
        self.assertEqual(report["stability"]["windows_without_selection"], ["w4"])
        self.assertEqual(len(report["carry_over"]), 3)
        self.assertEqual(
            (report["carry_over"][2]["from"], report["carry_over"][2]["to"]),
            ("w3", "w4"),
        )
        self.absent_pair(report["carry_over"][2], "metric_not_recorded")
        self.assertEqual(report["chain_breaks"], [])
        self.assertEqual(report["work"], 21)

    def test_e_exclusions_selection_and_carry_reasons(self):
        """E eligible2,2,2: winners1=3,3=5,0=3, A=3,5,3; work19.

        Status reasons precede missing/null reasons. Carried w2 id0 null gives
        metric_unavailable; w3 id3 absent gives metric_not_recorded. Catches
        refusing known names, swapping reasons, alphabetical keys or no_row.
        """
        report = analyse(fixture_e(), metric=PROFIT)
        self.assertEqual(
            (report["rows_read"], report["rows_eligible"], report["rows_excluded"]),
            (12, 6, 6),
        )
        exclusions = [
            {
                "status_pruned": {"count": 1, "ids": [2]},
                "metric_not_recorded": {"count": 1, "ids": [3]},
            },
            {
                "status_engine_error": {"count": 1, "ids": [2]},
                "metric_unavailable": {"count": 1, "ids": [0]},
            },
            {
                "status_constraint_violation": {"count": 1, "ids": [2]},
                "metric_not_recorded": {"count": 1, "ids": [3]},
            },
        ]
        for block, expected in zip(report["per_window"], exclusions):
            self.assertEqual(block["excluded"], expected)
            self.assertEqual(list(block["excluded"]), list(expected))
            self.assertEqual(
                (block["rows_read"], block["rows_eligible"], block["rows_excluded"]),
                (4, 2, 2),
            )
        self.assertEqual(
            [block["selected"] for block in report["per_window"]],
            [
                D_SELECTIONS[0],
                D_SELECTIONS[1],
                {
                    "trial_id": 0,
                    "value": 3.0,
                    "parameters": {"A": 3, "B": 0.5, "C": True},
                },
            ],
        )
        self.assertEqual(len(report["carry_over"]), 2)
        self.absent_pair(report["carry_over"][0], "metric_unavailable")
        self.absent_pair(report["carry_over"][1], "metric_not_recorded")
        self.assertEqual(report["chain_breaks"], [])
        self.a_dimension(report, [3, 5, 3], 2, 3, 5)
        self.assertEqual(
            report["stability"]["dimensions"]["B"],
            {
                "kind": "real",
                "values": [0.5, 0.75, 0.5],
                "distinct": 2,
                "minimum": 0.5,
                "maximum": 0.75,
            },
        )
        self.assertEqual(report["stability"]["dimensions"]["C"], D_DIMENSIONS["C"])
        self.assertEqual(report["work"], 19)

    def test_boolean_and_string_recorded_cells(self):
        """Fixture E with w1 id0 true or w2 id1 'x' has a non-number cell.

        Refusals name that window and recorded metric. Catches accepting bools or
        strings, or mistakenly using objective so only the reader's check fires.
        """
        for label, trial_id, cell in (("w1", 0, True), ("w2", 1, "x")):
            with self.subTest(label=label):
                sources = fixture_e_with_cell(label, trial_id, cell)
                self.refusal(
                    "input_invalid",
                    sources,
                    metric=PROFIT,
                    ids=[trial_id],
                    names=[label, PROFIT],
                )


class SpaceHashTests(ContractCase):
    def test_reference_uses_plan_position_before_trial_id(self):
        """w1 id5 hash aa precedes w2 id0 hash bb by plan position.

        Only w2 id0 differs: space_mismatch, ids (0,), names w1,w2,space_hash.
        Catches sorting by trial id before window position, which would instead
        take w2 id0 as reference and report differing id5.
        """
        first = row(5, "ok", 1.0, 1, 0.0, True)
        second = row(0, "ok", 2.0, 1, 0.0, True)
        first["space_hash"] = "aa"
        second["space_hash"] = "bb"
        self.refusal(
            "space_mismatch",
            {"w1": [first], "w2": [second]},
            ["w1", "w2"],
            ids=[0],
            names=["w1", "w2", "space_hash"],
        )

    def test_equal_absent_and_unpaired_hashes(self):
        """All 'aa', none recorded, or w1 hash/no space with others space/no hash
        must yield D unchanged: hash is not digest input; no computed hash check.

        Catches requiring hashes, comparing to a computed space hash or digesting it.
        """
        baseline = analyse()
        sources = fixture_d()
        for rows in sources.values():
            for raw in rows:
                raw["space_hash"] = "aa"
        self.assertEqual(analyse(sources), baseline)
        sources = fixture_d()
        for raw in sources["w1"]:
            raw.pop("space")
            raw["space_hash"] = "aa"
        self.assertEqual(analyse(sources), baseline)
        self.assertEqual(analyse(sources)["work"], 27)

    def test_w2_hash_differs(self):
        """Reference w1 id0='aa'; four w2='bb' differ, w3='aa' agrees.

        Catches inside-window-only hashing, missing labels or missing space_hash.
        """
        sources = fixture_d()
        for label, rows in sources.items():
            for raw in rows:
                raw["space_hash"] = "bb" if label == "w2" else "aa"
        self.refusal(
            "space_mismatch",
            sources,
            ids=[0, 1, 2, 3],
            names=["w1", "w2", "space_hash"],
        )

    def test_reference_is_first_not_majority_or_last(self):
        """w1='aa',w2/w3='bb': reference first, both later windows differ.

        Names all three labels; majority/last reference would name only two.
        """
        sources = fixture_d()
        for label, rows in sources.items():
            for raw in rows:
                raw["space_hash"] = "aa" if label == "w1" else "bb"
        self.refusal(
            "space_mismatch",
            sources,
            ids=[0, 0, 1, 1, 2, 2, 3, 3],
            names=["w1", "w2", "w3", "space_hash"],
        )


class RecordedScalarTests(ContractCase):
    def test_h_types_signed_zero_and_earliest_extremes(self):
        """H B=1,1.0,-0.0,0.0 has two identities and matches pairs1/3 only.

        Bounds first equal min=-0.0,max=int1; work4+0+3=7. Byte-exact JSON
        catches normalising values/extremes, which Python == cannot detect;
        pair checks catch typed identity splitting integer/real or signed zeros.
        """
        report = analyse(fixture_h(), ["w1", "w2", "w3", "w4"])
        self.assertEqual(
            [block["selected"]["value"] for block in report["per_window"]],
            [1.0, 2.0, 3.0, 4.0],
        )
        self.assertEqual(
            [entry["row_found"] for entry in report["carry_over"]], [True, False, True]
        )
        for index, value in ((0, 2.0), (2, 4.0)):
            entry = report["carry_over"][index]
            self.assertEqual(
                (
                    entry["trial_id"],
                    entry["value"],
                    entry["better"],
                    entry["equal"],
                    entry["rank_mid"],
                ),
                (0, value, 0, 1, 1.0),
            )
        self.absent_pair(report["carry_over"][1], "no_row")
        self.a_dimension(report, [1, 1, 1, 1], 1, 1, 1)
        self.assertEqual(
            json.dumps(
                report["stability"]["dimensions"]["B"],
                sort_keys=True,
                separators=(",", ":"),
            ),
            '{"distinct":2,"kind":"real","maximum":1,"minimum":-0.0,'
            '"values":[1,1.0,-0.0,0.0]}',
        )
        self.assertEqual(
            report["stability"]["dimensions"]["C"],
            {
                "kind": "boolean",
                "values": [True, True, True, True],
                "distinct": 1,
                "minimum": None,
                "maximum": None,
            },
        )
        self.assertEqual(report["work"], 7)


class RefusalTests(ContractCase):
    def test_unknown_mixed_type_keys_refuse_in_string_order(self):
        """Undeclared integer 1 and string z sort as (1, "z") by string form.

        Unknown keys precede reading: window_unknown, empty ids, names (1, "z").
        Catches plain sorted(), which raises TypeError for mixed int/string keys
        instead of returning the undeclared-label refusal.
        """
        self.refusal("window_unknown", {"z": 42, 1: 42}, ["w1"], names=[1, "z"])

    def test_unknown_equal_text_labels_refuse_in_type_name_order(self):
        """Integer 1 and string "1" share text; int sorts before str by type name.

        Both mapping orders give window_unknown, empty ids, names (1, "1").
        Catches sorting by string form alone, which keeps mapping order on ties
        and reports ("1", 1) when the string key is inserted first.
        """
        for sources in ({"1": 42, 1: 42}, {1: 42, "1": 42}):
            with self.subTest(sources=sources):
                self.refusal("window_unknown", sources, ["w1"], names=[1, "1"])

    def test_unknown_lone_surrogate_label_refuses_without_encoding(self):
        """Undeclared z and a lone surrogate sort as z,surrogate by code point.

        Unknown labels precede reading: window_unknown, empty ids, names
        (z,surrogate). Catches UTF-8 encoding during sorting, which crashes on
        the surrogate rather than returning the undeclared-label refusal.
        """
        self.refusal(
            "window_unknown", {"\ud800": 42, "z": 42}, ["w1"], names=["z", "\ud800"]
        )

    def test_recorded_integer_space_without_log_is_invalid(self):
        """Recorded w1 integer A lacks log, so its space cannot be canonicalised.

        The fault belongs to the recorded space, giving input_invalid with no
        row ids and names (w1,). Catches ignoring malformed recorded spaces or
        omitting the window label when translating the shared reader's refusal.
        """
        sources = {"w1": fixture_d()["w1"]}
        for raw in sources["w1"]:
            raw["space"]["parameters"]["A"].pop("log")
        self.refusal("input_invalid", sources, ["w1"], names=["w1"])

    def test_empty_passed_space_without_recorded_space_is_invalid(self):
        """No row records a space; passed {} lacks the parameters object.

        Canonicalising the fallback fails: input_invalid, ids (), names ().
        There is no recorded-space holder to name. Catches ignoring malformed
        fallback spaces or wrongly attributing a passed-space defect to w1.
        """
        raw = row(0, "ok", 1.0, 1, 0.0, True)
        raw.pop("space")
        self.refusal("input_invalid", {"w1": [raw]}, ["w1"], space={})

    def test_all_invalid_cells_in_first_failing_window_are_named(self):
        """w1 ok ids0/1 have bool/string cells; w2 id1 also has a string cell.

        Cell checks fail first in w1: input_invalid, ids (0,1), names (w1,profit).
        Catches raising at the first invalid row or collecting ids across later
        windows instead of all invalid cells in the first failing window.
        """
        sources = fixture_e_with_cell("w1", 0, True)
        sources["w1"][1]["metrics"][PROFIT] = "x"
        sources["w2"][1]["metrics"][PROFIT] = "x"
        self.refusal(
            "input_invalid", sources, metric=PROFIT, ids=[0, 1], names=["w1", PROFIT]
        )

    def test_none_required_arguments(self):
        """Each explicit None reaches the body and names that missing argument.

        Missing options precede invalid options. Catches accepting None, raising
        invalid instead of missing, or accidentally testing Python's TypeError.
        """
        cases = [
            (None, {}, "objective", "maximize", ("windows",)),
            (["w1"], None, "objective", "maximize", ("sources",)),
            (["w1"], {"w1": []}, None, "maximize", ("metric",)),
            (["w1"], {"w1": []}, "objective", None, ("direction",)),
            ([], [], None, None, ("metric", "direction")),
        ]
        for windows, sources, metric, direction, names in cases:
            with self.subTest(names=names):
                with self.assertRaises(WalkforwardError) as caught:
                    walkforward(windows, sources, metric=metric, direction=direction)
                self.assertEqual(caught.exception.code, "walkforward_parameter_missing")
                self.assertEqual(caught.exception.ids, ())
                self.assertEqual(caught.exception.names, names)

    def test_absent_arguments_are_python_type_errors(self):
        """Required positional and keyword arguments have no defaults.

        Catches adding inferred defaults, which would let absent arguments reach
        the body instead of Python raising TypeError before it.
        """
        calls = [
            lambda: walkforward(metric="objective", direction="maximize"),
            lambda: walkforward(["w1"]),
            lambda: walkforward(sources={}, metric="objective", direction="maximize"),
            lambda: walkforward(["w1"], {}),
            lambda: walkforward(["w1"], {}, direction="maximize"),
            lambda: walkforward(["w1"], {}, metric="objective"),
        ]
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(TypeError):
                    call()

    def test_invalid_options_table(self):
        """Empty/string plan, empty/repeated labels and bad option types refuse.

        Repeated labels are named after windows. Catches accepting malformed
        plans, bool max_work as int or deferring options until after reading.
        """
        cases = [
            ([], {}, {}, ["windows"]),
            ("w1", {}, {}, ["windows"]),
            (["w1", ""], {}, {}, ["windows"]),
            (["w1", 7], {}, {}, ["windows"]),
            (["w1", "w1"], {}, {}, ["windows", "w1"]),
            (["w1"], [("w1", "r.json")], {}, ["sources"]),
            (["w1"], {}, {"metric": 7}, ["metric"]),
            (["w1"], {}, {"direction": "best"}, ["direction"]),
            (["w1"], {}, {"space": 42}, ["space"]),
            (["w1"], {}, {"max_work": 0}, ["max_work"]),
            (["w1"], {}, {"max_work": True}, ["max_work"]),
            (["w1"], {}, {"max_work": 1.5}, ["max_work"]),
        ]
        for windows, sources, options, names in cases:
            with self.subTest(names=names, windows=windows, options=options):
                self.refusal(
                    "parameter_invalid", sources, windows, names=names, **options
                )

    def test_unknown_labels_then_no_supplied_source(self):
        """Unknown labels sort by code point regardless of mapping order; no sources
        over a declared one-window plan refuses with empty names and ids.

        Catches invented windows, reading unknown sources first or zero-row success.
        """
        self.refusal("window_unknown", {"w2": []}, ["w1"], names=["w2"])
        for sources in ({"é": 42, "z": 42, "a": 42}, {"a": 42, "z": 42, "é": 42}):
            self.refusal("window_unknown", sources, ["w1"], names=["a", "z", "é"])
        self.refusal("no_supplied_sources", {}, ["w1"])

    def test_incomplete_results(self):
        """trials_out=best refuses before even demanding a trials array.

        Count 2 vs one own row also refuses. Catches accepting partial rows or
        reporting input_invalid before the reader's rows_incomplete check.
        """
        self.refusal(
            "rows_incomplete",
            {"w1": {"trials_out": "best"}},
            ["w1"],
            space=SPACE,
            names=["w1"],
        )
        self.refusal(
            "rows_incomplete",
            {
                "w1": {
                    "trials": [row(0, "ok", 1.0, 1, 0.0, True)],
                    "trials_completed": 2,
                }
            },
            ["w1"],
            names=["w1"],
        )

    def test_missing_space_and_unknown_metric(self):
        """No recorded/passed space refuses; an unrecorded metric over D refuses.

        Catches invented spaces, treating an unknown metric as missing cells, or
        reporting an empty eligible set before checking the recorded name.
        """
        sources = fixture_d()
        for rows in sources.values():
            for raw in rows:
                raw.pop("space")
        self.refusal("space_missing", sources)
        self.refusal(
            "metric_unknown", metric="metrics.nope.nope", names=["metrics.nope.nope"]
        )
        self.refusal("no_eligible_rows", {"w1": []}, ["w1"], metric=PROFIT, space=SPACE)

    def test_recorded_directions_and_passed_space_control(self):
        """D common recorded maximize differs from minimize: direction alone.

        Result direction without space differs: w1,direction. A passed maximize
        space alone is not compared; recorded metrics also have no such check.
        Catches reversed check order or comparing the fallback space's direction.
        """
        self.refusal("direction_mismatch", direction="minimize", names=["direction"])
        raw = row(0, "ok", 1.0, 1, 0.0, True)
        raw.pop("space")
        sources = {"w1": {"direction": "maximize", "trials": [raw]}}
        self.refusal(
            "direction_mismatch",
            sources,
            ["w1"],
            direction="minimize",
            space=SPACE,
            names=["w1", "direction"],
        )
        report = analyse({"w1": [raw]}, ["w1"], direction="minimize", space=SPACE)
        self.assertEqual(report["per_window"][0]["selected"]["trial_id"], 0)
        self.assertEqual(report["direction"], "minimize")
        raw["metrics"] = {PROFIT: 2.0}
        report = analyse(
            sources, ["w1"], metric=PROFIT, direction="minimize", space=SPACE
        )
        self.assertEqual(report["per_window"][0]["selected"]["value"], 2.0)

    def test_reader_invalid_rows_and_metrics_members(self):
        """Malformed row and non-object metrics are refused before cell checks.

        Reader refusals name only the window, with id if known. Catches
        bypassing validation or interpreting non-object metrics as absent cells.
        """
        self.refusal("input_invalid", {"w1": {"trials": [{}]}}, ["w1"], names=["w1"])
        for metrics in ("x", True, [], None):
            with self.subTest(metrics=metrics):
                sources = fixture_e()
                sources["w1"][0]["metrics"] = metrics
                self.refusal(
                    "input_invalid", sources, metric=PROFIT, ids=[0], names=["w1"]
                )
        sources = fixture_d()
        sources["w1"][0]["objective"] = True
        self.refusal("input_invalid", sources, ids=[0], names=["w1"])

    def test_metric_cells_refuse_on_ok_and_nonfinite_on_any_status(self):
        """Ok bool/string/array/object and nonfinite or overflowing number refuse.

        Non-ok nonfinite and JSON-unwritable cells also refuse before scoring.
        Catches skipped digest validation, bool-number coercion or ignored overflow.
        """
        for cell in (True, "x", [], {}, float("nan"), float("inf"), 10**400):
            with self.subTest(cell_type=type(cell).__name__):
                sources = fixture_e()
                sources["w1"][0]["metrics"][PROFIT] = cell
                self.refusal(
                    "input_invalid",
                    sources,
                    metric=PROFIT,
                    ids=[0],
                    names=["w1", PROFIT],
                )
        for cell in (
            float("nan"),
            float("inf"),
            10**400,
            object(),
            {"x": float("inf")},
        ):
            with self.subTest(non_ok_type=type(cell).__name__):
                sources = fixture_e()
                sources["w1"][2]["metrics"][PROFIT] = cell
                self.refusal(
                    "input_invalid",
                    sources,
                    metric=PROFIT,
                    ids=[2],
                    names=["w1", PROFIT],
                )

    def test_every_rows_parameters_are_keyed_in_both_plan_orders(self):
        """Missing C on non-selected pruned id2 refuses even when w1 is not a
        carry-over target. Absent selected/non-selected parameters name A,B,C.

        Catches keying only winners/targets or depending on plan order for admission.
        """
        for plan in (PLAN, list(reversed(PLAN))):
            sources = fixture_d()
            sources["w1"][2]["parameters"] = {"A": 1, "B": 0.0}
            self.refusal("input_invalid", sources, plan, ids=[2], names=["w1", "C"])
        for trial_id in (1, 2):
            sources = fixture_d()
            sources["w1"][trial_id].pop("parameters")
            self.refusal(
                "input_invalid", sources, ids=[trial_id], names=["w1", "A", "B", "C"]
            )
        sources = fixture_d()
        sources["w1"][2]["parameters"]["A"] = True
        self.refusal("input_invalid", sources, ids=[2], names=["w1", "A"])

    def test_resource_bound_one_below_work_refuses(self):
        """D has 12 rows and three windows with 3 eligible rows each.

        Ranking pairs total 3*(3*2/2)=9; two carry-over targets contribute
        3+3=6. Thus work=12+9+6=27, above max_work=26, and the call refuses
        walkforward_resource_exceeded. Catches refusing only when work is
        greater than max_work+1, which would admit this same fixture at 26.
        """
        error = self.refusal("resource_exceeded", max_work=26)
        self.assertEqual(error.detail, "the admission figure 27 is above max_work 26")

    def test_resource_boundary_and_wrong_term_controls(self):
        """D work12+9+6=27: max25 refuses, max27/30 admit.

        Catches >= at equality, omitted lookups (21 admits25) and ordered pairs
        (36 refuses30). Unlimited admission must report the same figure.
        """
        self.refusal("resource_exceeded", max_work=25)
        for bound in (None, 27, 30):
            self.assertEqual(analyse(max_work=bound)["work"], 27)


class RefusalOrderTests(ContractCase):
    def test_hash_mismatch_before_direction_and_unknown_metric(self):
        """Spaces agree; w1/w3 hashes aa and w2 hashes bb disagree in four rows.

        space_mismatch has ids (0,1,2,3), names w1,w2,space_hash. It precedes
        both minimize versus recorded maximize and an unrecorded metric name.
        Catches moving the hash check after direction or metric-name checks.
        """
        sources = fixture_d()
        for label, rows in sources.items():
            for raw in rows:
                raw["space_hash"] = "bb" if label == "w2" else "aa"
        for options in ({"direction": "minimize"}, {"metric": "missing"}):
            with self.subTest(options=options):
                self.refusal(
                    "space_mismatch",
                    sources,
                    ids=[0, 1, 2, 3],
                    names=["w1", "w2", "space_hash"],
                    **options,
                )

    def test_read_context_before_next_source_and_cross_window_space(self):
        """w1 counter mismatch precedes both malformed later input and space6.

        Catches loading every source before checking contexts, or checking space
        across windows before each window's context.
        """
        sources = fixture_d()
        sources["w1"][0]["backtest"] = {"input_bars_processed": 100}
        sources["w1"][1]["backtest"] = {"input_bars_processed": 101}
        for raw in sources["w2"]:
            raw["space"]["parameters"]["A"]["high"] = 6
        self.refusal(
            "context_mismatch",
            sources,
            metric="missing",
            ids=[0, 1],
            names=["w1", "input_bars_processed"],
        )
        sources["w2"] = {"trials": [{}]}
        self.refusal(
            "context_mismatch",
            sources,
            ids=[0, 1],
            names=["w1", "input_bars_processed"],
        )

    def test_recorded_spaces_before_hashes_direction_and_unknown_metric(self):
        """A high6 in w2 and hashes aa/bb: space mismatch names A first.

        It also precedes objective-direction mismatch and unknown metric name.
        Catches hash/direction/name checks run before canonical recorded spaces.
        """
        sources = fixture_d()
        for label, rows in sources.items():
            for raw in rows:
                raw["space_hash"] = "bb" if label == "w2" else "aa"
                if label == "w2":
                    raw["space"]["parameters"]["A"]["high"] = 6
        for options in ({"direction": "minimize"}, {"metric": "missing"}):
            self.refusal(
                "space_mismatch",
                sources,
                ids=[0, 1, 2, 3],
                names=["w1", "w2", "A"],
                **options,
            )

    def test_cells_before_parameters_even_in_later_window(self):
        """w1 pruned row lacks C, but later w2 ok recorded cell is a string.

        Cell check over all windows precedes any parameter keying. Catches
        interleaving cell and parameter checks per window or doing keys first.
        """
        sources = fixture_e()
        sources["w1"][2]["parameters"].pop("C")
        sources["w2"][1]["metrics"][PROFIT] = "x"
        self.refusal(
            "input_invalid", sources, metric=PROFIT, ids=[1], names=["w2", PROFIT]
        )

    def test_parameters_and_duplicates_before_resource(self):
        """Missing C or carried values4/4.5 wins over max_work1.

        Catches checking the admission cap before input defects. Duplicate check
        is scoped to a formed pair; it must not disappear behind the cap.
        """
        sources = fixture_d()
        sources["w1"][2]["parameters"].pop("C")
        self.refusal("input_invalid", sources, max_work=1, ids=[2], names=["w1", "C"])
        sources = fixture_d_with_carried_duplicate(4.5)
        self.refusal("duplicate_vector", sources, max_work=1, ids=[0, 1], names=["w2"])
        sources = fixture_f_with_pruned_source()
        self.refusal("no_eligible_rows", sources, ["w1", "w2"], max_work=1)


class AdditionalContractTests(ContractCase):
    def test_large_integer_extremes_use_exact_comparison(self):
        """Selected A values 2^62+1 then 2^62 are two exact integer identities.

        Minimum is 4611686018427387904, maximum 4611686018427387905, distinct 2.
        Both convert to the same binary64 value. Catches float-key extrema,
        which wrongly keep the first (larger) integer as the minimum too.
        """
        sources = {
            "w1": [row(0, "ok", 1.0, 4611686018427387905, 0.0, True)],
            "w2": [row(0, "ok", 2.0, 4611686018427387904, 0.0, True)],
        }
        for rows in sources.values():
            rows[0]["space"]["parameters"]["A"]["high"] = 4611686018427387905
        report = analyse(sources, ["w1", "w2"])
        self.a_dimension(
            report,
            [4611686018427387905, 4611686018427387904],
            2,
            4611686018427387904,
            4611686018427387905,
        )

    def test_found_parameters_keep_target_spelling(self):
        """H first match has target B=1.0 and third has target B=+0.0.

        The failed middle pair retains source B=1.0. Catches always returning
        source parameters or normalising matching rows instead of reporting them.
        """
        report = analyse(fixture_h(), ["w1", "w2", "w3", "w4"])
        self.assertEqual(
            [
                json.dumps(entry["parameters"], sort_keys=True, separators=(",", ":"))
                for entry in report["carry_over"]
            ],
            [
                '{"A":1,"B":1.0,"C":true}',
                '{"A":1,"B":1.0,"C":true}',
                '{"A":1,"B":0.0,"C":true}',
            ],
        )

    def test_exact_extremes_separate_values_with_one_binary64_identity(self):
        """Real B records integer 2^53+1 then float 2^53: exact order differs.

        Identity rounds the integer to binary64 2^53, so distinct is one and the
        carry-over matches. Exact minimum is float 9007199254740992.0; maximum
        is integer 9007199254740993. Catches float-key extrema conflating them.
        """
        sources = {
            "w1": [row(0, "ok", 1.0, 1, 9007199254740993, True)],
            "w2": [row(0, "ok", 2.0, 1, 9007199254740992.0, True)],
        }
        for rows in sources.values():
            rows[0]["space"]["parameters"]["B"]["high"] = 9007199254740994.0
        report = analyse(sources, ["w1", "w2"])
        dimension = report["stability"]["dimensions"]["B"]
        self.assertEqual(dimension["distinct"], 1)
        self.assertEqual(dimension["minimum"], 9007199254740992.0)
        self.assertEqual(dimension["maximum"], 9007199254740993)
        self.assertIs(type(dimension["minimum"]), float)
        self.assertIs(type(dimension["maximum"]), int)
        self.assertIs(report["carry_over"][0]["row_found"], True)

    def test_digest_omits_metric_name_and_direction_and_rounds_integer_cells(self):
        """Recorded cells integer 2^53+1 become binary64 2^53, text .0.

        Two names with the same cells and either direction yield the same digest.
        Catches including option names/signs in the preimage or hashing raw ints.
        """
        raw = row(0, "ok", 1.0, 1, 0.0, True)
        raw["metrics"] = {PROFIT: 9007199254740993, "copy": 9007199254740993}
        expected = literal_digest(('["w1",0,"ok",9007199254740992.0]',))
        for metric, direction in ((PROFIT, "maximize"), ("copy", "minimize")):
            report = analyse({"w1": [raw]}, ["w1"], metric=metric, direction=direction)
            self.assertEqual(report["input_sha256"], expected)
            self.assertEqual(
                report["per_window"][0]["selected"]["value"], 9007199254740992.0
            )

    def test_reference_window_own_space_difference_does_not_name_matching_window(self):
        """Reference w1 id0 space5; only w1 id2 has space6; w2/w3 match space5.

        Refusal ids2,names w1,A. Catches using common_space(reference+candidate)
        repeatedly, which would wrongly implicate the matching windows too.
        """
        sources = fixture_d()
        sources["w1"][2]["space"]["parameters"]["A"]["high"] = 6
        self.refusal("space_mismatch", sources, ids=[2], names=["w1", "A"])

    def test_result_level_space_holders_are_named_without_row_ids(self):
        """Result reference w1 space5 vs result w2 space6; row spaces absent.

        No differing row holder, so ids empty but names w1,w2,A. Catches finding
        involved windows only from reader ids or ignoring result-level spaces.
        """
        sources = fixture_d()
        for rows in sources.values():
            for raw in rows:
                raw.pop("space")
        first = copy.deepcopy(SPACE)
        second = copy.deepcopy(SPACE)
        second["parameters"]["A"]["high"] = 6
        results = {
            "w1": {"space": first, "trials": sources["w1"]},
            "w2": {"space": second, "trials": sources["w2"]},
            "w3": {"space": first, "trials": sources["w3"]},
        }
        self.refusal("space_mismatch", results, names=["w1", "w2", "A"])

    def test_lowest_id_ineligible_match_reason(self):
        """Carried vector has target pruned id7 and engine_error id2, no eligible.

        Reason is status_engine_error at lower id2, even if read second. Catches
        first-file-match reasons or taking a window-level exclusion reason.
        """
        sources = fixture_f()
        sources["w2"] = [
            row(7, "pruned", 2.0, 1, 0.0, True),
            row(2, "engine_error", None, 1, 0.0, True),
        ]
        self.absent_pair(
            analyse(sources, ["w1", "w2"])["carry_over"][0], "status_engine_error"
        )

    def test_eligible_match_wins_over_lower_ineligible_id(self):
        """Target id0 pruned, id5 ok=2 both carry source key; ok id5 rank1 wins.

        Catches reporting the first matching row before checking eligibility.
        """
        sources = fixture_f()
        sources["w2"] = [
            row(0, "pruned", 9.0, 1, 0.0, True),
            row(5, "ok", 2.0, 1, 0.0, True),
        ]
        entry = analyse(sources, ["w1", "w2"])["carry_over"][0]
        self.assertIs(entry["row_found"], True)
        self.assertEqual(
            (entry["trial_id"], entry["value"], entry["rank_mid"]), (5, 2.0, 1.0)
        )

    def test_one_window_tuple_plan_mapping_and_warm_start(self):
        """D w1 alone has winner1 and work4+3=7, no pair/break.

        Warm ancestors plus own rows reproduce D; top-level result hashes differ
        but are unread. Catches refusing tuple/mapping/one-window, dropping warm
        rows or comparing result hashes beyond what the reader exposes. Tuple
        (w1,) echoes as list [w1]; catches preserving the tuple in the document.
        """
        sources = fixture_d()
        report = analyse(UserDict({"w1": sources["w1"]}), ("w1",))
        self.assertIs(type(report["windows"]), list)
        self.assertEqual(report["windows"], ["w1"])
        self.assertEqual(report["per_window"][0]["selected"], D_SELECTIONS[0])
        self.assertEqual(
            (report["carry_over"], report["chain_breaks"], report["work"]), ([], [], 7)
        )
        results = {
            label: {
                "warm_start_trials": rows[:2],
                "trials": rows[2:],
                "warm_start": {"trials": 2},
                "trials_completed": 2,
                "trials_out": "all",
                "space_hash": label,
            }
            for label, rows in sources.items()
        }
        self.assertEqual(analyse(results), analyse(sources))

    def test_all_nine_statuses_and_non_ok_json_cells(self):
        """Only ok=1 competes; each other status excluded even with a string,
        bool, array or object cell. Digest writes those excluded JSON values.

        Catches inspecting non-ok types for eligibility or dropping their digest.
        """
        statuses = [
            "ok",
            "constraint_violation",
            "objective_error",
            "constraint_error",
            "engine_error",
            "trial_error",
            "trial_timeout",
            "pruned",
            "partial",
        ]
        cells = [1, "x", True, [], {}, "y", False, [1, 2], {"x": 1}]
        rows = []
        for trial_id, (status, cell) in enumerate(zip(statuses, cells)):
            raw = row(
                trial_id,
                status,
                1.0 if status in ("ok", "constraint_violation") else None,
                1,
                0.0,
                True,
            )
            raw["metrics"] = {PROFIT: cell}
            rows.append(raw)
        texts = [
            '["w1",0,"ok",1.0]',
            '["w1",1,"constraint_violation","x"]',
            '["w1",2,"objective_error",true]',
            '["w1",3,"constraint_error",[]]',
            '["w1",4,"engine_error",{}]',
            '["w1",5,"trial_error","y"]',
            '["w1",6,"trial_timeout",false]',
            '["w1",7,"pruned",[1,2]]',
            '["w1",8,"partial",{"x":1}]',
        ]
        report = analyse({"w1": rows}, ["w1"], metric=PROFIT)
        self.assertEqual(
            (
                report["rows_read"],
                report["rows_eligible"],
                report["rows_excluded"],
                report["work"],
            ),
            (9, 1, 8, 9),
        )
        self.assertEqual(
            report["per_window"][0]["excluded"],
            {
                "status_" + status: {"count": 1, "ids": [trial_id]}
                for trial_id, status in enumerate(statuses)
                if trial_id
            },
        )
        self.assertEqual(report["input_sha256"], literal_digest(texts))

    def test_one_ulp_and_exact_parameter_lookup(self):
        """One ulp above3 beats3; parameter .5 plus one ulp does not match .5.

        Catches tolerance in selection or key lookup; no lattice snapping allowed.
        """
        sources = fixture_d()
        sources["w1"][3]["objective"] = math.nextafter(3.0, math.inf)
        self.assertEqual(analyse(sources)["per_window"][0]["selected"]["trial_id"], 3)
        sources = fixture_d()
        sources["w2"][0]["parameters"]["B"] = math.nextafter(0.5, math.inf)
        self.absent_pair(analyse(sources)["carry_over"][0], "no_row")

    def test_categorical_bounds_and_name_order(self):
        """Two sole winners categorical a/b give distinct2 and null extremes.

        Dimension insertion order z,A must output A,z. Catches invented category
        magnitude, normalising recorded choices or using space insertion order.
        """
        space = {
            "parameters": {
                "z": {"kind": "categorical", "choices": ["a", "b"]},
                "A": {"kind": "boolean"},
            },
            "objective": copy.deepcopy(SPACE["objective"]),
        }
        rows = [
            {
                "trial_id": 0,
                "status": "ok",
                "feasible": True,
                "objective": 1.0,
                "parameters": {"z": "a", "A": False},
                "space": space,
            },
            {
                "trial_id": 0,
                "status": "ok",
                "feasible": True,
                "objective": 2.0,
                "parameters": {"z": "b", "A": True},
                "space": space,
            },
        ]
        report = analyse({"w1": rows[:1], "w2": rows[1:]}, ["w1", "w2"])
        self.assertEqual(list(report["stability"]["dimensions"]), ["A", "z"])
        self.assertEqual(
            report["stability"]["dimensions"]["z"],
            {
                "kind": "categorical",
                "values": ["a", "b"],
                "distinct": 2,
                "minimum": None,
                "maximum": None,
            },
        )

    def test_returned_document_and_fixed_text(self):
        """Accepted document has every named key and fixed interpretation verbatim.

        Catches missing empty fields, incorrect contract/algorithm, renamed
        row_found or altered claims about finished studies.
        """
        report = analyse()
        self.assertEqual(
            set(report),
            {
                "contract",
                "status",
                "metric",
                "direction",
                "windows",
                "supplied",
                "never_run",
                "rows_read",
                "rows_eligible",
                "rows_excluded",
                "per_window",
                "carry_over",
                "chain_breaks",
                "stability",
                "work",
                "algorithm",
                "input_sha256",
                "interpretation",
            },
        )
        self.assertEqual(CONTRACT, "pineforge-hpo-walkforward/v1")
        self.assertEqual(ALGORITHM, "exact_per_window_v1")
        self.assertEqual(DIRECTIONS, ("maximize", "minimize"))
        self.assertEqual(
            (
                report["contract"],
                report["algorithm"],
                report["status"],
                report["metric"],
                report["direction"],
            ),
            (
                "pineforge-hpo-walkforward/v1",
                "exact_per_window_v1",
                "ok",
                "objective",
                "maximize",
            ),
        )
        self.assertEqual(report["interpretation"], FIXED_TEXT)
        self.assertEqual(INTERPRETATION, FIXED_TEXT)
        for entry in report["carry_over"]:
            self.assertIn("row_found", entry)
            self.assertNotIn("evaluated", entry)

    def test_refusal_class_document_and_pickle(self):
        """Error sorts ids3,1 to1,3, keeps names order and renders refused shape.

        Catches unsorted ids, sorted names, wrong document shape or pickle loss.
        """
        error = WalkforwardError(
            "walkforward_input_invalid", "bad cell", [3, 1], ["w2", PROFIT]
        )
        expected = {
            "contract": "pineforge-hpo-walkforward/v1",
            "status": "refused",
            "refusal": {
                "code": "walkforward_input_invalid",
                "detail": "bad cell",
                "ids": [1, 3],
                "names": ["w2", PROFIT],
            },
        }
        self.assertEqual(error.document(), expected)
        self.assertEqual(pickle.loads(pickle.dumps(error)).document(), expected)
        self.assertEqual(
            set(CODES),
            {
                "walkforward_" + name
                for name in (
                    "parameter_missing",
                    "parameter_invalid",
                    "window_unknown",
                    "no_supplied_sources",
                    "input_invalid",
                    "rows_incomplete",
                    "space_missing",
                    "space_mismatch",
                    "context_mismatch",
                    "direction_mismatch",
                    "metric_unknown",
                    "duplicate_vector",
                    "no_eligible_rows",
                    "resource_exceeded",
                )
            },
        )
        with self.assertRaises(ValueError):
            WalkforwardError("unknown", "bad")

    def test_no_input_mutation_or_return_alias(self):
        """Analysing D preserves all supplied data; editing returned parameters
        or stability lists leaves input unchanged and next call reproduces D.

        Catches modifying immutable rows or returning writable input aliases.
        """
        sources = fixture_d()
        before = copy.deepcopy(sources)
        report = analyse(sources)
        self.assertEqual(sources, before)
        report["per_window"][0]["selected"]["parameters"]["A"] = 99
        report["carry_over"][0]["parameters"]["C"] = True
        report["stability"]["dimensions"]["A"]["values"][0] = 99
        self.assertEqual(sources, before)
        self.assertEqual(analyse(sources), analyse())
