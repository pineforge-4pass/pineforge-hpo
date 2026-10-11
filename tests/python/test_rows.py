"""Tests of the shared rows reader.

Drafts cited: ``methods-neighbourhood-contract.proposed.md`` (N-draft) and
``methods-pareto-contract.proposed.md`` (P-draft); clause 3.2 is "Input", clause 3.8
(N-draft) and 3.7 (P-draft) are the refusals.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import pickle
import struct
import unittest

from pineforge_hpo import _rows, continuation
from pineforge_hpo._rows import (
    KINDS,
    STATUSES,
    Row,
    RowsError,
    Source,
    canonical_space,
    check_context,
    check_unique_ids,
    common_space,
    loads_strict,
    read_source,
    validate_row,
    vector_key,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rows"


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_document(name):
    return loads_strict(fixture_text(name))


def fixture_rows(name):
    return [loads_strict(line) for line in fixture_text(name).split("\n") if line.strip()]


def make_row(trial_id, status="ok", objective=1.0, **extra):
    row = {
        "trial_id": trial_id,
        "status": status,
        "feasible": status == "ok",
        "objective": objective,
    }
    row.update(extra)
    return row


SPACE = fixture_document("result_all.json")["space"]
REAL_SPACE = {
    "parameters": {
        "n": {"kind": "integer", "low": 1, "high": 9, "step": 1, "log": False},
        "x": {"kind": "real", "low": 0.0, "high": 1.0, "step": None, "log": False},
    },
    "objective": SPACE["objective"],
}


class RowsTestCase(unittest.TestCase):
    def refusal(self, function, *args, **kwargs):
        """Call ``function`` and return the RowsError it must raise."""
        with self.assertRaises(RowsError) as caught:
            function(*args, **kwargs)
        return caught.exception


class RowsErrorTests(RowsTestCase):
    def test_kind_is_one_of_the_five_kinds(self):
        """Rule: kind is one of the five of the fixed API (N-draft 3.8)."""
        self.assertEqual(
            KINDS,
            (
                "input_invalid",
                "rows_incomplete",
                "space_missing",
                "space_mismatch",
                "context_mismatch",
            ),
        )
        with self.assertRaises(ValueError):
            RowsError("made_up_kind", "a sentence")

    def test_fields_are_kept_and_ids_are_ascending(self):
        """Positive control: the four fields come back as given, ids sorted."""
        error = RowsError("input_invalid", "one sentence", [5, 2, 9], ["b", "a"])
        self.assertEqual(error.kind, "input_invalid")
        self.assertEqual(error.detail, "one sentence")
        self.assertEqual(error.ids, (2, 5, 9))
        self.assertEqual(error.names, ("b", "a"))
        self.assertEqual(str(error), "input_invalid: one sentence")
        pairs = RowsError("context_mismatch", "x", [(1, 0), (0, 7), (0, 2)])
        self.assertEqual(pairs.ids, ((0, 2), (0, 7), (1, 0)))
        empty = RowsError("space_missing", "nothing recorded")
        self.assertEqual((empty.ids, empty.names), ((), ()))

    def test_error_survives_pickling(self):
        """Rule: the error can cross a process boundary with its fields intact."""
        error = RowsError("space_mismatch", "differs", [(0, 1)], ["Fast"])
        # Round trip of an object built here; no untrusted bytes are loaded.
        again = pickle.loads(pickle.dumps(error))
        self.assertIsInstance(again, RowsError)
        self.assertEqual(
            (again.kind, again.detail, again.ids, again.names),
            ("space_mismatch", "differs", ((0, 1),), ("Fast",)),
        )


class StatusesTests(RowsTestCase):
    def test_statuses_are_the_nine_of_the_loader(self):
        """Rule: the status set is the nine of N-draft F14 (continuation.py:48-58)."""
        self.assertIsInstance(STATUSES, tuple)
        self.assertEqual(len(STATUSES), 9)
        self.assertEqual(len(set(STATUSES)), 9)
        self.assertEqual(set(STATUSES), set(continuation._STATUSES))
        self.assertEqual(STATUSES[:2], ("ok", "constraint_violation"))
        self.assertEqual(STATUSES[-2:], ("pruned", "partial"))

    def test_public_names_exist(self):
        """Positive control: every name of the fixed API is defined and exported."""
        for name in (
            "RowsError",
            "STATUSES",
            "loads_strict",
            "read_source",
            "Source",
            "Row",
            "validate_row",
            "check_unique_ids",
            "canonical_space",
            "common_space",
            "check_context",
            "vector_key",
        ):
            self.assertIn(name, _rows.__all__)
            self.assertTrue(hasattr(_rows, name), name)


class LoadsStrictTests(RowsTestCase):
    def detail(self, text):
        error = self.refusal(loads_strict, text)
        self.assertEqual(error.kind, "input_invalid")
        return error.detail

    def test_plain_json_is_returned_unchanged(self):
        """Positive control: ordinary JSON parses to the same values and types."""
        text = '{"a": 1, "b": [1.5, "x", null, true], "c": {"d": -0.0}}'
        self.assertEqual(
            loads_strict(text),
            {"a": 1, "b": [1.5, "x", None, True], "c": {"d": -0.0}},
        )
        self.assertIs(type(loads_strict("[1]")[0]), int)
        self.assertIs(type(loads_strict("[1.0]")[0]), float)
        self.assertEqual(math.copysign(1.0, loads_strict("-0.0")), -1.0)
        self.assertEqual(loads_strict(b'{"a": 1}'), {"a": 1})
        self.assertEqual(loads_strict("[100000000000000000000]"), [10**20])
        self.assertEqual(loads_strict('[{"a": 1}, {"a": 2}]'), [{"a": 1}, {"a": 2}])

    def test_duplicate_key_is_refused(self):
        """Rule: duplicate JSON keys refuse (N-draft 3.2(d); continuation.py:201-206)."""
        self.assertIn("duplicate JSON key", self.detail('{"a": 1, "a": 2}'))
        self.assertIn("duplicate JSON key", self.detail('{"o": {"a": 1, "b": 2, "a": 3}}'))
        self.assertIn("duplicate JSON key", self.detail('[{"x": 1}, {"y": 1, "y": 1}]'))

    def test_nan_token_is_refused(self):
        """Rule: the NaN constant refuses (N-draft 3.2(d); P-draft 3.2(b))."""
        self.assertIn("NaN", self.detail('{"x": NaN}'))
        self.assertIn("NaN", self.detail("[1, NaN]"))

    def test_infinity_tokens_are_refused(self):
        """Rule: the Infinity and -Infinity constants refuse (N-draft 3.2(d))."""
        self.assertIn("Infinity", self.detail("[Infinity]"))
        self.assertIn("-Infinity", self.detail('{"x": -Infinity}'))

    def test_overflow_number_is_refused(self):
        """Rule: a number that overflows to infinity refuses (N-draft 3.2(d))."""
        self.assertIn("overflows", self.detail("[1e999]"))
        self.assertIn("overflows", self.detail("[-1e999]"))
        self.assertIn("overflows", self.detail('{"a": 2.5e400}'))
        self.assertEqual(loads_strict("[1e308]"), [1e308])

    def test_integer_too_large_for_binary64_is_refused(self):
        """Rule: an integer token that no binary64 can hold refuses like 1e999."""
        self.assertIn("overflows", self.detail("[" + "9" * 400 + "]"))
        self.assertIn("overflows", self.detail("[2" + "0" * 308 + "]"))
        self.assertEqual(loads_strict("[1" + "0" * 308 + "]"), [10**308])

    def test_unreadable_text_is_refused(self):
        """Rule: every defect, including bad syntax and bad types, is input_invalid."""
        self.assertIn("invalid JSON", self.detail('{"a": '))
        self.assertIn("string", self.detail(5))
        self.assertIn("UTF-8", self.detail(b"\xff\xfe"))


class ValidateRowTests(RowsTestCase):
    def test_every_status_is_accepted_when_consistent(self):
        """Positive control: each of the nine statuses passes with matching fields."""
        for status in STATUSES:
            scored = status in ("ok", "constraint_violation")
            raw = make_row(7, status, 1.5 if scored else None)
            row = validate_row(raw, 3)
            self.assertIsInstance(row, Row)
            self.assertEqual(row.source_index, 3)
            self.assertEqual(row.trial_id, 7)
            self.assertEqual(row.status, status)
            self.assertEqual(row.feasible, status == "ok")
            self.assertEqual(row.objective, 1.5 if scored else None)
            self.assertIs(row.raw, raw)
            for member in (row.parameters, row.metrics, row.backtest, row.space_hash, row.space):
                self.assertIsNone(member)

    def test_unknown_status_is_refused(self):
        """Rule: status must be one of the nine (N-draft 3.2(b), 3.8 input_invalid)."""
        error = self.refusal(validate_row, make_row(4, "weird", None), 0)
        self.assertEqual(error.kind, "input_invalid")
        self.assertEqual(error.ids, (4,))
        for status in (5, None, ["ok"], "OK"):
            self.assertEqual(
                self.refusal(validate_row, make_row(4, status, None), 0).kind,
                "input_invalid",
            )

    def test_feasible_must_equal_status_ok(self):
        """Rule: feasible is a bool equal to (status is ok) (continuation.py:364-367)."""
        for raw in (
            make_row(2, "ok", 1.0, feasible=False),
            make_row(2, "engine_error", None, feasible=True),
            make_row(2, "constraint_violation", 1.0, feasible=True),
            make_row(2, "ok", 1.0, feasible=1),
            make_row(2, "ok", 1.0, feasible=None),
        ):
            error = self.refusal(validate_row, raw, 0)
            self.assertEqual(error.kind, "input_invalid")
            self.assertEqual(error.ids, (2,))
        missing = make_row(2)
        del missing["feasible"]
        self.assertEqual(self.refusal(validate_row, missing, 0).kind, "input_invalid")

    def test_scored_row_needs_a_finite_objective(self):
        """Rule: ok and constraint_violation rows need a finite number (N-draft 3.2(b))."""
        bad = (
            None,
            float("nan"),
            float("inf"),
            float("-inf"),
            "1.0",
            True,
            [1.0],
            10**400,
        )
        for status in ("ok", "constraint_violation"):
            for objective in bad:
                error = self.refusal(validate_row, make_row(3, status, objective), 0)
                self.assertEqual(error.kind, "input_invalid")
                self.assertEqual(error.ids, (3,))
        absent = make_row(3)
        del absent["objective"]
        self.assertEqual(self.refusal(validate_row, absent, 0).kind, "input_invalid")

    def test_other_rows_may_hold_null_but_not_a_non_finite_objective(self):
        """Rule: objective is null or a finite number (continuation.py:368-371)."""
        self.assertIsNone(validate_row(make_row(3, "engine_error", None), 0).objective)
        self.assertEqual(validate_row(make_row(3, "pruned", 2.5), 0).objective, 2.5)
        self.assertEqual(self.refusal(validate_row, make_row(3, "pruned", float("nan")), 0).kind, "input_invalid")
        seven = validate_row(make_row(3, "ok", 7), 0).objective
        self.assertEqual(seven, 7.0)
        self.assertIs(type(seven), float)
        zero = validate_row(make_row(3, "ok", -0.0), 0).objective
        self.assertEqual(math.copysign(1.0, zero), -1.0)

    def test_trial_id_rules(self):
        """Rule: trial_id is an int (not bool) with 0 <= id < 2**64 - 1 (continuation.py:356)."""
        self.assertEqual(validate_row(make_row(0), 0).trial_id, 0)
        self.assertEqual(validate_row(make_row(2**64 - 2), 0).trial_id, 2**64 - 2)
        for identifier in (True, False, 1.0, "1", None):
            error = self.refusal(validate_row, make_row(identifier), 0)
            self.assertEqual(error.kind, "input_invalid")
            self.assertEqual(error.ids, ())
        for identifier in (-1, 2**64 - 1, 2**64):
            error = self.refusal(validate_row, make_row(identifier), 0)
            self.assertEqual(error.kind, "input_invalid")
            self.assertEqual(error.ids, (identifier,))
        missing = make_row(1)
        del missing["trial_id"]
        self.assertEqual(self.refusal(validate_row, missing, 0).kind, "input_invalid")

    def test_optional_members_pass_through_or_are_none(self):
        """Rule: parameters, metrics, backtest, space and space_hash stay None when absent."""
        extras = {
            "parameters": {"Fast": 4},
            "metrics": {"m": 1.0},
            "backtest": {"input_bars_processed": 10},
            "space": {"parameters": {}},
            "space_hash": "abc",
        }
        row = validate_row(make_row(1, **extras), 0)
        self.assertEqual(row.parameters, {"Fast": 4})
        self.assertEqual(row.metrics, {"m": 1.0})
        self.assertEqual(row.backtest, {"input_bars_processed": 10})
        self.assertEqual(row.space, {"parameters": {}})
        self.assertEqual(row.space_hash, "abc")
        for key, wrong in (
            ("parameters", []),
            ("metrics", None),
            ("backtest", "x"),
            ("space", 5),
            ("space_hash", 7),
        ):
            error = self.refusal(validate_row, make_row(1, **{key: wrong}), 0)
            self.assertEqual(error.kind, "input_invalid")
            self.assertEqual(error.ids, (1,))

    def test_row_and_index_must_be_well_formed(self):
        """Rule: a row is a JSON object and the source index a non-negative int."""
        self.assertEqual(self.refusal(validate_row, [1, 2], 0).kind, "input_invalid")
        self.assertEqual(self.refusal(validate_row, make_row(1), -1).kind, "input_invalid")
        self.assertEqual(self.refusal(validate_row, make_row(1), True).kind, "input_invalid")


class UniqueIdsTests(RowsTestCase):
    def rows(self, identifiers, index=0):
        return [validate_row(make_row(identifier), index) for identifier in identifiers]

    def test_unique_ids_pass(self):
        """Positive control: distinct ids give None, also the same ids in two sources."""
        self.assertIsNone(check_unique_ids(self.rows([0, 1, 2])))
        self.assertIsNone(check_unique_ids(self.rows([0, 1], 0) + self.rows([0, 1], 1)))
        self.assertIsNone(check_unique_ids([]))

    def test_repeated_id_is_refused(self):
        """Rule: ids are unique inside one source (N-draft 3.2(a); continuation.py:358)."""
        error = self.refusal(check_unique_ids, self.rows([0, 3, 1, 3, 3, 0]))
        self.assertEqual(error.kind, "input_invalid")
        self.assertEqual(error.ids, (0, 3))

    def test_repeat_among_several_sources_is_reported_as_pairs(self):
        """Rule: with rows of several sources the repeated ids carry their source."""
        error = self.refusal(
            check_unique_ids, self.rows([0, 1], 0) + self.rows([5, 5], 1)
        )
        self.assertEqual(error.kind, "input_invalid")
        self.assertEqual(error.ids, ((1, 5),))

    def test_read_source_refuses_a_repeated_id(self):
        """Rule: a source never contains two rows with one id."""
        error = self.refusal(read_source, [make_row(1), make_row(2), make_row(1)])
        self.assertEqual(error.kind, "input_invalid")
        self.assertEqual(error.ids, (1,))


class ReadSourceTests(RowsTestCase):
    def test_complete_result_file(self):
        """Positive control: a complete result is read in file order with space and direction."""
        path = FIXTURES / "result_all.json"
        source = read_source(path)
        self.assertIsInstance(source, Source)
        self.assertEqual(source.index, 0)
        self.assertIsInstance(source.rows, tuple)
        self.assertEqual([row.trial_id for row in source.rows], [0, 1, 2, 3, 4, 5])
        self.assertEqual(
            [row.status for row in source.rows],
            ["ok", "ok", "ok", "constraint_violation", "engine_error", "ok"],
        )
        self.assertEqual(
            [row.objective for row in source.rows], [10.0, 9.5, 7.0, 12.0, None, 10.5]
        )
        self.assertIs(type(source.rows[2].objective), float)
        self.assertEqual(source.direction, "maximize")
        self.assertEqual(source.space, SPACE)
        self.assertEqual(source.rows[0].parameters, {"Fast": 4, "Flag": False, "Mode": "ema"})
        self.assertEqual(source.rows[4].metrics, {"metrics.all.net_profit_pct": None})
        self.assertEqual(source.rows[0].backtest["input_tf_seconds"], 3600)
        self.assertEqual(source.rows[0].space_hash, "0123456789abcdef" * 4)
        indexed = read_source(str(path), index=4)
        self.assertEqual(indexed.index, 4)
        self.assertTrue(all(row.source_index == 4 for row in indexed.rows))

    def test_warm_result_rows_are_the_ancestors_then_the_trials(self):
        """Rule: rows = warm_start_trials + trials (N-draft 3.2(a); continuation.py:289)."""
        source = read_source(FIXTURES / "result_warm.json")
        self.assertEqual([row.trial_id for row in source.rows], [0, 1, 2, 3])
        for ancestor in source.rows[:2]:
            self.assertIsNone(ancestor.metrics)
            self.assertIsNone(ancestor.backtest)
            self.assertIsNone(ancestor.space_hash)
            self.assertEqual(ancestor.raw["constraint_values"], [])
        self.assertEqual(source.rows[3].metrics, {"metrics.all.net_profit_pct": 10.5})
        self.assertEqual(source.space, SPACE)

    def test_best_k_result_is_incomplete(self):
        """Rule: trials_out other than all raises rows_incomplete (N-draft 3.8)."""
        error = self.refusal(read_source, FIXTURES / "result_best_k.json")
        self.assertEqual(error.kind, "rows_incomplete")
        for mode in ("best-k", "none", "summary", None):
            document = fixture_document("result_all.json")
            document["trials_out"] = mode
            self.assertEqual(self.refusal(read_source, document).kind, "rows_incomplete")
        document = fixture_document("result_all.json")
        document["trials_out"] = "all"
        self.assertEqual(len(read_source(document).rows), 6)
        del document["trials_out"]
        self.assertEqual(len(read_source(document).rows), 6)

    def test_trials_completed_mismatch_is_incomplete(self):
        """Rule: trials_completed must equal len(trials) (continuation.py:279-283)."""
        for wrong in (5, 7, 0, "6", 6.0, True, None):
            document = fixture_document("result_all.json")
            document["trials_completed"] = wrong
            self.assertEqual(self.refusal(read_source, document).kind, "rows_incomplete")
        document = fixture_document("result_all.json")
        self.assertEqual(read_source(document).rows[5].trial_id, 5)

    def test_warm_start_trials_mismatch_is_incomplete(self):
        """Rule: warm_start.trials must equal len(warm_start_trials) (continuation.py:284-288)."""
        document = fixture_document("result_warm.json")
        document["warm_start"]["trials"] = 3
        self.assertEqual(self.refusal(read_source, document).kind, "rows_incomplete")
        document = fixture_document("result_warm.json")
        del document["warm_start_trials"]
        self.assertEqual(self.refusal(read_source, document).kind, "rows_incomplete")
        document = fixture_document("result_warm.json")
        document["warm_start"] = None
        self.assertEqual(self.refusal(read_source, document).kind, "rows_incomplete")
        document = fixture_document("result_warm.json")
        self.assertEqual(len(read_source(document).rows), 4)

    def test_result_shape_defects_are_invalid(self):
        """Rule: a result without a usable trials array is input_invalid."""
        for document in ({"trials_out": "all"}, {"trials": {"a": 1}}, {"trials": []} | {"warm_start_trials": 3}):
            self.assertEqual(self.refusal(read_source, document).kind, "input_invalid")
        self.assertEqual(read_source({"trials": []}).rows, ())

    def test_result_direction_rules(self):
        """Rule: direction is maximize or minimize and agrees with the recorded space."""
        document = fixture_document("result_all.json")
        del document["direction"]
        self.assertEqual(read_source(document).direction, "maximize")
        document = fixture_document("result_all.json")
        del document["space"]
        self.assertEqual(read_source(document).direction, "maximize")
        self.assertIsNone(read_source(document).space)
        del document["direction"]
        self.assertIsNone(read_source(document).direction)
        for wrong in ("up", None, 1):
            document = fixture_document("result_all.json")
            document["direction"] = wrong
            self.assertEqual(self.refusal(read_source, document).kind, "input_invalid")
        document = fixture_document("result_all.json")
        document["direction"] = "minimize"
        self.assertEqual(self.refusal(read_source, document).kind, "input_invalid")
        document = fixture_document("result_all.json")
        document["space"] = 5
        self.assertEqual(self.refusal(read_source, document).kind, "input_invalid")

    def test_jsonl_file(self):
        """Positive control: one row per non-blank line, blank lines skipped."""
        source = read_source(FIXTURES / "rows.jsonl")
        self.assertEqual([row.trial_id for row in source.rows], [0, 1, 2, 3, 4, 5])
        self.assertEqual(
            [row.objective for row in source.rows], [10.0, 9.5, 7.0, 12.0, None, 10.5]
        )
        self.assertIsNone(source.space)
        self.assertIsNone(source.direction)
        spaced = read_source(FIXTURES / "rows_spaced.jsonl")
        self.assertEqual(len(spaced.rows), 2)
        self.assertIsNone(spaced.space)
        self.assertEqual(spaced.direction, "maximize")
        self.assertEqual(spaced.rows[1].space, SPACE)

    def test_jsonl_bad_line_names_the_line_number(self):
        """Rule: a bad JSONL line raises input_invalid naming its line (brief; N-draft 3.8)."""
        error = self.refusal(read_source, FIXTURES / "rows_bad_line.jsonl")
        self.assertEqual(error.kind, "input_invalid")
        self.assertIn("line 4", error.detail)
        self.assertNotIn("line 3", error.detail)

    def test_jsonl_strict_defects_name_the_line_number(self):
        """Rule: duplicate keys and NaN tokens inside a JSONL line name that line."""
        error = self.refusal(read_source, FIXTURES / "rows_dup_key_line.jsonl")
        self.assertEqual(error.kind, "input_invalid")
        self.assertIn("line 2", error.detail)
        self.assertIn("duplicate JSON key", error.detail)
        error = self.refusal(read_source, FIXTURES / "rows_nan_line.jsonl")
        self.assertEqual(error.kind, "input_invalid")
        self.assertIn("line 3", error.detail)
        self.assertIn("NaN", error.detail)

    def test_jsonl_row_defect_names_the_line_number(self):
        """Rule: a row defect inside a JSONL line names its line and its trial id."""
        lines = [json.dumps(make_row(0)), "", json.dumps(make_row(1, "weird", None))]
        error = self.refusal(_rows._source_from_text, "\n".join(lines), 0)
        self.assertEqual(error.kind, "input_invalid")
        self.assertIn("line 3", error.detail)
        self.assertEqual(error.ids, (1,))
        first = [json.dumps(make_row(0, "ok", float("nan"))), json.dumps(make_row(1))]
        error = self.refusal(_rows._source_from_text, "\n".join(first), 0)
        self.assertIn("line 1", error.detail)

    def test_jsonl_with_a_defect_on_its_first_line_still_names_line_one(self):
        """Rule: a strict defect on line 1 of a multi-line file names line 1."""
        text = '{"trial_id": 0, "trial_id": 1}\n' + json.dumps(make_row(2))
        error = self.refusal(_rows._source_from_text, text, 0)
        self.assertIn("line 1", error.detail)
        self.assertIn("duplicate JSON key", error.detail)

    def test_dict_list_and_path_give_the_same_rows(self):
        """Rule: a path, a parsed result and a parsed row list read equal (brief)."""
        path = FIXTURES / "result_all.json"
        document = fixture_document("result_all.json")
        from_path = read_source(path)
        self.assertEqual(from_path, read_source(document))
        self.assertEqual(from_path, read_source(str(path)))
        from_list = read_source(document["trials"])
        self.assertEqual(from_list.rows, from_path.rows)
        self.assertIsNone(from_list.space)
        from_jsonl = read_source(FIXTURES / "rows.jsonl")
        self.assertEqual(from_jsonl.rows, from_path.rows)
        self.assertEqual(read_source(fixture_rows("rows.jsonl")), from_jsonl)
        warm = fixture_document("result_warm.json")
        self.assertEqual(read_source(FIXTURES / "result_warm.json"), read_source(warm))
        self.assertEqual(
            read_source(warm["warm_start_trials"] + warm["trials"]).rows,
            read_source(warm).rows,
        )

    def test_text_layout_never_changes_the_result(self):
        """Rule: the same text read whole, re-laid-out or via a path is identical (brief)."""
        path = FIXTURES / "result_all.json"
        text = fixture_text("result_all.json")
        expected = read_source(path)
        self.assertEqual(_rows._source_from_text(path.read_bytes().decode("utf-8"), 0), expected)
        document = json.loads(text)
        for variant in (
            text,
            json.dumps(document),
            json.dumps(document, indent=4),
            "\n\n" + text + "\n\n",
        ):
            self.assertEqual(_rows._source_from_text(variant, 0), expected)
        jsonl = fixture_text("rows.jsonl")
        expected = read_source(FIXTURES / "rows.jsonl")
        lines = [line for line in jsonl.split("\n") if line.strip()]
        for variant in (
            jsonl,
            "\n".join(lines),
            "\r\n".join(lines) + "\r\n",
            "\n\n".join(lines) + "\n\n\n",
            "  " + "\n   ".join(lines) + "  ",
        ):
            self.assertEqual(_rows._source_from_text(variant, 0), expected)
        self.assertEqual(_rows._source_from_text(lines[0], 0).rows, expected.rows[:1])
        self.assertEqual(_rows._source_from_text("\n".join(lines[:2]), 0).rows, expected.rows[:2])

    def test_lone_row_object_is_a_one_row_source(self):
        """Rule: one row object, on one line or indented, is a one-row source."""
        raw = make_row(9, "ok", 2.5)
        for text in (json.dumps(raw), json.dumps(raw, indent=2), "\n" + json.dumps(raw) + "\n"):
            source = _rows._source_from_text(text, 2)
            self.assertEqual([row.trial_id for row in source.rows], [9])
            self.assertEqual(source.rows[0].source_index, 2)
            self.assertEqual(source.rows[0].objective, 2.5)

    def test_empty_sources_have_no_rows(self):
        """Rule: a source without rows is returned empty, not refused."""
        for text in ("", "\n", " \n \t\n"):
            source = _rows._source_from_text(text, 1)
            self.assertEqual((source.index, source.rows, source.space, source.direction), (1, (), None, None))
        self.assertEqual(read_source([]).rows, ())

    def test_source_must_be_a_path_a_result_or_a_list(self):
        """Rule: anything else, a missing file or a directory is input_invalid."""
        for source in (5, None, ("a",), 1.5):
            self.assertEqual(self.refusal(read_source, source).kind, "input_invalid")
        self.assertEqual(self.refusal(read_source, FIXTURES / "missing.json").kind, "input_invalid")
        self.assertEqual(self.refusal(read_source, FIXTURES).kind, "input_invalid")
        for index in (-1, True, "0", 1.0):
            self.assertEqual(self.refusal(read_source, [], index).kind, "input_invalid")

    def test_malformed_documents_are_invalid(self):
        """Rule: text that is neither one JSON document nor JSONL is input_invalid."""
        for text in ('{not json}\n{}', '"a string"', "12", "[1, 2]", '[{"trial_id": 1}]', "[]\n[]"):
            self.assertEqual(
                self.refusal(_rows._source_from_text, text, 0).kind, "input_invalid", text
            )


class CanonicalSpaceTests(RowsTestCase):
    def test_canonical_form_is_the_v1_form(self):
        """Positive control: the canonical form of the fixture space, exactly (continuation.py:143-181)."""
        expected = (
            '{"objective":{"constraints":[],"direction":"maximize",'
            '"expression":"metrics.all.net_profit_pct"},'
            '"parameters":{"Fast":{"high":"10","kind":"integer","log":false,'
            '"low":"2","step":"2"},"Flag":{"kind":"boolean"},'
            '"Mode":{"choices":[["string","ema"],["string","sma"],["string","wma"]],'
            '"kind":"categorical"}},"space_hash_version":1}'
        )
        self.assertEqual(canonical_space(SPACE), expected)
        self.assertEqual(canonical_space(SPACE), continuation.canonical_space(SPACE))

    def test_independent_of_key_order_and_number_formatting(self):
        """Rule: equal spaces have equal canonical forms (N-draft 3.2(c))."""
        reordered = {
            "parameters": dict(reversed(list(SPACE["parameters"].items()))),
            "objective": dict(reversed(list(SPACE["objective"].items()))),
        }
        self.assertEqual(canonical_space(reordered), canonical_space(SPACE))
        as_int = {"parameters": {"x": {"kind": "real", "low": 0, "high": 1, "step": None, "log": False}}, "objective": SPACE["objective"]}
        as_float = {"parameters": {"x": {"kind": "real", "low": 0.0, "high": 1.0, "step": None, "log": False}}, "objective": SPACE["objective"]}
        self.assertEqual(canonical_space(as_int), canonical_space(as_float))
        other = copy.deepcopy(SPACE)
        other["parameters"]["Fast"]["high"] = 12
        self.assertNotEqual(canonical_space(other), canonical_space(SPACE))

    def test_malformed_space_is_refused(self):
        """Rule: a space that cannot be canonicalised is input_invalid, not a stray exception."""
        unknown = {"parameters": {"x": {"kind": "weird"}}, "objective": SPACE["objective"]}
        for bad in ({}, {"parameters": {}}, unknown, None, 5, {"parameters": 1, "objective": {}}):
            self.assertEqual(self.refusal(canonical_space, bad).kind, "input_invalid", bad)


class CommonSpaceTests(RowsTestCase):
    def test_equal_spaces_are_returned_as_a_copy(self):
        """Positive control: equal result spaces give that space, as a fresh copy."""
        sources = [
            read_source(FIXTURES / "result_all.json", 0),
            read_source(FIXTURES / "result_warm.json", 1),
        ]
        space = common_space(sources)
        self.assertEqual(space, SPACE)
        self.assertIsNot(space, sources[0].space)
        space["parameters"]["Fast"]["high"] = 99
        self.assertEqual(sources[0].space["parameters"]["Fast"]["high"], 10)

    def test_row_level_spaces_are_used(self):
        """Positive control: a rows-only source gets its space from its rows."""
        source = read_source(FIXTURES / "rows_spaced.jsonl")
        self.assertEqual(common_space([source]), SPACE)

    def test_equal_canonical_form_is_not_a_mismatch(self):
        """Rule: spaces are compared by canonical form, not by text (N-draft 3.2(c))."""
        reordered = fixture_document("result_all.json")
        reordered["space"] = {
            "objective": dict(reversed(list(SPACE["objective"].items()))),
            "parameters": dict(reversed(list(SPACE["parameters"].items()))),
        }
        sources = [read_source(FIXTURES / "result_all.json", 0), read_source(reordered, 1)]
        self.assertEqual(common_space(sources), SPACE)

    def test_space_mismatch_between_two_sources(self):
        """Rule: unequal canonical spaces raise space_mismatch naming dimensions (N-draft 3.8)."""
        other = fixture_document("result_all.json")
        other["space"]["parameters"]["Fast"]["high"] = 12
        sources = [read_source(FIXTURES / "result_all.json", 0), read_source(other, 1)]
        error = self.refusal(common_space, sources)
        self.assertEqual(error.kind, "space_mismatch")
        self.assertEqual(error.names, ("Fast",))
        self.assertEqual(error.ids, ())
        self.assertIn("sources 0, 1", error.detail)

    def test_objective_direction_and_constraints_are_part_of_the_space(self):
        """Rule: objective, direction and constraints are compared too (N-draft 3.2(c))."""
        for change in (
            lambda space: space["objective"].update(direction="minimize"),
            lambda space: space["objective"].update(expression="metrics.all.profit_factor"),
            lambda space: space["objective"].update(constraints=["metrics.all.net_profit_pct > 0"]),
        ):
            other = fixture_document("result_all.json")
            change(other["space"])
            del other["direction"]
            sources = [read_source(FIXTURES / "result_all.json", 0), read_source(other, 1)]
            error = self.refusal(common_space, sources)
            self.assertEqual(error.kind, "space_mismatch")
            self.assertEqual(error.names, ())

    def test_row_level_mismatch_names_the_row(self):
        """Rule: rows that record a different space are named as (source, id) pairs."""
        rows = fixture_rows("rows_spaced.jsonl")
        rows[1]["space"]["parameters"]["Mode"]["choices"] = ["ema", "sma"]
        error = self.refusal(common_space, [read_source(rows, 3)])
        self.assertEqual(error.kind, "space_mismatch")
        self.assertEqual(error.ids, ((3, 1),))
        self.assertEqual(error.names, ("Mode",))

    def test_space_missing(self):
        """Rule: no space anywhere and none passed raises space_missing (N-draft 3.8)."""
        self.assertEqual(self.refusal(common_space, [read_source(FIXTURES / "rows.jsonl")]).kind, "space_missing")
        self.assertEqual(self.refusal(common_space, []).kind, "space_missing")

    def test_passed_space_is_only_a_fallback(self):
        """Rule: a passed space fills in when nothing records one and is never compared."""
        bare = read_source(FIXTURES / "rows.jsonl")
        space = common_space([bare], passed_space=SPACE)
        self.assertEqual(space, SPACE)
        self.assertIsNot(space, SPACE)
        different = copy.deepcopy(SPACE)
        different["parameters"]["Fast"]["high"] = 12
        recorded = read_source(FIXTURES / "result_all.json")
        self.assertEqual(common_space([recorded], passed_space=different), SPACE)
        self.assertEqual(common_space([bare], passed_space=different), different)

    def test_malformed_passed_space_is_refused(self):
        """Rule: a passed space that is not a usable space is input_invalid."""
        bare = [read_source(FIXTURES / "rows.jsonl")]
        for bad in ({"parameters": 1, "objective": {}}, 5, {}):
            self.assertEqual(self.refusal(common_space, bare, bad).kind, "input_invalid")


class CheckContextTests(RowsTestCase):
    def rows(self, mutate=None, name="result_all.json"):
        document = fixture_document(name)
        if mutate is not None:
            mutate(document)
        return read_source(document).rows

    def test_consistent_rows_pass_and_non_ok_rows_are_ignored(self):
        """Positive control: the fixture passes although its engine_error row has zero counters."""
        rows = self.rows()
        self.assertIsNone(check_context(rows))
        self.assertEqual(rows[4].backtest["input_bars_processed"], 0)
        self.assertEqual(rows[0].backtest["input_bars_processed"], 1000)
        self.assertIsNone(check_context(iter(rows)))
        self.assertIsNone(check_context([]))
        self.assertIsNone(check_context(self.rows(name="result_warm.json")))

    def test_backtest_counter_mismatch_among_ok_rows(self):
        """Rule: ok rows must agree on the four counters (N-draft 3.2(e), N14; P-draft 3.2(e))."""
        def mutate(document):
            document["trials"][1]["backtest"]["input_bars_processed"] = 999

        error = self.refusal(check_context, self.rows(mutate))
        self.assertEqual(error.kind, "context_mismatch")
        self.assertEqual(error.ids, ((0, 0), (0, 1)))
        self.assertEqual(error.names, ("input_bars_processed",))

    def test_every_counter_is_checked(self):
        """Rule: each of the four counters is compared."""
        for name in (
            "input_bars_processed",
            "script_bars_processed",
            "input_tf_seconds",
            "script_tf_seconds",
        ):
            def mutate(document, name=name):
                document["trials"][5]["backtest"][name] += 1

            error = self.refusal(check_context, self.rows(mutate))
            self.assertEqual(error.kind, "context_mismatch")
            self.assertEqual(error.names, (name,))
            self.assertEqual(error.ids, ((0, 0), (0, 5)))

    def test_counters_of_rows_that_are_not_ok_are_not_compared(self):
        """Rule: only status ok rows count, so a violation row may differ."""
        def mutate(document):
            document["trials"][3]["backtest"]["script_tf_seconds"] = 60

        self.assertIsNone(check_context(self.rows(mutate)))

    def test_space_hash_mismatch(self):
        """Rule: rows that carry space_hash must carry one value (N-draft 3.2(e))."""
        def mutate(document):
            document["trials"][4]["space_hash"] = "f" * 64

        error = self.refusal(check_context, self.rows(mutate))
        self.assertEqual(error.kind, "context_mismatch")
        self.assertEqual(error.names, ("space_hash",))
        self.assertEqual(error.ids, ((0, 0), (0, 4)))

    def test_rows_of_two_sources_are_compared_together(self):
        """Rule: the check spans sources and names (source, id) pairs."""
        other = fixture_document("result_all.json")
        other["trials"][2]["backtest"]["input_bars_processed"] = 5
        rows = list(read_source(FIXTURES / "result_all.json", 0).rows) + list(
            read_source(other, 1).rows
        )
        error = self.refusal(check_context, rows)
        self.assertEqual(error.kind, "context_mismatch")
        self.assertEqual(error.ids, ((0, 0), (1, 2)))
        same = list(read_source(FIXTURES / "result_all.json", 0).rows) + list(
            read_source(FIXTURES / "result_all.json", 1).rows
        )
        self.assertIsNone(check_context(same))


class VectorKeyTests(RowsTestCase):
    def test_key_of_the_fixture_vector(self):
        """Positive control: typed scalars, sorted names, compact JSON (continuation.py:119-140)."""
        self.assertEqual(
            vector_key({"Fast": 4, "Flag": False, "Mode": "ema"}, SPACE),
            '{"Fast":["integer","4"],"Flag":["boolean",false],"Mode":["string","ema"]}',
        )
        self.assertEqual(
            vector_key({"Mode": "wma", "Flag": True, "Fast": 10}, SPACE),
            '{"Fast":["integer","10"],"Flag":["boolean",true],"Mode":["string","wma"]}',
        )

    def test_real_values_are_binary64_hex(self):
        """Rule: reals are keyed by their binary64 bits (N-draft F23)."""
        bits = struct.pack(">d", 0.1).hex()
        self.assertEqual(bits, "3fb999999999999a")
        self.assertEqual(
            vector_key({"n": 4, "x": 0.1}, REAL_SPACE),
            '{"n":["integer","4"],"x":["real","' + bits + '"]}',
        )

    def test_signed_zero_is_one_value(self):
        """Rule: -0.0 and +0.0 give one key (N-draft N16; continuation.py:112-116)."""
        positive = vector_key({"n": 1, "x": 0.0}, REAL_SPACE)
        negative = vector_key({"n": 1, "x": -0.0}, REAL_SPACE)
        self.assertEqual(positive, negative)
        self.assertEqual(positive, '{"n":["integer","1"],"x":["real","0000000000000000"]}')

    def test_neighbouring_reals_have_different_keys(self):
        """Rule: equality is exact binary64, no tolerance (N-draft 3.4)."""
        first = vector_key({"n": 1, "x": 0.1}, REAL_SPACE)
        second = vector_key({"n": 1, "x": 0.1000000000000001}, REAL_SPACE)
        self.assertNotEqual(first, second)
        self.assertEqual(first, vector_key({"n": 1, "x": 0.1}, REAL_SPACE))
        self.assertNotEqual(
            vector_key({"n": 1, "x": 0.5}, REAL_SPACE),
            vector_key({"n": 1, "x": math.nextafter(0.5, 1.0)}, REAL_SPACE),
        )

    def test_int_in_a_real_dimension_is_the_same_real(self):
        """Rule: the native writer prints 1.0 as 1, so int and float keys agree."""
        self.assertEqual(
            vector_key({"n": 1, "x": 1}, REAL_SPACE), vector_key({"n": 1, "x": 1.0}, REAL_SPACE)
        )

    def test_insertion_order_does_not_matter(self):
        """Rule: the key is canonical, whatever order the parameters were built in."""
        self.assertEqual(
            vector_key({"x": 0.25, "n": 3}, REAL_SPACE), vector_key({"n": 3, "x": 0.25}, REAL_SPACE)
        )

    def test_categorical_values_keep_their_type(self):
        """Rule: a categorical value is keyed by the type of the matching choice."""
        space = {
            "parameters": {"c": {"kind": "categorical", "choices": ["a", 1, 2.5, True]}},
            "objective": SPACE["objective"],
        }
        self.assertEqual(vector_key({"c": "a"}, space), '{"c":["string","a"]}')
        self.assertEqual(vector_key({"c": 1}, space), '{"c":["integer","1"]}')
        self.assertEqual(vector_key({"c": True}, space), '{"c":["boolean",true]}')
        self.assertEqual(
            vector_key({"c": 2.5}, space),
            '{"c":["real","' + struct.pack(">d", 2.5).hex() + '"]}',
        )
        floats = {
            "parameters": {"c": {"kind": "categorical", "choices": [0.5, 1.0]}},
            "objective": SPACE["objective"],
        }
        self.assertEqual(vector_key({"c": 1}, floats), vector_key({"c": 1.0}, floats))
        self.assertEqual(self.refusal(vector_key, {"c": "zzz"}, space).kind, "input_invalid")

    def test_defective_vectors_are_refused(self):
        """Rule: names must equal the dimensions and each value must fit its kind."""
        error = self.refusal(vector_key, {"n": 1}, REAL_SPACE)
        self.assertEqual((error.kind, error.names), ("input_invalid", ("x",)))
        error = self.refusal(vector_key, {"n": 1, "x": 0.5, "extra": 1}, REAL_SPACE)
        self.assertEqual((error.kind, error.names), ("input_invalid", ("extra",)))
        for bad in (
            {"n": 1.0, "x": 0.5},
            {"n": True, "x": 0.5},
            {"n": "1", "x": 0.5},
            {"n": 1, "x": "0.5"},
            {"n": 1, "x": True},
            {"n": 1, "x": float("nan")},
            {"n": 1, "x": float("inf")},
            {"n": 2**63, "x": 0.5},
        ):
            error = self.refusal(vector_key, bad, REAL_SPACE)
            self.assertEqual(error.kind, "input_invalid", bad)
        self.assertEqual(self.refusal(vector_key, [], REAL_SPACE).kind, "input_invalid")
        self.assertEqual(self.refusal(vector_key, {"n": 1}, {}).kind, "input_invalid")
        flag = {"Fast": 4, "Flag": 1, "Mode": "ema"}
        self.assertEqual(self.refusal(vector_key, flag, SPACE).kind, "input_invalid")

    def test_key_equals_the_continuation_tried_key(self):
        """Positive control: the key is the one continuation.py:363 builds for the same values."""
        values = {"Fast": 4, "Flag": False, "Mode": "ema"}
        tried = continuation._dump(
            {name: continuation._scalar(value) for name, value in values.items()}
        )
        self.assertEqual(vector_key(values, SPACE), tried)


if __name__ == "__main__":
    unittest.main()
