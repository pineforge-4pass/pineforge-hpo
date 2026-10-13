#!/usr/bin/env python3
"""Independent exact-arithmetic checker for the deflated Sharpe ratio.

Phase 1 uses embedded worked examples, centred Fraction variance, exact
Fraction denominator, decimal normal series and inverse bisection. It prints
computed pins for fixtures with excluded rows. Phase 2 imports the module only
after phase 1 and compares public results with those frozen answers.
No expected value comes from the module. The two normal arguments deliberately
replay the specified binary64 rounding sequence; this is labelled reproduction.

Python 3.11+, standard library only. Writes no files. Exit codes: 0 agreement
including the module, 1 disagreement or harness failure, 2 usage, 3 phase 1 only.
v1 analyses finished studies; native in-loop walk-forward comes later.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from fractions import Fraction
from functools import lru_cache
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import random
import sys

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
G = Fraction(0.5772156649015329)
E = Fraction(2.718281828459045)
CELLS = ("sharpe_per_period", "count", "skew", "kurt_raw", "status")
STATUSES = {
    "ok",
    "constraint_violation",
    "objective_error",
    "constraint_error",
    "engine_error",
    "trial_error",
    "trial_timeout",
    "pruned",
    "partial",
}
COUNTERS = (
    "input_bars_processed",
    "script_bars_processed",
    "input_tf_seconds",
    "script_tf_seconds",
)
EXACT_FIELDS = (
    "selected",
    "sharpe_per_period",
    "count",
    "skew",
    "kurt_raw",
    "n_trials",
    "n_trials_source",
    "variance_basis",
    "variance",
    "den",
    "selected_is_max",
    "rows_read",
    "rows_eligible",
    "rows_excluded",
    "eligible",
    "excluded",
)
APPROX_FIELDS = ("sr0", "z", "dsr")
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
NOT_COMPARED = (
    "values of input_sha256 except the oversized-integer digest case; "
    "interpretation; contract; status; algorithm; series",
    "the module's normal functions directly (only their propagated results)",
    "an exact arithmetic route versus every possible two-pass variance or "
    "binary64 denominator route; equality checks the definition and rounding",
    "p1 and p2 in the module (not result fields); the replay is checked internally",
    "the platform square root; it supplies the same rounded roots on both sides",
    "saturation versus a clamp that produces the same reported number",
    "the unreachable private T < 2 guard; refusal detail text "
    "and refusal document shape",
    "all malformed-source variants; only the explicit source cases are exercised",
    "file splitting and repeated-call invariance; random admitted rows are shuffled",
    "full statistic at N=2**50; large T; integer-cell rounding above 2**53",
    "backtest-counter context mismatches and unrepresented trial-status variants",
)


def same(left, right) -> bool:
    """Type-strict recursive equality, including binary64 bits and object order."""
    if type(left) is not type(right):
        return False
    if isinstance(left, float):
        return math.isfinite(left) and left.hex() == right.hex()
    if isinstance(left, dict):
        return list(left) == list(right) and all(
            same(left[key], right[key]) for key in left
        )
    if isinstance(left, (tuple, list)):
        return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
    return left == right


def close(actual, expected, tolerance) -> bool:
    """Absolute decimal comparison; a non-finite or wrongly typed result fails."""
    if type(actual) is float:
        if not math.isfinite(actual):
            return False
        actual = Decimal.from_float(actual)
    elif type(actual) is not Decimal:
        return False
    with localcontext() as context:
        context.prec = 80
        return (
            actual.is_finite()
            and expected.is_finite()
            and tolerance.is_finite()
            and tolerance >= 0
            and abs(actual - expected) <= tolerance
        )


class Ledger:
    def __init__(self, echo=True):
        self.echo = echo
        self.name = ""
        self.made = 0
        self.counts = dict.fromkeys(("fixture", "internal", "control", "module"), 0)
        self.failures = []

    def begin(self, name):
        self.name = name
        self.made = 0
        if self.echo:
            print(f"\n== {name}")

    def fail(self, message):
        self.failures.append(message)
        if self.echo:
            print(f"FAIL {message}")

    def check(self, kind, label, verdict, evidence=""):
        self.made += 1
        self.counts[kind] += 1
        if verdict is not True:
            self.failures.append(f"{self.name}: {label}")
        if self.echo:
            word = "AGREE" if verdict is True else "DISAGREE"
            print(f"{word} {kind}: {label}" + (f" | {evidence}" if evidence else ""))
        return verdict

    def compare(self, kind, label, expected, actual, tolerance=None):
        verdict = (
            same(expected, actual)
            if tolerance is None
            else close(actual, expected, tolerance)
        )
        evidence = f"oracle={expected!r} | {kind}={actual!r}"
        if tolerance is not None:
            evidence += f" | absolute tolerance={tolerance}"
        return self.check(kind, label, verdict, evidence)

    def end(self, expected):
        if self.echo:
            print(f"-- {self.name}: {self.made} comparisons made, {expected} expected")
        if self.made != expected:
            self.fail(f"{self.name}: {self.made} comparisons, expected {expected}")


def harness_self_test():
    """Primitive is-based verdicts prevent the comparator from vouching for itself."""
    equal = [(1, 1), (True, True), ([1, 2.0], [1, 2.0]), ({"x": 0.5}, {"x": 0.5})]
    different = [
        (1, 2),
        (True, 1),
        (1, 1.0),
        (0.0, -0.0),
        (0.5, math.nextafter(0.5, 1.0)),
        ([1, 2], [1, 3]),
        ({"x": 1}, {"x": 2}),
        ({"x": 1, "y": 2}, {"y": 2, "x": 1}),
        (float("nan"), float("nan")),
    ]
    checks = [same(a, b) is True for a, b in equal]
    checks += [same(a, b) is False for a, b in different]
    checks += [
        close(0.5, Decimal("0.5"), Decimal("1e-20")) is True,
        close(0.6, Decimal("0.5"), Decimal("1e-20")) is False,
        close(float("nan"), Decimal("0.5"), Decimal(1)) is False,
        close(True, Decimal(1), Decimal(0)) is False,
        close(0.5, Decimal("0.5"), Decimal(-1)) is False,
    ]
    if not all(checks):
        raise RuntimeError("harness self-test: comparison helper failed")
    probe = Ledger(echo=False)
    probe.begin("probe")
    original = {"variance": 0.25}
    mutated = copy.deepcopy(original)
    mutated["variance"] = 0.5
    probe.compare("control", "equal", original, original)
    probe.compare("control", "planted difference", original, mutated)
    probe.end(2)
    if (
        probe.failures != ["probe: planted difference"]
        or probe.made != 2
        or probe.counts["control"] != 2
        or original != {"variance": 0.25}
    ):
        raise RuntimeError("harness self-test: ledger missed a one-sided mutation")
    missing = Ledger(echo=False)
    missing.begin("missing")
    missing.end(1)
    if len(missing.failures) != 1:
        raise RuntimeError("harness self-test: missing comparison accepted")
    print("harness self-test passed")


def decimal_fraction(value):
    return Decimal(value.numerator) / Decimal(value.denominator)


@lru_cache(maxsize=1)
def normal_scale():
    """1/sqrt(2*pi); pi comes from Machin's rational arctangent series."""
    with localcontext() as context:
        context.prec = 90

        def arctangent(reciprocal):
            x = Decimal(1) / reciprocal
            power = x
            total = x
            for index in range(1, 300):
                power *= -(x * x)
                term = power / (2 * index + 1)
                total += term
                if abs(term) < Decimal("1e-85"):
                    return total
            raise ArithmeticError("arctangent series did not converge")

        pi = 16 * arctangent(5) - 4 * arctangent(239)
        return +(1 / (2 * pi).sqrt())


def density(z):
    with localcontext() as context:
        context.prec = 80
        if abs(z) > 40:
            # A density below exp(-800) cannot affect the additive 1e-15 bound.
            return Decimal(0)
        return normal_scale() * (-(z * z) / 2).exp()


def complementary_tail(x):
    """Q(x) by a convergent series for the tail integral, without subtraction.

    Integrate exp(-(a+t)^2/2) on quarter-unit panels. For u=t/h, the
    coefficients of exp(-a*h*u-h*h*u*u/2) obey
    c[0]=1, c[1]=-a*h, c[k]=-(a*h*c[k-1]+h*h*c[k-2])/k.
    The integral of each coefficient is h*c[k]/(k+1). This is an entire
    power series, unlike the divergent asymptotic Mills expansion. Mills'
    bound phi(a)/a bounds the unintegrated tail at each panel boundary.
    """
    with localcontext() as context:
        context.prec = 80
        h = Decimal("0.25")
        total = Decimal(0)
        a = x
        for _ in range(400):
            previous = Decimal(1)
            current = -a * h
            integral = previous + current / 2
            for index in range(2, 300):
                following = -(a * h * current + h * h * previous) / index
                integral += following / (index + 1)
                previous, current = current, following
                if max(abs(previous), abs(current)) < Decimal("1e-70"):
                    break
            else:
                raise ArithmeticError("complementary panel series did not converge")
            total += normal_scale() * (-(a * a) / 2).exp() * h * integral
            a += h
            remaining_bound = normal_scale() * (-(a * a) / 2).exp() / a
            if remaining_bound < total * Decimal("1e-65"):
                return +total
        raise ArithmeticError("complementary tail did not converge")


def normal_cdf(z):
    """Decimal Phi: central Maclaurin series or direct complementary tail.

    Beyond |z|=40, Mills' bound is below 1e-349, below half the smallest
    binary64 subnormal. Return the binary64 saturation that this bound justifies
    without trying to represent exp(-z*z/2) for an arbitrarily large finite z.
    """
    with localcontext() as context:
        context.prec = 80
        x = abs(z)
        if x > 40:
            return Decimal(0) if z < 0 else Decimal(1)
        if x > 3:
            tail = complementary_tail(x)
            return +tail if z < 0 else +(1 - tail)
        term = z
        integral = z
        for index in range(1, 300):
            term *= -(z * z) * (2 * index - 1) / (2 * index * (2 * index + 1))
            integral += term
            if abs(term) < Decimal("1e-70"):
                return +(Decimal("0.5") + normal_scale() * integral)
        raise ArithmeticError("central normal series did not converge")


@lru_cache(maxsize=256)
def normal_inverse(probability):
    """Bisect [-40, 40]; 160 steps leave a bracket narrower than 6e-47."""
    if not 0 < probability < 1:
        raise ArithmeticError("inverse normal requires a probability inside (0, 1)")
    with localcontext() as context:
        context.prec = 80
        target = Decimal.from_float(probability)
        low, high = Decimal(-40), Decimal(40)
        for _ in range(160):
            middle = (low + high) / 2
            value = normal_cdf(middle)
            if value == target:
                low = high = middle
                break
            if value < target:
                low = middle
            else:
                high = middle
        answer = (low + high) / 2
        context.prec = 40
        return +answer


def normal_arguments(n_trials):
    """Replay each binary64 rounding separately, using rational operations."""
    n64 = Fraction(float(n_trials))
    reciprocal = float(1 / n64)
    p1 = float(1 - Fraction(reciprocal))
    product = float(n64 * E)
    reciprocal2 = float(1 / Fraction(product))
    p2 = float(1 - Fraction(reciprocal2))
    return p1, p2


def centred_variance(values):
    xs = [Fraction(value) for value in values]
    mean = sum(xs, Fraction()) / len(xs)
    return sum(((value - mean) ** 2 for value in xs), Fraction()) / (len(xs) - 1)


def exact_denominator(sr, skew, kurtosis):
    """Exact polynomial over input dyadics; no decimal or float intermediate."""
    x, g3, g4 = map(Fraction, (sr, skew, kurtosis))
    return 1 + x * (((g4 - 1) * x) / 4 - g3)


def finite_number(value):
    if type(value) not in (int, float):
        return None
    try:
        converted = float(value)
    except OverflowError:
        return None
    return converted if math.isfinite(converted) else None


class OracleRefusal(Exception):
    def __init__(self, suffix, ids=(), names=()):
        self.value = {
            "code": "dsr_" + suffix,
            "ids": tuple(sorted(ids)),
            "names": tuple(names),
        }
        super().__init__(self.value["code"])


@dataclass(frozen=True)
class TextSource:
    """A small JSON file payload, delivered to phase 2 through a read-only pipe."""

    text: str


def source_snapshot(source):
    """Freeze represented inputs, including types, order and NaN spellings."""
    if type(source) is int:
        try:
            json.dumps(source)
        except ValueError:
            # Hexadecimal retains the exact integer without the decimal text limit.
            return ("int", hex(source))
    if type(source) is float:
        return ("float", source.hex())
    if type(source) is dict:
        entries = tuple((key, source_snapshot(value)) for key, value in source.items())
        return ("dict", entries)
    if type(source) in (list, tuple):
        entries = tuple(source_snapshot(value) for value in source)
        return (type(source).__name__, entries)
    if isinstance(source, TextSource):
        return ("TextSource", source.text)
    return (type(source).__name__, source)


def strict_json(text):
    def reject(_):
        raise OracleRefusal("input_invalid")

    def pairs(items):
        result = dict(items)
        if len(result) != len(items):
            reject(None)
        return result

    def number(token, convert):
        value = convert(token)
        if finite_number(value) is None:
            reject(None)
        return value

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=reject,
            parse_float=lambda token: number(token, float),
            parse_int=lambda token: number(token, int),
        )
    except (ValueError, TypeError, OverflowError):
        raise OracleRefusal("input_invalid") from None


def independent_rows(source):
    """Check the represented source, without importing the shared reader.

    Validation is limited to the documented forms this checker exercises. It
    does not claim to replace the shared reader or reproduce every refusal.
    """
    if isinstance(source, TextSource):
        source = strict_json(source.text)
    if type(source) is dict:
        if source.get("trials_out", "all") != "all":
            raise OracleRefusal("rows_incomplete")
        trials = source.get("trials")
        ancestors = source.get("warm_start_trials", [])
        if type(trials) is not list or type(ancestors) is not list:
            raise OracleRefusal("input_invalid")
        if source.get("trials_completed", len(trials)) != len(trials):
            raise OracleRefusal("rows_incomplete")
        source = ancestors + trials
    if type(source) is not list:
        raise OracleRefusal("input_invalid")
    identifiers = []
    for row in source:
        if type(row) is not dict:
            raise OracleRefusal("input_invalid")
        identifier = row.get("trial_id")
        status = row.get("status")
        objective = row.get("objective")
        valid = [
            type(identifier) is int and 0 <= identifier <= 2**64 - 2,
            type(status) is str and status in STATUSES,
            type(row.get("feasible")) is bool,
            row.get("feasible") == (status == "ok"),
            "objective" in row,
            objective is None or finite_number(objective) is not None,
            status not in ("ok", "constraint_violation") or objective is not None,
        ]
        valid += [
            name not in row or type(row[name]) is dict
            for name in ("metrics", "parameters", "backtest", "space")
        ]
        valid += ["space_hash" not in row or type(row["space_hash"]) is str]
        if not all(valid):
            raise OracleRefusal("input_invalid")
        identifiers.append(identifier)
    if len(set(identifiers)) != len(identifiers):
        raise OracleRefusal("input_invalid")
    ordered = sorted(source, key=lambda row: row["trial_id"])
    # Pairwise disagreement sets avoid relying on the module's context helper.
    scored = [r for r in ordered if r["status"] == "ok" and "backtest" in r]
    if scored:
        values = [tuple(r["backtest"].get(k) for k in COUNTERS) for r in scored]
        names = sorted(
            name
            for position, name in enumerate(COUNTERS)
            if any(v[position] != values[0][position] for v in values)
        )
        if names:
            ids = [scored[0]["trial_id"]] + [
                r["trial_id"]
                for r, value in zip(scored[1:], values[1:])
                if value != values[0]
            ]
            raise OracleRefusal("context_mismatch", ids, names)
    hashed = [r for r in ordered if "space_hash" in r]
    if hashed:
        apart = [r for r in hashed if r["space_hash"] != hashed[0]["space_hash"]]
        if apart:
            raise OracleRefusal(
                "context_mismatch",
                [hashed[0]["trial_id"], *(r["trial_id"] for r in apart)],
                ["space_hash"],
            )
    return ordered


def check_series_entries(rows, series):
    """Check JSON writability; oversized integers are allowed in value entries."""
    ids = set()
    names = set()
    for item in rows:
        cells = item.get("metrics", {})
        for name in CELLS:
            key = f"returns.{series}.{name}"
            if key not in cells:
                continue
            try:
                json.dumps(cells[key], allow_nan=False, sort_keys=True)
            except (ValueError, TypeError, OverflowError):
                if name != "status" and type(cells[key]) is int:
                    continue
                ids.add(item["trial_id"])
                names.add(key)
    if ids:
        raise OracleRefusal("input_invalid", ids, sorted(names))


def input_digest(rows, series):
    """Reproduce the sorted per-row JSON digest, including unwritable integers."""

    def cell(value):
        if value is None:
            return None
        if type(value) is bool:
            return ["bool", value]
        if type(value) is float and not math.isfinite(value):
            return ["float", str(value)]
        try:
            text = json.dumps(
                value, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (ValueError, TypeError, OverflowError):
            return ["other", "<unwritable>"]
        if type(value) in (int, float):
            return value
        return ["other", text]

    digests = []
    for item in rows:
        metrics = item.get("metrics", {})
        cells = {
            f"returns.{series}.{name}": cell(metrics.get(f"returns.{series}.{name}"))
            for name in CELLS
        }
        payload = json.dumps(
            [item["trial_id"], item["status"], cells],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        digests.append(hashlib.sha256(payload).digest())
    return hashlib.sha256(b"".join(sorted(digests))).hexdigest()


def classify(rows, series):
    """Partition by sets of eligibility predicates, with first-reason priority."""
    prefix = f"returns.{series}."
    keys = [prefix + name for name in CELLS]
    categories = {}
    status_values = {}
    eligible = []
    for row in rows:
        cells = row.get("metrics", {})
        complete = all(key in cells for key in keys)
        status = cells.get(prefix + "status")
        integer_zero = type(status) is int and status == 0
        count = cells.get(prefix + "count")
        count_valid = type(count) is int and 2 <= count <= 2**53
        converted = [
            finite_number(cells.get(prefix + name))
            for name in ("sharpe_per_period", "skew", "kurt_raw")
        ]
        predicates = (
            (row["status"] != "ok", "status_" + row["status"]),
            (not complete, "returns_not_recorded"),
            (not integer_zero, "returns_status_nonzero"),
            (not count_valid or None in converted, "returns_unavailable"),
        )
        reason = next((name for bad, name in predicates if bad), None)
        if reason is None:
            eligible.append((row["trial_id"], converted[0], count, *converted[1:]))
        else:
            categories.setdefault(reason, []).append(row["trial_id"])
            if reason == "returns_status_nonzero":
                text = json.dumps(status, sort_keys=True, separators=(",", ":"))
                status_values[text] = status
    excluded = {
        reason: {"count": len(ids), "ids": ids}
        for reason, ids in sorted(categories.items())
    }
    if status_values:
        integers = sorted(v for v in status_values.values() if type(v) is int)
        others = [
            value
            for _, value in sorted(status_values.items())
            if type(value) is not int
        ]
        excluded["returns_status_nonzero"]["statuses"] = integers + others
    accounted = [entry[0] for entry in eligible] + [
        identifier for ids in categories.values() for identifier in ids
    ]
    if sorted(accounted) != [row["trial_id"] for row in rows]:
        raise ArithmeticError("classification did not partition all rows exactly once")
    return eligible, excluded


@dataclass
class Answer:
    values: dict
    tolerances: dict = field(default_factory=dict)
    variance_exact: Fraction | None = None
    den_exact: Fraction | None = None
    arguments: tuple = ()


def statistic(values, variance, denominator, n_trials, *, one_term=False):
    """Independent decimal evaluation, supplied only with the case's own inputs."""
    sr, count, _, _ = values
    p1, p2 = normal_arguments(n_trials)
    with localcontext() as context:
        context.prec = 80
        q1, q2 = normal_inverse(p1), normal_inverse(p2)
        weight = decimal_fraction(1 - G) * q1
        if not one_term:
            weight += decimal_fraction(G) * q2
        root_v = Decimal.from_float(math.sqrt(float(variance)))
        root_t = Decimal.from_float(math.sqrt(float(count) - 1.0))
        root_den = Decimal.from_float(math.sqrt(float(denominator)))
        sr0 = root_v * weight
        difference = Decimal.from_float(sr) - sr0
        numerator = difference * root_t
        z = numerator / root_den
        if not all(math.isfinite(float(v)) for v in (sr0, difference, numerator, z)):
            raise OverflowError("non-finite binary64 intermediate")
        dsr = normal_cdf(z)
        tolerance_sr0 = 8 * Decimal.from_float(math.ulp(float(sr0)))
        tolerance_z = tolerance_sr0 * root_t / root_den + 4 * Decimal.from_float(
            math.ulp(abs(float(z)))
        )
        tolerances = {
            "sr0": tolerance_sr0,
            "z": tolerance_z,
            "dsr": density(z) * tolerance_z + Decimal("1e-15"),
        }
        context.prec = 40
        figures = {"sr0": +sr0, "z": +z, "dsr": +dsr}
    return figures, tolerances, (p1, p2)


def oracle(source, *, series="bar", selected=0, n_trials=None, max_work=None):
    """Return either independent numbers or the refusal code, ids and names."""
    try:
        parameter_rules = (
            ("series", type(series) is str and series in ("bar", "monthly")),
            (
                "selected",
                type(selected) is int and 0 <= selected <= 2**64 - 2,
            ),
            (
                "n_trials",
                n_trials is None or (type(n_trials) is int and 2 <= n_trials <= 2**50),
            ),
            (
                "max_work",
                max_work is None or (type(max_work) is int and max_work > 0),
            ),
        )
        bad = next((name for name, valid in parameter_rules if not valid), None)
        if bad is not None:
            raise OracleRefusal("parameter_invalid", names=[bad])
        rows = independent_rows(source)
        check_series_entries(rows, series)
        eligible, excluded = classify(rows, series)
        ids = [entry[0] for entry in eligible]
        if not ids:
            raise OracleRefusal("no_eligible_rows")
        if len(ids) == 1:
            raise OracleRefusal("too_few_eligible_rows", ids)
        if selected not in [row["trial_id"] for row in rows]:
            raise OracleRefusal("selected_unknown", [selected])
        if selected not in ids:
            reason = next(k for k, v in excluded.items() if selected in v["ids"])
            raise OracleRefusal("selected_not_eligible", [selected], [reason])
        if n_trials is not None and n_trials < len(rows):
            raise OracleRefusal("trials_inconsistent", names=["n_trials"])
        if max_work is not None and len(rows) + len(ids) > max_work:
            raise OracleRefusal("resource_exceeded")
        variance = centred_variance([entry[1] for entry in eligible])
        if variance <= 0 or float(variance) <= 0:
            raise OracleRefusal("zero_sharpe_variance", ids)
        chosen = next(entry[1:] for entry in eligible if entry[0] == selected)
        sr, count, skew, kurtosis = chosen
        denominator = exact_denominator(sr, skew, kurtosis)
        if denominator <= 0 or float(denominator) <= 0:
            raise OracleRefusal("statistic_undefined", [selected])
        n = len(rows) if n_trials is None else n_trials
        figures, tolerances, arguments = statistic(chosen, variance, denominator, n)
        values = {
            "selected": selected,
            "sharpe_per_period": sr,
            "count": count,
            "skew": skew,
            "kurt_raw": kurtosis,
            "n_trials": n,
            "n_trials_source": "rows_read" if n_trials is None else "caller",
            "variance_basis": "sample_n_minus_1",
            "variance": float(variance),
            "den": float(denominator),
            **figures,
            "selected_is_max": sr == max(entry[1] for entry in eligible),
            "rows_read": len(rows),
            "rows_eligible": len(ids),
            "rows_excluded": len(rows) - len(ids),
            "eligible": ids,
            "excluded": excluded,
            "input_sha256": input_digest(rows, series),
        }
        return Answer(values, tolerances, variance, denominator, arguments)
    except OracleRefusal as refusal:
        return Answer(refusal.value)
    except (OverflowError, ZeroDivisionError):
        return Answer(
            {"code": "dsr_statistic_undefined", "ids": (selected,), "names": ()}
        )


def row(identifier, sr=None, count=10, skew=0.0, kurtosis=3.0, *, status="ok"):
    result = {
        "trial_id": identifier,
        "status": status,
        "feasible": status == "ok",
        "objective": 100.0 + identifier if status == "ok" else None,
    }
    if sr is not None:
        result["metrics"] = {
            "returns.bar.sharpe_per_period": sr,
            "returns.bar.count": count,
            "returns.bar.skew": skew,
            "returns.bar.kurt_raw": kurtosis,
            "returns.bar.status": 0,
        }
    return result


@dataclass
class Case:
    name: str
    source: object
    options: dict
    pins: dict = field(default_factory=dict)
    tabular: dict = field(default_factory=dict)
    code_only: bool = False


def fixtures():
    """Literal worked inputs and derived pins, never from a module run."""
    a = [
        row(0, 0.5, 8, 0.5, 4.0),
        row(1, 1.5, 12, -0.2, 5.0),
        row(2, 1.0),
        row(3, status="pruned"),
        row(4),
    ]
    for item in a[:3]:
        item["metrics"]["returns.bar.periods_per_year"] = 10.0
    b = [row(0, 0.5), row(1, 1.5)]
    notlast = [row(0, 1.0), row(1, 1.5, 20, 0.4, 5.0), row(2, 0.5, 6, -0.3, 4.0)]
    c = [
        row(0, 1.0, None),
        row(1, 1.0),
        row(2, 1.0, "10"),
        row(3, 1.0, 1),
        row(4, 1.0, 3, None, None),
        row(5),
        row(6, status="engine_error"),
        row(7, 0.5),
        row(8, 1.5),
    ]
    for name in CELLS:
        c[1]["metrics"]["returns.bar." + name] = None
    c[1]["metrics"]["returns.bar.status"] = 8
    c[4]["metrics"]["returns.bar.status"] = 1
    c[5]["metrics"] = {
        key.replace(".bar.", ".monthly."): value
        for key, value in b[0]["metrics"].items()
    }
    integer = [row(0, 1, 10, 0, 3), row(1, 2, 10, 0, 3)]
    boolean = [row(0, 1.0, skew=True), row(1, 2.0), row(2, 1.0)]
    maximum = [row(0, 1.0), row(1, 0.5), row(2, 1.5, status="pruned")]
    dual = copy.deepcopy(b)
    for item, sr, skew, kurt in zip(dual, (0.25, 0.75), (0.1, -0.1), (3.2, 3.4)):
        item["metrics"].update(
            {
                "returns.monthly.sharpe_per_period": sr,
                "returns.monthly.count": 12,
                "returns.monthly.skew": skew,
                "returns.monthly.kurt_raw": kurt,
                "returns.monthly.status": 0,
            }
        )
    worked = {"sr0": "0.4264022", "z": "1.4050218", "dsr": "0.9199926"}
    b2 = {"sr0": "0.3675226", "z": "2.330619", "dsr": "0.990113"}
    b3 = {"sr0": "0.6030238", "z": "1.8459618", "dsr": "0.9675522"}
    int_table = {"sr0": "0.3675226", "z": "2.8275339", "dsr": "0.9976546"}
    cases = [
        Case(
            "selected nonmaximum, all read trials counted",
            a,
            {"selected": 2},
            {
                "variance": 0.25,
                "den": 1.5,
                "sharpe_per_period": 1.0,
                "count": 10,
                "skew": 0.0,
                "kurt_raw": 3.0,
                "n_trials": 5,
                "n_trials_source": "rows_read",
                "rows_read": 5,
                "rows_eligible": 3,
                "rows_excluded": 2,
                "eligible": [0, 1, 2],
                "selected_is_max": False,
                "excluded": {
                    "returns_not_recorded": {"count": 1, "ids": [4]},
                    "status_pruned": {"count": 1, "ids": [3]},
                },
            },
        ),
        Case(
            "selected first eligible",
            notlast,
            {"selected": 0},
            {
                "variance": 0.25,
                "den": 1.5,
                "sharpe_per_period": 1.0,
                "count": 10,
                "skew": 0.0,
                "kurt_raw": 3.0,
                "n_trials": 3,
                "selected_is_max": False,
                "eligible": [0, 1, 2],
            },
            worked,
        ),
        Case(
            "two eligible rows: caller N=2",
            b,
            {"selected": 1, "n_trials": 2},
            {
                "variance": 0.5,
                "den": 2.125,
                "n_trials": 2,
                "n_trials_source": "caller",
                "sharpe_per_period": 1.5,
                "selected_is_max": True,
            },
            b2,
        ),
        Case(
            "two eligible rows: caller N=3",
            b,
            {"selected": 1, "n_trials": 3},
            {
                "variance": 0.5,
                "den": 2.125,
                "n_trials": 3,
                "n_trials_source": "caller",
            },
            b3,
        ),
        Case(
            "two eligible rows: default N=2",
            b,
            {"selected": 1},
            {"n_trials": 2, "n_trials_source": "rows_read", "den": 2.125},
            b2,
        ),
        Case(
            "all exclusion reasons",
            c,
            {"selected": 8},
            {
                "variance": 0.5,
                "den": 2.125,
                "n_trials": 9,
                "rows_read": 9,
                "rows_eligible": 2,
                "rows_excluded": 7,
                "eligible": [7, 8],
                "excluded": {
                    "returns_not_recorded": {"count": 1, "ids": [5]},
                    "returns_status_nonzero": {
                        "count": 2,
                        "ids": [1, 4],
                        "statuses": [1, 8],
                    },
                    "returns_unavailable": {"count": 3, "ids": [0, 2, 3]},
                    "status_engine_error": {"count": 1, "ids": [6]},
                },
            },
        ),
        Case(
            "rational variance identity",
            [row(0, 0.1), row(1, 0.2), row(2, 0.3)],
            {"selected": 0},
            {"variance": float(Fraction(5764607523034234, 2**59))},
        ),
        Case(
            "rational denominator identity",
            [row(0, 2.0, skew=0.1, kurtosis=3.3), row(1, 1.0)],
            {"selected": 0},
            {"den": float(Fraction(6980579422424268, 2**51)), "variance": 0.5},
        ),
        Case(
            "integer cells are converted",
            integer,
            {"selected": 1},
            {
                "variance": 0.5,
                "den": 3.0,
                "sharpe_per_period": 2.0,
                "count": 10,
                "skew": 0.0,
                "kurt_raw": 3.0,
            },
            int_table,
        ),
        Case(
            "boolean skew is excluded",
            boolean,
            {"selected": 1},
            {
                "variance": 0.5,
                "den": 3.0,
                "n_trials": 3,
                "eligible": [1, 2],
                "rows_excluded": 1,
                "excluded": {"returns_unavailable": {"count": 1, "ids": [0]}},
            },
        ),
        Case(
            "positive saturation",
            [row(0, 1.0, 1000), row(1, 1.0, 1000), row(2, 1.0 + 2**-52, 1000)],
            {"selected": 0},
            {"variance": float(Fraction(1, 3 * 2**104)), "den": 1.5, "dsr": 1.0},
        ),
        Case(
            "eligible maximum only",
            maximum,
            {"selected": 0},
            {"variance": 0.125, "den": 1.5, "n_trials": 3, "selected_is_max": True},
        ),
        Case(
            "exact maximum equality",
            [row(0, 1.5), row(1, math.nextafter(1.5, 1.0))],
            {"selected": 1},
            {"selected_is_max": False, "variance": 2**-105},
        ),
        Case(
            "two recorded series: bar",
            dual,
            {"selected": 1, "series": "bar"},
            {"variance": 0.5, "den": 2.125},
            b2,
        ),
        Case(
            "two recorded series: monthly",
            dual,
            {"selected": 1, "series": "monthly"},
            {"variance": 0.125, "sharpe_per_period": 0.75, "count": 12},
            {"sr0": "0.1837613", "z": "1.580162", "dsr": "0.942965"},
        ),
        Case(
            "negative saturation",
            [row(0, 1.0), row(1, 1e16), row(2, -1e16)],
            {"selected": 0},
            {"variance": 1e32, "den": 1.5, "dsr": 0.0},
        ),
    ]
    return cases


def refusal_cases(base):
    """Each expected refusal pins the first applicable documented defect."""
    a, b = base[0].source, base[2].source
    cases = []

    def add(name, source, options, suffix, ids=(), names=(), *, code_only=False):
        pins = {"code": "dsr_" + suffix}
        if not code_only:
            pins.update(ids=tuple(ids), names=tuple(names))
        cases.append(Case(name, source, options, pins, code_only=code_only))

    parameters = [
        ("series", "bar "),
        ("series", None),
        ("selected", 1.0),
        ("selected", True),
        ("n_trials", 1),
        ("n_trials", 2.0),
        ("n_trials", 2**50 + 1),
        ("max_work", 0),
    ]
    for name, value in parameters:
        add(
            f"parameter {name}={value!r}",
            a,
            {"selected": 2, name: value},
            "parameter_invalid",
            names=[name],
        )
    malformed = copy.deepcopy(a)
    malformed[0]["metrics"] = None
    add("metrics null", malformed, {"selected": 2}, "input_invalid", code_only=True)
    repeated = copy.deepcopy(b)
    repeated[1]["trial_id"] = 0
    add("repeated id", repeated, {"selected": 0}, "input_invalid", code_only=True)
    missing = copy.deepcopy(a)
    del missing[3]["objective"]
    add("objective absent", missing, {"selected": 2}, "input_invalid", code_only=True)
    null_ok = copy.deepcopy(b)
    null_ok[0]["objective"] = None
    add("ok objective null", null_ok, {"selected": 1}, "input_invalid", code_only=True)
    unwritable = copy.deepcopy(a)
    unwritable[3]["metrics"] = {"returns.bar.status": float("nan")}
    add(
        "unwritable series status in a pruned row before exclusion",
        unwritable,
        {"selected": 2},
        "input_invalid",
        [3],
        ["returns.bar.status"],
    )
    pruned_nan = [row(0, status="pruned")]
    pruned_nan[0]["metrics"] = {"returns.bar.status": float("nan")}
    add(
        "unwritable status before no eligible rows",
        pruned_nan,
        {"selected": 0},
        "input_invalid",
        [0],
        ["returns.bar.status"],
    )
    huge_status = copy.deepcopy(b)
    huge_status[0]["metrics"]["returns.bar.status"] = 10**5000
    add(
        "oversized integer status is refused",
        huge_status,
        {"selected": 1},
        "input_invalid",
        [0],
        ["returns.bar.status"],
    )
    mixed_keys = copy.deepcopy(b) + [row(2, 1.0, status="pruned")]
    mixed_keys[2]["metrics"]["returns.bar.skew"] = {1: "a", "b": 2}
    add(
        "unsortable object keys in pruned skew are refused",
        mixed_keys,
        {"selected": 1},
        "input_invalid",
        [2],
        ["returns.bar.skew"],
    )
    two_keys = [row(9, 1.0), row(3, 0.5), row(7, 1.5)]
    two_keys[0]["metrics"]["returns.bar.skew"] = float("nan")
    two_keys[1]["metrics"]["returns.bar.kurt_raw"] = float("inf")
    two_keys[2]["metrics"]["returns.bar.skew"] = float("inf")
    add(
        "two unwritable keys report union ids and sorted names",
        two_keys,
        {"selected": 7},
        "input_invalid",
        [3, 7, 9],
        ["returns.bar.kurt_raw", "returns.bar.skew"],
    )
    for name, payload in (
        ("NaN token", "NaN"),
        ("Infinity token", "Infinity"),
        ("overflow token", "1e400"),
    ):
        text = json.dumps(b).replace(
            '"returns.bar.skew": 0.0', f'"returns.bar.skew": {payload}', 1
        )
        add(name, TextSource(text), {"selected": 1}, "input_invalid", code_only=True)
    duplicate = json.dumps(b).replace(
        '"trial_id": 0', '"trial_id": 0, "trial_id": 0', 1
    )
    add(
        "duplicate JSON key",
        TextSource(duplicate),
        {"selected": 1},
        "input_invalid",
        code_only=True,
    )
    add(
        "best-only result",
        {"trials": a, "trials_out": "best"},
        {"selected": 2},
        "rows_incomplete",
        code_only=True,
    )
    add(
        "result count disagrees",
        {"trials": a, "trials_completed": 4},
        {"selected": 2},
        "rows_incomplete",
        code_only=True,
    )
    mismatch = copy.deepcopy(b)
    mismatch[0]["space_hash"], mismatch[1]["space_hash"] = "first", "second"
    add(
        "different space hashes",
        mismatch,
        {"selected": 1},
        "context_mismatch",
        [0, 1],
        ["space_hash"],
    )
    add("unknown selection", a, {"selected": 9}, "selected_unknown", [9])
    for identifier, reason in ((3, "status_pruned"), (4, "returns_not_recorded")):
        add(
            f"excluded selection {identifier}",
            a,
            {"selected": identifier},
            "selected_not_eligible",
            [identifier],
            [reason],
        )
    add("empty source before selection", [], {"selected": 0}, "no_eligible_rows")
    add(
        "pruned-only source",
        [row(0, status="pruned")],
        {"selected": 0},
        "no_eligible_rows",
    )
    add(
        "one eligible before selection",
        [row(0, status="pruned"), row(1, 1.5)],
        {"selected": 0},
        "too_few_eligible_rows",
        [1],
    )
    for n_trials in (2, 3, 4):
        add(
            f"caller N={n_trials} below five rows read",
            a,
            {"selected": 2, "n_trials": n_trials},
            "trials_inconsistent",
            names=["n_trials"],
        )
    add(
        "zero variance",
        [row(0, 1.0), row(1, 1.0)],
        {"selected": 0},
        "zero_sharpe_variance",
        [0, 1],
    )
    add(
        "resource refusal before zero variance",
        [row(0, 1.0), row(1, 1.0)],
        {"selected": 0, "max_work": 1},
        "resource_exceeded",
    )
    add(
        "inconsistent trial count before resource refusal",
        a,
        {"selected": 2, "n_trials": 4, "max_work": 1},
        "trials_inconsistent",
        names=["n_trials"],
    )
    # Differences of 2^-537 give V=2^-1075, a positive halfway-to-subnormal
    # rational which rounds to the even zero.
    add(
        "positive variance rounds to zero",
        [row(0, 0.0), row(1, 2**-537)],
        {"selected": 0},
        "zero_sharpe_variance",
        [0, 1],
    )
    add(
        "negative denominator",
        [row(0, 1.0, skew=5.0), row(1, 0.5)],
        {"selected": 0},
        "statistic_undefined",
        [0],
    )
    add(
        "exact zero denominator",
        [row(0, 1.0, skew=1.5), row(1, 0.5)],
        {"selected": 0},
        "statistic_undefined",
        [0],
    )
    # g3*SR=1 exactly; the remaining SR^2/2=2^-1993 is positive
    # but rounds to zero, while the other row keeps V nonzero.
    add(
        "positive denominator rounds to zero",
        [row(0, 2**-996, skew=2**996), row(1, 1.0)],
        {"selected": 0},
        "statistic_undefined",
        [0],
    )
    add(
        "non-finite rounded variance",
        [row(0, 1e308), row(1, -1e308)],
        {"selected": 0},
        "statistic_undefined",
        [0],
    )
    # den=(2^-52/4)*2^-1020=2^-1074, still a positive binary64.
    # V is about 5e307, so sqrt(V)/sqrt(den) exceeds the binary64 range.
    add(
        "non-finite z with positive rounded denominator",
        [
            row(0, 2**-510, skew=2**510, kurtosis=1.0 + 2**-52),
            row(1, 1e154),
        ],
        {"selected": 0},
        "statistic_undefined",
        [0],
    )
    for bound in (1, 7):
        add(
            f"work bound {bound} below eight",
            a,
            {"selected": 2, "max_work": bound},
            "resource_exceeded",
        )
    return cases


def supplemental_cases(base):
    """Additional boundaries with literal, input-derived assertions."""
    a, b = base[0].source, base[2].source
    status_rows = [row(0, 0.5), row(1, 1.5)]
    for identifier, value in enumerate((1, 1.0, True, 0.0, False, "bad", None), 2):
        item = row(identifier, 1.0)
        item["metrics"]["returns.bar.status"] = value
        status_rows.append(item)
    odd_status = copy.deepcopy(b) + [row(2, 1.0)]
    odd_status[2]["metrics"]["returns.bar.status"] = "x"
    large_status = copy.deepcopy(b) + [row(2, 1.0)]
    large_status[2]["metrics"]["returns.bar.status"] = 10**400
    huge_value = copy.deepcopy(b) + [row(2, 10**400)]
    huge_pruned = copy.deepcopy(b) + [row(2, 1.0, skew=10**5000, status="pruned")]
    # Literal compact, key-sorted preimages pin the oversized-integer fallback.
    # The eligible rows use ordinary numeric cells; the excluded row's skew
    # is ["other", "<unwritable>"] under the interpreter's 4300-digit limit.
    digest_payloads = (
        b'[0,"ok",{"returns.bar.count":10,"returns.bar.kurt_raw":3.0,'
        b'"returns.bar.sharpe_per_period":0.5,"returns.bar.skew":0.0,'
        b'"returns.bar.status":0}]',
        b'[1,"ok",{"returns.bar.count":10,"returns.bar.kurt_raw":3.0,'
        b'"returns.bar.sharpe_per_period":1.5,"returns.bar.skew":0.0,'
        b'"returns.bar.status":0}]',
        b'[2,"pruned",{"returns.bar.count":10,"returns.bar.kurt_raw":3.0,'
        b'"returns.bar.sharpe_per_period":1.0,'
        b'"returns.bar.skew":["other","<unwritable>"],"returns.bar.status":0}]',
    )
    huge_digest = hashlib.sha256(
        b"".join(
            sorted(hashlib.sha256(payload).digest() for payload in digest_payloads)
        )
    ).hexdigest()
    return [
        Case(
            "integer beyond binary64 excludes an ok row",
            huge_value,
            {"selected": 1},
            {
                "variance": 0.5,
                "den": 2.125,
                "n_trials": 3,
                "n_trials_source": "rows_read",
                "rows_read": 3,
                "rows_eligible": 2,
                "rows_excluded": 1,
                "eligible": [0, 1],
                "excluded": {"returns_unavailable": {"count": 1, "ids": [2]}},
            },
        ),
        Case(
            "integer beyond text limit in pruned skew permits a full result",
            huge_pruned,
            {"selected": 1},
            {
                "variance": 0.5,
                "den": 2.125,
                "n_trials": 3,
                "n_trials_source": "rows_read",
                "rows_read": 3,
                "rows_eligible": 2,
                "rows_excluded": 1,
                "eligible": [0, 1],
                "excluded": {"status_pruned": {"count": 1, "ids": [2]}},
                "input_sha256": huge_digest,
            },
        ),
        Case(
            "valid JSON payload through the strict-reading pipe",
            TextSource(json.dumps(b)),
            {"selected": 1},
            {
                "variance": 0.5,
                "den": 2.125,
                "n_trials": 2,
                "n_trials_source": "rows_read",
            },
        ),
        Case(
            "sparse ids select a trial id rather than a row position",
            [
                row(10, 0.5, 8, 0.5, 4.0),
                row(20, 1.0),
                row(30, 1.5, 12, -0.2, 5.0),
                row(5, status="pruned"),
                row(15),
            ],
            {"selected": 20},
            {
                "selected": 20,
                "sharpe_per_period": 1.0,
                "count": 10,
                "skew": 0.0,
                "kurt_raw": 3.0,
                "variance": 0.25,
                "den": 1.5,
                "n_trials": 5,
                "n_trials_source": "rows_read",
                "rows_read": 5,
                "rows_eligible": 3,
                "rows_excluded": 2,
                "eligible": [10, 20, 30],
                "selected_is_max": False,
                "excluded": {
                    "returns_not_recorded": {"count": 1, "ids": [15]},
                    "status_pruned": {"count": 1, "ids": [5]},
                },
            },
        ),
        Case(
            "writable odd series status remains a reported exclusion",
            odd_status,
            {"selected": 1},
            {
                "variance": 0.5,
                "den": 2.125,
                "n_trials": 3,
                "n_trials_source": "rows_read",
                "rows_read": 3,
                "rows_eligible": 2,
                "rows_excluded": 1,
                "eligible": [0, 1],
                "excluded": {
                    "returns_status_nonzero": {
                        "count": 1,
                        "ids": [2],
                        "statuses": ["x"],
                    },
                },
            },
        ),
        Case(
            "large writable integer status remains a reported exclusion",
            large_status,
            {"selected": 1},
            {
                "variance": 0.5,
                "den": 2.125,
                "n_trials": 3,
                "n_trials_source": "rows_read",
                "rows_read": 3,
                "rows_eligible": 2,
                "rows_excluded": 1,
                "eligible": [0, 1],
                "excluded": {
                    "returns_status_nonzero": {
                        "count": 1,
                        "ids": [2],
                        "statuses": [10**400],
                    },
                },
            },
        ),
        Case(
            "resource boundary eight admitted",
            a,
            {"selected": 2, "max_work": 8},
            {"variance": 0.25, "rows_read": 5, "rows_eligible": 3},
        ),
        Case(
            "warm-start rows count toward N",
            {"warm_start_trials": a[3:], "trials": a[:3], "trials_out": "all"},
            {"selected": 2},
            {"n_trials": 5, "rows_read": 5, "rows_eligible": 3, "variance": 0.25},
        ),
        Case(
            "status values preserve JSON types",
            status_rows,
            {"selected": 1},
            {
                "eligible": [0, 1],
                "n_trials": 9,
                "excluded": {
                    "returns_status_nonzero": {
                        "count": 7,
                        "ids": [2, 3, 4, 5, 6, 7, 8],
                        "statuses": [1, "bad", 0.0, 1.0, False, None, True],
                    },
                },
            },
        ),
    ]


def compare_pins(ledger, case, answer):
    for name, expected in case.pins.items():
        actual = answer.values.get(name)
        if name in APPROX_FIELDS and type(actual) is Decimal:
            actual = float(actual)
        ledger.compare("fixture", f"{case.name}: {name}", expected, actual)
    for name, expected in case.tabular.items():
        ledger.compare(
            "fixture",
            f"{case.name}: {name} [normal-table derivation, delta=5e-4]",
            Decimal(expected),
            answer.values.get(name),
            Decimal("5e-4"),
        )


def print_computed_pins(cases, answers):
    """Print computed values; this output is information, never a comparison."""
    print("\nPINNED VALUES FOR TESTS")
    print("# Computed from exact fixture inputs; no module has been imported.")
    for case, answer in zip(cases, answers):
        if (
            "code" in answer.values
            or case.options.get("n_trials") is not None
            or answer.values["rows_read"] == answer.values["rows_eligible"]
        ):
            continue
        values = answer.values
        print(f"# {case.name}")
        print("{")
        for key in ("n_trials", "variance", "den", "sr0", "z", "dsr"):
            value = values[key]
            if type(value) is Decimal:
                print(f"    {key!r}: {float(value)!r},  # decimal oracle: {value}")
            else:
                print(f"    {key!r}: {value!r},")
        print(f"    # exact V = {answer.variance_exact}")
        print(f"    # exact den = {answer.den_exact}")
        print("}")
    print("END PINNED VALUES FOR TESTS")


def argument_information(case, answer):
    if not answer.arguments:
        return
    n = answer.values["n_trials"]
    exact = (Fraction(n - 1, n), 1 - 1 / (n * E))
    for label, rounded, rational in zip(("p1", "p2"), answer.arguments, exact):
        difference = abs(Fraction(rounded) - rational)
        ulp = Fraction(math.ulp(rounded))
        print(
            f"info only: {case.name} {label}, N={n}: "
            f"argument difference={float(difference):.8g}, "
            f"difference/ulp={float(difference / ulp):.8g}; "
            "reference bound one ulp (shared rounding replay)"
        )


def section_controls(ledger, cases, answers):
    """Every planted change lives in an oracle copy; the baseline stays frozen."""
    ledger.begin("one-sided controls before importing the module")
    baseline = answers[0]
    changed_inputs = copy.deepcopy(cases[0].source)
    changed_inputs[0]["metrics"]["returns.bar.sharpe_per_period"] += 1e-9
    changed = oracle(changed_inputs, **cases[0].options)
    directions = {"variance": -1, "sr0": -1, "z": 1, "dsr": 1}
    for name, direction in directions.items():
        before, after = baseline.values[name], changed.values[name]
        if name == "variance":
            differs = same(before, after) is False
        else:
            differs = close(after, before, baseline.tolerances[name]) is False
        ledger.check(
            "control",
            f"one ratio +1e-9: {name} changes beyond its tolerance, correct direction",
            differs and direction * (after - before) > 0,
            f"original={before!r}, mutated={after!r}",
        )
    chosen = (
        baseline.values["sharpe_per_period"],
        baseline.values["count"],
        baseline.values["skew"],
        baseline.values["kurt_raw"],
    )
    mutation = copy.deepcopy(baseline)
    mutation.values.update(
        statistic(
            chosen,
            baseline.variance_exact,
            baseline.den_exact,
            baseline.values["n_trials"],
            one_term=True,
        )[0]
    )
    ledger.check(
        "control",
        "one-term formula fails the sr0 comparison and lowers sr0",
        close(
            mutation.values["sr0"], baseline.values["sr0"], baseline.tolerances["sr0"]
        )
        is False
        and mutation.values["sr0"] < baseline.values["sr0"],
        f"original={baseline.values['sr0']}, mutated={mutation.values['sr0']}",
    )
    decimal_case = answers[6]
    numbers = [0.1, 0.2, 0.3]
    # Explicit left-to-right operations avoid the compensated sum(float)
    # of Python 3.12+ in this deliberately raw control.
    s1 = 0.0
    s2 = 0.0
    for value in numbers:
        s1 += value
        s2 += value * value
    mutation = copy.deepcopy(decimal_case)
    mutation.values["variance"] = (3 * s2 - s1**2) / 6
    ledger.check(
        "internal",
        "one-sided raw one-pass variance fails exact comparison",
        same(decimal_case.values["variance"], mutation.values["variance"]) is False,
        f"exact={decimal_case.values['variance']!r}, "
        f"raw one-pass={mutation.values['variance']!r}",
    )
    ledger.compare(
        "internal",
        "raw one-pass is the hand-derived five-ulp-lower value",
        float(Fraction(5764607523034229, 2**59)),
        mutation.values["variance"],
    )
    mean = s1 / 3
    squared_deviations = 0.0
    for value in numbers:
        squared_deviations += (value - mean) ** 2
    two_pass = squared_deviations / 2
    den_float = 1.0 - 0.1 * 2.0 + ((3.3 - 1) / 4) * 2.0 * 2.0
    print(
        "info only, non-separating: "
        f"two-pass V={two_pass!r}, "
        f"exact rounded V={decimal_case.values['variance']!r}; "
        f"binary64 den={den_float!r}, exact rounded den={answers[7].values['den']!r}"
    )
    replay = normal_arguments(3)
    mutation_arguments = list(replay)
    mutation_arguments[0] = float(Fraction(2, 3))
    ledger.check(
        "internal",
        "one-sided single-rounding p1 fails replay comparison",
        same(replay, tuple(mutation_arguments)) is False,
        f"replayed p1={replay[0]!r}, single-rounding p1={mutation_arguments[0]!r}",
    )
    ledger.compare("internal", "N=3 replay hand value", 0.6666666666666667, replay[0])
    ledger.compare(
        "internal",
        "N=3 single-rounding hand value",
        0.6666666666666666,
        mutation_arguments[0],
    )
    for n in (2, 3, 5, 9, 2**50):
        direct = (1.0 - 1.0 / float(n), 1.0 - 1.0 / (float(n) * float(E)))
        ledger.compare(
            "internal",
            f"N={n}: replay agrees with plain binary64 operations",
            direct,
            normal_arguments(n),
        )
    ledger.check(
        "internal",
        "both arguments are strictly inside (0,1) at the admitted upper bound",
        all(0 < p < 1 for p in normal_arguments(2**50)),
    )
    ledger.compare(
        "internal", "inverse at 0.5 from bisection", Decimal(0), normal_inverse(0.5)
    )
    ledger.compare(
        "internal", "Phi(0) from series", Decimal("0.5"), normal_cdf(Decimal(0))
    )
    tail = normal_cdf(Decimal(-10))
    ledger.check(
        "internal",
        "complementary decimal tail at -10 stays positive "
        "below binary64 erf resolution",
        0 < tail < Decimal("1e-16"),
        f"Phi(-10)={tail}",
    )
    # Normal-table derivations, independent of these decimal functions:
    # Phi(3)=0.9986501019683699 gives Phi(-3)=1-Phi(3)=0.0013498980316301.
    # The direct survival-table entry Q(10)=7.6198530241605e-24 gives Phi(-10)
    # by symmetry, without subtracting a rounded Phi(10) from one.
    # Phi(1.96)=0.9750021048517798, phi(1.96)=0.05844094433345148.
    # d=(0.975-Phi(1.96))/phi(1.96) is about -0.000036016731,
    # so 1.96+d+1.96*d*d/2 is 1.959963984540 to the shown precision.
    # The cubic correction is below 1e-13; table rounding is below 1e-12
    # relative for all three pins. A relative tolerance of 1e-9 covers both.
    references = (
        ("Phi(-3)", Decimal("1.3498980316301e-3"), normal_cdf(Decimal(-3))),
        ("Phi(-10)", Decimal("7.6198530241605e-24"), tail),
        ("Phi_inv(0.975)", Decimal("1.959963984540054"), normal_inverse(0.975)),
    )
    for label, expected, actual in references:
        ledger.compare(
            "internal",
            f"{label}: normal-table reference, relative tolerance=1e-9",
            expected,
            actual,
            abs(expected) * Decimal("1e-9"),
        )
    # Four perturbation comparisons, one-term, raw difference
    # and raw pin, single-rounding difference and two pins, five replay pairs,
    # bound, inverse midpoint, central midpoint, complementary tail, three
    # normal-table references. Source immutability is checked separately.
    ledger.end(22)


def random_case(generator, number):
    eligible_count = generator.randint(2, 6)
    ratios = (-2, -1, -0.5, 0, 0.5, 1, 1.5, 2)
    identifiers = generator.sample(range(5, 1000, 5), eligible_count + 4)
    rows = [
        row(
            identifier,
            generator.choice(ratios),
            generator.randint(2, 12),
            generator.uniform(-3, 3),
            generator.uniform(1.5, 12),
        )
        for identifier in identifiers[:eligible_count]
    ]
    selected = generator.choice(identifiers[:eligible_count])
    excluded = [
        row(identifiers[eligible_count], status="pruned"),
        row(identifiers[eligible_count + 1]),
        row(identifiers[eligible_count + 2], 1.0),
        row(identifiers[eligible_count + 3], 1.0, skew=True),
    ]
    excluded[2]["metrics"]["returns.bar.status"] = 1
    excluded[2]["metrics"]["returns.bar.skew"] = None
    rows += excluded
    # A caller's N must be at least the rows read, excluded rows included.
    n_trials = generator.randint(len(rows), len(rows) + 4) if number % 2 else None
    return Case(f"random {number}", rows, {"selected": selected, "n_trials": n_trials})


def generate_random(ledger, base, count, seed):
    generator = random.Random(seed)
    instances = []
    replacements = 0
    rejected = 0
    print(f"\nPreparing random inputs and oracle answers (seed={seed})")
    print("No hand pins for random inputs; numeric comparisons occur only in phase 2.")
    ledger.begin("random source immutability during phase-one evaluation")

    def evaluate(case):
        snapshot = source_snapshot(case.source)
        answer = oracle(case.source, **case.options)
        ledger.compare(
            "control",
            f"{case.name}: source unchanged since before oracle evaluation",
            snapshot,
            source_snapshot(case.source),
        )
        return answer

    for number in range(count):
        for _ in range(200):
            case = random_case(generator, number)
            answer = evaluate(case)
            if "code" in answer.values or -3 <= answer.values["z"] <= 3:
                break
            rejected += 1
        else:
            case = copy.deepcopy(base[0])
            case.name = f"random {number}: fixed worked fallback"
            answer = evaluate(case)
            replacements += 1
        instances.append((case, answer))
    ledger.end(count + rejected)
    print(
        f"random redraws outside range: {rejected}; "
        f"fixed fallback replacements: {replacements}"
    )
    return instances, replacements


def check_no_package(ledger, label):
    ledger.begin(label)
    imported = [
        name
        for name in sys.modules
        if name == "pineforge_hpo" or name.startswith("pineforge_hpo.")
    ]
    ledger.compare("internal", "no package imports during phase 1", [], imported)
    ledger.end(1)
    return not imported


def phase_one(ledger, count, seed):
    print("\n##### phase 1: independent oracle, no module")
    if not check_no_package(ledger, "independence before phase 1"):
        raise RuntimeError("package already imported before phase 1")
    base = fixtures()
    cases = base + supplemental_cases(base) + refusal_cases(base)
    snapshots = [source_snapshot(case.source) for case in cases]
    answers = [oracle(case.source, **case.options) for case in cases]
    ledger.begin("worked fixtures and refusal pins")
    for case, answer in zip(cases, answers):
        compare_pins(ledger, case, answer)
    expected = sum(len(case.pins) + len(case.tabular) for case in cases)
    ledger.end(expected)
    for case, answer in zip(cases, answers):
        argument_information(case, answer)
    section_controls(ledger, base, answers[: len(base)])
    ledger.begin("source immutability across phase-one evaluation and controls")
    for case, snapshot in zip(cases, snapshots):
        ledger.compare(
            "control",
            f"{case.name}: source unchanged since before oracle evaluation",
            snapshot,
            source_snapshot(case.source),
        )
    ledger.end(len(cases))
    instances, replacements = generate_random(ledger, base, count, seed)
    if not check_no_package(ledger, "independence after phase 1"):
        raise RuntimeError("package imported during phase 1")
    if not ledger.failures:
        print_computed_pins(cases, answers)
    return list(zip(cases, answers)), instances, replacements


def module_call(module, case):
    """The only route to the module: public calls, never expected calculations."""
    source = copy.deepcopy(case.source)
    read_fd = None
    try:
        if isinstance(source, TextSource):
            payload = source.text.encode("utf-8")
            if len(payload) > 4096:
                raise RuntimeError("strict-file pipe payload exceeds its bounded size")
            read_fd, write_fd = os.pipe()
            try:
                written = os.write(write_fd, payload)
                if written != len(payload):
                    raise RuntimeError("short write to strict-file pipe")
            finally:
                os.close(write_fd)
            source = Path(f"/dev/fd/{read_fd}")
        options = {"series": "bar", **case.options}
        try:
            result = module.dsr(source, **options)
        except module.DsrError as refusal:
            return {
                "code": refusal.code,
                "ids": refusal.ids,
                "names": refusal.names,
            }
        if type(result) is not dict:
            raise TypeError("module returned a non-dict result")
        return result
    finally:
        if read_fd is not None:
            os.close(read_fd)


def compare_module(ledger, case, answer, actual):
    if "code" in answer.values:
        names = ("code",) if case.code_only else ("code", "ids", "names")
        for name in names:
            ledger.compare(
                "module", f"{case.name}: {name}", answer.values[name], actual.get(name)
            )
        return
    ledger.compare(
        "module", f"{case.name}: exact result key order", RESULT_KEYS, tuple(actual)
    )
    for name in EXACT_FIELDS:
        ledger.compare(
            "module", f"{case.name}: {name}", answer.values[name], actual.get(name)
        )
    if "input_sha256" in case.pins:
        ledger.compare(
            "module",
            f"{case.name}: input_sha256",
            answer.values["input_sha256"],
            actual.get("input_sha256"),
        )
    for name in APPROX_FIELDS:
        ledger.check(
            "module",
            f"{case.name}: {name} is binary64 float",
            type(actual.get(name)) is float,
        )
        ledger.compare(
            "module",
            f"{case.name}: {name}",
            answer.values[name],
            actual.get(name),
            answer.tolerances[name],
        )


def comparison_count(case, answer):
    if "code" in answer.values:
        return 1 if case.code_only else 3
    return (
        1
        + len(EXACT_FIELDS)
        + 2 * len(APPROX_FIELDS)
        + int("input_sha256" in case.pins)
    )


def phase_two(ledger, cases, random_instances, seed):
    print("\n##### phase 2: compare the module with frozen oracle answers")
    if not check_no_package(ledger, "independence before module import"):
        raise RuntimeError("package imported before phase 2")
    sys.path.insert(0, str(ROOT / "python"))
    module = importlib.import_module("pineforge_hpo.dsr")
    ledger.begin("module fixtures and refusals")
    for case, answer in cases:
        actual = module_call(module, case)
        compare_module(ledger, case, answer, actual)
    ledger.end(sum(comparison_count(case, answer) for case, answer in cases))
    ledger.begin("module seeded random cases and shuffled admitted rows")
    shuffler = random.Random(seed + 1)
    expected = sum(
        comparison_count(case, answer) * (1 if "code" in answer.values else 2)
        for case, answer in random_instances
    )
    for case, answer in random_instances:
        compare_module(ledger, case, answer, module_call(module, case))
        if "code" not in answer.values:
            shuffled = copy.deepcopy(case)
            shuffled.name += ": shuffled"
            shuffler.shuffle(shuffled.source)
            compare_module(ledger, shuffled, answer, module_call(module, shuffled))
    ledger.end(expected)


def finish(ledger, no_module, replacements):
    required = ("fixture", "internal", "control")
    if not no_module:
        required += ("module",)
    for kind in required:
        if ledger.counts[kind] == 0:
            ledger.fail(f"coverage: no {kind} comparisons were made")
    print("\nSUMMARY")
    for kind, count in ledger.counts.items():
        print(f"  {kind} comparisons: {count}")
    print(f"  fixed random fallback replacements: {replacements}")
    print(f"  failures: {len(ledger.failures)}")
    for message in ledger.failures:
        print(f"  - {message}")
    print("NOT COMPARED")
    for limitation in NOT_COMPARED:
        print(f"  - {limitation}")
    print(
        "REPRODUCTIONS: exclusion priority, refusal order, p1/p2 roundings, "
        "and final Fraction-to-binary64 conversion."
    )
    if ledger.failures:
        print("RESULT: FAIL")
        return 1
    if no_module:
        print("RESULT: PHASE 1 ONLY (exit 3); the module is NOT run or compared")
        return 3
    print("RESULT: PASS (oracle agrees with hand pins and with the module)")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-module", action="store_true", help="phase 1 only; exit 3, never 0"
    )
    parser.add_argument(
        "--cases", type=int, default=60, help="positive case count (default 60)"
    )
    parser.add_argument(
        "--seed", type=int, default=20261012, help="random seed (default 20261012)"
    )
    arguments = parser.parse_args(argv)
    if arguments.cases < 1:
        parser.error("--cases must be positive")
    ledger = Ledger()
    replacements = 0
    print(
        f"DSR oracle: cases={arguments.cases}, seed={arguments.seed}, "
        f"Python={sys.version.split()[0]}"
    )
    try:
        harness_self_test()
        cases, instances, replacements = phase_one(
            ledger, arguments.cases, arguments.seed
        )
        if not arguments.no_module and not ledger.failures:
            phase_two(ledger, cases, instances, arguments.seed)
        elif not arguments.no_module:
            print("phase 2 NOT RUN: phase 1 failed; no module agreement claimed")
        else:
            print("phase 2 NOT RUN (--no-module)")
    except Exception as error:
        ledger.fail(f"oracle or module exception: {type(error).__name__}: {error}")
    return finish(ledger, arguments.no_module, replacements)


if __name__ == "__main__":
    sys.exit(main())
