"""Tests for the deflated Sharpe ratio of recorded trials.

Exact and tabular expectations for all-eligible inline fixtures are hand-derived
in the test docstrings. ORACLE_PINNED contains the expected SR0, z and DSR values
for fixtures with excluded rows, computed by the independent oracle with exact
arithmetic. No expectation is computed from the product module.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import pickle
from fractions import Fraction
from pathlib import Path
from statistics import NormalDist
from tempfile import TemporaryDirectory
import unittest

from pineforge_hpo import dsr as module
from pineforge_hpo.dsr import ALGORITHM, CODES, CONTRACT, INTERPRETATION, DsrError, dsr

ORACLE_PINNED = {
    "a": {
        "sr0": 0.5962970005073946,
        "z": 0.9888663563879396,
        "dsr": 0.8386357334617517,
    },
    "c": {
        "sr0": 1.075352571326559,
        "z": 0.8739171984227947,
        "dsr": 0.8089183260288177,
    },
    "bool": {
        "sr0": 0.6030238422545333,
        "z": 2.419633682177503,
        "dsr": 0.9922319254993271,
    },
    "max": {
        "sr0": 0.30151192112726666,
        "z": 1.710939384655088,
        "dsr": 0.9564538484322632,
    },
}

RESULT_KEYS = (
    "contract",
    "status",
    "series",
    "selected",
    "sharpe_per_period",
    "count",
    "skew",
    "kurt_raw",
    "n_trials",
    "n_trials_source",
    "variance_basis",
    "variance",
    "sr0",
    "den",
    "z",
    "dsr",
    "selected_is_max",
    "rows_read",
    "rows_eligible",
    "rows_excluded",
    "eligible",
    "excluded",
    "algorithm",
    "input_sha256",
    "interpretation",
)

# Typed copy of the fixed text, independent of the module constant.
FIXED_TEXT = (
    "(a) The number is conditional on the trial count N and on the variance V, and "
    "both are read from the finished rows of this one source: the default N is the "
    "number of rows read, every trial of the record whatever its status, so it is "
    "the conservative count of the trials this file holds, and V uses only the "
    "eligible rows. If the selection was made over trials this source does not "
    "hold — a sweep split over several studies, or a grid whose other arms were "
    "written elsewhere — then N counts only the trials this file holds, and the "
    "number is too favourable, unless the caller passes n_trials. "
    "(b) The module assumes that the returns of the selected trial are the ones its "
    "sharpe_per_period was computed on; it reads a recorded number and cannot check "
    "what data any trial saw, so the recording is taken as given. "
    "(c) The trials of a tuned study are not independent: the search correlates them, "
    "the runs share a dataset and a window the module cannot see, and the N of the "
    "paper is an effective number of independent trials, not a count of runs. The "
    "number is therefore a deflated reading under a stated N, and it is not a "
    "significance test of the strategy. "
    "(d) No threshold is applied: 0.95 and every other level are the caller's to set, "
    "and the module reports a number only. "
    "(e) The number is in-sample: it says nothing about how the strategy would perform "
    "on data the study did not see, and no out-of-sample performance is claimed. "
    "(f) v1 analyses finished studies; native in-loop walk-forward comes later."
)

FIELDS = ("sharpe_per_period", "count", "skew", "kurt_raw", "status")


def row(identifier, sr=1.0, count=10, skew=0.0, kurt=3.0, status=0, series="bar"):
    """A reader-valid row with the five recorded statistics, not an objective SR."""
    return {
        "trial_id": identifier,
        "status": "ok",
        "feasible": True,
        "objective": 100.0 + identifier,
        "metrics": {
            f"returns.{series}.{name}": value
            for name, value in zip(FIELDS, (sr, count, skew, kurt, status))
        },
    }


def excluded_row(identifier, status="pruned"):
    """A reader-valid excluded row; constraint violations need finite objectives."""
    return {
        "trial_id": identifier,
        "status": status,
        "feasible": False,
        "objective": 0.0 if status == "constraint_violation" else None,
    }


def a_rows():
    """Worked fixture: three eligible ratios and two excluded trials."""
    rows = [
        row(0, 0.5, 8, 0.5, 4.0),
        row(1, 1.5, 12, -0.2, 5.0),
        row(2),
        excluded_row(3),
        {"trial_id": 4, "status": "ok", "feasible": True, "objective": 0.0},
    ]
    for entry in rows[:3]:
        entry["metrics"]["returns.bar.periods_per_year"] = 10.0
    return rows


def b_rows():
    """Two eligible rows, selecting the larger Sharpe of 1.5."""
    return [row(0, 0.5), row(1, 1.5)]


def notlast_rows():
    """The selected id 0 differs in all four statistics from last eligible id 2."""
    return [row(0), row(1, 1.5, 20, 0.4, 5.0), row(2, 0.5, 6, -0.3, 4.0)]


def c_rows():
    """One row per exclusion branch, plus the two eligible ratios of fixture B."""
    return [
        row(0, count=None),
        row(1, None, None, None, None, 8),
        row(2, count="10"),
        row(3, count=1),
        row(4, count=3, skew=None, kurt=None, status=1),
        row(5, series="monthly"),
        excluded_row(6, "engine_error"),
        row(7, 0.5),
        row(8, 1.5),
    ]


def d_rows():
    """Both eligible rows record both series, whose five key names differ."""
    rows = b_rows()
    rows[0]["metrics"].update(row(0, 0.25, 12, 0.1, 3.2, series="monthly")["metrics"])
    rows[1]["metrics"].update(row(1, 0.75, 12, -0.1, 3.4, series="monthly")["metrics"])
    return rows


def digest_from_recipe(rows, series="bar"):
    """Rebuild the documented typed-cell digest without calling product helpers."""

    def compact(value):
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    parts = []
    for entry in rows:
        cells = {}
        for field in FIELDS:
            name = f"returns.{series}.{field}"
            value = entry.get("metrics", {}).get(name)
            if value is None or type(value) is int:
                cell = value
            elif type(value) is bool:
                cell = ["bool", value]
            elif type(value) is float:
                if math.isfinite(value):
                    cell = value
                else:
                    tag = "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"
                    cell = ["float", tag]
            else:
                try:
                    text = compact(value)
                except (TypeError, ValueError, OverflowError, RecursionError):
                    text = "<unwritable>"
                cell = ["other", text]
            cells[name] = cell
        payload = [entry["trial_id"], entry["status"], cells]
        parts.append(hashlib.sha256(compact(payload).encode("ascii")).digest())
    return hashlib.sha256(b"".join(sorted(parts))).hexdigest()


class DsrTestCase(unittest.TestCase):
    def refusal(self, source, *, selected=0, series="bar", **options):
        """Require a public-path refusal and return its four fields for assertions."""
        with self.assertRaises(DsrError) as caught:
            dsr(source, series=series, selected=selected, **options)
        return caught.exception

    def assertRefused(self, error, code, ids=None, names=None):
        """Check the code and refusal document without requiring unspecified detail."""
        self.assertEqual(error.code, code)
        if ids is not None:
            self.assertEqual(error.ids, tuple(ids))
        if names is not None:
            self.assertEqual(error.names, tuple(names))
        self.assertEqual(
            error.document(),
            {
                "contract": "pineforge-hpo-dsr/v1",
                "status": "refused",
                "refusal": {
                    "code": code,
                    "detail": error.detail,
                    "ids": list(error.ids),
                    "names": list(error.names),
                },
            },
        )

    def assertTabular(self, result, sr0, z, probability):
        """Printed normal tables allow delta 5e-4, not a binary64 accuracy claim."""
        for name, expected in zip(("sr0", "z", "dsr"), (sr0, z, probability)):
            self.assertAlmostEqual(result[name], expected, delta=5e-4, msg=name)

    def assertPinned(self, result, fixture):
        """Compare the independent oracle values within the stated numeric bounds."""
        expected = ORACLE_PINNED[fixture]
        tol_sr0 = 8 * math.ulp(expected["sr0"])
        tol_z = tol_sr0 * math.sqrt(result["count"] - 1) / math.sqrt(
            result["den"]
        ) + 4 * math.ulp(abs(expected["z"]))
        density = math.exp(-(expected["z"] ** 2) / 2) / math.sqrt(2 * math.pi)
        self.assertAlmostEqual(result["sr0"], expected["sr0"], delta=tol_sr0)
        self.assertAlmostEqual(result["z"], expected["z"], delta=tol_z)
        self.assertAlmostEqual(
            result["dsr"], expected["dsr"], delta=density * tol_z + 1e-15
        )


class ConstantsTests(DsrTestCase):
    def test_constants_and_codes(self):
        """Catches changed constants, identifiers or substituted refusal codes."""
        self.assertEqual(CONTRACT, "pineforge-hpo-dsr/v1")
        self.assertEqual(ALGORITHM, "deflated_sharpe_exact_v1")
        self.assertEqual(module._G, 0.5772156649015329)
        self.assertEqual(module._E, 2.718281828459045)
        self.assertEqual(module._E, math.e)
        self.assertEqual(
            CODES,
            (
                "dsr_input_invalid",
                "dsr_rows_incomplete",
                "dsr_context_mismatch",
                "dsr_parameter_invalid",
                "dsr_selected_unknown",
                "dsr_selected_not_eligible",
                "dsr_no_eligible_rows",
                "dsr_too_few_eligible_rows",
                "dsr_trials_inconsistent",
                "dsr_zero_sharpe_variance",
                "dsr_statistic_undefined",
                "dsr_resource_exceeded",
            ),
        )

    def test_interpretation(self):
        """Catches text drift, favourable default-N wording or lost limitations."""
        self.assertEqual(INTERPRETATION, FIXED_TEXT)
        self.assertIn(
            "v1 analyses finished studies; native in-loop walk-forward comes later.",
            INTERPRETATION,
        )

    def test_reader_and_normal_distribution_dependencies(self):
        """Catches optimizer/extra reader imports or missing statistics imports."""
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add("." * node.level + (node.module or ""))
                if node.module == "_rows":
                    self.assertEqual(
                        {alias.name for alias in node.names},
                        {"RowsError", "check_context", "read_source"},
                    )
        self.assertEqual(
            imports,
            {
                "__future__",
                "hashlib",
                "json",
                "math",
                "fractions",
                "statistics",
                "typing",
                "._rows",
            },
        )


class DsrErrorTests(DsrTestCase):
    def test_fields_are_kept_and_ids_are_ascending(self):
        """Ids 5,2,9 sort to 2,5,9; names b,a keep their supplied order.

        Catches dropped fields, unsorted ids, sorted names or altered error text.
        """
        error = DsrError("dsr_input_invalid", "one sentence", [5, 2, 9], ["b", "a"])
        self.assertEqual(error.code, "dsr_input_invalid")
        self.assertEqual(error.detail, "one sentence")
        self.assertEqual(error.ids, (2, 5, 9))
        self.assertEqual(error.names, ("b", "a"))
        self.assertEqual(str(error), "dsr_input_invalid: one sentence")
        empty = DsrError("dsr_no_eligible_rows", "nothing left")
        self.assertEqual((empty.ids, empty.names), ((), ()))

    def test_unknown_code_is_a_programming_error(self):
        """Unknown local and catalog codes must raise ValueError.

        Catches allowing arbitrary refusal codes into the public error class.
        """
        for code in ("dsr_made_up", "hpo_made_up"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                DsrError(code, "a sentence")

    def test_error_survives_pickling(self):
        """A pickle round trip keeps the code, detail, sorted ids and names.

        Catches missing exception reconstruction or loss of its refusal fields.
        """
        error = DsrError("dsr_input_invalid", "missing", [3, 1], ["m"])
        # Round trip of an object built here; no untrusted bytes are loaded.
        again = pickle.loads(pickle.dumps(error))
        self.assertIsInstance(again, DsrError)
        self.assertEqual(
            (again.code, again.detail, again.ids, again.names),
            ("dsr_input_invalid", "missing", (1, 3), ("m",)),
        )
        self.assertEqual(again.document(), error.document())


class WorkedFixtureTests(DsrTestCase):
    def test_a_exact(self):
        """Fixture A: sum x=3, sum x²=3.5, V=(10.5-9)/6=1/4; den=3/2.

        N=5 rows read, three eligible, two excluded. SR=1, T=10, g3=0, g4=3.
        Catches population variance, annualisation, objective-as-Sharpe, wrong-row
        moments and putting missing returns ahead of trial status.
        """
        result = dsr(a_rows(), series="bar", selected=2)
        for name, expected in {
            "variance": 0.25,
            "den": 1.5,
            "sharpe_per_period": 1.0,
            "count": 10,
            "skew": 0.0,
            "kurt_raw": 3.0,
            "rows_read": 5,
            "rows_eligible": 3,
            "rows_excluded": 2,
            "n_trials": 5,
            "n_trials_source": "rows_read",
            "eligible": [0, 1, 2],
            "excluded": {
                "returns_not_recorded": {"count": 1, "ids": [4]},
                "status_pruned": {"count": 1, "ids": [3]},
            },
        }.items():
            self.assertEqual(result[name], expected, name)
        self.assertIs(result["selected_is_max"], False)

    def test_a_oracle_pins(self):
        """Fixture A: expected values were computed by the independent oracle
        (exact rationals for V and den, decimal series for the normal functions).

        N is the five rows read, not the three eligible rows; V=1/4 and den=3/2.
        Catches using eligible count for N, dropping q1 or changing normal weights.
        """
        self.assertPinned(dsr(a_rows(), series="bar", selected=2), "a")

    def test_notlast_selected(self):
        """Non-last selection: x=1,1.5,0.5; sum=3, squares=3.5; V=1/4, den=3/2.

        N=3; w=.4227843*.4307273+.5772157*1.1619570=.8528045.
        SR0=.5*w=.4264022; z=(1-SR0)*3/1.224744871=1.4050218;
        Phi(1.40)+.1497275*.0050218-.0000026=.9199926.
        Catches reading any of SR,T,g3,g4 from the last eligible row, dropping q1,
        annualising, or changing the kurtosis convention. Table tolerance 5e-4.
        """
        result = dsr(notlast_rows(), series="bar", selected=0)
        self.assertEqual(result["eligible"], [0, 1, 2])
        self.assertEqual(result["variance"], 0.25)
        self.assertEqual(result["den"], 1.5)
        self.assertEqual(result["sharpe_per_period"], 1.0)
        self.assertEqual(result["count"], 10)
        self.assertEqual(result["skew"], 0.0)
        self.assertEqual(result["kurt_raw"], 3.0)
        self.assertEqual(result["n_trials"], 3)
        self.assertIs(result["selected_is_max"], False)
        self.assertTabular(result, 0.4264022, 1.4050218, 0.9199926)

    def test_b_caller_and_default_counts(self):
        """Fixture B: V=(.25+.25)/1=1/2; SR=1.5; den=1+.5*2.25=17/8.

        N=2: q1=0; q2=.9004526; w=.5772157*q2=.5197554;
        SR0=.7071068*w=.3675226; z=1.1324774*2.057983=2.330619;
        Phi(2.33)+.0264265*.000619=.990113.
        N=3: w=.8528045; SR0=.6030238; z=.8969762*2.057983=1.8459618;
        Phi(1.85)-.0720649*.0040382=.9675522. Catches echoing caller N but
        computing with row count, hard-coded source labels and dropping SR².
        """
        for options, n, source, expected in (
            ({"n_trials": 2}, 2, "caller", (0.3675226, 2.330619, 0.990113)),
            ({"n_trials": 3}, 3, "caller", (0.6030238, 1.8459618, 0.9675522)),
            ({}, 2, "rows_read", (0.3675226, 2.330619, 0.990113)),
        ):
            with self.subTest(options=options):
                result = dsr(b_rows(), series="bar", selected=1, **options)
                self.assertEqual(result["variance"], 0.5)
                self.assertEqual(result["den"], 2.125)
                self.assertEqual(result["sharpe_per_period"], 1.5)
                self.assertEqual(result["count"], 10)
                self.assertEqual(result["skew"], 0.0)
                self.assertEqual(result["kurt_raw"], 3.0)
                self.assertEqual(result["n_trials"], n)
                self.assertEqual(result["n_trials_source"], source)
                self.assertIs(result["selected_is_max"], True)
                self.assertTabular(result, *expected)

    def test_inverse_normal_midpoint(self):
        """Symmetry gives Phi_inv(.5)=0 exactly; catches a platform mismatch."""
        self.assertEqual(NormalDist().inv_cdf(0.5), 0.0)


class EligibilityTests(DsrTestCase):
    def test_c_exclusion_ladder(self):
        """Fixture C: 9 read=2 eligible+7 excluded; V=1/2, den=17/8, N=9.

        Count-null/string/1 rows have status 0; short-curve status 1 precedes
        unavailable values. Catches changed reason precedence, split/dropped
        statuses, folded reasons, unsorted reason keys and lost exclusions.
        """
        result = dsr(c_rows(), series="bar", selected=8)
        self.assertEqual(result["rows_read"], 9)
        self.assertEqual(result["rows_eligible"], 2)
        self.assertEqual(result["rows_excluded"], 7)
        self.assertEqual(result["eligible"], [7, 8])
        self.assertEqual(result["n_trials"], 9)
        expected = {
            "returns_not_recorded": {"count": 1, "ids": [5]},
            "returns_status_nonzero": {"count": 2, "ids": [1, 4], "statuses": [1, 8]},
            "returns_unavailable": {"count": 3, "ids": [0, 2, 3]},
            "status_engine_error": {"count": 1, "ids": [6]},
        }
        self.assertEqual(result["excluded"], expected)
        self.assertEqual(list(result["excluded"]), list(expected))
        self.assertEqual(sum(entry["count"] for entry in expected.values()), 7)
        self.assertEqual(result["variance"], 0.5)
        self.assertEqual(result["den"], 2.125)

    def test_c_oracle_pins(self):
        """Fixture C: expected values were computed by the independent oracle
        (exact rationals for V and den, decimal series for the normal functions).

        N=9 rather than 2; moments remain V=1/2, den=17/8. Catches using only
        the eligible rows to set N or taking the wrong normal argument.
        """
        self.assertPinned(dsr(c_rows(), series="bar", selected=8), "c")

    def test_status_type_and_distinct_json_order(self):
        """Integer 0 alone is eligible; 1,1.0,true are distinct JSON texts.

        Integers sort numerically first; other compact texts sort lexically.
        Catches accepting 0.0/false, Python equality deduplication or mixed sorting.
        """
        values = [8, 1, 1.0, True, None, "bad", 0.0, False, 1]
        rows = b_rows() + [row(i + 2, status=value) for i, value in enumerate(values)]
        result = dsr(rows, series="bar", selected=1)
        self.assertEqual(result["eligible"], [0, 1])
        expected = [1, 8, "bad", 0.0, 1.0, False, None, True]
        actual = result["excluded"]["returns_status_nonzero"]["statuses"]
        self.assertEqual(json.dumps(actual), json.dumps(expected))
        self.assertEqual(
            result["excluded"]["returns_status_nonzero"]["ids"], list(range(2, 11))
        )

    def test_writable_odd_status_remains_excluded(self):
        """The JSON string x is writable but is not the integer zero.

        Id 2 is excluded as returns_status_nonzero with statuses=["x"]; ids 0,1
        remain eligible. Catches refusing a writable odd status or omitting it
        from statuses when enforcing JSON-writability.
        """
        rows = b_rows() + [row(2, status="x")]
        result = dsr(rows, series="bar", selected=1)
        self.assertEqual(result["eligible"], [0, 1])
        self.assertEqual(
            result["excluded"],
            {"returns_status_nonzero": {"count": 1, "ids": [2], "statuses": ["x"]}},
        )
        json.dumps(result, allow_nan=False)

    def test_large_writable_integer_status_remains_excluded(self):
        """Status 10**400 has 401 digits, so it is writable but not integer zero.

        Ok id 2 is excluded as returns_status_nonzero: count=1, ids=[2],
        statuses=[10**400]. Ids 0,1 remain eligible, giving 3 read, 2 eligible,
        1 excluded and default N=3 from rows_read. Their ratios .5,1.5 have
        mean 1 and V=(.25+.25)/1=.5; selected id 1 has den=1+.5*1.5²=2.125.
        Catches refusing every integer whose float() overflows in the status
        entry instead of producing the document for the remaining rows.
        """
        rows = b_rows() + [row(2, status=10**400)]
        result = dsr(rows, series="bar", selected=1)
        self.assertEqual(tuple(result), RESULT_KEYS)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["eligible"], [0, 1])
        self.assertEqual(result["rows_read"], 3)
        self.assertEqual(result["rows_eligible"], 2)
        self.assertEqual(result["rows_excluded"], 1)
        self.assertEqual(result["n_trials"], 3)
        self.assertEqual(result["n_trials_source"], "rows_read")
        self.assertEqual(result["variance"], 0.5)
        self.assertEqual(result["den"], 2.125)
        self.assertEqual(
            result["excluded"],
            {"returns_status_nonzero": {"count": 1, "ids": [2], "statuses": [10**400]}},
        )
        json.dumps(result, allow_nan=False)

    def test_unusable_values_and_all_trial_statuses(self):
        """Unusable writable values are excluded; non-finite entries refuse.

        Catches accepting bool/string/container/nonfinite/overflowing value cells,
        floating or out-of-range counts, or counting only ok trials for N.
        """
        bad = [
            None,
            True,
            "1",
            [],
            {},
            float("nan"),
            float("inf"),
            -float("inf"),
            10**400,
        ]
        rows = b_rows()
        for value in bad:
            for field in ("sharpe_per_period", "skew", "kurt_raw"):
                entry = row(len(rows))
                entry["metrics"][f"returns.bar.{field}"] = value
                if type(value) is float and not math.isfinite(value):
                    self.assertRefused(
                        self.refusal([entry]),
                        "dsr_input_invalid",
                        (entry["trial_id"],),
                        (f"returns.bar.{field}",),
                    )
                    continue
                rows.append(entry)
        for value in (None, True, 1, 10.0, "10", 2**53 + 1):
            rows.append(row(len(rows), count=value))
        unavailable = list(range(2, len(rows)))
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
        for status in statuses:
            rows.append(excluded_row(len(rows), status))
        result = dsr(rows, series="bar", selected=1)
        self.assertEqual(result["eligible"], [0, 1])
        self.assertEqual(result["n_trials"], len(rows))
        self.assertEqual(result["excluded"]["returns_unavailable"]["ids"], unavailable)
        for identifier, status in enumerate(statuses, start=2 + len(unavailable)):
            self.assertEqual(
                result["excluded"][f"status_{status}"]["ids"], [identifier]
            )

    def test_every_missing_key_is_not_recorded(self):
        """Each absent key precedes status/value checks; catches partial keys."""
        for field in FIELDS:
            with self.subTest(field=field):
                rows = b_rows() + [row(2)]
                del rows[2]["metrics"][f"returns.bar.{field}"]
                result = dsr(rows, series="bar", selected=1)
                self.assertEqual(result["eligible"], [0, 1])
                self.assertEqual(
                    result["excluded"],
                    {"returns_not_recorded": {"count": 1, "ids": [2]}},
                )

    def test_count_and_integer_conversion_boundaries(self):
        """T accepts 2 and 2^53; an integer 2^53+1 rounds to binary64 2^53.

        Catches exclusive count bounds, refusing integral numeric metrics, or
        retaining arbitrary-precision moment inputs instead of float conversion.
        """
        for count in (2, 2**53):
            with self.subTest(count=count):
                rows = [row(0, 1, count, 0, 2**53 + 1), row(1, 2)]
                result = dsr(rows, series="bar", selected=0)
                self.assertEqual(result["eligible"], [0, 1])
                self.assertEqual(result["count"], count)
                self.assertEqual(result["kurt_raw"], float(2**53))
                self.assertIs(type(result["kurt_raw"]), float)
                self.assertEqual(result["input_sha256"], digest_from_recipe(rows))


class ExactnessTests(DsrTestCase):
    def test_i_a_rational_identity(self):
        """Fixture A: (3*3.5-3²)/6=1/4; den=1+(3-1)/4=3/2.

        Catches changing divisor or denominator; this dyadic case does not
        distinguish every binary64 two-pass route from exact arithmetic.
        """
        result = dsr(a_rows(), series="bar", selected=2)
        self.assertEqual(result["variance"], 0.25)
        self.assertEqual(result["den"], 1.5)

    def test_ii_exact_variance(self):
        """Exact variance: u=2^-55, A=3602879701896397; x=A*u,2A*u,(3A-1)*u.

        Mean=(2A-1/3)*u; V=(A²-A+1/3)*u²; in 2^-59 units this is
        5764607523034233.92, rounding to 5764607523034234.
        Catches raw one-pass binary64 V (5 ulps low), not a two-pass value that
        happens to have identical bits. Raw and centred rationals agree exactly.
        """
        xs = [Fraction(0.1), Fraction(0.2), Fraction(0.3)]
        s1 = sum(xs)
        s2 = sum(x * x for x in xs)
        raw = (3 * s2 - s1 * s1) / 6
        mean = s1 / 3
        centred = sum((x - mean) ** 2 for x in xs) / 2
        expected = float(Fraction(5764607523034234, 2**59))
        self.assertEqual(raw, centred)
        self.assertEqual(float(raw), expected)
        self.assertEqual(expected, 0.009999999999999998)
        result = dsr(
            [row(i, x) for i, x in enumerate((0.1, 0.2, 0.3))], series="bar", selected=0
        )
        self.assertEqual(result["variance"], expected)

    def test_iii_den_rational_identity(self):
        """Denominator identity: SR=2,g3=.1,g4=3.3; den=3.3_b64-.2_b64.

        Nearest value is 6980579422424268*2^-51=3.0999999999999996.
        Catches changed moment convention/coefficient; the specified binary64
        reference coincides here, so this case does not separate those routes.
        """
        rational = 1 - Fraction(0.1) * 2 + (Fraction(3.3) - 1) / 4 * 4
        expected = float(Fraction(6980579422424268, 2**51))
        self.assertEqual(float(rational), expected)
        self.assertEqual(expected, 3.0999999999999996)
        result = dsr(
            [row(0, 2.0, skew=0.1, kurt=3.3), row(1)], series="bar", selected=0
        )
        self.assertEqual(result["den"], expected)

    def test_iv_integer_cells_and_digest_type(self):
        """Integer cells: ratios 1,2 have V=(.25+.25)/1=1/2; den=1+.5*4=3.

        SR0=.7071068*.5197554=.3675226; z=1.6324774*1.7320508=2.8275339.
        Q(2.83)=.0023274; moving -.0024661 adds .0000179+.00000006;
        DSR=1-.0023454=.9976546. Catches rejecting integers or digesting int
        kurtosis 3 as float 3.0; their texts and digests must differ.
        """
        rows = [row(0, 1, 10, 0, 3), row(1, 2, 10, 0, 3)]
        result = dsr(rows, series="bar", selected=1)
        self.assertEqual(result["variance"], 0.5)
        self.assertEqual(result["den"], 3.0)
        for name, expected in {
            "sharpe_per_period": 2.0,
            "count": 10,
            "skew": 0.0,
            "kurt_raw": 3.0,
        }.items():
            self.assertEqual(result[name], expected)
        self.assertIs(type(result["sharpe_per_period"]), float)
        self.assertTabular(result, 0.3675226, 2.8275339, 0.9976546)
        self.assertEqual(result["input_sha256"], digest_from_recipe(rows))
        floats = copy.deepcopy(rows)
        for entry in floats:
            entry["metrics"]["returns.bar.kurt_raw"] = 3.0
        other = dsr(floats, series="bar", selected=1)
        self.assertNotEqual(result["input_sha256"], other["input_sha256"])

    def test_iv_boolean_skew_excluded(self):
        """Boolean skew: ratios 2,1 give V=1/2, selected den=3, N=3 rows read.

        True sits in skew, where accepting it as 1.0 would admit id 0 and change
        V to 1/3. A bool in count would not distinguish that wrong route.
        Catches boolean-as-number and an eligible-count default N.
        """
        rows = [row(0, skew=True), row(1, 2.0), row(2)]
        result = dsr(rows, series="bar", selected=1)
        self.assertEqual(result["eligible"], [1, 2])
        self.assertEqual(result["rows_excluded"], 1)
        self.assertEqual(
            result["excluded"], {"returns_unavailable": {"count": 1, "ids": [0]}}
        )
        self.assertEqual(result["variance"], 0.5)
        self.assertEqual(result["den"], 3.0)
        self.assertEqual(result["n_trials"], 3)

    def test_iv_boolean_oracle_pins(self):
        """Boolean skew: expected values were computed by the independent oracle
        (exact rationals for V and den, decimal series for the normal functions).

        N=3 rows read, with one boolean row excluded. Catches eligible N
        in the formula even if counts are echoed correctly.
        """
        self.assertPinned(
            dsr([row(0, skew=True), row(1, 2.0), row(2)], series="bar", selected=1),
            "bool",
        )

    def test_v_positive_saturation(self):
        """Saturation: x=1,1,1+2^-52; raw numerator=2^-103, V=2^-103/6.

        den=1.5; SR0≈1.0937e-16; sqrt(999)/sqrt(1.5)≈25.807, Phi=1.0.
        Catches refusing/clamping a valid saturation or a cancelled-to-zero V.
        """
        rows = [
            row(0, count=1000),
            row(1, count=1000),
            row(2, math.nextafter(1.0, 2.0), 1000),
        ]
        result = dsr(rows, series="bar", selected=0)
        self.assertEqual(result["variance"], float(Fraction(1, 6 * 2**103)))
        self.assertEqual(result["den"], 1.5)
        self.assertGreater(result["z"], 0)
        self.assertEqual(result["dsr"], 1.0)


class ResultShapeTests(DsrTestCase):
    def test_exact_shape_and_types(self):
        """Result shape: ordered keys, no null fields, integer counts and bool maximum.

        Catches renamed/reordered/omitted keys, changed labels or stringified
        numeric fields. Fixture B has no exclusions; V=1/2 and den=17/8.
        """
        result = dsr(b_rows(), series="bar", selected=1)
        self.assertEqual(tuple(result), RESULT_KEYS)
        self.assertTrue(all(value is not None for value in result.values()))
        for name in (
            "selected",
            "count",
            "n_trials",
            "rows_read",
            "rows_eligible",
            "rows_excluded",
        ):
            self.assertIs(type(result[name]), int, name)
        for name in (
            "sharpe_per_period",
            "skew",
            "kurt_raw",
            "variance",
            "sr0",
            "den",
            "z",
            "dsr",
        ):
            self.assertIs(type(result[name]), float, name)
            self.assertTrue(math.isfinite(result[name]), name)
        self.assertIs(type(result["selected_is_max"]), bool)
        self.assertEqual(result["contract"], "pineforge-hpo-dsr/v1")
        self.assertEqual(result["algorithm"], "deflated_sharpe_exact_v1")
        self.assertEqual(result["variance_basis"], "sample_n_minus_1")
        self.assertEqual(result["interpretation"], FIXED_TEXT)
        self.assertEqual(result["excluded"], {})
        json.dumps(result, allow_nan=False)
        for rows, selected in ((a_rows(), 2), (c_rows(), 8)):
            excluded = dsr(rows, series="bar", selected=selected)["excluded"]
            self.assertEqual(list(excluded), sorted(excluded))
            for entry in excluded.values():
                self.assertEqual(list(entry)[:2], ["count", "ids"])

    def test_max_over_eligible_only(self):
        """Eligible maximum: 1,.5 mean=.75, V=(.0625+.0625)/1=1/8.

        den=1.5; N=3 rows read. Pruned id 2 carries larger SR=1.5.
        Catches computing selected_is_max over all rows instead of eligible rows.
        """
        rows = [row(0), row(1, 0.5), row(2, 1.5)]
        rows[2].update(status="pruned", feasible=False, objective=None)
        result = dsr(rows, series="bar", selected=0)
        self.assertIs(result["selected_is_max"], True)
        self.assertEqual(result["eligible"], [0, 1])
        self.assertEqual(result["variance"], 0.125)
        self.assertEqual(result["den"], 1.5)
        self.assertEqual(result["n_trials"], 3)

    def test_max_oracle_pins(self):
        """Eligible maximum: expected values were computed by the independent oracle
        (exact rationals for V and den, decimal series for the normal functions).

        The pruned higher-SR row counts toward N=3 but not V=1/8. Catches default
        N=2 even if selected_is_max is correctly computed over eligible rows.
        """
        rows = [row(0), row(1, 0.5), row(2, 1.5)]
        rows[2].update(status="pruned", feasible=False, objective=None)
        self.assertPinned(dsr(rows, series="bar", selected=0), "max")

    def test_max_has_no_tolerance(self):
        """Exact maximum: SR=1.5-2^-52 differs from max 1.5; V=2^-105.

        den rational=17/8-3*2^-53+2^-105; selected_is_max must be False.
        Catches tolerance-based equality even though the difference is one ulp.
        """
        rows = [row(0, 1.5), row(1, math.nextafter(1.5, 1.0))]
        result = dsr(rows, series="bar", selected=1)
        self.assertIs(result["selected_is_max"], False)
        self.assertEqual(result["variance"], 2.0**-105)

    def test_tied_maxima_all_count(self):
        """Equal maxima 1.5,1.5 both exceed .5; V>0 so both calls are admitted.

        Catches selecting only the first id among tied maxima.
        """
        rows = [row(0, 1.5), row(1, 1.5), row(2, 0.5)]
        for selected in (0, 1):
            self.assertIs(
                dsr(rows, series="bar", selected=selected)["selected_is_max"], True
            )

    def test_digest_total_cell_recipe(self):
        """Digest cells cover excluded rows and distinguish their recorded types.

        Absent/null collapse; true/1/1.0 retain separate cells. Non-finite floats
        and unwritable objects refuse even on excluded rows. Catches omission,
        float coercion, missing tags or silent admission of unwritable entries.
        """
        values = [
            None,
            True,
            1,
            1.0,
            float("nan"),
            float("inf"),
            -float("inf"),
            "text",
            [],
            {},
            [float("nan")],
            object(),
        ]
        digests = []
        for index, value in enumerate(values):
            rows = b_rows() + [excluded_row(2)]
            rows[2]["metrics"] = {"returns.bar.skew": value}
            if index in (4, 5, 6, 10, 11):
                self.assertRefused(
                    self.refusal(rows, selected=1),
                    "dsr_input_invalid",
                    (2,),
                    ("returns.bar.skew",),
                )
                continue
            result = dsr(rows, series="bar", selected=1)
            self.assertEqual(result["input_sha256"], digest_from_recipe(rows))
            digests.append(result["input_sha256"])
        # Seven writable inputs have distinct cells; five inputs refuse.
        self.assertEqual(len(set(digests)), len(values) - 5)
        absent = dsr(b_rows() + [excluded_row(2)], series="bar", selected=1)
        self.assertEqual(absent["input_sha256"], digests[0])
        no_excluded = dsr(b_rows(), series="bar", selected=1)
        self.assertNotEqual(no_excluded["input_sha256"], digests[0])

    def test_digest_non_ascii_string_cell(self):
        r"""An excluded string é has inner JSON text "\u00e9".

        The outer cell escapes the quotes and backslash again. The handwritten
        preimages below include both eligible rows and the pruned row, in sorted
        key order. Catches omitting ensure_ascii or changing the nested text cell.
        """
        rows = b_rows() + [excluded_row(2)]
        rows[2]["metrics"] = {"returns.bar.skew": "é"}
        first = (
            '[0,"ok",{"returns.bar.count":10,"returns.bar.kurt_raw":3.0,'
            '"returns.bar.sharpe_per_period":0.5,"returns.bar.skew":0.0,'
            '"returns.bar.status":0}]'
        )
        second = (
            '[1,"ok",{"returns.bar.count":10,"returns.bar.kurt_raw":3.0,'
            '"returns.bar.sharpe_per_period":1.5,"returns.bar.skew":0.0,'
            '"returns.bar.status":0}]'
        )
        pruned = (
            '[2,"pruned",{"returns.bar.count":null,"returns.bar.kurt_raw":null,'
            '"returns.bar.sharpe_per_period":null,"returns.bar.skew":'
            r'["other","\"\\u00e9\""],"returns.bar.status":null}]'
        )
        preimages = (first, second, pruned)
        parts = [hashlib.sha256(text.encode("ascii")).digest() for text in preimages]
        expected = hashlib.sha256(b"".join(sorted(parts))).hexdigest()
        result = dsr(rows, series="bar", selected=1)
        self.assertEqual(result["input_sha256"], expected)

    def test_digest_oversized_parsed_integer(self):
        """A parsed pruned row's 10**5000 exceeds the 4300-digit text limit.

        Its skew cell is ["other", "<unwritable>"]; other missing cells are null.
        The handwritten preimages pin that sentinel without decimal conversion.
        Catches an uncaught ValueError, refusing the integer or omitting the row.
        """
        rows = b_rows() + [excluded_row(2)]
        rows[2]["metrics"] = {"returns.bar.skew": 10**5000}
        source = {"trials": rows, "trials_out": "all", "trials_completed": 3}
        first = (
            '[0,"ok",{"returns.bar.count":10,"returns.bar.kurt_raw":3.0,'
            '"returns.bar.sharpe_per_period":0.5,"returns.bar.skew":0.0,'
            '"returns.bar.status":0}]'
        )
        second = (
            '[1,"ok",{"returns.bar.count":10,"returns.bar.kurt_raw":3.0,'
            '"returns.bar.sharpe_per_period":1.5,"returns.bar.skew":0.0,'
            '"returns.bar.status":0}]'
        )
        pruned = (
            '[2,"pruned",{"returns.bar.count":null,"returns.bar.kurt_raw":null,'
            '"returns.bar.sharpe_per_period":null,"returns.bar.skew":'
            '["other","<unwritable>"],"returns.bar.status":null}]'
        )
        preimages = (first, second, pruned)
        parts = [hashlib.sha256(text.encode("ascii")).digest() for text in preimages]
        expected = hashlib.sha256(b"".join(sorted(parts))).hexdigest()
        result = dsr(source, series="bar", selected=1)
        self.assertEqual(result["input_sha256"], expected)
        self.assertEqual(
            result["excluded"], {"status_pruned": {"count": 1, "ids": [2]}}
        )
        json.dumps(result, allow_nan=False)


class DeterminismTests(DsrTestCase):
    def test_a_order_jsonl_and_repeated_calls(self):
        """Determinism: V=1/4, den=3/2; rows and reason lists are ascending.

        Catches dependence on file order, blank-line layout or mutable call state.
        This is a determinism check, not proof against every binary64 summation.
        """
        rows = a_rows()
        baseline = dsr(rows, series="bar", selected=2)
        self.assertEqual(baseline, dsr(list(reversed(rows)), series="bar", selected=2))
        self.assertEqual(baseline, dsr(rows, series="bar", selected=2))
        with TemporaryDirectory() as directory:
            for index, separator in enumerate(("\n", "\n\n")):
                path = Path(directory) / f"rows{index}.jsonl"
                path.write_text(
                    separator.join(json.dumps(entry) for entry in reversed(rows))
                    + "\n",
                    encoding="utf-8",
                )
                self.assertEqual(baseline, dsr(path, series="bar", selected=2))
        self.assertEqual(rows, a_rows())

    def test_selection_and_trial_count_not_in_digest(self):
        """Digest scope: all calls read the same five rows; selection and N are absent.

        Catches digesting selected id or n_trials rather than just recorded rows.
        """
        first = dsr(a_rows(), series="bar", selected=2)
        second = dsr(a_rows(), series="bar", selected=0, n_trials=6)
        self.assertEqual(first["input_sha256"], second["input_sha256"])
        self.assertEqual(first["input_sha256"], digest_from_recipe(a_rows()))
        self.assertNotEqual(first["selected"], second["selected"])
        self.assertNotEqual(first["n_trials"], second["n_trials"])

    def test_monthly_series_and_digest(self):
        """Monthly series: .25,.75 -> V=1/8; SR=.75,T=12,g3=-.1,g4=3.4.

        den=1+.075+.3375=1.4125 (binary64 inputs rounded once);
        SR0=.3535534*.5197554=.1837613; sqrt(11)/sqrt(den)=2.7906292;
        z=.5662387*2.7906292=1.580162; Phi(1.58)+.114506*.000162=.942965.
        Catches reading bar cells for monthly, wrong selected statistics or a
        digest that omits series key names. Table tolerance 5e-4 for Phi values.
        """
        rows = d_rows()
        bar = dsr(rows, series="bar", selected=1)
        monthly = dsr(rows, series="monthly", selected=1)
        self.assertNotEqual(bar["input_sha256"], monthly["input_sha256"])
        self.assertEqual(monthly["input_sha256"], digest_from_recipe(rows, "monthly"))
        self.assertEqual(monthly["variance"], 0.125)
        self.assertEqual(monthly["sharpe_per_period"], 0.75)
        self.assertEqual(monthly["count"], 12)
        self.assertEqual(monthly["skew"], -0.1)
        self.assertEqual(monthly["kurt_raw"], 3.4)
        self.assertEqual(monthly["den"], 1.4125)
        self.assertTabular(monthly, 0.1837613, 1.580162, 0.942965)

    def test_order_fixture_and_negative_saturation(self):
        """Order fixture: x=1,1e16,-1e16; mean=1/3, V=1e32+1/3 -> 1e32.

        den=1.5; SR0≈8.528045e15; z≈-2.088936e16, Phi(z)=0.0.
        Catches rejecting/clamping a finite-z zero saturation or changing the
        document on reversal. This does not prove file-order exact accumulation.
        """
        rows = [row(0), row(1, 1e16), row(2, -1e16)]
        result = dsr(rows, series="bar", selected=0)
        self.assertEqual(result["variance"], 1e32)
        self.assertEqual(result["den"], 1.5)
        self.assertLess(result["z"], 0)
        self.assertEqual(result["dsr"], 0.0)
        self.assertEqual(result, dsr(list(reversed(rows)), series="bar", selected=0))

    def test_complete_result_and_ancestors(self):
        """Complete result combines ancestors and trials; all five rows still count.

        Catches omitting warm-start ancestors or requiring a recorded space.
        """
        rows = a_rows()
        result = {
            "warm_start_trials": [rows[4]],
            "trials": rows[:4],
            "trials_out": "all",
            "trials_completed": 4,
            "warm_start": {"trials": 1},
        }
        self.assertEqual(
            dsr(result, series="bar", selected=2), dsr(rows, series="bar", selected=2)
        )


class ParameterRefusalTests(DsrTestCase):
    def test_parameter_table(self):
        """Parameter refusals: reject types/ranges with the offending name.

        Catches bool-as-int, floating counts/ids, trailing whitespace, removed
        N cap and an invalid max_work admitted as no bound.
        """
        cases = {
            "series": ("bar ", None, "BAR", 0, []),
            "selected": (1.0, True, None, -1, 2**64 - 1),
            "n_trials": (1, 2.0, True, 2**50 + 1),
            "max_work": (0, True, 1.0, -1),
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    options = {"series": "bar", "selected": 0, name: value}
                    error = self.refusal(b_rows(), **options)
                    self.assertRefused(error, "dsr_parameter_invalid", (), (name,))

    def test_required_keyword_only_arguments(self):
        """Missing arguments are Python TypeError; catches added/inferred defaults."""
        for arguments in ({}, {"series": "bar"}, {"selected": 0}):
            with self.subTest(arguments=arguments), self.assertRaises(TypeError):
                dsr(b_rows(), **arguments)
        with self.assertRaises(TypeError):
            dsr(b_rows(), "bar", 0)

    def test_parameter_order_precedes_source(self):
        """series, selected, n_trials, max_work precede unreadable source.

        Catches reordered checks and later parameters hiding an earlier defect.
        """
        options = {"series": "bar ", "selected": True, "n_trials": 1, "max_work": 0}
        for name, replacement in (
            ("series", "bar"),
            ("selected", 0),
            ("n_trials", 2),
            ("max_work", None),
        ):
            error = self.refusal(None, **options)
            self.assertRefused(error, "dsr_parameter_invalid", (), (name,))
            options[name] = replacement
        self.assertRefused(self.refusal(None, **options), "dsr_input_invalid")

    def test_trial_count_cap_is_admitted(self):
        """N=2^50 keeps p2 below one; catches off-by-one cap or inverse-cdf crash."""
        result = dsr(b_rows(), series="bar", selected=1, n_trials=2**50)
        self.assertEqual(result["n_trials"], 2**50)
        self.assertEqual(result["n_trials_source"], "caller")
        self.assertTrue(
            all(math.isfinite(result[name]) for name in ("sr0", "z", "dsr"))
        )


class SourceRefusalTests(DsrTestCase):
    def test_pruned_skew_with_unsortable_object_keys_refuses(self):
        """Pruned id 2 has skew {1: "a", "b": 2}, whose keys cannot be sorted.

        The key-sorted writer cannot compare int 1 with str b. The cell check
        precedes exclusions, so dsr_input_invalid has ids=(2,) and
        names=(returns.bar.skew,). Catches a writer without sorted keys that
        writes the cell and goes on to produce a document.
        """
        rows = b_rows() + [excluded_row(2)]
        rows[2]["metrics"] = {"returns.bar.skew": {1: "a", "b": 2}}
        self.assertRefused(
            self.refusal(rows, selected=1),
            "dsr_input_invalid",
            (2,),
            ("returns.bar.skew",),
        )

    def test_oversized_series_status_refuses(self):
        """An ok id 2 has status 10**5000, beyond the 4300-digit text limit.

        Only returns.bar.status is unwritable, so the refusal is dsr_input_invalid
        with ids=(2,) and names=(returns.bar.status,). Catches exempting every
        integer from the cell check and leaving an unwritable value in statuses.
        """
        rows = b_rows() + [row(2, status=10**5000)]
        self.assertRefused(
            self.refusal(rows, selected=1),
            "dsr_input_invalid",
            (2,),
            ("returns.bar.status",),
        )

    def test_multiple_unwritable_entries_collect_ids_and_sorted_keys(self):
        """Id 9 has NaN Sharpe; id 2 has infinite kurtosis, both unwritable.

        Their id union sorts to (2,9). Alphabetically kurt_raw precedes
        sharpe_per_period, so names=(returns.bar.kurt_raw,
        returns.bar.sharpe_per_period), opposite to the statistic field order.
        Catches reporting only the first key or its ids, or unsorted entry keys.
        """
        rows = b_rows() + [row(9, float("nan")), row(2, kurt=float("inf"))]
        self.assertRefused(
            self.refusal(rows, selected=1),
            "dsr_input_invalid",
            (2, 9),
            ("returns.bar.kurt_raw", "returns.bar.sharpe_per_period"),
        )

    def test_single_pruned_unwritable_status_precedes_no_eligible_rows(self):
        """A sole pruned id 0 has a NaN series status, which is not JSON-writable.

        The cell check precedes exclusions: dsr_input_invalid with ids=(0,) and
        names=(returns.bar.status,), even though no row can compete. Catches
        reporting dsr_no_eligible_rows before checking excluded rows' entries.
        """
        pruned = excluded_row(0)
        pruned["metrics"] = {"returns.bar.status": float("nan")}
        self.assertRefused(
            self.refusal([pruned]),
            "dsr_input_invalid",
            (0,),
            ("returns.bar.status",),
        )

    def test_pruned_unwritable_series_status_refuses(self):
        """A pruned id 2 with series status NaN refuses before exclusions.

        The parsed source is reader-valid, but NaN cannot be written as JSON.
        Expected code is dsr_input_invalid, ids=(2,), names=(returns.bar.status,).
        Catches skipping cells of pruned rows or silently using fallback text.
        """
        rows = b_rows() + [excluded_row(2)]
        rows[2]["metrics"] = {"returns.bar.status": float("nan")}
        source = {"trials": rows, "trials_out": "all", "trials_completed": 3}
        self.assertRefused(
            self.refusal(source, selected=1),
            "dsr_input_invalid",
            (2,),
            ("returns.bar.status",),
        )

    def test_unwritable_entries_name_all_ids_and_each_key(self):
        """Each requested key is checked on all rows before the eligibility ladder.

        A pruned id 9 and incomplete ok id 2 share an unwritable entry, so ids
        must be (2,9) and names must hold that key. Catches stopping at one id,
        skipping missing-key rows or checking only the series status entry.
        """
        for field in FIELDS:
            for value in (float("inf"), {1}, lambda: None):
                with self.subTest(field=field, value=value):
                    name = f"returns.bar.{field}"
                    pruned = excluded_row(9)
                    pruned["metrics"] = {name: value}
                    incomplete = {
                        "trial_id": 2,
                        "status": "ok",
                        "feasible": True,
                        "objective": 0.0,
                        "metrics": {name: value},
                    }
                    rows = [pruned, incomplete]
                    self.assertRefused(
                        self.refusal(rows), "dsr_input_invalid", (2, 9), (name,)
                    )

    def test_input_invalid_table(self):
        """Input refusals: metrics null, repeated id, absent objective and ok null.

        Catches bypassing the shared reader or silently repairing malformed rows.
        """
        metrics_null = row(0)
        metrics_null["metrics"] = None
        missing_objective = excluded_row(0)
        del missing_objective["objective"]
        null_objective = row(0)
        null_objective["objective"] = None
        for rows in (
            [metrics_null],
            [row(0), row(0)],
            [missing_objective],
            [null_objective],
        ):
            with self.subTest(rows=rows):
                self.assertRefused(self.refusal(rows), "dsr_input_invalid")
        # pruned + null objective is reader-valid and excluded, not input-invalid.
        result = dsr(a_rows(), series="bar", selected=2)
        self.assertEqual(result["excluded"]["status_pruned"]["ids"], [3])

    def test_strict_file_reading(self):
        """Strict file reading: NaN, duplicate keys, Infinity and overflow refuse.

        Catches a permissive JSON parser; parsed nonfinite metrics also refuse,
        so this exercises the distinct file route through the shared reader.
        """
        template = json.dumps(row(0))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rows.jsonl"
            for token in ("NaN", "Infinity", "-Infinity", "1e999"):
                with self.subTest(token=token):
                    path.write_text(
                        template.replace(
                            '"returns.bar.skew": 0.0', f'"returns.bar.skew": {token}'
                        ),
                        encoding="utf-8",
                    )
                    self.assertRefused(self.refusal(path), "dsr_input_invalid")
            path.write_text(
                template.replace('"trial_id": 0', '"trial_id": 0, "trial_id": 1'),
                encoding="utf-8",
            )
            self.assertRefused(self.refusal(path), "dsr_input_invalid")

    def test_incomplete_result_table(self):
        """Summary results keep their own code, not input-invalid.

        Catches accepting best-k rows or count mismatches as a complete study.
        """
        for result in (
            {"trials": a_rows(), "trials_out": "best"},
            {"trials": b_rows(), "trials_completed": 1},
            {"trials": b_rows(), "warm_start": {"trials": 1}},
        ):
            with self.subTest(result=result):
                self.assertRefused(self.refusal(result), "dsr_rows_incomplete")

    def test_context_ids_are_unwrapped(self):
        """Context mismatch: different hashes name ids 0,1, not (source,id) pairs.

        Catches omitting context checks or keeping the reader's pair identifiers.
        """
        rows = b_rows()
        rows[0]["space_hash"] = "a"
        rows[1]["space_hash"] = "b"
        self.assertRefused(
            self.refusal(rows), "dsr_context_mismatch", (0, 1), ("space_hash",)
        )
        rows = b_rows()
        rows[0]["backtest"] = {"input_bars_processed": 10}
        rows[1]["backtest"] = {"input_bars_processed": 11}
        self.assertRefused(
            self.refusal(rows),
            "dsr_context_mismatch",
            (0, 1),
            ("input_bars_processed",),
        )

    def test_selection_table(self):
        """Selection refusals: unknown 9, pruned 3, no returns 4 in a five-row source.

        Catches invented selection, generic selection codes or omitted reason names.
        """
        self.assertRefused(
            self.refusal(a_rows(), selected=9), "dsr_selected_unknown", (9,), ()
        )
        for selected, reason in ((3, "status_pruned"), (4, "returns_not_recorded")):
            with self.subTest(selected=selected):
                self.assertRefused(
                    self.refusal(a_rows(), selected=selected),
                    "dsr_selected_not_eligible",
                    (selected,),
                    (reason,),
                )

    def test_count_checks_precede_selection(self):
        """Eligible count: empty/pruned -> no rows; B with id 0 pruned -> one id 1.

        Catches selection-before-count; unknown selected 9 must not hide the
        too-few refusal, even if caller N is larger than one.
        """
        for source in ([], [excluded_row(0)]):
            self.assertRefused(self.refusal(source), "dsr_no_eligible_rows", (), ())
        rows = [excluded_row(0), row(1, 1.5)]
        self.assertRefused(
            self.refusal(rows, selected=1), "dsr_too_few_eligible_rows", (1,), ()
        )
        self.assertRefused(
            self.refusal(rows, selected=9, n_trials=2),
            "dsr_too_few_eligible_rows",
            (1,),
            (),
        )

    def test_trials_inconsistent_counts_rows_read(self):
        """Trial count: A has five rows read; caller 2,3,4 all fall below that floor.

        Catches an eligible-count floor, and resource/statistic checks hiding
        inconsistency. Unknown/excluded selection still precedes this check.
        """
        for n in (2, 3, 4):
            self.assertRefused(
                self.refusal(a_rows(), selected=2, n_trials=n, max_work=1),
                "dsr_trials_inconsistent",
                (),
                ("n_trials",),
            )
        self.assertRefused(
            self.refusal(a_rows(), selected=9, n_trials=2, max_work=1),
            "dsr_selected_unknown",
            (9,),
            (),
        )
        self.assertRefused(
            self.refusal(a_rows(), selected=3, n_trials=2, max_work=1),
            "dsr_selected_not_eligible",
            (3,),
            ("status_pruned",),
        )


class StatisticRefusalTests(DsrTestCase):
    def test_zero_variance_table(self):
        """Zero variance: equal ratios 1,1 -> exact V=0; catches fabricated SR0=0.

        Variance refusal precedes invalid denominator on the selected row.
        """
        rows = [row(0, skew=5.0), row(1)]
        self.assertRefused(self.refusal(rows), "dsr_zero_sharpe_variance", (0, 1), ())

    def test_positive_variance_rounding_to_zero(self):
        """Variance underflow: x=0,2^-537 -> V=(difference²)/2=2^-1075.

        Positive rational is a half-subnormal tie rounding to even zero. Catches
        guarding only exact V and publishing SR0=0 after underflow.
        """
        rows = [row(0, 0.0), row(1, 2.0**-537)]
        self.assertRefused(self.refusal(rows), "dsr_zero_sharpe_variance", (0, 1), ())

    def test_nonpositive_denominator_table(self):
        """Nonpositive denominator: SR=1,g3=5,g4=3 -> den=1-5+.5=-3.5.

        SR=1,g3=1,g4=1 -> exact zero. Catches a missing rational positivity
        guard, sqrt exception or substitution of a favourable statistic.
        """
        for skew, kurt in ((5.0, 3.0), (1.0, 1.0)):
            rows = [row(0, skew=skew, kurt=kurt), row(1, 0.5)]
            self.assertRefused(self.refusal(rows), "dsr_statistic_undefined", (0,), ())

    def test_positive_denominator_rounding_to_zero(self):
        """Denominator underflow: SR=2^-996,g3=2^996,g4=3.

        1-g3*SR cancels exactly; den=.5*2^-1992=2^-1993>0 rounds to zero.
        Catches a rational-only guard that would divide by sqrt(0).
        """
        rows = [row(0, 2.0**-996, skew=2.0**996), row(1, 1.0)]
        self.assertRefused(self.refusal(rows), "dsr_statistic_undefined", (0,), ())

    def test_nonfinite_z_refuses_after_finite_variance_and_denominator(self):
        """SRs 2^-500 and 2^500 give V=2^999-1+2^-1001, rounding to 2^999.

        Selected id 0 has den=1-1+(2^-52/4)*2^-1000=2^-1054>0,
        sqrt(den)=2^-527 and sqrt(T-1)=3. With N=2, w is about .5197554,
        so SR0 is about .5197554*2^499.5, finite. The final division gives
        z about -1.56*2^1026.5, beyond binary64: dsr_statistic_undefined, ids=(0,).
        Catches a missing z-finiteness guard that would publish a saturated DSR.
        """
        rows = [row(0, 2**-500, 10, 2**500, 1 + 2**-52), row(1, 2**500, 10, 0.0, 3.0)]
        error = self.refusal(rows)
        self.assertRefused(error, "dsr_statistic_undefined", (0,), ())

    def test_T_private_guard(self):
        """T guard: public eligibility excludes T=1; call the private guard.

        Catches a deleted T>=2 assertion; the guard takes T only, with no id.
        """
        with self.assertRaises(DsrError) as caught:
            module._require_T(1)
        self.assertRefused(caught.exception, "dsr_statistic_undefined", (), ())
        module._require_T(2)

    def test_nonfinite_intermediates_refuse(self):
        """Intermediate overflow: finite inputs can overflow exact V or exact den.

        Ratios +/-1e308 imply V=2e616; SR=2,skew=-1e308 implies den>2e308.
        Catches uncaught Fraction overflow or returning an infinite statistic.
        """
        for rows in (
            [row(0, 1e308), row(1, -1e308)],
            [row(0, 2.0, skew=-1e308), row(1)],
        ):
            self.assertRefused(self.refusal(rows), "dsr_statistic_undefined", (0,), ())


class ResourceTests(DsrTestCase):
    def test_resource_table_and_boundary(self):
        """Resource boundary: A's work=5 read+3 eligible=8; 1/7 refuse, 8 admits.

        Catches >= instead of >, counting eligible rows alone or built-in caps.
        """
        for maximum in (1, 7):
            self.assertRefused(
                self.refusal(a_rows(), selected=2, max_work=maximum),
                "dsr_resource_exceeded",
                (),
                (),
            )
        admitted = dsr(a_rows(), series="bar", selected=2, max_work=8)
        self.assertEqual(admitted, dsr(a_rows(), series="bar", selected=2))

    def test_resource_before_exact_statistic(self):
        """Work=2+2=4 precedes equal-SR V=0 or den<=0.

        Catches computing/refusing the statistic before checking caller admission.
        """
        rows = [row(0, skew=5.0), row(1)]
        self.assertRefused(
            self.refusal(rows, max_work=3), "dsr_resource_exceeded", (), ()
        )
        self.assertRefused(
            self.refusal(rows, max_work=4), "dsr_zero_sharpe_variance", (0, 1), ()
        )


if __name__ == "__main__":
    unittest.main()
