#!/usr/bin/env python3
"""Hand-derived contracts for exact curve replay; no native executable is required."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import copy
import csv
from fractions import Fraction
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "g2_oracles"))
import pbo_real_rows as checker
import pbo_native_controls as controls
import pbo_native_proof as proof

F = Fraction


def number(bits: int) -> float:
    return struct.unpack(">d", struct.pack(">Q", bits))[0]


def hand_result() -> tuple:
    # Returns (1, 2, 3, 0, 1): floor grouping at K=5, S=2 is (3, 2).
    # Q=(6,1); means=(2,1/2); M=(2,1/2), also pairwise M=(6/3,1/2).
    curve = [(10 * index, F(equity))
             for index, equity in enumerate((1, 2, 6, 24, 24, 48))]
    rows = [
        {"trial_id": trial_id, "status": "ok",
         "backtest": {"input_bars_processed": 6},
         "block_returns": {"status": "ok", "count": [3, 2],
                           "sum": [6.0, 1.0], "m2": [2.0, 0.5]}}
        for trial_id in (0, 1)
    ]
    result = {
        "return_blocks": {
            "contract": "pineforge-hpo-return-blocks/v1", "series": "bar", "S": 2, "K": 5,
            "n_b": [3, 2], "boundary_time_ms": [0, 30, 50],
            "numeric_build_identity": "hand-derived-unit-input",
        },
        "trials": rows,
    }
    return result, {0: curve, 1: list(curve)}


def control_result() -> tuple:
    # Four two-interval blocks: (0,0), (1,2), (3,0), (1,-1/2).
    # Q=(0,3,3,1/2), M=(0,1/2,9/2,9/8). Only the first block has A=0.
    curve = [(10 * index, F(equity))
             for index, equity in enumerate((1, 1, 1, 2, 6, 24, 24, 48, 24))]
    rows = [
        {"trial_id": trial_id, "status": "ok", "backtest": {"input_bars_processed": 9},
         "block_returns": {"status": "ok", "count": [2, 2, 2, 2],
                           "sum": [0.0, 3.0, 3.0, 0.5], "m2": [0.0, 0.5, 4.5, 1.125]}}
        for trial_id in (0, 1)
    ]
    result = {"trials": rows, "return_blocks": {
        "contract": "pineforge-hpo-return-blocks/v1", "series": "bar", "S": 4, "K": 8,
        "n_b": [2, 2, 2, 2], "boundary_time_ms": [0, 20, 40, 60, 80],
        "numeric_build_identity": "hand-derived-unit-input",
    }}
    return result, {0: curve, 1: list(curve)}


class RoundingTests(unittest.TestCase):
    def test_nearest_even_normal_subnormal_and_sign(self):
        cases = [
            (F(1), 0x3ff0000000000000),
            (F(1) + F(1, 1 << 53), 0x3ff0000000000000),
            (F(1) + F(3, 1 << 53), 0x3ff0000000000002),
            (F(-1) - F(3, 1 << 53), 0xbff0000000000002),
            (F(1, 1 << 1074), 0x0000000000000001),
            (F(1, 1 << 1075), 0x0000000000000000),
            (F(-1, 1 << 1075), 0x8000000000000000),
            (F(3, 1 << 1075), 0x0000000000000002),
            (F(1, 1 << 1022) - F(1, 1 << 1075), 0x0010000000000000),
            (F(2) - F(1, 1 << 53), 0x4000000000000000),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(checker.round_binary64_bits(value), expected)
        self.assertEqual(checker.fraction_from_bits(0x3fd5555555555555),
                         F((1 << 54) - 1, 3 * (1 << 54)))

    def test_division_rounds_before_return_subtraction(self):
        curve = [(index, F(equity)) for index, equity in enumerate((3, 4, 4, 8, 8))]
        first = checker.derive_curve(curve, 2)["blocks"][0]
        # RN(4/3)-1 is 0x1.5555555555554p-2; RN(4/3-1) would end in 5.
        self.assertEqual(first["sum_bits"], 0x3fd5555555555554)
        self.assertEqual(first["Q"], F((1 << 52) - 1, 3 * (1 << 52)))
        self.assertNotEqual(first["sum_bits"], checker.round_binary64_bits(F(1, 3)))

    def test_rounded_mean_deviation_square_and_accumulation(self):
        # (1,0,0) has Q=1 and M=2/3. The rounded mean is 0x1.5555555555555p-2.
        # RN(1-mean) rounds the midpoint up to an even significand. Separately rounded
        # squares and additions leave m2 at the upper neighbour of 2/3.
        block = checker.exact_block([F(1), F(0), F(0)])
        self.assertEqual(block["Q"], F(1))
        self.assertEqual(block["M"], F(2, 3))
        self.assertEqual(block["pairwise_M"], F(2, 3))
        self.assertEqual(block["sum_bits"], 0x3ff0000000000000)
        self.assertEqual(block["m2_bits"], 0x3fe5555555555556)

    def test_m2_identity_requires_equal_exact_values(self):
        checker.check_m2_identity(F(2, 3), F(4, 6))
        with self.assertRaises(checker.CurveCheckError) as caught:
            checker.check_m2_identity(F(2), F(5, 2))
        self.assertEqual(caught.exception.code, "curve_m2_identity")

    def test_exact_block_rejects_an_incorrect_centred_sum(self):
        # Returns (1,2,3) have Q=6, mean=2, centred M=1+0+1=2,
        # and pairwise M=(1+4+1)/3=2. Perturbing only centred M gives 5/2.
        returns = [F(1), F(2), F(3)]
        block = checker.exact_block(returns)
        self.assertEqual((block["Q"], block["M"], block["pairwise_M"]), (F(6), F(2), F(2)))
        centred_sum = checker.exact_centred_m2

        def perturbed(values, mean):
            return centred_sum(values, mean) + F(1, 2)

        with patch.object(checker, "exact_centred_m2", side_effect=perturbed) as altered:
            with self.assertRaises(checker.CurveCheckError) as caught:
                checker.exact_block(returns)
        altered.assert_called_once_with(returns, F(2))
        self.assertEqual(caught.exception.code, "curve_m2_identity")


class CurveTests(unittest.TestCase):
    def setUp(self):
        self.result, self.curves = hand_result()

    def check(self, result=None, curves=None, roster=None):
        selected = self.result if result is None else result
        return checker.check_curves(
            selected, selected["trials"], self.curves if curves is None else curves,
            2, roster=roster,
        )

    def rejects(self, code, action):
        with self.assertRaises(checker.CurveCheckError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_all_rows_and_blocks_with_uneven_floor_grouping(self):
        checked = self.check(roster=[0, 1])
        self.assertEqual((checked["rows"], checked["blocks"]), (2, 4))
        for derived in checked["derived"].values():
            self.assertEqual(derived["K"], 5)
            self.assertEqual(derived["n_b"], [3, 2])
            self.assertEqual(derived["boundary_time_ms"], [0, 30, 50])
            self.assertEqual([block["Q"] for block in derived["blocks"]], [F(6), F(1)])
            self.assertEqual([block["M"] for block in derived["blocks"]], [F(2), F(1, 2)])

    def test_zero_comparisons_fail(self):
        result = copy.deepcopy(self.result)
        result["trials"] = []
        self.rejects("curve_zero_comparisons", lambda: self.check(result, {}))
        self.rejects("curve_zero_comparisons", lambda: checker.check_curves({}, [], {}, 2))

    def test_no_blocks_compared_fails_even_with_a_valid_layout(self):
        layout = {"K": 5, "n_b": [3, 2], "boundary_time_ms": [0, 30, 50], "blocks": []}
        with patch.object(checker, "derive_curve", return_value=layout):
            self.rejects("curve_zero_comparisons", self.check)

    def test_missing_and_extra_curve_ids_fail(self):
        self.rejects("curve_id_mismatch", lambda: self.check(curves={0: self.curves[0]}))
        curves = {**self.curves, 2: self.curves[0]}
        self.rejects("curve_id_mismatch", lambda: self.check(curves=curves))

    def test_duplicate_native_id_fails(self):
        self.result["trials"][1]["trial_id"] = 0
        self.rejects("curve_row_duplicate", self.check)

    def test_admitted_roster_finds_gap_even_if_rows_and_curves_match(self):
        self.rejects("curve_roster_mismatch", lambda: self.check(roster=[0, 1, 2]))

    def test_count_is_exact_even_if_wire_layout_is_self_consistent(self):
        for row in self.result["trials"]:
            row["block_returns"]["count"][1] += 1
        self.result["return_blocks"]["n_b"][1] += 1
        self.result["return_blocks"]["K"] += 1
        self.rejects("curve_count", self.check)

    def test_layout_fields_are_derived_independently(self):
        for field, value, code in (
            ("K", 6, "curve_K"),
            ("n_b", [2, 3], "curve_n_b"),
            ("boundary_time_ms", [0, 31, 50], "curve_boundary"),
        ):
            result = copy.deepcopy(self.result)
            result["return_blocks"][field] = value
            with self.subTest(field=field):
                self.rejects(code, lambda: self.check(result))

    def test_last_row_last_block_is_compared(self):
        self.result["trials"][-1]["block_returns"]["sum"][-1] = 2.0
        self.rejects("curve_sum_bound", self.check)

    def test_sum_bits_are_stricter_than_the_sum_error_bound(self):
        # A one-ulp difference is inside the loose 8*n*u*A bound for this block.
        self.result["trials"][0]["block_returns"]["sum"][0] = number(0x4018000000000001)
        self.rejects("curve_sum_bits", self.check)

    def test_m2_bound_and_bits_are_both_required(self):
        result = copy.deepcopy(self.result)
        result["trials"][0]["block_returns"]["m2"][0] = 4.0
        self.rejects("curve_m2_bound", lambda: self.check(result))
        self.result["trials"][0]["block_returns"]["m2"][0] = number(0x4000000000000001)
        self.rejects("curve_m2_bits", self.check)

    def test_file_errors_have_named_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "curves.jsonl"
            self.rejects("curve_file_missing", lambda: checker.load_curves(path))
            path.write_text("", encoding="utf-8")
            self.rejects("curve_zero_comparisons", lambda: checker.load_curves(path))
            row = {"trial_id": 0, "curve": [[0, "3ff0000000000000"],
                                         [1, "4000000000000000"], [2, "4008000000000000"]]}
            path.write_text((json.dumps(row) + "\n") * 2, encoding="utf-8")
            self.rejects("curve_id_duplicate", lambda: checker.load_curves(path))
            row["curve"][-1][1] = "7ff0000000000000"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            self.rejects("curve_non_finite", lambda: checker.load_curves(path))
            row["curve"][-1][1] = "not-bits"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            self.rejects("curve_bits_invalid", lambda: checker.load_curves(path))

    def test_lossless_file_decoding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "curves.jsonl"
            row = {"trial_id": 4, "curve": [[0, "3ff0000000000001"],
                                         [1, "4000000000000000"], [2, "4008000000000000"]]}
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            self.assertEqual(checker.load_curves(path),
                             {4: [(0, F(1) + F(1, 1 << 52)), (1, F(2)), (2, F(3))]})


class BoundRegimeTests(unittest.TestCase):
    def verify_regime(self, increments, sum_bits, m2_bits, exact_m):
        # These exact dyadic equities have E[i]=2**i*(1+offset[i]*2**-52).
        # Increment 1 gives RN(E[i+1]/E[i])-1 = 1+2**-51; increment 2 gives 1+2**-50.
        # Each block has eight returns. Expected wire values below are hand-derived literals.
        offset = 0
        curve = [(0, F(1))]
        for index, increment in enumerate(increments * 2, 1):
            offset += increment
            curve.append((index, (1 << index) * (F(1) + F(offset, 1 << 52))))
        derived = checker.derive_curve(curve, 2)
        payload = {"count": [8, 8], "sum": [number(sum_bits)] * 2,
                   "m2": [number(m2_bits)] * 2}
        for index, block in enumerate(derived["blocks"]):
            self.assertEqual(block["M"], exact_m)
            self.assertEqual(block["M"], block["pairwise_M"])
            self.assertEqual(block["sum_bits"], sum_bits)
            self.assertEqual(block["m2_bits"], m2_bits)
            residual = abs(F(payload["m2"][index]) - exact_m)
            self.assertGreater(residual, 16 * 8 * checker.U * exact_m)
            self.assertLessEqual(residual, block["m2_bound"])
            checker.compare_block(payload, index, block, self.id())

    def test_constant_series(self):
        """constant series: M=0, but a rounded mean leaves correct m2=2**-101."""
        # Adding eight copies of 1+2*a (a=2**-52) yields RN(sum)=8+8*a,
        # hence mean=1+a and all deviations are a. m2=8*a*a.
        self.verify_regime([1] * 8, 0x4020000000000001, 0x39a0000000000000, F(0))

    def test_tiny_spread_around_a_large_mean(self):
        """tiny spread around a large mean: the second error term covers mean rounding."""
        # Seven returns are 1+2*a and the last is 1+4*a. Exact M=(7/2)*a*a.
        # RN(sum)=8+16*a; mean=1+2*a; only the last deviation contributes 4*a*a.
        self.verify_regime([1] * 7 + [2], 0x4020000000000002, 0x3990000000000000,
                           F(7, 2 * (1 << 104)))


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.result, self.curves = control_result()
        controls.module_table(self.result, 4, [0, 1], "hand-derived-dataset")
        self.derived = checker.check_curves(self.result, self.result["trials"], self.curves, 4)

    def check_control(self, control):
        return controls.check_control(control, self.curves, 4, [0, 1], "hand-derived-dataset")

    def test_curve_control_rejects_a_different_check_code(self):
        control = {"name": "boundary_time", "stage": "curve", "code": "curve_boundary",
                   "result": self.result}
        error = checker.CurveCheckError("curve_count", "count differs")
        with patch.object(checker, "check_curves", side_effect=error) as checked:
            with self.assertRaisesRegex(
                AssertionError,
                "boundary_time: wrong curve check curve_count, expected curve_boundary",
            ):
                self.check_control(control)
        checked.assert_called_once_with(self.result, self.result["trials"], self.curves, 4,
                                        roster=[0, 1])

    def test_curve_control_accepts_its_intended_check_code(self):
        control = {"name": "boundary_time", "stage": "curve", "code": "curve_boundary",
                   "result": self.result}
        error = checker.CurveCheckError("curve_boundary", "boundary differs")
        with patch.object(checker, "check_curves", side_effect=error) as checked:
            self.assertEqual(self.check_control(control),
                             {"name": "boundary_time", "stage": "curve", "code": "curve_boundary"})
        checked.assert_called_once_with(self.result, self.result["trials"], self.curves, 4,
                                        roster=[0, 1])

    def test_all_seven_controls_reach_their_named_checks(self):
        original = copy.deepcopy(self.result)
        expected = [
            ("count_mismatch", "module", "pbo_block_mismatch"),
            ("sum_one_ulp", "curve", "curve_sum_bound"),
            ("self_consistent_count", "curve", "curve_count"),
            ("boundary_time", "curve", "curve_boundary"),
            ("non_positive_equity", "module", "pbo_non_positive_equity"),
            ("m2_one_ulp", "curve", "curve_m2_bits"),
            ("null_payload", "module", "pbo_rows_incomplete"),
        ]
        report = controls.run_controls(self.result, self.curves, 4, [0, 1], "hand-derived-dataset")
        self.assertEqual(
            [(item["name"], item["stage"], item["code"]) for item in report["controls"]],
            expected,
        )
        self.assertEqual(self.result, original)

    def test_one_ulp_sum_control_uses_a_zero_absolute_return_block(self):
        plans = controls.make_controls(self.result, self.derived["derived"])
        plan = next(item for item in plans if item["name"] == "sum_one_ulp")
        original = self.result["trials"][0]["block_returns"]["sum"]
        changed = plan["result"]["trials"][0]["block_returns"]["sum"]
        differences = [index for index in range(4) if original[index] != changed[index]]
        self.assertEqual(len(differences), 1)
        index = differences[0]
        block = self.derived["derived"][0]["blocks"][index]
        self.assertEqual((block["A"], block["Q"], block["sum_bound"]), (F(0), F(0), F(0)))
        self.assertEqual(checker.wire_bits(changed[index]), 1)
        self.assertEqual(F(changed[index]), F(1, 1 << 1074))
        self.assertEqual(self.check_control(plan)["code"], "curve_sum_bound")

    def test_sum_bound_control_without_a_flat_block_fails_explicitly(self):
        result, curves = hand_result()
        derived = checker.check_curves(result, result["trials"], curves, 2)
        with self.assertRaisesRegex(AssertionError, "zero-return curve block"):
            controls.make_controls(result, derived["derived"])

    def test_every_mutated_copy_is_actually_checked(self):
        with patch.object(controls, "check_control", wraps=controls.check_control) as observed:
            controls.run_controls(self.result, self.curves, 4, [0, 1], "hand-derived-dataset")
        self.assertEqual(
            [call.args[0]["name"] for call in observed.call_args_list],
            ["count_mismatch", "sum_one_ulp", "self_consistent_count", "boundary_time",
             "non_positive_equity", "m2_one_ulp", "null_payload"],
        )

    def test_each_control_fails_if_its_mutation_is_removed(self):
        for control in controls.make_controls(self.result, self.derived["derived"]):
            with self.subTest(name=control["name"]):
                control["result"] = copy.deepcopy(self.result)
                with self.assertRaises(AssertionError):
                    self.check_control(control)

    def test_each_control_fails_for_an_unrelated_refusal(self):
        from pineforge_hpo import pbo

        for control in controls.make_controls(self.result, self.derived["derived"]):
            with self.subTest(name=control["name"]):
                control["result"] = copy.deepcopy(self.result)
                control["result"]["trials"][0]["status"] = "trial_error"
                # A curve control propagates the module refusal; a module control rejects
                # its code. Neither can be counted as the intended outcome.
                if control["stage"] == "module":
                    with self.assertRaisesRegex(AssertionError,
                                                "wrong module refusal pbo_status_error"):
                        self.check_control(control)
                else:
                    with self.assertRaises(pbo.PboError) as caught:
                        self.check_control(control)
                    self.assertEqual(caught.exception.code, "pbo_status_error")

    def test_skipping_any_control_is_a_failure(self):
        original = controls.make_controls(self.result, self.derived["derived"])
        for omitted in range(len(original)):
            with self.subTest(omitted=original[omitted]["name"]):
                with patch.object(controls, "make_controls",
                                  return_value=original[:omitted] + original[omitted + 1:]):
                    with self.assertRaisesRegex(AssertionError, "seven-control roster"):
                        controls.run_controls(self.result, self.curves, 4, [0, 1],
                                              "hand-derived-dataset")

    def test_skipping_a_named_check_is_not_evidence(self):
        original = controls.make_controls(self.result, self.derived["derived"])
        for control in original:
            with self.subTest(name=control["name"]):
                if control["stage"] == "module":
                    with patch.object(controls, "module_table", return_value=None):
                        with self.assertRaisesRegex(AssertionError, "accepted the mutation"):
                            self.check_control(control)
                else:
                    with patch.object(checker, "check_curves", return_value=None):
                        with self.assertRaisesRegex(AssertionError, "accepted the mutation"):
                            self.check_control(control)


class DriverTests(unittest.TestCase):
    def test_only_exit_zero_and_an_agree_line_pass(self):
        agreed = ("RESULT: AGREE (module and exact oracle give the same counts "
                  "and rates on these rows)")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                proof.final_agreement(subprocess.CompletedProcess([], 0, agreed)), agreed,
            )
            for code in (1, 2, 3, 4, 5, 6):
                with self.subTest(code=code):
                    with self.assertRaisesRegex(AssertionError, "exited without agreement"):
                        proof.final_agreement(subprocess.CompletedProcess([], code, agreed))
            for output in ("", "RESULT: INCONCLUSIVE", agreed + "\n" + agreed):
                with self.subTest(output=output):
                    with self.assertRaisesRegex(AssertionError, "exactly one RESULT: AGREE"):
                        proof.final_agreement(subprocess.CompletedProcess([], 0, output))

    def test_curve_verdict_requires_all_blocks_and_nonzero_rows(self):
        valid = subprocess.CompletedProcess([], 0, "CURVES: AGREE rows=25 blocks=400\n")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(proof.curve_agreement(valid, 25, 16),
                             "CURVES: AGREE rows=25 blocks=400")
        self.assertEqual(output.getvalue().splitlines(),
                         ["curve-check exit code: 0", "CURVES: AGREE rows=25 blocks=400"])
        for code, text, rows in (
            (1, valid.stdout, 25), (0, "", 25),
            (0, "CURVES: AGREE rows=25 blocks=399\n", 25),
            (0, "CURVES: AGREE rows=24 blocks=384\n", 25),
            (0, valid.stdout * 2, 25), (0, "CURVES: AGREE rows=0 blocks=0\n", 0),
        ):
            with self.subTest(code=code, rows=rows, output=text):
                output = io.StringIO()
                with redirect_stdout(output):
                    with self.assertRaisesRegex(AssertionError, "every native row and block"):
                        proof.curve_agreement(
                            subprocess.CompletedProcess([], code, text), rows, 16,
                        )
                self.assertNotIn("CURVES:", output.getvalue())

    def test_driver_requires_every_observed_control_code(self):
        result, curves = control_result()
        report = controls.run_controls(result, curves, 4, [0, 1], "hand-derived-dataset")
        proof.control_agreement(report)
        for position in range(7):
            changed = copy.deepcopy(report)
            changed["controls"][position]["code"] = "unrelated_failure"
            with self.subTest(position=position):
                with self.assertRaisesRegex(AssertionError, "every intended control check"):
                    proof.control_agreement(changed)

    def test_missing_proof_inputs_fail_without_a_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            native, plugin, harness = (path / name for name in ("native", "plugin", "harness"))
            argv = ["--native", str(native), "--plugin", str(plugin), "--harness", str(harness),
                    "--engine-root", str(path), "--out", str(path / "output")]
            for label, create in (("proof executable", native), ("strategy plugin", plugin),
                                  ("C-ABI curve probe", harness)):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(proof.main(argv), 1)
                self.assertIn(f"missing {label}", output.getvalue())
                create.write_bytes(b"input placeholder; never executed")
            self.assertFalse((path / "output").exists())

    def test_driver_roster_is_independent_of_rows_and_requires_distinct_curves(self):
        study = json.loads(proof.STUDY.read_text(encoding="utf-8"))
        names, vectors, roster = proof.admitted_grid(study)
        rows = [{"trial_id": trial_id,
                 "parameters": dict(zip(names, map(float, vector))),
                 "backtest": {"input_bars_processed": 2048}}
                for trial_id, vector in enumerate(sorted(vectors))]
        result = {"trials": rows}
        curves = {trial_id: [(0, F(trial_id + 1)), (1, F(trial_id + 2)),
                             (2, F(trial_id + 3))] for trial_id in roster}
        proof.check_roster(result, curves, names, vectors, roster, 2048)
        changed = copy.deepcopy(result)
        changed["trials"].pop()
        with self.assertRaisesRegex(AssertionError, "incomplete native grid"):
            proof.check_roster(changed, {key: value for key, value in curves.items() if key != 24},
                               names, vectors, roster, 2048)
        changed = copy.deepcopy(result)
        changed["trials"][1]["parameters"] = changed["trials"][0]["parameters"]
        with self.assertRaisesRegex(AssertionError, "parameter vectors differ"):
            proof.check_roster(changed, curves, names, vectors, roster, 2048)
        duplicate_curves = {trial_id: curves[0] for trial_id in roster}
        with self.assertRaisesRegex(AssertionError, "distinct original trial curves"):
            proof.check_roster(result, duplicate_curves, names, vectors, roster, 2048)


class ProducerTests(unittest.TestCase):
    def exercise_forwarding(self, blocks, with_curves):
        sys.path.insert(0, str(ROOT / "python"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            args = argparse.Namespace(
                study=None, engine_root="engine", cache_dir=str(path / "cache"),
                native="native", out=str(path / "rows.json"), pbo_blocks=blocks,
                proof_curves=str(path / "curves.jsonl") if with_curves else None,
            )
            axes = [{"name": "metrics.all.net_profit"},
                    {"name": "metrics.equity.max_equity_drawdown"}]
            expected = ["native", "run", "--record-metric", "metrics.all.net_profit",
                        "--record-metric", "metrics.equity.max_equity_drawdown"]
            if blocks is not None:
                expected += ["--pbo-blocks", str(blocks)]
            if with_curves:
                expected += ["--proof-curves", args.proof_curves]
            completed = subprocess.CompletedProcess(expected, 0, '{"trials": []}', "")
            prepared = (["native", "run"], {})
            with patch("pineforge_hpo.prepare_run", return_value=prepared) as prepare:
                with patch.object(proof.producer.subprocess, "run", return_value=completed) as run:
                    with redirect_stdout(io.StringIO()):
                        proof.producer.run_native(args, axes)
            prepare.assert_called_once_with(proof.producer.STUDY, "engine", args.cache_dir,
                                            native="native")
            run.assert_called_once_with(expected, text=True, capture_output=True, check=False)
            metadata = path / "rows.artifact.json"
            if with_curves:
                document = json.loads(metadata.read_text(encoding="utf-8"))
                self.assertEqual(document["command"], expected)
            else:
                self.assertFalse(metadata.exists())

    def test_default_command_is_unchanged(self):
        self.exercise_forwarding(None, False)

    def test_blocks_and_proof_curves_follow_metric_flags(self):
        self.exercise_forwarding(16, True)

    def test_blocks_only_do_not_request_or_write_curves(self):
        self.exercise_forwarding(16, False)


class SyntheticInputTests(unittest.TestCase):
    def test_generator_reproduces_committed_bytes_twice(self):
        path = ROOT / "examples" / "pbo_proof" / "generate.py"
        spec = importlib.util.spec_from_file_location("pbo_proof_generator", path)
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        original = path.with_name("ohlcv.csv").read_bytes()
        self.assertEqual(generator.csv_bytes(), original)
        self.assertEqual(generator.csv_bytes(), original)
        bars = list(csv.DictReader(io.StringIO(original.decode("ascii"))))
        self.assertGreaterEqual(len(bars), 2000)
        times = [int(bar["timestamp"]) for bar in bars]
        self.assertTrue(all(left < right for left, right in zip(times, times[1:])))
        for bar in bars:
            opened, high, low, close = (F(bar[key]) for key in ("open", "high", "low", "close"))
            self.assertGreater(low, 0)
            self.assertLessEqual(low, min(opened, close))
            self.assertGreaterEqual(high, max(opened, close))

    def test_grid_reuses_the_threshold_source_and_has_25_vectors(self):
        directory = ROOT / "examples" / "pbo_proof"
        study = json.loads((directory / "study.json").read_text(encoding="utf-8"))
        strategy = study["strategies"][0]
        self.assertEqual((directory / strategy["source"]).resolve(),
                         ROOT / "examples" / "single_strategy" / "threshold.pine")
        count = 1
        for dimension in strategy["search_space"].values():
            count *= int((F(dimension["high"]) - F(dimension["low"])) / F(dimension["step"])) + 1
        self.assertEqual(count, 25)
        self.assertEqual(study["sampler"]["trials"], count)


if __name__ == "__main__":
    unittest.main()
