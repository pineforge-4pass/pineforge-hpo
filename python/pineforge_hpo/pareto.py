"""Post hoc Pareto front over the metrics recorded per trial.

``pareto_front`` takes the finished rows of one study (a complete result, a rows JSONL
file or a list of rows, read by the shared reader ``_rows``) and two or more axes, each
a recorded metric name with a caller-supplied direction, and returns the rows that no
other eligible row dominates. Nothing here runs a trial, writes a file, keeps state or
touches a native result: the only side effect is reading the one file named. There is
no Pareto sampler, no vector objective, no rank, hypervolume or crowding.

Contract ``pineforge-hpo-pareto-front/v1``, algorithm ``exact_all_pairs_v1``:

* Only rows with status ``ok`` compete. Every other status is excluded with the reason
  ``status_<status>``. An ``ok`` row whose ``metrics`` object, or an axis key in it, is
  absent is excluded as ``metric_not_recorded``; one whose axis value is ``null`` as
  ``metric_unavailable`` (the first reason that applies, in that order).
* An axis value of an ``ok`` row must be a finite number or ``null`` (a bool, string,
  array, object, non-finite number or overflowing integer refuses the call). A
  non-finite number in an axis cell of any row refuses too, as the ``NaN`` token does
  when the same data is read from a file.
* Row ``a`` dominates row ``b`` when ``a`` is no worse on every axis (in that axis's
  direction) and strictly better on at least one. Values are compared as exact binary64
  numbers, ``-0.0`` equals ``+0.0``, and equal vectors never dominate each other: both
  stay on the front. Every pair of eligible rows is compared in both directions.
* The result is ordered by ascending trial id and does not depend on the order of the
  rows or of the axes. ``input_sha256`` is the SHA-256 of the sorted per-row SHA-256
  digests (raw bytes) of the compact, key-sorted, ASCII JSON of
  ``[trial_id, status, {axis name: number as binary64, or null when absent or null}]``
  for every row read; the digest does not include the directions.
* ``interpretation`` is the fixed text of ``INTERPRETATION``: what the front is not.
* The admission figure is rows read + N(N-1)/2 for N eligible rows. The call is
  refused as ``pareto_resource_exceeded`` only against a ``max_work`` the caller gave.

A refusal is raised as ``ParetoError``; ``ParetoError.document()`` renders it as the
refused result document (no front). The checks run in this order, so the first defect
decides the code: axes and options (``pareto_parameter_missing`` before
``pareto_parameter_invalid``); the source (``pareto_input_invalid``,
``pareto_rows_incomplete``); the context of the rows (``pareto_context_mismatch``); the
axis names (``pareto_axis_unknown``, checked only when at least one row was read); the
axis values (``pareto_input_invalid``); ``pareto_no_eligible_rows``; the admission
figure (``pareto_resource_exceeded``).
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable

from ._rows import RowsError, check_context, read_source

__all__ = [
    "ALGORITHM",
    "CODES",
    "CONTRACT",
    "DIRECTIONS",
    "INTERPRETATION",
    "ParetoError",
    "pareto_front",
]

CONTRACT = "pineforge-hpo-pareto-front/v1"
ALGORITHM = "exact_all_pairs_v1"
DIRECTIONS = ("maximize", "minimize")
CODES = (
    "pareto_input_invalid",
    "pareto_rows_incomplete",
    "pareto_context_mismatch",
    "pareto_parameter_missing",
    "pareto_parameter_invalid",
    "pareto_axis_unknown",
    "pareto_no_eligible_rows",
    "pareto_resource_exceeded",
)

INTERPRETATION = (
    "(a) No Pareto sampler and no multi-objective search (P-F8). "
    "(b) The front is over the trials that a scalar-objective study chose to "
    "evaluate: TPE learns from one value and abandons constraint-violating trials "
    "(study-spec.md:765-767, 790-794), so the sampler never searched the other axes. "
    "The front is the front of the evaluated rows, not the true front, and its extent "
    "depends on the sampler, the seed and the budget. "
    "(c) A row excluded for a missing value, or for violating a constraint, may belong "
    "to the true front. "
    "(d) In-sample: no holdout (study-spec.md:376-377). "
    "(e) No claim of spread, diversity, optimality or hypervolume; equal vectors are "
    "not merged. "
    "(f) Directions are the caller's; the module does not know a metric's sign "
    "convention (P-F5). "
    "(g) Rows of one lineage may come from different scoring contexts that the space "
    "hash cannot detect (P-F13); the clause 2(e) checks are necessary, not sufficient."
)

_AXIS_KEYS = ("name", "direction")
_METRIC_REASONS = ("metric_not_recorded", "metric_unavailable")
_KIND_CODES = {
    "input_invalid": "pareto_input_invalid",
    "rows_incomplete": "pareto_rows_incomplete",
    "context_mismatch": "pareto_context_mismatch",
}


def _clip(value: Any, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


class ParetoError(Exception):
    """A refusal of the Pareto front contract; ``code`` is one of ``CODES``.

    ``ids`` holds offending trial ids, ascending; ``names`` holds offending axis or
    field names. Either may be empty.
    """

    code: str
    detail: str
    ids: tuple
    names: tuple

    def __init__(
        self,
        code: str,
        detail: str,
        ids: Iterable = (),
        names: Iterable = (),
    ) -> None:
        if code not in CODES:
            raise ValueError(f"unknown pareto refusal code: {code!r}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.ids = tuple(sorted(ids))
        self.names = tuple(names)

    def __reduce__(self):
        return (type(self), (self.code, self.detail, self.ids, self.names))

    def document(self) -> dict:
        """The refused result document: the refusal and the offenders, no front."""
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


def _from_rows_error(error: RowsError) -> ParetoError:
    """Map a reader refusal to the Pareto code; space kinds are not used here."""
    code = _KIND_CODES.get(error.kind)
    if code is None:
        raise error
    ids = [item[1] if isinstance(item, tuple) else item for item in error.ids]
    return ParetoError(code, error.detail, ids, error.names)


def _check_axes(axes: Any) -> tuple:
    """Validate the axes request; return (names, directions) in request order."""
    if axes is None:
        raise ParetoError("pareto_parameter_missing", "no axes were given")
    if not isinstance(axes, (list, tuple)):
        raise ParetoError(
            "pareto_parameter_invalid",
            "axes must be a list of {name, direction} objects",
        )
    if not axes:
        raise ParetoError("pareto_parameter_missing", "no axes were given")
    lacking = []
    for position, axis in enumerate(axes):
        if not isinstance(axis, dict):
            continue
        name = axis.get("name")
        if name is None or axis.get("direction") is None:
            lacking.append(name if type(name) is str else f"axes[{position}]")
    if lacking:
        raise ParetoError(
            "pareto_parameter_missing",
            "every axis needs a name and a direction, and there is no default: "
            + ", ".join(_clip(label) for label in lacking),
            names=lacking,
        )
    names: list = []
    directions: list = []
    repeated: list = []
    for position, axis in enumerate(axes):
        if not isinstance(axis, dict):
            raise ParetoError(
                "pareto_parameter_invalid",
                f"axes[{position}] must be an object with a name and a direction",
            )
        extra = sorted(str(key) for key in axis if key not in _AXIS_KEYS)
        if extra:
            raise ParetoError(
                "pareto_parameter_invalid",
                f"axes[{position}] holds fields other than name and direction",
                names=extra,
            )
        name = axis["name"]
        direction = axis["direction"]
        if type(name) is not str:
            raise ParetoError(
                "pareto_parameter_invalid", f"axes[{position}] name must be a string"
            )
        if type(direction) is not str or direction not in DIRECTIONS:
            raise ParetoError(
                "pareto_parameter_invalid",
                f"axis {_clip(name)} direction must be exactly maximize or minimize",
                names=(name,),
            )
        if name in names and name not in repeated:
            repeated.append(name)
        names.append(name)
        directions.append(direction)
    if repeated:
        raise ParetoError(
            "pareto_parameter_invalid",
            "an axis name is used more than once",
            names=repeated,
        )
    if len(names) < 2:
        raise ParetoError("pareto_parameter_invalid", "at least two axes are needed")
    return tuple(names), tuple(directions)


def _check_options(with_domination_counts: Any, max_work: Any) -> None:
    if type(with_domination_counts) is not bool:
        raise ParetoError(
            "pareto_parameter_invalid", "with_domination_counts must be true or false"
        )
    if max_work is not None and (type(max_work) is not int or max_work < 1):
        raise ParetoError(
            "pareto_parameter_invalid", "max_work must be a positive integer or None"
        )


def _number(value: Any) -> float | None:
    """The binary64 of a finite JSON number, else None (type, overflow, NaN, infinity)."""
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _axis_cells(row: Any, names: tuple) -> tuple:
    """Return (cells, absent, null, unfit) of one row for the axis names.

    ``cells`` maps each name to what the digest records: the binary64 of a finite
    number, None for an absent or null value, any other value as read. ``absent`` and
    ``null`` list the names with no key and with a null value. ``unfit`` lists the
    names that refuse the input: a non-finite number in any row, or a value that is
    neither a number nor null in an ``ok`` row.
    """
    metrics = row.metrics
    cells: dict = {}
    absent: list = []
    null: list = []
    unfit: list = []
    for name in names:
        if metrics is None or name not in metrics:
            absent.append(name)
            cells[name] = None
            continue
        value = metrics[name]
        if value is None:
            null.append(name)
            cells[name] = None
        elif type(value) in (int, float):
            number = _number(value)
            if number is None:
                unfit.append(name)
            cells[name] = number
        else:
            if row.status == "ok":
                unfit.append(name)
            cells[name] = value
    return cells, absent, null, unfit


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _input_digest(entries: list) -> str:
    """SHA-256 of the sorted per-row digests of [trial_id, status, cells] (clause 3.5)."""
    parts = []
    unwritable = []
    for trial_id, status, cells in entries:
        try:
            parts.append(hashlib.sha256(_canonical([trial_id, status, cells])).digest())
        except (TypeError, ValueError, OverflowError, RecursionError):
            unwritable.append(trial_id)
    if unwritable:
        raise ParetoError(
            "pareto_input_invalid",
            "an axis value cannot be written as JSON",
            unwritable,
        )
    parts.sort()
    return hashlib.sha256(b"".join(parts)).hexdigest()


def _dominates(first: tuple, second: tuple) -> bool:
    """Whether ``first`` is no worse everywhere and better somewhere (larger is better)."""
    better = False
    for left, right in zip(first, second):
        if left < right:
            return False
        if left > right:
            better = True
    return better


def pareto_front(
    source: Any,
    axes: Any,
    *,
    with_domination_counts: bool = False,
    max_work: int | None = None,
) -> dict:
    """Return the Pareto front of the eligible rows of ``source`` over ``axes``.

    ``source`` is whatever ``_rows.read_source`` takes: a path to a result JSON or a
    rows JSONL, a parsed result dict or a parsed list of rows. ``axes`` is a list of at
    least two ``{"name": <metric name as recorded>, "direction": "maximize" or
    "minimize"}`` objects with distinct names; a direction has no default.
    ``with_domination_counts`` adds, for every eligible id, how many eligible rows
    dominate it (0 exactly on the front). ``max_work`` is a positive integer bound on
    the admission figure rows read + N(N-1)/2; None means no bound.

    Returns the ``pineforge-hpo-pareto-front/v1`` document (see the module text).
    Raises ``ParetoError`` for every refusal; nothing is returned for a refused call.
    """
    names, directions = _check_axes(axes)
    _check_options(with_domination_counts, max_work)
    try:
        loaded = read_source(source)
        check_context(loaded.rows)
    except RowsError as error:
        raise _from_rows_error(error) from None
    rows = sorted(loaded.rows, key=lambda row: row.trial_id)
    rows_read = len(rows)
    if rows_read:
        unknown = [
            name
            for name in names
            if not any(row.metrics is not None and name in row.metrics for row in rows)
        ]
        if unknown:
            raise ParetoError(
                "pareto_axis_unknown",
                "an axis name is recorded in no row's metrics object",
                names=unknown,
            )
    excluded: dict = {}
    involved: dict = {reason: set() for reason in _METRIC_REASONS}
    eligible: list = []
    entries: list = []
    unfit_ids: list = []
    unfit_names: set = set()
    for row in rows:
        cells, absent, null, unfit = _axis_cells(row, names)
        if unfit:
            unfit_ids.append(row.trial_id)
            unfit_names.update(unfit)
            continue
        entries.append((row.trial_id, row.status, cells))
        if row.status != "ok":
            excluded.setdefault(f"status_{row.status}", []).append(row.trial_id)
        elif absent:
            excluded.setdefault("metric_not_recorded", []).append(row.trial_id)
            involved["metric_not_recorded"].update(absent)
        elif null:
            excluded.setdefault("metric_unavailable", []).append(row.trial_id)
            involved["metric_unavailable"].update(null)
        else:
            eligible.append((row.trial_id, tuple(cells[name] for name in names)))
    if unfit_ids:
        raise ParetoError(
            "pareto_input_invalid",
            "an axis value is not a finite number, or null on an ok row",
            unfit_ids,
            [name for name in names if name in unfit_names],
        )
    digest = _input_digest(entries)
    count = len(eligible)
    if not count:
        reasons = ", ".join(
            f"{reason} {len(excluded[reason])}" for reason in sorted(excluded)
        )
        raise ParetoError(
            "pareto_no_eligible_rows",
            f"no row remains after exclusions ({rows_read} rows read: {reasons})"
            if rows_read
            else "no row remains after exclusions (the source holds no rows)",
        )
    pairs = count * (count - 1) // 2
    work = rows_read + pairs
    if max_work is not None and work > max_work:
        raise ParetoError(
            "pareto_resource_exceeded",
            f"the admission figure {work} (rows read {rows_read} + {pairs} pairs of "
            f"{count} eligible rows) is above max_work {max_work}",
        )
    ids = [trial_id for trial_id, _ in eligible]
    vectors = [vector for _, vector in eligible]
    scored = [
        tuple(
            -value if direction == "minimize" else value
            for value, direction in zip(vector, directions)
        )
        for vector in vectors
    ]
    dominated_by = [0] * count
    for first in range(count):
        for second in range(first + 1, count):
            if _dominates(scored[first], scored[second]):
                dominated_by[second] += 1
            elif _dominates(scored[second], scored[first]):
                dominated_by[first] += 1
    front = [index for index in range(count) if dominated_by[index] == 0]
    excluded_document: dict = {}
    for reason in sorted(excluded):
        entry: dict = {"count": len(excluded[reason]), "ids": excluded[reason]}
        if reason in involved:
            entry["axes"] = [name for name in names if name in involved[reason]]
        excluded_document[reason] = entry
    document: dict = {
        "contract": CONTRACT,
        "status": "ok",
        "axes": [
            {"name": name, "direction": direction}
            for name, direction in zip(names, directions)
        ],
        "rows_read": rows_read,
        "rows_eligible": count,
        "rows_excluded": rows_read - count,
        "excluded": excluded_document,
        "pareto_front": [ids[index] for index in front],
        "n_front": len(front),
        "n_distinct_vectors_on_front": len({vectors[index] for index in front}),
    }
    if with_domination_counts:
        document["domination_counts"] = {
            ids[index]: dominated_by[index] for index in range(count)
        }
    document["algorithm"] = ALGORITHM
    document["input_sha256"] = digest
    document["interpretation"] = INTERPRETATION
    return document
