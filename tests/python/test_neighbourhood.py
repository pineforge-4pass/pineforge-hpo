"""Tests of the neighbourhood module (post hoc, over finished rows).

Drafts cited: ``methods-neighbourhood-contract.proposed.md`` (N-draft; clause 3.x
is a numbered clause of its section 3, 4.1 holds the worked fixtures NF1-NF6 and the
refusal fixtures) and ``methods-neighbourhood-contract.pin.md`` (the choices
N1-N16).

None of this file was run when it was written. Every expected value is derived by
hand in the docstring of the test that uses it, from the fixture rows and the
generation table, and each derivation was redone a second time before it was
trusted. An expected value that comes from the draft is named as such; none was
copied without that second derivation.
"""

from __future__ import annotations

import ast
import copy
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction
import hashlib
import itertools
import json
from pathlib import Path
import pickle
import random
import tempfile
import unittest

from pineforge_hpo import _rows, neighbourhood as neighbourhood_module, sensitivity
from pineforge_hpo.neighbourhood import (
    CLASSES,
    CODES,
    CONTRACT,
    INTERPRETATION,
    Moves,
    NeighbourhoodError,
    Study,
    dimension,
    neighbourhood,
    prepare,
    top_ids,
)
from pineforge_hpo.study_spec import _fused_grid_value

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "neighbourhood"
SENSITIVITY_FIXTURES = HERE / "fixtures" / "sensitivity"
NF1 = FIXTURES / "nf1_result.json"
NF2_0 = FIXTURES / "nf2_source0.json"
NF2_1 = FIXTURES / "nf2_source1.json"
NF3_0 = FIXTURES / "nf3_source0.json"
NF3_1 = FIXTURES / "nf3_source1.json"
NF6 = FIXTURES / "nf6_result.json"
NF5 = SENSITIVITY_FIXTURES / "nf5_result.json"
NF5B = SENSITIVITY_FIXTURES / "nf5b_result.json"

ABSOLUTE_1 = {"mode": "absolute", "value": 1.0}
OBJECTIVE = {
    "expression": "metrics.all.net_profit_pct",
    "direction": "maximize",
    "constraints": [],
}


def document(path):
    return json.loads(path.read_text(encoding="utf-8"))


def dumped(result):
    return json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)


def cell(value, klass, objective, gap, near, rows):
    return {
        "value": value,
        "class": klass,
        "objective": objective,
        "gap": gap,
        "near": near,
        "rows": rows,
    }


def row(trial_id, parameters, objective=1.0, status="ok"):
    return {
        "trial_id": trial_id,
        "status": status,
        "feasible": status == "ok",
        "objective": objective,
        "parameters": parameters,
    }


FORBIDDEN_CALLS = frozenset(
    {"log", "log2", "log10", "log1p", "exp", "expm1", "exp2", "pow", "system"}
)
FORBIDDEN_MODULES = frozenset({"subprocess", "ctypes"})


def audit(text):
    """List the forbidden calls, power operators and imports of a source text."""
    problems = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in FORBIDDEN_CALLS:
                problems.append(f"call of {name} at line {node.lineno}")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            problems.append(f"power operator at line {node.lineno}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    problems.append(f"import of {alias.name} at line {node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in FORBIDDEN_MODULES:
                problems.append(f"import from {node.module} at line {node.lineno}")
    return problems


class RefusalTestCase(unittest.TestCase):
    def refusal(self, code, *args, **kwargs):
        """Call ``neighbourhood`` and return the refusal it must raise."""
        with self.assertRaises(NeighbourhoodError) as caught:
            neighbourhood(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class ConstantsTests(unittest.TestCase):
    def test_contract_codes_and_classes(self):
        """Rule: the contract string, the 13 codes of clause 3.8 and the 4 classes."""
        self.assertEqual(CONTRACT, "pineforge-hpo-neighbourhood/v1")
        self.assertEqual(
            CODES,
            (
                "neighbourhood_input_invalid",
                "neighbourhood_rows_incomplete",
                "neighbourhood_space_missing",
                "neighbourhood_space_mismatch",
                "neighbourhood_context_mismatch",
                "neighbourhood_parameter_missing",
                "neighbourhood_parameter_invalid",
                "neighbourhood_band_degenerate",
                "neighbourhood_centre_invalid",
                "neighbourhood_no_varying_dimension",
                "neighbourhood_no_feasible_rows",
                "neighbourhood_duplicate_conflict",
                "neighbourhood_resource_exceeded",
            ),
        )
        self.assertEqual(len(set(CODES)), 13)
        self.assertEqual(
            CLASSES,
            ("scored_ok", "scored_infeasible", "unscorable", "never_evaluated"),
        )

    def test_interpretation_is_the_fixed_text(self):
        """Rule: ``interpretation`` states clause 3.10 in fixed words."""
        self.assertEqual(
            INTERPRETATION,
            "This describes the trials that were supplied and nothing else. "
            "It states no significance, confidence or probability. "
            "A neighbour that was never evaluated is not imputed, and nothing is "
            "said about it. "
            "The reference value is the best of many trials, so it is optimistic. "
            "With an adaptive sampler the neighbours that exist are not a random "
            "draw: the share near the best describes its stated neighbourhood "
            "only when coverage is 1. "
            "Every number is in-sample. "
            "Constraints, relations between parameters and whether two runs used "
            "the same data are not checked.",
        )

    def test_error_fields_ids_and_document(self):
        """Positive control: the four fields, ascending ids, the refusal document."""
        error = NeighbourhoodError(
            "neighbourhood_centre_invalid", "a sentence", [(1, 0), (0, 7), (0, 2)], ["b", "a"]
        )
        self.assertEqual(error.code, "neighbourhood_centre_invalid")
        self.assertEqual(error.detail, "a sentence")
        self.assertEqual(error.ids, ((0, 2), (0, 7), (1, 0)))
        self.assertEqual(error.names, ("b", "a"))
        self.assertEqual(str(error), "neighbourhood_centre_invalid: a sentence")
        self.assertEqual(
            error.document(),
            {
                "contract": CONTRACT,
                "status": "refused",
                "code": "neighbourhood_centre_invalid",
                "detail": "a sentence",
                "ids": [[0, 2], [0, 7], [1, 0]],
                "names": ["b", "a"],
            },
        )
        empty = NeighbourhoodError("neighbourhood_no_feasible_rows", "none")
        self.assertEqual((empty.ids, empty.names), ((), ()))
        with self.assertRaises(ValueError):
            NeighbourhoodError("made_up_code", "a sentence")

    def test_error_survives_pickling(self):
        """Rule: the error crosses a process boundary with its fields intact."""
        error = NeighbourhoodError("neighbourhood_space_mismatch", "differs", [(0, 1)], ["Fast"])
        # Round trip of an object built here; no untrusted bytes are loaded.
        again = pickle.loads(pickle.dumps(error))
        self.assertIsInstance(again, NeighbourhoodError)
        self.assertEqual(
            (again.code, again.detail, again.ids, again.names),
            ("neighbourhood_space_mismatch", "differs", ((0, 1),), ("Fast",)),
        )


class StaticAuditTests(unittest.TestCase):
    def test_audit_catches_planted_violations(self):
        """Positive control: the audit reports each construct it is told to forbid."""
        # These texts are only parsed by ``ast``; none of them is ever executed.
        for text in (
            "import math\nmath.log(2.0)\n",
            "from math import exp\nexp(1.0)\n",
            "x = pow(2, 3)\n",
            "y = 2 ** 3\n",
            "import subprocess\n",
            "from ctypes import CDLL\n",
            "import os\nos.system('x')\n",
        ):
            self.assertTrue(audit(text), text)
        self.assertEqual(audit("import math\nmath.floor(2.5)\n"), [])

    def test_modules_call_no_log_exp_pow_and_no_process_or_ctypes(self):
        """Rule: no log, exp or pow call, no subprocess, ctypes or os.system (N5)."""
        for module in (neighbourhood_module, sensitivity):
            text = Path(module.__file__).read_text(encoding="utf-8")
            self.assertEqual(audit(text), [], module.__name__)

    def test_sensitivity_imports_only_public_names_and_generates_nothing(self):
        """Rule: sensitivity uses neighbourhood only through its public names."""
        text = Path(sensitivity.__file__).read_text(encoding="utf-8")
        tree = ast.parse(text)
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "neighbourhood"
            for alias in node.names
        ]
        self.assertTrue(imported)
        for name in imported:
            self.assertIn(name, neighbourhood_module.__all__)
        calls = {
            getattr(node.func, "attr", None)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        self.assertNotIn("neighbours", calls)


class NF1Tests(unittest.TestCase):
    """NF1: kinds, classes, bands and a missing vector (N-draft 4.1)."""

    def run_nf1(self, band, sources=None):
        return neighbourhood(
            [NF1] if sources is None else sources,
            centres=[(0, 0)],
            band=band,
            pair_k=0,
        )

    def test_absolute_band(self):
        """NF1, band absolute 1.0, derived again by hand.

        Space: Fast integer 2..10 step 2, Flag boolean, Mode [ema, sma, wma]. The ok
        rows score 10.0, 9.5, 7.0 and 10.5, so R = 10.5 at row 0:5 (the 12.0 row is a
        constraint violation). Centre 0:0 = (Fast 4, Flag false, Mode ema), 10.0:
        gap 10.5 - 10.0 = 0.5, near. Names in byte order: Fast, Flag, Mode. Neighbours:
        Fast 2 and 6, Flag true, Mode sma and wma, so geometric 5 and nothing dropped.
        Fast 2 is row 0:1 (gap 1.0, near because the band is inclusive); Fast 6 is
        row 0:2 (gap 3.5, not near); Flag true has no row; Mode sma is row 0:3 (a
        constraint violation, objective 12.0, gap null); Mode wma is row 0:4 (engine
        error). Scored 3, near 1: share 1/3; covered 3 + 1 of 5: coverage 0.8; no
        scored ok neighbour is above 10.0. The vector (4, true, ema) is missing.
        """
        result = self.run_nf1(ABSOLUTE_1)
        self.assertEqual(result["contract"], "pineforge-hpo-neighbourhood/v1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["direction"], "maximize")
        self.assertEqual(
            result["reference"], {"objective": 10.5, "source": 0, "trial_id": 5}
        )
        self.assertEqual(
            result["band"], {"mode": "absolute", "value": 1.0, "delta": 1.0}
        )
        self.assertEqual(
            result["rules"],
            {
                "fraction": None,
                "ratio": None,
                "pair_k": 0,
                "by_dimension": {
                    "Fast": "step",
                    "Flag": "other_value",
                    "Mode": "other_choices",
                },
            },
        )
        self.assertEqual(
            result["sources"], [{"index": 0, "rows": 6, "space_hash": None}]
        )
        self.assertEqual(result["duplicate_rows"], 0)
        self.assertEqual(result["constant_dimensions"], [])
        self.assertEqual(
            result["missing_vectors"], [{"Fast": 4, "Flag": True, "Mode": "ema"}]
        )
        self.assertEqual(
            result["admission"],
            {"rows": 6, "lookups": 5, "work": 11, "max_work": None},
        )
        self.assertEqual(result["interpretation"], INTERPRETATION)
        self.assertEqual(len(result["centres"]), 1)
        centre = result["centres"][0]
        self.assertEqual(
            (centre["source"], centre["trial_id"], centre["objective"]), (0, 0, 10.0)
        )
        self.assertEqual(centre["centre_gap"], 0.5)
        self.assertIs(centre["centre_near"], True)
        self.assertEqual(
            centre["counts"],
            {
                "geometric": 5,
                "scored_ok": 2,
                "scored_infeasible": 1,
                "unscorable": 1,
                "never_evaluated": 1,
                "near": 1,
                "better_neighbours": 0,
            },
        )
        self.assertEqual(centre["dropped"], {"bounds": 0, "same_as_centre": 0})
        self.assertEqual(
            centre["share_near_best"],
            {"numerator": 1, "denominator": 3, "value": 0.3333333333333333},
        )
        self.assertEqual(
            centre["coverage"], {"numerator": 4, "denominator": 5, "value": 0.8}
        )
        self.assertEqual(list(centre["profile"]), ["Fast", "Flag", "Mode"])
        self.assertEqual(
            centre["profile"]["Fast"],
            [
                cell(2, "scored_ok", 9.5, 1.0, True, [[0, 1]]),
                cell(6, "scored_ok", 7.0, 3.5, False, [[0, 2]]),
            ],
        )
        self.assertEqual(
            centre["profile"]["Flag"],
            [cell(True, "never_evaluated", None, None, False, [])],
        )
        self.assertIs(centre["profile"]["Flag"][0]["value"], True)
        self.assertEqual(
            centre["profile"]["Mode"],
            [
                cell("sma", "scored_infeasible", 12.0, None, False, [[0, 3]]),
                cell("wma", "unscorable", None, None, False, [[0, 4]]),
            ],
        )
        self.assertEqual(centre["pair_maps"], [])

    def test_relative_band_gives_the_same_classes(self):
        """NF1 variant: band relative 0.125, delta = 0.125 * 10.5 = 1.3125.

        0.125 * 10.5 = 21/16 exactly. Gap 1.0 is within 1.3125 and gap 3.5 is not, so
        the profile and the share equal those of the absolute band 1.0.
        """
        base = self.run_nf1(ABSOLUTE_1)
        relative = self.run_nf1({"mode": "relative", "value": 0.125})
        self.assertEqual(
            relative["band"], {"mode": "relative", "value": 0.125, "delta": 1.3125}
        )
        self.assertEqual(relative["centres"][0]["profile"], base["centres"][0]["profile"])
        self.assertEqual(
            relative["centres"][0]["share_near_best"],
            base["centres"][0]["share_near_best"],
        )

    def test_wide_band_counts_the_far_neighbour(self):
        """NF1 variant: band absolute 4.0 makes Fast 2 (1.0) and Fast 6 (3.5) near.

        Near 2 of scored 3 = 0.6666666666666666 (the quotient of 2 by 3 in binary64).
        """
        wide = self.run_nf1({"mode": "absolute", "value": 4.0})
        centre = wide["centres"][0]
        self.assertEqual(centre["counts"]["near"], 2)
        self.assertEqual(
            centre["share_near_best"],
            {"numerator": 2, "denominator": 3, "value": 0.6666666666666666},
        )

    def test_band_value_zero_keeps_only_the_best(self):
        """Rule: a zero band is allowed; only a neighbour with gap 0.0 is near."""
        centre = self.run_nf1({"mode": "absolute", "value": 0})["centres"][0]
        self.assertEqual(centre["counts"]["near"], 0)
        self.assertIs(centre["centre_near"], False)

    def test_same_file_twice_is_two_sources_with_duplicates(self):
        """Rule N11: duplicates group by vector across sources, identity (source, id).

        Reading NF1 twice gives six vectors with two agreeing rows each, so
        ``duplicate_rows`` is 6, R stays 10.5 at the lower identity 0:5 and the
        cells list both rows, lower identity first; classes and counts equal NF1.
        """
        single = self.run_nf1(ABSOLUTE_1)
        double = self.run_nf1(ABSOLUTE_1, sources=[NF1, NF1])
        self.assertEqual(double["duplicate_rows"], 6)
        self.assertEqual(
            double["reference"], {"objective": 10.5, "source": 0, "trial_id": 5}
        )
        self.assertEqual(
            double["sources"],
            [
                {"index": 0, "rows": 6, "space_hash": None},
                {"index": 1, "rows": 6, "space_hash": None},
            ],
        )
        centre = double["centres"][0]
        self.assertEqual(centre["counts"], single["centres"][0]["counts"])
        self.assertEqual(
            centre["profile"]["Fast"][0],
            cell(2, "scored_ok", 9.5, 1.0, True, [[0, 1], [1, 1]]),
        )
        self.assertEqual(centre["profile"]["Mode"][1]["rows"], [[0, 4], [1, 4]])
        self.assertEqual(double["missing_vectors"], single["missing_vectors"])


class NF2Tests(unittest.TestCase):
    """NF2: bounds, exact matching and a second source (N-draft 4.1)."""

    def run_nf2(self, sources):
        return neighbourhood(
            sources,
            centres=[(0, 0)],
            band={"mode": "absolute", "value": 0.5},
            fraction=0.25,
            ratio=2.0,
            pair_k=0,
        )

    def test_one_source(self):
        """NF2 with source 0 only, derived again by hand.

        Space: Lr log real 0.125..8.0, Mult real 0.5..2.0 step 0.5 (ordinals 0..3 =
        0.5, 1.0, 1.5, 2.0: the product 4*0.5+0.5 = 2.5 is above 2.0, so 3 is the last
        ordinal), Thr real 0..1 without step. Centre (Lr 8.0, Mult 2.0, Thr 0.875),
        5.0. R = 5.0, the lowest identity at 5.0 is 0:0. Lr: 8.0/2 = 4.0 kept, 8.0*2 =
        16.0 above the bound. Mult: ordinal 3 gives 2 -> 1.5 kept and 4 -> beyond the
        last ordinal. Thr: delta = 0.25 * (1.0 - 0.0) = 0.25, so 0.625 kept and 1.125
        above the bound. Dropped by bounds: 3. Lr 4.0 has no row; Mult 1.5 is row 0:1
        (4.0, gap 1.0, not near with band 0.5); Thr 0.625 is row 0:2 (5.0, gap 0.0,
        near); row 0:3 (Thr 0.75) matches nothing. Scored ok 2, near 1: share 1/2;
        covered 2 of 3: 0.6666666666666666; no neighbour is above 5.0.
        """
        result = self.run_nf2([NF2_0])
        self.assertEqual(
            result["reference"], {"objective": 5.0, "source": 0, "trial_id": 0}
        )
        self.assertEqual(
            result["rules"],
            {
                "fraction": 0.25,
                "ratio": 2.0,
                "pair_k": 0,
                "by_dimension": {"Lr": "ratio", "Mult": "ordinal", "Thr": "fraction"},
            },
        )
        centre = result["centres"][0]
        self.assertEqual(centre["dropped"], {"bounds": 3, "same_as_centre": 0})
        self.assertEqual(centre["centre_gap"], 0.0)
        self.assertIs(centre["centre_near"], True)
        self.assertEqual(
            centre["profile"],
            {
                "Lr": [cell(4.0, "never_evaluated", None, None, False, [])],
                "Mult": [cell(1.5, "scored_ok", 4.0, 1.0, False, [[0, 1]])],
                "Thr": [cell(0.625, "scored_ok", 5.0, 0.0, True, [[0, 2]])],
            },
        )
        self.assertEqual(
            centre["counts"],
            {
                "geometric": 3,
                "scored_ok": 2,
                "scored_infeasible": 0,
                "unscorable": 0,
                "never_evaluated": 1,
                "near": 1,
                "better_neighbours": 0,
            },
        )
        self.assertEqual(
            centre["share_near_best"],
            {"numerator": 1, "denominator": 2, "value": 0.5},
        )
        self.assertEqual(
            centre["coverage"],
            {"numerator": 2, "denominator": 3, "value": 0.6666666666666666},
        )
        self.assertEqual(
            result["missing_vectors"], [{"Lr": 4.0, "Mult": 2.0, "Thr": 0.875}]
        )

    def test_second_source_changes_the_reference_and_fills_the_gap(self):
        """NF2 with source 1 added: row 1:0 = (4.0, 2.0, 0.875), 5.5.

        R = 5.5 at 1:0, so the gaps are Lr 4.0: 0.0 (near), Mult 1.5: 5.5 - 4.0 = 1.5
        (not near), Thr 0.625: 5.5 - 5.0 = 0.5 (near, 0.5 <= 0.5 is inclusive). The
        centre's gap is 0.5, near. Scored ok 3, near 2, better neighbours 1 (5.5 is
        above 5.0): share 2/3, coverage 3/3 = 1.0, nothing is missing.
        """
        result = self.run_nf2([NF2_0, NF2_1])
        self.assertEqual(
            result["reference"], {"objective": 5.5, "source": 1, "trial_id": 0}
        )
        centre = result["centres"][0]
        self.assertEqual(centre["centre_gap"], 0.5)
        self.assertIs(centre["centre_near"], True)
        self.assertEqual(
            centre["profile"],
            {
                "Lr": [cell(4.0, "scored_ok", 5.5, 0.0, True, [[1, 0]])],
                "Mult": [cell(1.5, "scored_ok", 4.0, 1.5, False, [[0, 1]])],
                "Thr": [cell(0.625, "scored_ok", 5.0, 0.5, True, [[0, 2]])],
            },
        )
        counts = centre["counts"]
        self.assertEqual(
            (counts["scored_ok"], counts["near"], counts["better_neighbours"]),
            (3, 2, 1),
        )
        self.assertEqual(
            centre["share_near_best"],
            {"numerator": 2, "denominator": 3, "value": 0.6666666666666666},
        )
        self.assertEqual(
            centre["coverage"], {"numerator": 3, "denominator": 3, "value": 1.0}
        )
        self.assertEqual(result["missing_vectors"], [])
        self.assertEqual(
            result["sources"],
            [
                {"index": 0, "rows": 4, "space_hash": None},
                {"index": 1, "rows": 1, "space_hash": None},
            ],
        )

    def test_missing_fraction_or_ratio_is_refused_with_the_names(self):
        """Rule: a varying dimension that needs a fraction or a ratio names itself."""
        band = {"mode": "absolute", "value": 0.5}
        with self.assertRaises(NeighbourhoodError) as caught:
            neighbourhood([NF2_0], centres=[(0, 0)], band=band, ratio=2.0, pair_k=0)
        self.assertEqual(caught.exception.code, "neighbourhood_parameter_missing")
        self.assertEqual(caught.exception.names, ("Thr",))
        with self.assertRaises(NeighbourhoodError) as caught:
            neighbourhood(
                [NF2_0],
                centres=[(0, 0)],
                band=band,
                fraction=0.25,
                pair_k=0,
            )
        self.assertEqual(caught.exception.code, "neighbourhood_parameter_missing")
        self.assertEqual(caught.exception.names, ("Lr",))


class NF3Tests(RefusalTestCase):
    """NF3: duplicates, colliding ids, unscorable (N-draft 4.1)."""

    def sources(self):
        return [document(NF3_0), document(NF3_1)]

    def test_base(self):
        """NF3, band absolute 1.0, derived again by hand.

        Space: Fast integer 2..8 step 2, Mode [ema, sma]. R = 7.0 (row 0:3); the
        centre 0:0 = (4, ema), 6.0 has gap 1.0, near. Neighbours: Fast 2 and 6, Mode
        sma. Fast 2 is row 0:1 (5.0, gap 2.0, not near); Fast 6 is row 1:0 of the
        second source (6.5, gap 0.5, near); Mode sma is row 1:1 (engine error). The
        vector (4, ema) has the rows 0:0 and 0:2, so ``duplicate_rows`` is 1. Scored
        2, near 1: share 1/2; covered 3 of 3: coverage 1.0; one neighbour (6.5) is
        above the centre's 6.0.
        """
        result = neighbourhood(
            self.sources(), centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0
        )
        self.assertEqual(
            result["reference"], {"objective": 7.0, "source": 0, "trial_id": 3}
        )
        self.assertEqual(result["duplicate_rows"], 1)
        centre = result["centres"][0]
        self.assertEqual(centre["centre_gap"], 1.0)
        self.assertIs(centre["centre_near"], True)
        self.assertEqual(
            centre["profile"],
            {
                "Fast": [
                    cell(2, "scored_ok", 5.0, 2.0, False, [[0, 1]]),
                    cell(6, "scored_ok", 6.5, 0.5, True, [[1, 0]]),
                ],
                "Mode": [cell("sma", "unscorable", None, None, False, [[1, 1]])],
            },
        )
        self.assertEqual(
            centre["counts"],
            {
                "geometric": 3,
                "scored_ok": 2,
                "scored_infeasible": 0,
                "unscorable": 1,
                "never_evaluated": 0,
                "near": 1,
                "better_neighbours": 1,
            },
        )
        self.assertEqual(
            centre["share_near_best"],
            {"numerator": 1, "denominator": 2, "value": 0.5},
        )
        self.assertEqual(
            centre["coverage"], {"numerator": 3, "denominator": 3, "value": 1.0}
        )
        self.assertEqual(result["missing_vectors"], [])
        self.assertEqual(
            result["sources"],
            [
                {"index": 0, "rows": 4, "space_hash": None},
                {"index": 1, "rows": 2, "space_hash": None},
            ],
        )

    def test_disagreeing_duplicates_are_refused(self):
        """NF3 variant (a): row 0:2 scores 6.5 while 0:0 scores 6.0 at one vector."""
        sources = self.sources()
        sources[0]["trials"][2]["objective"] = 6.5
        error = self.refusal(
            "neighbourhood_duplicate_conflict",
            sources,
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
        )
        self.assertEqual(error.ids, ((0, 0), (0, 2)))

    def test_disagreeing_status_is_refused(self):
        """Rule N11: an ok row and a constraint violation at one vector disagree."""
        sources = self.sources()
        trial = sources[0]["trials"][2]
        trial["status"] = "constraint_violation"
        trial["feasible"] = False
        error = self.refusal(
            "neighbourhood_duplicate_conflict",
            sources,
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
        )
        self.assertEqual(error.ids, ((0, 0), (0, 2)))

    def test_unscorable_duplicate_is_ignored_for_the_outcome(self):
        """NF3 variant (b): row 0:2 is an engine error; the outcome stays row 0:0's.

        The group of (4, ema) is {0:0 ok, 0:2 engine_error}: one scored row speaks
        for the vector, ``duplicate_rows`` is still 1, and the neighbour cells are
        those of the base fixture.
        """
        base = neighbourhood(
            self.sources(), centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0
        )
        sources = self.sources()
        trial = sources[0]["trials"][2]
        trial["status"] = "engine_error"
        trial["feasible"] = False
        trial["objective"] = None
        result = neighbourhood(sources, centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)
        self.assertEqual(result["duplicate_rows"], 1)
        self.assertEqual(result["centres"][0]["profile"], base["centres"][0]["profile"])
        self.assertEqual(result["centres"][0]["counts"], base["centres"][0]["counts"])
        self.assertNotEqual(result["input_sha256"], base["input_sha256"])


class NF4Tests(unittest.TestCase):
    """NF4: the generation table, one exact-set control per row (N-draft 4.1)."""

    fraction = 0.25
    ratio = 2.0

    def moves(self, spec, value):
        return dimension("X", spec).neighbours(value, self.fraction, self.ratio)

    def values(self, spec, value):
        return self.moves(spec, value).values

    def test_integer_step_two(self):
        """Table row 1: integer 2..10 step 2. Centre 2 -> {4}; 6 -> {4, 8}; 10 -> {8}."""
        spec = {"kind": "integer", "low": 2, "high": 10, "step": 2, "log": False}
        self.assertEqual(self.moves(spec, 2), Moves((4,), 1, 0))
        self.assertEqual(self.moves(spec, 6), Moves((4, 8), 0, 0))
        self.assertEqual(self.moves(spec, 10), Moves((8,), 1, 0))

    def test_integer_step_one(self):
        """Table row 2: integer 1..3. Centre 2 -> {1, 3}; 1 -> {2}."""
        spec = {"kind": "integer", "low": 1, "high": 3, "step": 1, "log": False}
        self.assertEqual(self.values(spec, 2), (1, 3))
        self.assertEqual(self.values(spec, 1), (2,))

    def test_integer_constant(self):
        """Table row 3: integer 5..5 has one legal value, no neighbour."""
        spec = {"kind": "integer", "low": 5, "high": 5, "step": 1, "log": False}
        constant = dimension("X", spec)
        self.assertTrue(constant.constant)
        self.assertEqual(self.moves(spec, 5), Moves((), 0, 0))

    def test_integer_lattice_with_one_ordinal_is_constant(self):
        """Draft F11: low 2, high 3, step 2 has the single legal value 2."""
        spec = {"kind": "integer", "low": 2, "high": 3, "step": 2, "log": False}
        self.assertTrue(dimension("X", spec).constant)

    def test_real_with_one_legal_value_is_constant_even_with_a_step(self):
        """Draft F11: real 2.0..2.0 with a step, and 0.0..0.05 step 0.1, hold one value.

        The first has equal bounds. The second has the ordinal 0 only: the next one,
        1 * 0.1 = 0.1, is above 0.05 by more than one representable step. Both are
        constant, accept their one value and refuse any other.
        """
        fixed = dimension(
            "X", {"kind": "real", "low": 2.0, "high": 2.0, "step": 0.1, "log": False}
        )
        self.assertTrue(fixed.constant)
        self.assertEqual(fixed.normalise(2.0), 2.0)
        with self.assertRaises(ValueError):
            fixed.normalise(2.1)
        self.assertEqual(fixed.neighbours(2.0), Moves((), 0, 0))
        short = dimension(
            "X", {"kind": "real", "low": 0.0, "high": 0.05, "step": 0.1, "log": False}
        )
        self.assertTrue(short.constant)
        self.assertEqual(short.last, 0)
        self.assertEqual(short.normalise(0.0), 0.0)
        with self.assertRaises(ValueError):
            short.normalise(0.03)

    def test_stepped_real(self):
        """Table row 4: real 0.5..2.0 step 0.5. 1.0 -> {0.5, 1.5}; 2.0 -> {1.5}; 0.5 -> {1.0}."""
        spec = {"kind": "real", "low": 0.5, "high": 2.0, "step": 0.5, "log": False}
        self.assertEqual(self.moves(spec, 1.0), Moves((0.5, 1.5), 0, 0))
        self.assertEqual(self.moves(spec, 2.0), Moves((1.5,), 1, 0))
        self.assertEqual(self.moves(spec, 0.5), Moves((1.0,), 1, 0))

    def test_continuous_real(self):
        """Table row 5: real 0..1, f = 0.25, delta 0.25. 0.5 -> {0.25, 0.75}; 0.875 -> {0.625}; 0.0 -> {0.25}; 1.0 -> {0.75}."""
        spec = {"kind": "real", "low": 0.0, "high": 1.0, "step": None, "log": False}
        self.assertEqual(self.values(spec, 0.5), (0.25, 0.75))
        self.assertEqual(self.values(spec, 0.875), (0.625,))
        self.assertEqual(self.values(spec, 0.0), (0.25,))
        self.assertEqual(self.values(spec, 1.0), (0.75,))

    def test_log_real(self):
        """Table row 6: log real 0.125..8.0, rho = 2. 1.0 -> {0.5, 2.0}; 0.125 -> {0.25}; 8.0 -> {4.0}."""
        spec = {"kind": "real", "low": 0.125, "high": 8.0, "step": None, "log": True}
        self.assertEqual(self.values(spec, 1.0), (0.5, 2.0))
        self.assertEqual(self.moves(spec, 0.125), Moves((0.25,), 1, 0))
        self.assertEqual(self.moves(spec, 8.0), Moves((4.0,), 1, 0))

    def test_log_integer(self):
        """Table row 7: log integer 1..1000, rho = 2, rounded half up with exact rationals.

        1: 1/2 rounds to 1 (the centre, dropped), 2 kept -> {2}. 3: 3/2 = 1.5 -> 2,
        6 -> {2, 6}. 5: 2.5 -> 3, 10 -> {3, 10}. 7: 3.5 -> 4, 14 -> {4, 14}. 600: 300,
        1200 above the bound -> {300}. 1000: 500, 2000 above -> {500}.
        """
        spec = {"kind": "integer", "low": 1, "high": 1000, "step": 1, "log": True}
        self.assertEqual(self.moves(spec, 1), Moves((2,), 0, 1))
        self.assertEqual(self.values(spec, 3), (2, 6))
        self.assertEqual(self.values(spec, 5), (3, 10))
        self.assertEqual(self.values(spec, 7), (4, 14))
        self.assertEqual(self.moves(spec, 600), Moves((300,), 1, 0))
        self.assertEqual(self.moves(spec, 1000), Moves((500,), 1, 0))

    def test_boolean(self):
        """Table row 8: boolean. false -> {true}; true -> {false}."""
        spec = {"kind": "boolean"}
        self.assertEqual(self.values(spec, False), (True,))
        self.assertEqual(self.values(spec, True), (False,))
        self.assertIs(self.values(spec, False)[0], True)

    def test_categorical(self):
        """Table row 9: [ema, sma, wma]. sma -> {ema, wma}; ema -> {sma, wma}."""
        spec = {"kind": "categorical", "choices": ["ema", "sma", "wma"]}
        self.assertEqual(self.values(spec, "sma"), ("ema", "wma"))
        self.assertEqual(self.values(spec, "ema"), ("sma", "wma"))

    def test_categorical_single_choice_is_constant(self):
        """Table row 10: [only] has one legal value, no neighbour."""
        spec = {"kind": "categorical", "choices": ["only"]}
        self.assertTrue(dimension("X", spec).constant)
        self.assertEqual(self.moves(spec, "only"), Moves((), 0, 0))

    def test_equal_categorical_choices_collapse(self):
        """Choice: a repeated choice is one value, so [a, a, b] has the neighbour b only."""
        spec = {"kind": "categorical", "choices": ["a", "a", "b"]}
        self.assertEqual(self.values(spec, "a"), ("b",))
        self.assertTrue(dimension("X", {"kind": "categorical", "choices": ["a", "a"]}).constant)

    def test_categorical_choice_types_stay_apart(self):
        """Rule: 1, 1.0 and true are three choices (typed keys), declared order kept."""
        spec = {"kind": "categorical", "choices": [1, 1.0, True]}
        self.assertEqual(self.values(spec, 1.0), (1, True))

    def test_stepped_real_endpoint_snap(self):
        """Table rule: a stepped real snaps its last ordinal to ``high``.

        Real 0.0..0.3 step 0.1. In binary64 3 * 0.1 is the tie between 0.3 and
        0.30000000000000004 and rounds to the even neighbour, 0.30000000000000004,
        which is the next value above 0.3; so ordinal 3 is the last one and its value
        snaps to 0.3. Ordinals 0, 1, 2 give 0.0, 0.1 and 0.2 exactly (2 * 0.1 is the
        double nearest 0.2). Centre 0.2 -> {0.1, 0.3}; 0.3 (ordinal round(2.9999999999999996) = 3)
        -> {0.2} with one move beyond the last ordinal; 0.1 -> {0.0, 0.2}; 0.0 -> {0.1}.
        """
        spec = {"kind": "real", "low": 0.0, "high": 0.3, "step": 0.1, "log": False}
        stepped = dimension("X", spec)
        self.assertEqual(stepped.last, 3)
        self.assertEqual(stepped.decode(3), 0.3)
        self.assertEqual(stepped.decode(2), 0.2)
        self.assertEqual(stepped.decode(1), 0.1)
        self.assertEqual(stepped.decode(2), _fused_grid_value(2, 0.1, 0.0))
        self.assertEqual(stepped.ordinal(0.3), 3)
        self.assertEqual(self.moves(spec, 0.2), Moves((0.1, 0.3), 0, 0))
        self.assertEqual(self.moves(spec, 0.3), Moves((0.2,), 1, 0))
        self.assertEqual(self.moves(spec, 0.1), Moves((0.0, 0.2), 0, 0))
        self.assertEqual(self.moves(spec, 0.0), Moves((0.1,), 1, 0))
        self.assertEqual(stepped.normalise(0.3), 0.3)
        with self.assertRaises(ValueError):
            stepped.normalise(0.30000000000000004)

    def test_stepped_real_row_value_within_four_units_is_taken_as_the_lattice_point(self):
        """Choice: as continuation does, a value within 4 ulps of a lattice point is that point."""
        spec = {"kind": "real", "low": 0.5, "high": 2.0, "step": 0.5, "log": False}
        stepped = dimension("X", spec)
        near = 1.5 + 2 * 2.220446049250313e-16
        self.assertEqual(stepped.normalise(near), 1.5)
        with self.assertRaises(ValueError):
            stepped.normalise(1.5 + 1e-9)
        with self.assertRaises(ValueError):
            stepped.normalise(1.25)

    def test_dimension_defects_are_input_invalid(self):
        """Rule: a recorded dimension the study spec would refuse is input_invalid."""
        for spec in (
            {"kind": "integer", "low": 5, "high": 1, "step": 1, "log": False},
            {"kind": "integer", "low": 1, "high": 5, "step": 0, "log": False},
            {"kind": "integer", "low": 1.0, "high": 5, "step": 1, "log": False},
            {"kind": "integer", "low": 0, "high": 5, "step": 1, "log": True},
            {"kind": "integer", "low": 1, "high": 5, "step": 2, "log": True},
            {"kind": "real", "low": 1.0, "high": 0.5, "step": None, "log": False},
            {"kind": "real", "low": 0.0, "high": 1.0, "step": 0.0, "log": False},
            {"kind": "real", "low": 0.0, "high": 1.0, "step": None, "log": True},
            {"kind": "real", "low": 0.5, "high": 1.0, "step": 0.1, "log": True},
            {"kind": "real", "low": 0.0, "high": 1e300, "step": 1e-300, "log": False},
            {"kind": "categorical", "choices": []},
            {"kind": "categorical", "choices": [None]},
            {"kind": "weird"},
            {"kind": "integer", "low": 1, "high": 5, "step": 1, "log": "yes"},
        ):
            with self.assertRaises(NeighbourhoodError) as caught:
                dimension("X", spec)
            self.assertEqual(caught.exception.code, "neighbourhood_input_invalid", spec)
            self.assertEqual(caught.exception.names, ("X",))
        with self.assertRaises(NeighbourhoodError):
            dimension("X", "not an object")

    def test_a_rule_needing_fraction_or_ratio_refuses_without_it(self):
        """Rule: ``neighbours`` raises when its fraction or ratio is None."""
        with self.assertRaises(ValueError):
            dimension("X", {"kind": "real", "low": 0.0, "high": 1.0, "step": None, "log": False}).neighbours(0.5)
        with self.assertRaises(ValueError):
            dimension("X", {"kind": "real", "low": 1.0, "high": 8.0, "step": None, "log": True}).neighbours(2.0)
        with self.assertRaises(ValueError):
            dimension("X", {"kind": "integer", "low": 1, "high": 8, "step": 1, "log": True}).neighbours(2)

    def test_whole_table_through_the_public_function(self):
        """The table once more, through ``neighbourhood`` with one centre row.

        Nine dimensions in byte order A..I, centre (A 6, B 7, C 1.0, D 0.5, E 1.0,
        F false, G sma, H 5, I only). By the table: A {4, 8}; B (log integer, 7/2 =
        3.5 -> 4, 14) {4, 14}; C {0.5, 1.5}; D {0.25, 0.75}; E {0.5, 2.0}; F {true};
        G {ema, wma}; H and I are constant and listed apart. Thirteen neighbours, none
        with a row, none dropped.
        """
        space = {
            "parameters": {
                "A": {"kind": "integer", "low": 2, "high": 10, "step": 2, "log": False},
                "B": {"kind": "integer", "low": 1, "high": 1000, "step": 1, "log": True},
                "C": {"kind": "real", "low": 0.5, "high": 2.0, "step": 0.5, "log": False},
                "D": {"kind": "real", "low": 0.0, "high": 1.0, "step": None, "log": False},
                "E": {"kind": "real", "low": 0.125, "high": 8.0, "step": None, "log": True},
                "F": {"kind": "boolean"},
                "G": {"kind": "categorical", "choices": ["ema", "sma", "wma"]},
                "H": {"kind": "integer", "low": 5, "high": 5, "step": 1, "log": False},
                "I": {"kind": "categorical", "choices": ["only"]},
            },
            "objective": OBJECTIVE,
        }
        centre = {
            "A": 6,
            "B": 7,
            "C": 1.0,
            "D": 0.5,
            "E": 1.0,
            "F": False,
            "G": "sma",
            "H": 5,
            "I": "only",
        }
        source = {"space": space, "trials": [row(0, centre)]}
        result = neighbourhood(
            [source],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            fraction=self.fraction,
            ratio=self.ratio,
            pair_k=0,
        )
        profile = result["centres"][0]["profile"]
        self.assertEqual(list(profile), ["A", "B", "C", "D", "E", "F", "G"])
        self.assertEqual([c["value"] for c in profile["A"]], [4, 8])
        self.assertEqual([c["value"] for c in profile["B"]], [4, 14])
        self.assertEqual([c["value"] for c in profile["C"]], [0.5, 1.5])
        self.assertEqual([c["value"] for c in profile["D"]], [0.25, 0.75])
        self.assertEqual([c["value"] for c in profile["E"]], [0.5, 2.0])
        self.assertEqual([c["value"] for c in profile["F"]], [True])
        self.assertEqual([c["value"] for c in profile["G"]], ["ema", "wma"])
        self.assertEqual(result["constant_dimensions"], ["H", "I"])
        counts = result["centres"][0]["counts"]
        self.assertEqual((counts["geometric"], counts["never_evaluated"]), (13, 13))
        self.assertEqual(
            result["centres"][0]["dropped"], {"bounds": 0, "same_as_centre": 0}
        )
        self.assertIsNone(result["centres"][0]["share_near_best"])
        self.assertEqual(
            result["centres"][0]["coverage"],
            {"numerator": 0, "denominator": 13, "value": 0.0},
        )
        expected = {
            json.dumps(dict(centre, **{name: value}), sort_keys=True)
            for name, values in (
                ("A", [4, 8]),
                ("B", [4, 14]),
                ("C", [0.5, 1.5]),
                ("D", [0.25, 0.75]),
                ("E", [0.5, 2.0]),
                ("F", [True]),
                ("G", ["ema", "wma"]),
            )
            for value in values
        }
        found = {json.dumps(v, sort_keys=True) for v in result["missing_vectors"]}
        self.assertEqual(found, expected)
        self.assertEqual(len(result["missing_vectors"]), 13)
        self.assertEqual(result["missing_vectors"][0], dict(centre, A=4))
        self.assertEqual(result["missing_vectors"][-1], dict(centre, A=8))


class BruteForceTests(unittest.TestCase):
    """Independent checks: the finite lattice listed by hand-written enumeration."""

    @staticmethod
    def domain(spec):
        kind = spec["kind"]
        if kind == "integer":
            return list(range(spec["low"], spec["high"] + 1, spec["step"]))
        if kind == "boolean":
            return [False, True]
        if kind == "categorical":
            return list(spec["choices"])
        count = int((spec["high"] - spec["low"]) / spec["step"]) + 1
        return [spec["low"] + index * spec["step"] for index in range(count)]

    def one_move(self, specs, centre):
        """Every vector of the full product that is one move from ``centre``."""
        names = sorted(specs)
        domains = {name: self.domain(specs[name]) for name in names}
        found = set()
        for combo in itertools.product(*(domains[name] for name in names)):
            vector = dict(zip(names, combo))
            moved = [name for name in names if vector[name] != centre[name]]
            if len(moved) != 1:
                continue
            name = moved[0]
            if specs[name]["kind"] in ("integer", "real"):
                gap = domains[name].index(vector[name]) - domains[name].index(
                    centre[name]
                )
                if abs(gap) != 1:
                    continue
            found.add(tuple(vector[each] for each in names))
        return found

    def module_vectors(self, specs, centre):
        names = sorted(specs)
        source = {
            "space": {"parameters": specs, "objective": OBJECTIVE},
            "trials": [row(0, centre)],
        }
        result = neighbourhood([source], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)
        return {
            tuple(vector[each] for each in names) for vector in result["missing_vectors"]
        }

    def check_every_centre(self, specs):
        names = sorted(specs)
        domains = [self.domain(specs[name]) for name in names]
        count = 0
        for combo in itertools.product(*domains):
            centre = dict(zip(names, combo))
            self.assertEqual(
                self.module_vectors(specs, centre),
                self.one_move(specs, centre),
                centre,
            )
            count += 1
        return count

    def test_integer_boolean_categorical_space(self):
        """Every centre of A 1..4, B boolean, C [x, y, z] (24 vectors): the sets agree."""
        specs = {
            "A": {"kind": "integer", "low": 1, "high": 4, "step": 1, "log": False},
            "B": {"kind": "boolean"},
            "C": {"kind": "categorical", "choices": ["x", "y", "z"]},
        }
        self.assertEqual(self.check_every_centre(specs), 24)

    def test_stepped_integer_and_stepped_real_space(self):
        """Every centre of A 2..10 step 4, R 0.5..2.0 step 0.5, B boolean (24 vectors)."""
        specs = {
            "A": {"kind": "integer", "low": 2, "high": 10, "step": 4, "log": False},
            "R": {"kind": "real", "low": 0.5, "high": 2.0, "step": 0.5, "log": False},
            "B": {"kind": "boolean"},
        }
        self.assertEqual(self.check_every_centre(specs), 24)

    def test_a_constant_dimension_adds_no_vector(self):
        """Every centre of A 1..3 and a constant K 7..7 (3 vectors): K never moves."""
        specs = {
            "A": {"kind": "integer", "low": 1, "high": 3, "step": 1, "log": False},
            "K": {"kind": "integer", "low": 7, "high": 7, "step": 1, "log": False},
        }
        self.assertEqual(self.check_every_centre(specs), 3)

    def test_continuous_real_against_exact_rational_arithmetic(self):
        """Real 0..4 without step, f = 0.125, delta 0.5: exact Fraction arithmetic agrees.

        All values are dyadic, so every product and sum is exact in binary64.
        """
        spec = {"kind": "real", "low": 0.0, "high": 4.0, "step": None, "log": False}
        fraction = 0.125
        reals = dimension("X", spec)
        for centre in (0.0, 0.25, 0.5, 1.0, 1.75, 3.5, 3.75, 4.0):
            delta = Fraction(fraction) * (Fraction(4) - Fraction(0))
            expected = tuple(
                float(target)
                for target in (Fraction(centre) - delta, Fraction(centre) + delta)
                if target != Fraction(centre) and 0 <= target <= 4
            )
            self.assertEqual(reals.neighbours(centre, fraction, None).values, expected, centre)

    def test_log_real_against_exact_rational_arithmetic(self):
        """Log real 0.0625..16, rho = 4: v/4 and v*4 are exact; bounds applied exactly."""
        spec = {"kind": "real", "low": 0.0625, "high": 16.0, "step": None, "log": True}
        reals = dimension("X", spec)
        for centre in (0.0625, 0.125, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0):
            expected = tuple(
                float(target)
                for target in (Fraction(centre) / 4, Fraction(centre) * 4)
                if Fraction(1, 16) <= target <= 16
            )
            self.assertEqual(reals.neighbours(centre, None, 4.0).values, expected, centre)

    def test_log_integer_against_decimal_arithmetic(self):
        """Log integer 1..1000 against a Decimal computation rounded half up.

        Decimal at 120 digits, with the exact value of the binary64 ratio, rounds each
        of v/rho and v*rho half up and then drops the centre and the out-of-range
        values. The module does the same with Fraction; the two are written apart.
        """
        spec = {"kind": "integer", "low": 1, "high": 1000, "step": 1, "log": True}
        integers = dimension("X", spec)
        checked = 0
        with localcontext() as context:
            context.prec = 120
            for ratio in (1.5, 2.0, 2.5, 3.0, 1.1):
                exact = Decimal(ratio)
                for value in range(1, 65):
                    expected = []
                    for raw in (Decimal(value) / exact, Decimal(value) * exact):
                        near = int(raw.quantize(Decimal(1), rounding=ROUND_HALF_UP))
                        if near != value and 1 <= near <= 1000:
                            expected.append(near)
                    self.assertEqual(
                        integers.neighbours(value, None, ratio).values,
                        tuple(expected),
                        (ratio, value),
                    )
                    checked += 1
        self.assertEqual(checked, 320)

    def test_log_integer_half_cases_round_up(self):
        """Rule N5: x.5 rounds up. 3/2 = 1.5 -> 2; 5/2 = 2.5 -> 3; 7/2 = 3.5 -> 4; 9/2 = 4.5 -> 5."""
        spec = {"kind": "integer", "low": 1, "high": 1000, "step": 1, "log": True}
        integers = dimension("X", spec)
        self.assertEqual(integers.neighbours(3, None, 2.0).values[0], 2)
        self.assertEqual(integers.neighbours(5, None, 2.0).values[0], 3)
        self.assertEqual(integers.neighbours(7, None, 2.0).values[0], 4)
        self.assertEqual(integers.neighbours(9, None, 2.0).values[0], 5)


class NF6Tests(unittest.TestCase):
    def test_minimize(self):
        """NF6 (minimize), derived again by hand.

        Fast 2..6 step 2, rows 0:0 Fast 4 -> 3.0, 0:1 Fast 2 -> 2.5, 0:2 Fast 6 ->
        5.0. R is the lowest ok objective, 2.5 at 0:1. The centre 0:0 has gap
        3.0 - 2.5 = 0.5, near. Fast 2: gap 2.5 - 2.5 = 0.0, near; Fast 6: 5.0 - 2.5 =
        2.5, not near. One neighbour (2.5) is below the centre's 3.0, so better
        neighbours 1; share 1/2; coverage 2/2 = 1.0.
        """
        result = neighbourhood([NF6], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)
        self.assertEqual(result["direction"], "minimize")
        self.assertEqual(
            result["reference"], {"objective": 2.5, "source": 0, "trial_id": 1}
        )
        centre = result["centres"][0]
        self.assertEqual(centre["centre_gap"], 0.5)
        self.assertIs(centre["centre_near"], True)
        self.assertEqual(
            centre["profile"]["Fast"],
            [
                cell(2, "scored_ok", 2.5, 0.0, True, [[0, 1]]),
                cell(6, "scored_ok", 5.0, 2.5, False, [[0, 2]]),
            ],
        )
        self.assertEqual(centre["counts"]["better_neighbours"], 1)
        self.assertEqual(
            centre["share_near_best"],
            {"numerator": 1, "denominator": 2, "value": 0.5},
        )
        self.assertEqual(
            centre["coverage"], {"numerator": 2, "denominator": 2, "value": 1.0}
        )

    def test_pair_k_with_one_varying_dimension_builds_no_map(self):
        """Rule: pairs need two varying dimensions; with one, ``pair_maps`` is empty."""
        result = neighbourhood([NF6], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=2)
        self.assertEqual(result["centres"][0]["pair_maps"], [])
        self.assertEqual(result["admission"]["lookups"], 2)


class CentreTests(unittest.TestCase):
    def test_two_centres_from_top_ids(self):
        """Rule N13: ``top_ids`` feeds ``centres``; results come in ascending identity.

        NF5 (Fast 2..6 step 2, Slow 10..30 step 10): rows 0:0 (4,20) and 0:5 (2,10)
        tie at 8.0, so ``top_ids`` with k = 2 is [(0,0), (0,5)] (lower identity first).
        Centre 0:5 = (Fast 2, Slow 10): Fast 2 +- 2 gives 0 (below the bound, dropped)
        and 4; Slow 10 +- 10 gives 0 (dropped) and 20. (4, 10) is row 0:3 (7.5, gap
        0.5, near); (2, 20) is row 0:1 (7.0, gap 1.0, near with band 1.0). Dropped by
        bounds 2, geometric 2, near 2, share 2/2 = 1.0, coverage 1.0, nothing above 8.0.
        """
        chosen = top_ids([NF5], 2)
        self.assertEqual(chosen, [(0, 0), (0, 5)])
        result = neighbourhood(
            [NF5], centres=list(reversed(chosen)), band=ABSOLUTE_1, pair_k=0
        )
        self.assertEqual([c["trial_id"] for c in result["centres"]], [0, 5])
        second = result["centres"][1]
        self.assertEqual(second["centre_gap"], 0.0)
        self.assertIs(second["centre_near"], True)
        self.assertEqual(second["dropped"], {"bounds": 2, "same_as_centre": 0})
        self.assertEqual(
            second["profile"],
            {
                "Fast": [cell(4, "scored_ok", 7.5, 0.5, True, [[0, 3]])],
                "Slow": [cell(20, "scored_ok", 7.0, 1.0, True, [[0, 1]])],
            },
        )
        self.assertEqual(
            second["share_near_best"],
            {"numerator": 2, "denominator": 2, "value": 1.0},
        )
        self.assertEqual(second["counts"]["better_neighbours"], 0)
        again = neighbourhood([NF5], centres=chosen, band=ABSOLUTE_1, pair_k=0)
        self.assertEqual(dumped(again), dumped(result))

    def test_nf5_axis_part_without_pair_maps(self):
        """NF5 with ``pair_k`` 0: the four axis cells of centre 0:0 and no missing vector.

        R = 8.0 (rows 0:0 and 0:5 tie; the lower identity 0:0 is named). Fast 2: 7.0,
        gap 1.0, near; Fast 6: 6.0, gap 2.0; Slow 10: 7.5, gap 0.5, near; Slow 30:
        4.0, gap 4.0. Geometric 4, scored ok 4, near 2, share 2/4 = 0.5, coverage 1.0,
        better neighbours 0. Every axis cell has a row, so nothing is missing.
        """
        result = neighbourhood([NF5], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)
        self.assertEqual(
            result["reference"], {"objective": 8.0, "source": 0, "trial_id": 0}
        )
        centre = result["centres"][0]
        self.assertEqual(
            centre["profile"],
            {
                "Fast": [
                    cell(2, "scored_ok", 7.0, 1.0, True, [[0, 1]]),
                    cell(6, "scored_ok", 6.0, 2.0, False, [[0, 2]]),
                ],
                "Slow": [
                    cell(10, "scored_ok", 7.5, 0.5, True, [[0, 3]]),
                    cell(30, "scored_ok", 4.0, 4.0, False, [[0, 4]]),
                ],
            },
        )
        self.assertEqual(
            centre["share_near_best"],
            {"numerator": 2, "denominator": 4, "value": 0.5},
        )
        self.assertEqual(
            centre["coverage"], {"numerator": 4, "denominator": 4, "value": 1.0}
        )
        self.assertEqual(centre["pair_maps"], [])
        self.assertEqual(result["missing_vectors"], [])
        self.assertEqual(
            result["admission"], {"rows": 8, "lookups": 4, "work": 12, "max_work": None}
        )


class TopIdsTests(RefusalTestCase):
    def test_best_distinct_vectors_first_with_lower_identity_on_ties(self):
        """Rule N13, derived again by hand on NF3.

        Ok rows: 0:0 (4, ema) 6.0, 0:1 (2, ema) 5.0, 0:2 (4, ema) 6.0 (a duplicate of
        0:0), 0:3 (6, sma) 7.0, 1:0 (6, ema) 6.5. Distinct ok vectors by objective:
        0:3 (7.0), 1:0 (6.5), 0:0 (6.0, represented by its lower identity), 0:1 (5.0).
        """
        sources = [NF3_0, NF3_1]
        self.assertEqual(top_ids(sources, 1), [(0, 3)])
        self.assertEqual(top_ids(sources, 3), [(0, 3), (1, 0), (0, 0)])
        self.assertEqual(top_ids(sources, 10), [(0, 3), (1, 0), (0, 0), (0, 1)])

    def test_tie_goes_to_the_lower_identity(self):
        """NF5: rows 0:0 and 0:5 both score 8.0 at different vectors; 0:0 comes first."""
        self.assertEqual(top_ids([NF5], 2), [(0, 0), (0, 5)])
        self.assertEqual(top_ids([NF5], 1), [(0, 0)])

    def test_minimize_takes_the_lowest(self):
        """NF6 (minimize): ok objectives 3.0 (0:0), 2.5 (0:1), 5.0 (0:2) -> [0:1, 0:0]."""
        self.assertEqual(top_ids([NF6], 2), [(0, 1), (0, 0)])

    def test_k_must_be_a_positive_integer(self):
        """Rule: k below 1, a bool or a non-integer is parameter_invalid."""
        for k in (0, -1, True, 1.5, "2", None):
            with self.assertRaises(NeighbourhoodError) as caught:
                top_ids([NF5], k)
            self.assertEqual(caught.exception.code, "neighbourhood_parameter_invalid", k)
            self.assertEqual(caught.exception.names, ("k",))

    def test_sources_are_refused_like_in_the_analysis(self):
        """Rule: ``top_ids`` reads, checks and refuses exactly as ``neighbourhood`` does."""
        broken = document(NF3_0)
        broken["trials"][2]["objective"] = 6.5
        with self.assertRaises(NeighbourhoodError) as caught:
            top_ids([broken], 2)
        self.assertEqual(caught.exception.code, "neighbourhood_duplicate_conflict")
        none_ok = document(NF1)
        for trial in none_ok["trials"]:
            trial.update(status="engine_error", feasible=False, objective=None)
        with self.assertRaises(NeighbourhoodError) as caught:
            top_ids([none_ok], 1)
        self.assertEqual(caught.exception.code, "neighbourhood_no_feasible_rows")


class InvarianceTests(unittest.TestCase):
    def test_row_order_never_changes_the_bytes(self):
        """Rule 3.7: rows reversed or shuffled give the same JSON bytes (NF5, pair_k 3)."""
        original = document(NF5)
        base = dumped(
            neighbourhood([original], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=3)
        )
        reversed_doc = copy.deepcopy(original)
        reversed_doc["trials"].reverse()
        self.assertEqual(
            dumped(
                neighbourhood([reversed_doc], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=3)
            ),
            base,
        )
        for seed in range(5):
            shuffled = copy.deepcopy(original)
            random.Random(seed).shuffle(shuffled["trials"])
            self.assertEqual(
                dumped(
                    neighbourhood([shuffled], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=3)
                ),
                base,
            )

    def test_path_text_and_parsed_sources_agree(self):
        """Rule 3.7: a result path (Path or str), a parsed result, a JSONL file and a row list agree."""
        parsed = document(NF1)
        base = dumped(
            neighbourhood([parsed], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)
        )
        self.assertEqual(
            dumped(neighbourhood([NF1], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)),
            base,
        )
        self.assertEqual(
            dumped(
                neighbourhood([str(NF1)], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)
            ),
            base,
        )
        self.assertEqual(
            dumped(
                neighbourhood(
                    [parsed["trials"]],
                    centres=[(0, 0)],
                    band=ABSOLUTE_1,
                    pair_k=0,
                    space=parsed["space"],
                )
            ),
            base,
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.jsonl"
            lines = [json.dumps(item) for item in parsed["trials"]]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertEqual(
                dumped(
                    neighbourhood(
                        [path],
                        centres=[(0, 0)],
                        band=ABSOLUTE_1,
                        pair_k=0,
                        space=parsed["space"],
                    )
                ),
                base,
            )

    def test_repeats_are_identical_and_inputs_are_untouched(self):
        """Rule 3.7: three repeats give identical bytes; the parsed input is not changed."""
        parsed = [document(NF3_0), document(NF3_1)]
        before = copy.deepcopy(parsed)
        runs = [
            dumped(
                neighbourhood(
                    parsed, centres=[(0, 0), (0, 3)], band=ABSOLUTE_1, pair_k=0
                )
            )
            for _ in range(3)
        ]
        self.assertEqual(len(set(runs)), 1)
        self.assertEqual(parsed, before)

    def test_result_is_plain_json(self):
        """Rule 3.7: the result survives a strict JSON round trip unchanged."""
        result = neighbourhood(
            [NF5B], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=3
        )
        again = json.loads(dumped(result))
        self.assertEqual(again, result)

    def test_input_digest(self):
        """Rule 3.6: ``input_sha256`` is the hash of the sorted per-row digests.

        Recomputed here from the fixture rows with the recipe of the docs: each row is
        the canonical JSON of [source, id, status, objective, vector key], hashed with
        SHA-256; the 32-byte digests are sorted, joined and hashed again. A changed
        objective changes it; the order of the rows does not.
        """
        parsed = document(NF1)
        space = parsed["space"]
        digests = []
        for item in parsed["trials"]:
            key = _rows.vector_key(item["parameters"], space)
            text = json.dumps(
                [0, item["trial_id"], item["status"], item["objective"], key],
                separators=(",", ":"),
                allow_nan=False,
            )
            digests.append(hashlib.sha256(text.encode("ascii")).digest())
        expected = hashlib.sha256(b"".join(sorted(digests))).hexdigest()
        result = neighbourhood([parsed], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)
        self.assertEqual(result["input_sha256"], expected)
        self.assertEqual(len(expected), 64)
        changed = copy.deepcopy(parsed)
        changed["trials"][1]["objective"] = 9.25
        other = neighbourhood([changed], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)
        self.assertNotEqual(other["input_sha256"], expected)
        shuffled = copy.deepcopy(parsed)
        shuffled["trials"].reverse()
        same = neighbourhood([shuffled], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)
        self.assertEqual(same["input_sha256"], expected)

    def test_recorded_space_hash_is_reported_per_source(self):
        """Rule 3.6: a source reports the space_hash its rows record, else null."""
        parsed = document(NF1)
        for item in parsed["trials"]:
            item["space_hash"] = "ab" * 32
        result = neighbourhood(
            [parsed, NF1], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0
        )
        self.assertEqual(
            result["sources"],
            [
                {"index": 0, "rows": 6, "space_hash": "ab" * 32},
                {"index": 1, "rows": 6, "space_hash": None},
            ],
        )


class RefusalTests(RefusalTestCase):
    """One forced-failure control per refusal code of clause 3.8 (N-draft 4.1)."""

    def nf1(self):
        return document(NF1)

    def test_every_code_has_a_forced_failure(self):
        """Controls: each of the 13 codes is raised by a one-mutation input of NF1/NF3."""
        counters = {
            "input_bars_processed": 100,
            "script_bars_processed": 100,
            "input_tf_seconds": 3600,
            "script_tf_seconds": 3600,
        }

        def off_lattice():
            doc = self.nf1()
            doc["trials"][2]["parameters"]["Fast"] = 5
            return neighbourhood([doc], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)

        def incomplete():
            doc = self.nf1()
            doc["trials_out"] = "best-k"
            return neighbourhood([doc], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)

        def no_space():
            return neighbourhood(
                [self.nf1()["trials"]], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0
            )

        def different_spaces():
            second = document(NF3_1)
            second["space"]["parameters"]["Fast"]["high"] = 10
            return neighbourhood(
                [document(NF3_0), second], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0
            )

        def different_contexts():
            first, second = document(NF3_0), document(NF3_1)
            for doc in (first, second):
                for trial in doc["trials"]:
                    trial["backtest"] = dict(counters)
            second["trials"][0]["backtest"]["input_bars_processed"] = 101
            return neighbourhood(
                [first, second], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0
            )

        def no_band():
            return neighbourhood([NF1], centres=[(0, 0)], band=None, pair_k=0)

        def bad_ratio():
            return neighbourhood(
                [NF2_0],
                centres=[(0, 0)],
                band=ABSOLUTE_1,
                fraction=0.25,
                ratio=1.0,
                pair_k=0,
            )

        def zero_reference():
            doc = self.nf1()
            for trial in doc["trials"]:
                if trial["status"] == "ok":
                    trial["objective"] = 0.0
            return neighbourhood(
                [doc],
                centres=[(0, 0)],
                band={"mode": "relative", "value": 0.5},
                pair_k=0,
            )

        def infeasible_centre():
            return neighbourhood([NF1], centres=[(0, 3)], band=ABSOLUTE_1, pair_k=0)

        def only_constants():
            space = {
                "parameters": {
                    "A": {"kind": "integer", "low": 5, "high": 5, "step": 1, "log": False},
                    "B": {"kind": "categorical", "choices": ["only"]},
                },
                "objective": OBJECTIVE,
            }
            source = {"space": space, "trials": [row(0, {"A": 5, "B": "only"})]}
            return neighbourhood([source], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)

        def no_ok_row():
            doc = self.nf1()
            for trial in doc["trials"]:
                trial.update(status="engine_error", feasible=False, objective=None)
            return neighbourhood([doc], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)

        def conflicting_duplicates():
            first = document(NF3_0)
            first["trials"][2]["objective"] = 6.5
            return neighbourhood([first], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)

        def too_much_work():
            return neighbourhood(
                [NF1], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0, max_work=1
            )

        cases = {
            "neighbourhood_input_invalid": off_lattice,
            "neighbourhood_rows_incomplete": incomplete,
            "neighbourhood_space_missing": no_space,
            "neighbourhood_space_mismatch": different_spaces,
            "neighbourhood_context_mismatch": different_contexts,
            "neighbourhood_parameter_missing": no_band,
            "neighbourhood_parameter_invalid": bad_ratio,
            "neighbourhood_band_degenerate": zero_reference,
            "neighbourhood_centre_invalid": infeasible_centre,
            "neighbourhood_no_varying_dimension": only_constants,
            "neighbourhood_no_feasible_rows": no_ok_row,
            "neighbourhood_duplicate_conflict": conflicting_duplicates,
            "neighbourhood_resource_exceeded": too_much_work,
        }
        self.assertEqual(set(cases), set(CODES))
        for code, call in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(NeighbourhoodError) as caught:
                    call()
                error = caught.exception
                self.assertEqual(error.code, code)
                document_ = error.document()
                self.assertEqual(document_["status"], "refused")
                self.assertEqual(document_["code"], code)
                json.dumps(document_, allow_nan=False)

    def test_off_lattice_row_names_the_row_and_the_dimension(self):
        """Refusal fixture: ``Fast`` 5 in row 0:2 -> input_invalid naming 0:2 and Fast."""
        doc = self.nf1()
        doc["trials"][2]["parameters"]["Fast"] = 5
        error = self.refusal(
            "neighbourhood_input_invalid",
            [doc],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
        )
        self.assertEqual(error.ids, ((0, 2),))
        self.assertEqual(error.names, ("Fast",))

    def test_other_input_defects_are_input_invalid(self):
        """Rule 3.2: each defect below is input_invalid and names the row it can."""
        mutations = {
            "feasible disagrees": lambda d: d["trials"][1].update(feasible=False),
            "unknown status": lambda d: d["trials"][1].update(status="weird"),
            "ok without objective": lambda d: d["trials"][1].update(objective=None),
            "unknown parameter": lambda d: d["trials"][1]["parameters"].update(Extra=1),
            "missing parameter": lambda d: d["trials"][1]["parameters"].pop("Mode"),
            "no parameters": lambda d: d["trials"][1].pop("parameters"),
            "wrong type": lambda d: d["trials"][1]["parameters"].update(Fast=4.0),
            "bool for integer": lambda d: d["trials"][1]["parameters"].update(Fast=True),
            "not a choice": lambda d: d["trials"][1]["parameters"].update(Mode="hma"),
            "above the bound": lambda d: d["trials"][1]["parameters"].update(Fast=12),
            "repeated id": lambda d: d["trials"][1].update(trial_id=0),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                doc = self.nf1()
                mutate(doc)
                with self.assertRaises(NeighbourhoodError) as caught:
                    neighbourhood([doc], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)
                self.assertEqual(caught.exception.code, "neighbourhood_input_invalid")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nan.jsonl"
            first = json.dumps(self.nf1()["trials"][0])
            path.write_text(first + "\n" + first.replace("10.0", "NaN") + "\n", encoding="utf-8")
            error = self.refusal(
                "neighbourhood_input_invalid",
                [path],
                centres=[(0, 0)],
                band=ABSOLUTE_1,
                pair_k=0,
                space=self.nf1()["space"],
            )
            self.assertIn("NaN", error.detail)
        error = self.refusal(
            "neighbourhood_input_invalid",
            [],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
        )
        self.assertEqual(error.names, ("sources",))
        self.refusal(
            "neighbourhood_input_invalid",
            str(NF1),
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
        )

    def test_reader_ids_carry_their_source(self):
        """Rule: a reader refusal names the row as (source, id), also for source 1."""
        doc = self.nf1()
        doc["trials"][1]["feasible"] = False
        error = self.refusal(
            "neighbourhood_input_invalid",
            [NF1, doc],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
        )
        self.assertEqual(error.ids, ((1, 1),))

    def test_rows_incomplete_variants(self):
        """Refusal fixture: a best-k result, and a trials_completed that disagrees."""
        doc = self.nf1()
        doc["trials_out"] = "best-k"
        self.refusal(
            "neighbourhood_rows_incomplete",
            [doc],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
        )
        doc = self.nf1()
        doc["trials_completed"] = 7
        self.refusal(
            "neighbourhood_rows_incomplete",
            [doc],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
        )

    def test_space_missing_and_the_passed_space(self):
        """Refusal fixture: bare rows and no space; the same call with a space works."""
        rows = self.nf1()["trials"]
        self.refusal(
            "neighbourhood_space_missing",
            [rows],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
        )
        passed = neighbourhood(
            [rows],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
            space=self.nf1()["space"],
        )
        self.assertEqual(passed["reference"]["objective"], 10.5)

    def test_space_mismatch_names_the_dimension(self):
        """Refusal fixture: two sources with different ``Fast`` bounds -> names (Fast,)."""
        second = document(NF3_1)
        second["space"]["parameters"]["Fast"]["high"] = 10
        error = self.refusal(
            "neighbourhood_space_mismatch",
            [document(NF3_0), second],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
        )
        self.assertEqual(error.names, ("Fast",))

    def test_direction_disagreement_is_a_space_mismatch(self):
        """Rule: a result that records minimize while the space says maximize refuses."""
        doc = self.nf1()
        space = doc.pop("space")
        doc["direction"] = "minimize"
        error = self.refusal(
            "neighbourhood_space_mismatch",
            [doc],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
            space=space,
        )
        self.assertEqual(error.names, ("direction",))

    def test_context_mismatch_names_the_rows_and_the_counter(self):
        """Refusal fixture: ok rows of two sources differ in input_bars_processed."""
        counters = {
            "input_bars_processed": 100,
            "script_bars_processed": 100,
            "input_tf_seconds": 3600,
            "script_tf_seconds": 3600,
        }
        first, second = document(NF3_0), document(NF3_1)
        for doc in (first, second):
            for trial in doc["trials"]:
                trial["backtest"] = dict(counters)
        second["trials"][0]["backtest"]["input_bars_processed"] = 101
        error = self.refusal(
            "neighbourhood_context_mismatch",
            [first, second],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
        )
        self.assertEqual(error.ids, ((0, 0), (1, 0)))
        self.assertEqual(error.names, ("input_bars_processed",))

    def test_different_recorded_space_hashes_are_a_context_mismatch(self):
        """Rule N14: rows that record different space_hash values refuse."""
        doc = self.nf1()
        for item in doc["trials"]:
            item["space_hash"] = "aa" * 32
        doc["trials"][3]["space_hash"] = "bb" * 32
        error = self.refusal(
            "neighbourhood_context_mismatch",
            [doc],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
        )
        self.assertEqual(error.names, ("space_hash",))

    def test_parameter_missing(self):
        """Refusal fixture: no band; also no centres, an empty list of centres."""
        error = self.refusal(
            "neighbourhood_parameter_missing",
            [NF1],
            centres=[(0, 0)],
            band=None,
            pair_k=0,
        )
        self.assertEqual(error.names, ("band",))
        for centres in (None, [], ()):
            error = self.refusal(
                "neighbourhood_parameter_missing",
                [NF1],
                centres=centres,
                band=ABSOLUTE_1,
                pair_k=0,
            )
            self.assertEqual(error.names, ("centres",))

    def test_absent_pair_k_is_refused_as_parameter_missing(self):
        """Rule N12: ``pair_k`` has no default; an absent one is parameter_missing.

        ``pair_k=None`` is the absent value, as ``band=None`` is for the band. The call
        is refused with ``neighbourhood_parameter_missing``, naming ``pair_k`` and no
        row, and no result comes back. The check belongs to the caller parameters and is
        made before any source is read: a source with a row off the lattice (which would
        be input_invalid if it were read) gives the same refusal.

        Mutation: in ``_check_parameters`` change ``if pair_k is None:`` to
        ``if False:``. ``None`` then reaches the validity check, the refusal is
        ``neighbourhood_parameter_invalid`` and the code assertions fail.
        """
        error = self.refusal(
            "neighbourhood_parameter_missing",
            [NF1],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=None,
        )
        self.assertEqual(error.names, ("pair_k",))
        self.assertEqual(error.ids, ())
        self.assertEqual(error.document()["status"], "refused")
        doc = self.nf1()
        doc["trials"][2]["parameters"]["Fast"] = 5
        error = self.refusal(
            "neighbourhood_parameter_missing",
            [doc],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=None,
        )
        self.assertEqual(error.names, ("pair_k",))

    def test_leaving_pair_k_out_is_an_error_of_the_call(self):
        """Rule N12: no default stands in for ``pair_k``; a call without it fails.

        The argument is required and keyword-only, like ``centres`` and ``band``. A call
        that leaves it out raises ``TypeError`` naming ``pair_k`` and returns no result:
        the body of the function is never entered, so no assumed value is computed.
        (An explicit ``None`` is the module's own refusal, tested above.)

        Mutation: in the signature of ``neighbourhood`` change ``pair_k: Any,`` to
        ``pair_k: Any = 0,``. The call then returns a result, nothing is raised and the
        assertion fails.
        """
        with self.assertRaises(TypeError) as caught:
            neighbourhood([NF1], centres=[(0, 0)], band=ABSOLUTE_1)
        self.assertIn("pair_k", str(caught.exception))

    def test_explicit_pair_k_zero_is_accepted_and_builds_no_map(self):
        """Rule N12: an explicit 0 means none, as the default 0 did (NF5).

        The first call is the one that was written without the argument before
        ``pair_k`` lost its default, now with the 0 spelt out, and it returns what that
        call returned. NF5 (Fast 2..6 step 2, Slow 10..30 step 10): R = 8.0 (rows 0:0
        and 0:5 tie; the lower identity 0:0 is named); the centre 0:0 is (Fast 4, Slow
        20). Fast 2 is row 0:1 (7.0, gap 1.0, near with band 1.0), Fast 6 row 0:2 (6.0,
        gap 2.0), Slow 10 row 0:3 (7.5, gap 0.5, near) and Slow 30 row 0:4 (4.0, gap
        4.0). Every axis neighbour has a row, so nothing is missing, and the work is 8
        rows plus 4 lookups. With ``pair_k`` 0 no pair map is built; the control with
        ``pair_k`` 1 builds the one map of (Fast, Slow) and keeps the same profile.

        Mutation: in ``_check_parameters`` change ``if pair_k is None:`` to
        ``if not pair_k:``. 0 is then refused as missing and the first call raises.
        """
        result = neighbourhood([NF5], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rules"]["pair_k"], 0)
        centre = result["centres"][0]
        self.assertEqual(
            centre["profile"],
            {
                "Fast": [
                    cell(2, "scored_ok", 7.0, 1.0, True, [[0, 1]]),
                    cell(6, "scored_ok", 6.0, 2.0, False, [[0, 2]]),
                ],
                "Slow": [
                    cell(10, "scored_ok", 7.5, 0.5, True, [[0, 3]]),
                    cell(30, "scored_ok", 4.0, 4.0, False, [[0, 4]]),
                ],
            },
        )
        self.assertEqual(centre["pair_maps"], [])
        self.assertEqual(result["missing_vectors"], [])
        self.assertEqual(
            result["admission"], {"rows": 8, "lookups": 4, "work": 12, "max_work": None}
        )
        with_map = neighbourhood([NF5], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=1)
        self.assertEqual(len(with_map["centres"][0]["pair_maps"]), 1)
        self.assertEqual(with_map["centres"][0]["profile"], centre["profile"])

    def test_parameter_invalid_variants(self):
        """Rule 3.8: every malformed caller parameter is parameter_invalid, naming it."""
        variants = [
            ("band", {"band": {"mode": "median", "value": 1.0}}),
            ("band", {"band": {"mode": "absolute", "value": -1.0}}),
            ("band", {"band": {"mode": "absolute", "value": float("nan")}}),
            ("band", {"band": {"mode": "absolute", "value": float("inf")}}),
            ("band", {"band": {"mode": "absolute", "value": True}}),
            ("band", {"band": {"mode": "absolute", "value": "1"}}),
            ("band", {"band": {"mode": "absolute"}}),
            ("band", {"band": {"mode": "absolute", "value": 1.0, "extra": 1}}),
            ("band", {"band": "wide"}),
            ("fraction", {"fraction": 0.0}),
            ("fraction", {"fraction": 1.5}),
            ("fraction", {"fraction": -0.25}),
            ("fraction", {"fraction": float("nan")}),
            ("fraction", {"fraction": True}),
            ("fraction", {"fraction": "0.5"}),
            ("ratio", {"ratio": 1.0}),
            ("ratio", {"ratio": 0.5}),
            ("ratio", {"ratio": float("inf")}),
            ("ratio", {"ratio": True}),
            ("pair_k", {"pair_k": -1}),
            ("pair_k", {"pair_k": 1.5}),
            ("pair_k", {"pair_k": True}),
            ("pair_k", {"pair_k": "2"}),
            ("max_work", {"max_work": 0}),
            ("max_work", {"max_work": -3}),
            ("max_work", {"max_work": True}),
            ("max_work", {"max_work": 2.5}),
            ("centres", {"centres": [(0,)]}),
            ("centres", {"centres": "ab"}),
            ("centres", {"centres": [(0, "a")]}),
            ("centres", {"centres": [(True, 0)]}),
            ("centres", {"centres": [(-1, 0)]}),
            ("centres", {"centres": [(0, 0), 5]}),
        ]
        for name, change in variants:
            with self.subTest(change=str(change)):
                arguments = {"centres": [(0, 0)], "band": ABSOLUTE_1, "pair_k": 0}
                arguments.update(change)
                with self.assertRaises(NeighbourhoodError) as caught:
                    neighbourhood([NF1], **arguments)
                self.assertEqual(caught.exception.code, "neighbourhood_parameter_invalid")
                self.assertEqual(caught.exception.names, (name,))

    def test_relative_band_at_a_zero_reference_is_degenerate(self):
        """Refusal fixture: all ok objectives 0.0 -> R = 0, a relative band refuses.

        The same data with an absolute band is accepted (control).
        """
        doc = self.nf1()
        for trial in doc["trials"]:
            if trial["status"] == "ok":
                trial["objective"] = 0.0
        self.refusal(
            "neighbourhood_band_degenerate",
            [doc],
            centres=[(0, 0)],
            band={"mode": "relative", "value": 0.5},
            pair_k=0,
        )
        accepted = neighbourhood([doc], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0)
        self.assertEqual(accepted["reference"]["objective"], 0.0)

    def test_centre_invalid_variants(self):
        """Refusal fixture: absent, constraint violation, engine error, listed twice."""
        for centres, ids in (
            ([(0, 99)], ((0, 99),)),
            ([(1, 0)], ((1, 0),)),
            ([(0, 3)], ((0, 3),)),
            ([(0, 4)], ((0, 4),)),
            ([(0, 0), (0, 0)], ((0, 0),)),
            ([(0, 0), (0, 3), (0, 99)], ((0, 3), (0, 99))),
        ):
            with self.subTest(centres=centres):
                error = self.refusal(
                    "neighbourhood_centre_invalid",
                    [NF1],
                    centres=centres,
                    band=ABSOLUTE_1,
                    pair_k=0,
                )
                self.assertEqual(error.ids, ids)

    def test_only_constant_dimensions(self):
        """Refusal fixture: every dimension has one legal value -> names list them."""
        space = {
            "parameters": {
                "A": {"kind": "integer", "low": 5, "high": 5, "step": 1, "log": False},
                "B": {"kind": "categorical", "choices": ["only"]},
            },
            "objective": OBJECTIVE,
        }
        source = {"space": space, "trials": [row(0, {"A": 5, "B": "only"})]}
        error = self.refusal(
            "neighbourhood_no_varying_dimension",
            [source],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
        )
        self.assertEqual(error.names, ("A", "B"))

    def test_no_ok_row(self):
        """Refusal fixture: a source with no ok row has no reference value."""
        doc = self.nf1()
        for trial in doc["trials"]:
            trial.update(status="engine_error", feasible=False, objective=None)
        self.refusal(
            "neighbourhood_no_feasible_rows",
            [doc],
            centres=[(0, 0)],
            band=ABSOLUTE_1,
            pair_k=0,
        )

    def test_work_above_max_work_is_refused_at_the_boundary(self):
        """Rule 3.9, NF1: rows 6 + lookups 5 = 11; 11 passes, 10 and 1 refuse.

        The refusal names ``max_work`` and states the figures. Without ``max_work`` no
        cap exists.
        """
        passed = neighbourhood(
            [NF1], centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0, max_work=11
        )
        self.assertEqual(
            passed["admission"], {"rows": 6, "lookups": 5, "work": 11, "max_work": 11}
        )
        for limit in (10, 1):
            error = self.refusal(
                "neighbourhood_resource_exceeded",
                [NF1],
                centres=[(0, 0)],
                band=ABSOLUTE_1,
                pair_k=0,
                max_work=limit,
            )
            self.assertEqual(error.names, ("max_work",))
            self.assertIn("work figure 11", error.detail)

    def test_pair_cells_are_admitted_after_the_ranking(self):
        """Rule 3.9, NF5b with pair_k 3: rows 6, axis lookups 5, pair lookups 18.

        The pair maps span 9, 6 and 6 cells, so they add 8 + 5 + 5 = 18 lookups and
        the work is 6 + 5 + 18 = 29. The cells depend on the ranking, so they are
        counted after it and before the first one is looked up: max_work 29 passes,
        28 and 20 refuse with the figure 29, and 10 refuses already at 11.
        """
        arguments = {"centres": [(0, 0)], "band": ABSOLUTE_1, "pair_k": 3}
        passed = neighbourhood([NF5B], max_work=29, **arguments)
        self.assertEqual(
            passed["admission"], {"rows": 6, "lookups": 23, "work": 29, "max_work": 29}
        )
        for limit in (28, 20):
            error = self.refusal(
                "neighbourhood_resource_exceeded", [NF5B], max_work=limit, **arguments
            )
            self.assertIn("work figure 29", error.detail)
        error = self.refusal(
            "neighbourhood_resource_exceeded", [NF5B], max_work=10, **arguments
        )
        self.assertIn("work figure 11", error.detail)

    def test_first_defect_in_the_documented_order_is_reported(self):
        """Rule: with two defects the earlier rule of the documented order wins."""
        doc = self.nf1()
        doc["trials"][2]["parameters"]["Fast"] = 5
        error = self.refusal(
            "neighbourhood_parameter_missing",
            [doc],
            centres=[(0, 0)],
            band=None,
            pair_k=0,
        )
        self.assertEqual(error.names, ("band",))
        error = self.refusal(
            "neighbourhood_input_invalid",
            [doc],
            centres=[(0, 99)],
            band=ABSOLUTE_1,
            pair_k=0,
        )
        self.assertEqual(error.ids, ((0, 2),))


class StudyTests(unittest.TestCase):
    def test_prepare_exposes_the_indexed_rows(self):
        """Positive control: ``prepare`` reads NF1 and indexes its six vectors."""
        study = prepare([NF1])
        self.assertIsInstance(study, Study)
        self.assertEqual(study.names, ("Fast", "Flag", "Mode"))
        self.assertEqual(study.varying, ("Fast", "Flag", "Mode"))
        self.assertEqual(study.constants, ())
        self.assertEqual(study.direction, "maximize")
        self.assertEqual(study.reference, 10.5)
        self.assertEqual(study.reference_id, (0, 5))
        self.assertEqual(study.row_count, 6)
        self.assertEqual(len(study.groups), 6)
        self.assertEqual(study.duplicate_rows, 0)
        with self.assertRaises(ValueError):
            study.cell({"Fast": 4, "Flag": False, "Mode": "ema"})

    def test_cell_after_binding_the_band(self):
        """Positive control: with a band bound, ``cell`` classifies a full vector."""
        study = prepare([NF1])
        study.bind({"mode": "absolute", "value": 1.0}, None, None)
        self.assertEqual(study.delta, 1.0)
        self.assertEqual(
            study.cell({"Fast": 8, "Flag": True, "Mode": "sma"}),
            {
                "class": "scored_ok",
                "objective": 10.5,
                "gap": 0.0,
                "near": True,
                "rows": [[0, 5]],
            },
        )
        self.assertEqual(
            study.cell({"Fast": 10, "Flag": True, "Mode": "wma"})["class"],
            "never_evaluated",
        )

    def test_names_sort_by_utf8_bytes(self):
        """Rule 3.3: dimensions are ordered by byte-wise UTF-8 name."""
        names = ["b", "B", "a", "é", "Z", "_"]
        space = {
            "parameters": {name: {"kind": "boolean"} for name in names},
            "objective": OBJECTIVE,
        }
        parameters = {name: False for name in names}
        study = prepare([{"space": space, "trials": [row(0, parameters)]}])
        self.assertEqual(study.names, ("B", "Z", "_", "a", "b", "é"))
