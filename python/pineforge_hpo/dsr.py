"""Deflated Sharpe ratio of one selected trial from a finished study.

``dsr`` reads one complete result, rows JSONL file, parsed result dictionary or
list of rows through the shared reader. The caller must name ``series`` (``bar``
or ``monthly``) and ``selected`` (a trial id); neither has a default. This library
function runs no trial, writes no file, keeps no state and changes no native result.

Contract ``pineforge-hpo-dsr/v1``, algorithm ``deflated_sharpe_exact_v1`` uses the
selected row's recorded excess ``sharpe_per_period``, ``count``, population skewness
and raw population kurtosis (normal = 3). It never reads the objective as a Sharpe,
annualises a ratio, or converts the recorded moment conventions.

Only ``ok`` rows with all five requested return-statistic keys, a series status
that is exactly the integer zero, finite numeric values and an integer count in
2..2^53 are eligible. The first exclusion reason decides: ``status_<status>``,
``returns_not_recorded``, ``returns_status_nonzero``, then ``returns_unavailable``.
Eligible rows alone feed the sample variance and the selected row's statistics.
Default N counts every row read, including excluded trials and warm-start ancestors:
this is the conservative choice because every recorded trial was still searched.
A caller's ``n_trials`` must be an integer in 2..2^50 and at least the rows read.

Before exclusions, a requested series entry of any row that cannot be written as
key-sorted JSON refuses the call as ``dsr_input_invalid``. An object whose keys
cannot be sorted in any of the five entries also refuses. Its ``ids`` is the union
of the offending trial ids in ascending order, and ``names`` lists the offending entry
keys in ascending alphabetical order. An ``ok`` row with all five keys, series
status zero and an integer value whose float conversion overflows is excluded as
``returns_unavailable``. An integer beyond the interpreter's integer-to-text
limit in a value entry is not refused: its digest cell is
``["other", "<unwritable>"]``. An integer beyond the interpreter's integer-to-text
limit in the status entry is refused so the result's ``statuses`` can be written;
a smaller large integer there is a status that is not ok.

The sample variance V and the denominator ``1 - g3*SR + ((g4-1)/4)*SR^2`` are exact
rationals over the binary64 inputs, each rounded once, ties to even. Both must be
positive as rationals and after rounding. The same rounded denominator is reported
and used for its square root. The remaining scalar binary64 operations follow a
fixed order: float(N); division then subtraction for ``1 - 1/N``; multiplication,
division then subtraction for ``1 - 1/(N*E)``; the two inverse normal calls; the
weighted sum; sqrt(V) times that sum; sqrt(float(T)-1); sqrt(den); subtraction,
multiplication then division for z; the normal cdf. G is 0.5772156649015329 and E is
2.718281828459045. Phi and its inverse come from ``statistics.NormalDist``. Every
intermediate must be finite; a finite z whose cdf saturates to 0.0 or 1.0 is valid.

The returned document names every eligible and excluded id in ascending order,
with ascending reason keys and distinct nonzero series statuses ordered by integer
value first, then compact JSON text. Its SHA-256 covers every row read through
sorted per-row digests of the id, status and five typed return-statistic cells;
it covers neither the selection nor N. Absent and null cells coincide, while
integers, floats and booleans retain distinct representations.
The admission figure is rows read + eligible rows, with no built-in cap.

A refusal raises ``DsrError``; ``document()`` renders only the refusal. The first
defect decides: parameters (series, selected, n_trials, max_work); source,
context and series cells; eligible count (zero before one); selection (unknown
before excluded); N against rows read; admission figure; variance then the
remaining statistic.
Under the paper's assumptions the DSR reads as the probability that the selected
trial's true Sharpe exceeds the expected maximum of N unskilled trials. Tuned
trials are correlated, so this is a deflated reading under a stated N, not a
significance test. Recorded data is taken as given, no threshold is applied and
no out-of-sample performance is claimed. v1 analyses finished studies; native
in-loop walk-forward comes later.
"""

from __future__ import annotations

import hashlib
import json
import math
from fractions import Fraction
from statistics import NormalDist, StatisticsError
from typing import Any, Iterable

from ._rows import RowsError, check_context, read_source

__all__ = ["ALGORITHM", "CODES", "CONTRACT", "INTERPRETATION", "DsrError", "dsr"]

CONTRACT = "pineforge-hpo-dsr/v1"
ALGORITHM = "deflated_sharpe_exact_v1"
CODES = (
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
)
INTERPRETATION = (
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

_G = 0.5772156649015329
_E = 2.718281828459045
_FIELDS = ("sharpe_per_period", "count", "skew", "kurt_raw", "status")


class DsrError(Exception):
    """A refusal with a module-local code, detail, sorted ids and field names."""

    def __init__(
        self, code: str, detail: str, ids: Iterable = (), names: Iterable = ()
    ) -> None:
        if code not in CODES:
            raise ValueError(f"unknown dsr refusal code: {code!r}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.ids = tuple(sorted(ids))
        self.names = tuple(names)

    def __reduce__(self):
        return (type(self), (self.code, self.detail, self.ids, self.names))

    def document(self) -> dict:
        """Return the refused document, without any statistic."""
        return {
            "contract": CONTRACT,
            "status": "refused",
            "refusal": {
                "code": self.code,
                "detail": self.detail,
                "ids": list(self.ids),
                "names": list(self.names),
            },
        }


def _check_parameters(series, selected, n_trials, max_work) -> None:
    checks = (
        ("series", type(series) is str and series in ("bar", "monthly")),
        ("selected", type(selected) is int and 0 <= selected <= (1 << 64) - 2),
        (
            "n_trials",
            n_trials is None or (type(n_trials) is int and 2 <= n_trials <= 1 << 50),
        ),
        ("max_work", max_work is None or (type(max_work) is int and max_work > 0)),
    )
    for name, valid in checks:
        if not valid:
            raise DsrError(
                "dsr_parameter_invalid", f"invalid {name} value", names=(name,)
            )


def _number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _other_text(value: Any) -> str:
    try:
        return _json(value)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return "<unwritable>"


def _cell(value: Any) -> Any:
    if value is None:
        return value
    if type(value) is int:
        try:
            _json(value)
        except ValueError:
            return ["other", "<unwritable>"]
        return value
    if type(value) is bool:
        return ["bool", value]
    if type(value) is float:
        if math.isfinite(value):
            return value
        text = "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"
        return ["float", text]
    return ["other", _other_text(value)]


def _input_digest(rows, names) -> str:
    parts = []
    for row in rows:
        cells = {
            name: _cell(row.metrics.get(name) if row.metrics is not None else None)
            for name in names
        }
        parts.append(
            hashlib.sha256(
                _json([row.trial_id, row.status, cells]).encode("ascii")
            ).digest()
        )
    return hashlib.sha256(b"".join(sorted(parts))).hexdigest()


def _statuses(values) -> list:
    distinct = {}
    for value in values:
        # JSON text, rather than Python equality, distinguishes 1, 1.0 and True.
        text = _json(value)
        distinct.setdefault(text, value)
    return sorted(
        distinct.values(),
        key=lambda value: (0, value) if type(value) is int else (1, _json(value)),
    )


def _variance(values) -> Fraction:
    ratios = [value.as_integer_ratio() for value in values]
    scale = max(denominator for _, denominator in ratios)
    total = 0
    squares = 0
    for numerator, denominator in ratios:
        integer = numerator * (scale // denominator)
        total += integer
        squares += integer * integer
    count = len(values)
    return Fraction(count * squares - total * total, count * (count - 1) * scale**2)


def _require_T(count: int) -> None:
    """Assert the statistic's T >= 2 guard, independently of row eligibility."""
    if count < 2:
        raise DsrError("dsr_statistic_undefined", "T must be at least two")


def _finite(value, selected, name) -> float:
    if not math.isfinite(value):
        raise DsrError(
            "dsr_statistic_undefined", f"{name} is not finite", ids=(selected,)
        )
    return value


def _rounded(rational, selected, name) -> float:
    try:
        rounded = float(rational)
    except OverflowError:
        raise DsrError(
            "dsr_statistic_undefined", f"{name} overflows binary64", ids=(selected,)
        ) from None
    return _finite(rounded, selected, name)


def _statistic(values, statistics, n_trials, selected) -> tuple:
    sr, count, skew, kurt = statistics
    variance = _variance(list(values.values()))
    if variance <= 0:
        raise DsrError(
            "dsr_zero_sharpe_variance",
            "exact Sharpe variance is zero",
            ids=values.keys(),
        )
    v = _rounded(variance, selected, "variance")
    if v <= 0:
        raise DsrError(
            "dsr_zero_sharpe_variance",
            "Sharpe variance rounds to zero",
            ids=values.keys(),
        )
    n64 = float(n_trials)
    p1 = 1.0 - 1.0 / n64
    p2 = 1.0 - 1.0 / (n64 * _E)
    normal = NormalDist()
    try:
        q1 = normal.inv_cdf(p1)
        q2 = normal.inv_cdf(p2)
    except StatisticsError:
        raise DsrError(
            "dsr_statistic_undefined",
            "inverse normal argument is undefined",
            (selected,),
        ) from None
    w = (1.0 - _G) * q1 + _G * q2
    sv = math.sqrt(v)
    sr0 = _finite(sv * w, selected, "sr0")
    r_sr, r_skew, r_kurt = Fraction(sr), Fraction(skew), Fraction(kurt)
    den_rational = 1 - r_skew * r_sr + ((r_kurt - 1) / 4) * r_sr**2
    if den_rational <= 0:
        raise DsrError(
            "dsr_statistic_undefined", "exact denominator is not positive", (selected,)
        )
    den = _rounded(den_rational, selected, "den")
    if den <= 0:
        raise DsrError(
            "dsr_statistic_undefined", "denominator rounds to zero", (selected,)
        )
    _require_T(count)
    t = math.sqrt(float(count) - 1.0)
    sd = math.sqrt(den)
    z = _finite((sr - sr0) * t / sd, selected, "z")
    result = _finite(normal.cdf(z), selected, "dsr")
    return v, sr0, den, z, result


def dsr(
    source: Any,
    *,
    series: str,
    selected: int,
    n_trials: int | None = None,
    max_work: int | None = None,
) -> dict:
    """Return one selected trial's DSR document, or raise ``DsrError``.

    N defaults to all rows read; a caller may only increase it. ``max_work`` bounds
    rows read + eligible rows before exact arithmetic is performed.
    """
    _check_parameters(series, selected, n_trials, max_work)
    try:
        loaded = read_source(source)
        check_context(loaded.rows)
    except RowsError as error:
        ids = [item[1] if isinstance(item, tuple) else item for item in error.ids]
        code = {
            "rows_incomplete": "dsr_rows_incomplete",
            "context_mismatch": "dsr_context_mismatch",
        }.get(error.kind, "dsr_input_invalid")
        raise DsrError(code, error.detail, ids, error.names) from None
    rows = sorted(loaded.rows, key=lambda row: row.trial_id)
    names = tuple(f"returns.{series}.{field}" for field in _FIELDS)
    invalid_ids = set()
    invalid_names = set()
    for name in names:
        for row in rows:
            if row.metrics is None or name not in row.metrics:
                continue
            value = row.metrics[name]
            try:
                _json(value)
            except (TypeError, ValueError, OverflowError, RecursionError):
                # Oversized integer values have a total digest representation.
                if type(value) is int and name != names[-1]:
                    continue
                invalid_ids.add(row.trial_id)
                invalid_names.add(name)
    if invalid_ids:
        raise DsrError(
            "dsr_input_invalid",
            "a series entry cannot be written as JSON",
            invalid_ids,
            sorted(invalid_names),
        )
    eligible = {}
    reasons = {}
    status_values = []
    for row in rows:
        reason = None
        if row.status != "ok":
            reason = f"status_{row.status}"
        elif row.metrics is None or any(name not in row.metrics for name in names):
            reason = "returns_not_recorded"
        else:
            sr, count, skew, kurt, status = (row.metrics[name] for name in names)
            if type(status) is not int or status != 0:
                reason = "returns_status_nonzero"
                status_values.append(status)
            else:
                sr, skew, kurt = _number(sr), _number(skew), _number(kurt)
                if (
                    sr is None
                    or skew is None
                    or kurt is None
                    or type(count) is not int
                    or not 2 <= count <= 1 << 53
                ):
                    reason = "returns_unavailable"
                else:
                    eligible[row.trial_id] = (sr, count, skew, kurt)
        if reason is not None:
            reasons.setdefault(reason, []).append(row.trial_id)
    if not eligible:
        raise DsrError("dsr_no_eligible_rows", "no row remains after exclusions")
    if len(eligible) == 1:
        raise DsrError(
            "dsr_too_few_eligible_rows",
            "only one eligible row remains",
            eligible.keys(),
        )
    if selected not in {row.trial_id for row in rows}:
        raise DsrError(
            "dsr_selected_unknown", "selected trial was not read", (selected,)
        )
    if selected not in eligible:
        reason = next(reason for reason, ids in reasons.items() if selected in ids)
        raise DsrError(
            "dsr_selected_not_eligible",
            "selected trial is excluded",
            (selected,),
            (reason,),
        )
    rows_read = len(rows)
    if n_trials is not None and n_trials < rows_read:
        raise DsrError(
            "dsr_trials_inconsistent",
            "n_trials is below the number of rows read",
            names=("n_trials",),
        )
    work = rows_read + len(eligible)
    if max_work is not None and work > max_work:
        raise DsrError(
            "dsr_resource_exceeded",
            f"admission figure {work} is above max_work {max_work}",
        )
    n = rows_read if n_trials is None else n_trials
    values = {identifier: stats[0] for identifier, stats in eligible.items()}
    v, sr0, den, z, result = _statistic(values, eligible[selected], n, selected)
    sr, count, skew, kurt = eligible[selected]
    excluded = {}
    for reason in sorted(reasons):
        entry = {"count": len(reasons[reason]), "ids": reasons[reason]}
        if reason == "returns_status_nonzero":
            entry["statuses"] = _statuses(status_values)
        excluded[reason] = entry
    return {
        "contract": CONTRACT,
        "status": "ok",
        "series": series,
        "selected": selected,
        "sharpe_per_period": sr,
        "count": count,
        "skew": skew,
        "kurt_raw": kurt,
        "n_trials": n,
        "n_trials_source": "rows_read" if n_trials is None else "caller",
        "variance_basis": "sample_n_minus_1",
        "variance": v,
        "sr0": sr0,
        "den": den,
        "z": z,
        "dsr": result,
        "selected_is_max": sr == max(values.values()),
        "rows_read": rows_read,
        "rows_eligible": len(eligible),
        "rows_excluded": rows_read - len(eligible),
        "eligible": list(eligible),
        "excluded": excluded,
        "algorithm": ALGORITHM,
        "input_sha256": _input_digest(rows, names),
        "interpretation": INTERPRETATION,
    }
