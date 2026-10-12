"""Post hoc comparison of the best rows of two finished studies.

``holdout_compare`` takes the finished rows of two studies that already exist - the
SELECTION source (the study the user tuned on) and the HOLDOUT source (a second study
the user ran, outside this module, on data the caller says was set aside). It reads
both with the shared reader ``_rows``, joins their rows by the exact parameter vector
and reports, for the ``k`` best eligible vectors of the selection source, what the
holdout source recorded for the same vectors. Nothing here splits data, runs a trial,
writes a file, keeps state or touches a native result: the only side effect is reading
the two sources named. It is a library function; there is no command-line flag and no
field in a native result.

Contract ``pineforge-hpo-holdout/v1``, algorithm ``exact_join_midrank_v1``:

* Every row of both sources is keyed by ``_rows.vector_key(parameters, space)``, the
  canonical key of the typed parameters of one space, which the rows and results of
  both sources must share. Equality is exact: no tolerance and no lattice snapping, so
  a near-equal vector is a different vector.
* A row is eligible when its status is ``ok`` and its value for the named metric is a
  finite number. Every other row is excluded with one reason: ``status_<status>`` for
  a status other than ``ok``, else ``metric_not_recorded`` when the metric is a name
  and the row records no such key, else ``metric_unavailable`` when the recorded cell
  is ``null``. A metric cell that is a non-finite number, or an ``ok`` row's cell of
  another type, or a cell that cannot be written as JSON, or an integer too large for
  a binary64, refuses the call.
* Eligible rows of one source that carry one vector are one vector. Their values must
  all be equal; a disagreement refuses ``holdout_duplicate_vector``, and the vector is
  otherwise represented by its eligible row of lowest ``trial_id`` and counted once.
  When several vectors conflict, every eligible row of every conflicting vector of the
  first conflicting source (selection first) is named; the other source is not examined.
* The eligible selection vectors are ranked by their value in the caller's direction,
  ties broken by ascending representative trial id, so every rank is a distinct
  1-based integer. The followed vectors are the best ``min(k, n_sel)`` of them. A
  followed vector with no eligible row in the holdout source is reported as unmatched,
  with the reason ``absent_in_holdout`` or ``holdout_row_not_eligible`` and the ids of
  the holdout rows that hold it; it is never given a value of zero.
* The POOL is the eligible holdout vectors whose key matches the key of at least one
  eligible selection vector, of any rank. Equal values in the pool share a mid-rank,
  the exact integer or half-integer ``(p + q) / 2`` of the positions the tie group
  occupies.
* For every matched followed vector the document reports the difference
  ``holdout - selection`` (one subtraction) and whether the holdout value is strictly
  better in the direction. Over the matched vectors two statistics are reported: the
  rank correlation of the matched pairs, whose two rank vectors mid-rank equal values
  on each side within the matched set alone, and the share of matched vectors whose
  pool mid-rank is at most ``(M + 1) / 2`` with ``M`` the pool size. Every rank,
  mid-rank, term and cut is computed in exact rationals and reported as a
  ``[numerator, denominator]`` pair of integers in lowest terms; ``difference``, the
  share and ``spearman`` are single correctly rounded binary64 operations.
* ``spearman`` is ``null``, with a reason in ``spearman_undefined``, when fewer than
  three vectors are matched or when one of the two rank vectors is constant. No
  significance test, no p-value, no threshold and no verdict accompanies it.
* ``input_sha256`` is the SHA-256 of the sorted per-row SHA-256 digests of the compact,
  key-sorted, ASCII JSON of ``[source_index, trial_id, status, value, vector_key]`` for
  every row read of both sources; the metric name, the direction and ``k`` are not
  covered. The document depends only on the rows, the space and the arguments.
* ``interpretation`` is the fixed text of ``INTERPRETATION``: what the comparison is
  not.
* The admission figure is ``rows_read + n_sel(n_sel - 1)/2 + n_hol(n_hol - 1)/2`` over
  the eligible VECTOR counts. The call is refused as ``holdout_resource_exceeded`` only
  against a ``max_work`` the caller gave; a figure equal to it is admitted.
* A complete result may carry the ancestor rows of an earlier study it continued from.
  The module counts them for each source and refuses ``holdout_ancestor_rows`` when the
  holdout source's ancestor rows repeat a trial id of the selection source, because
  such a source may be reporting the selection study's own rows; the trial id is the
  only witness available, so an id that repeats by a coincidence of numbering is
  refused like a real overlap.

A refusal is raised as ``HoldoutError``; ``HoldoutError.document()`` renders it as the
refused result document (no comparison). The checks run in this order, so the first
defect decides the code: the arguments (``holdout_parameter_missing`` before
``holdout_parameter_invalid``); the two sources (``holdout_input_invalid``,
``holdout_rows_incomplete``); the space of both sources (``holdout_space_missing``,
``holdout_space_mismatch``); the direction rule; the context of each source on its own
(``holdout_context_mismatch``); the ancestor rows of the holdout source
(``holdout_ancestor_rows``); the key of every row (``holdout_input_invalid``); the
metric name (``holdout_metric_unknown``); every metric value
(``holdout_value_invalid``); the eligible rows of the selection source
(``holdout_no_eligible_rows``); the repeated vectors (``holdout_duplicate_vector``);
the admission figure (``holdout_resource_exceeded``); and a difference that is not
finite (``holdout_value_invalid``).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from fractions import Fraction
from typing import Any, Iterable

from ._rows import (
    _content_lines,
    _is_one_value,
    RowsError,
    check_context,
    common_space,
    loads_strict,
    read_source,
    vector_key,
)
from .study_spec import StudySpec

__all__ = [
    "ALGORITHM",
    "CODES",
    "CONTRACT",
    "DIRECTIONS",
    "INTERPRETATION",
    "HoldoutError",
    "holdout_compare",
]

CONTRACT = "pineforge-hpo-holdout/v1"
ALGORITHM = "exact_join_midrank_v1"
DIRECTIONS = ("maximize", "minimize")
CODES = (
    "holdout_input_invalid",
    "holdout_rows_incomplete",
    "holdout_space_missing",
    "holdout_space_mismatch",
    "holdout_context_mismatch",
    "holdout_ancestor_rows",
    "holdout_parameter_missing",
    "holdout_parameter_invalid",
    "holdout_metric_unknown",
    "holdout_value_invalid",
    "holdout_no_eligible_rows",
    "holdout_duplicate_vector",
    "holdout_resource_exceeded",
)

INTERPRETATION = (
    "(a) The module splits no data and runs no trial: it reads two sets of rows that "
    "two finished studies already wrote, and joins them by the exact parameter vector. "
    "It cannot know what data either study used, so a value recorded by the second "
    "study is only as held-out as the caller made it. "
    "(b) A study is a holdout only when the caller ran it on data set aside before the "
    "selection rows were read; the module cannot check that, and reading the best "
    "selection rows while the second study's values are in view contaminates it. "
    "(c) Equality of parameter vectors is exact: no tolerance and no lattice snapping, "
    "so a near-equal vector is a different vector and is reported unmatched, never "
    "merged. "
    "(d) No significance test, no threshold and no verdict: a few vectors give a noisy "
    "rank correlation, and the module states no probability, interval or error bar. "
    "(e) The selection side is the best of many trials, so its values are optimistic; "
    "every number is in-sample for the selection source and only as held-out as the "
    "caller made it for the other. "
    "(f) v1 analyses finished studies; native in-loop walk-forward comes later. "
    "(g) The scoring-context check is necessary, not sufficient, and it runs on each "
    "source on its own, never between the two studies: a space hash names the "
    "parameter space and the objective, not the data, so an equal hash says nothing "
    "about the data, and this module requires the two studies to share that space, "
    "so their hashes are normally equal and can differ only through a space-hash "
    "version change. "
    "(h) When a source records no space, its rows are keyed with the space the other "
    "source records or the caller passes, so the module cannot tell whether the second "
    "study ran in that space. "
    "(i) A complete result may carry the ancestor rows of a study it continued from: "
    "the module counts them for each source, reports the counts, and refuses a holdout "
    "source whose ancestor rows repeat a trial id of the selection source, because "
    "such a source may be reporting the selection study's own rows; an overlap that "
    "the trial ids do not show cannot be detected, and an id that repeats by a "
    "coincidence of numbering is refused like a real one. "
    "(j) No claim is made about the data of either source, about the run that produced "
    "it, or about any strategy."
)

_CODES_BY_KIND = {
    "input_invalid": "holdout_input_invalid",
    "rows_incomplete": "holdout_rows_incomplete",
    "space_missing": "holdout_space_missing",
    "space_mismatch": "holdout_space_mismatch",
    "context_mismatch": "holdout_context_mismatch",
}
_SOURCE_WORDS = ("selection", "holdout")


class HoldoutError(Exception):
    """A refusal of the holdout comparison contract; ``code`` is one of ``CODES``.

    ``ids`` holds offending rows as ``(source_index, trial_id)`` pairs, ascending;
    ``names`` holds offending metric, parameter or argument names. Either may be empty.
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
            raise ValueError(f"unknown holdout refusal code: {code!r}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.ids = tuple(sorted(ids))
        self.names = tuple(names)

    def __reduce__(self):
        return (type(self), (self.code, self.detail, self.ids, self.names))

    def document(self) -> dict:
        """The refused result document: the refusal and the offenders, no comparison."""
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


def _from_rows_error(error: RowsError, index: int | None = None) -> HoldoutError:
    """Map a reader refusal; a bare trial id gains its source index."""
    code = _CODES_BY_KIND.get(error.kind)
    if code is None:
        raise error
    ids = [item if isinstance(item, tuple) else (index, item) for item in error.ids]
    return HoldoutError(code, error.detail, ids, error.names)


def _range(row: Any) -> tuple:
    return (row.source_index, row.trial_id)


def _cell_error(row: Any, detail: str) -> HoldoutError:
    return HoldoutError("holdout_value_invalid", detail, [_range(row)])


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _pair(value: Fraction) -> list:
    """A ``[numerator, denominator]`` pair of integers in lowest terms."""
    return [value.numerator, value.denominator]


def _ranked_value(value: float, direction: str) -> float:
    """The value as it is ordered: larger is better once the direction is applied."""
    return -value if direction == "maximize" else value


def _best_first(items: list, direction: str) -> list:
    """Sort ``(trial_id, value, vector_key)`` items best first, ties by trial id."""
    return sorted(items, key=lambda item: (_ranked_value(item[1], direction), item[0]))


def _vector_items(vectors: dict) -> list:
    """The ``(trial_id, value, vector_key)`` items of one keyed vector map."""
    return [(trial_id, value, key) for key, (trial_id, value) in vectors.items()]


def _differs(rows: list) -> bool:
    """Whether two or more eligible rows of one vector disagree on the value."""
    return len(rows) > 1 and len({value for _, value in rows}) > 1


def _mid_ranks(values: list, direction: str) -> list:
    """Exact mid-ranks of one list of values, in the order the values are given.

    A maximal run of equal values at the positions ``p..q`` (1-based) of the order in
    ``direction`` gets the exact rational ``(p + q) / 2``.
    """
    order = sorted(
        range(len(values)),
        key=lambda position: _ranked_value(values[position], direction),
    )
    ranks = [None] * len(values)
    start = 0
    while start < len(order):
        stop = start
        while stop + 1 < len(order) and values[order[stop + 1]] == values[order[start]]:
            stop += 1
        mid = Fraction(start + stop + 2, 2)
        for position in range(start, stop + 1):
            ranks[order[position]] = mid
        start = stop + 1
    return ranks


def _ancestors_from_value(value: Any) -> list:
    """The trial ids of the ancestor rows of one parsed source value."""
    if not isinstance(value, dict):
        return []
    if "trials" not in value and "trials_out" not in value:
        return []
    inherited = value.get("warm_start_trials")
    if not isinstance(inherited, list):
        return []
    return [
        raw["trial_id"]
        for raw in inherited
        if isinstance(raw, dict) and type(raw.get("trial_id")) is int
    ]


def _document_of_text(text: str) -> Any:
    """The one JSON value a path holds when the reader reads it as a document."""
    lines = _content_lines(text)
    first = next(lines, None)
    if first is None:
        return None
    if next(lines, None) is None:
        return loads_strict(first[1])
    if _is_one_value(first[1]):
        return None
    return loads_strict(text)


def _ancestor_ids(source: Any) -> list:
    """The ancestor trial ids one source carries, by the reader's own dispatch.

    A complete result carries its ``warm_start_trials`` before its own rows; a rows
    file, a parsed list of rows or an empty source carries none. A source given as a
    path is read again here as the reader read it.
    """
    if isinstance(source, dict):
        return _ancestors_from_value(source)
    if not isinstance(source, (str, os.PathLike)):
        return []
    try:
        with open(source, "rb") as stream:
            content = stream.read()
        document = _document_of_text(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, RowsError):
        return []
    return _ancestors_from_value(document)


def _metric_cell(row: Any, metric: str) -> tuple:
    """The digest value and the binary64 value of one row for the metric.

    The digest value is the binary64 of a number cell, ``None`` when the row records no
    value, and, in a row that is not ``ok``, a cell that is not a number exactly as
    read. A defect raises ``holdout_value_invalid`` for that row.
    """
    if metric == "objective":
        return row.objective, row.objective
    metrics = row.metrics
    if metrics is None or metric not in metrics:
        return None, None
    cell = metrics[metric]
    if cell is None:
        return None, None
    if type(cell) in (int, float):
        try:
            number = float(cell)
        except OverflowError:
            detail = "the metric cell is an integer too large for a binary64"
            raise _cell_error(row, detail) from None
        if not math.isfinite(number):
            raise _cell_error(row, "the metric cell is not a finite number")
        return number, number
    if row.status == "ok":
        raise _cell_error(row, "an ok row's metric cell is not a number")
    try:
        _canonical(cell)
    except (TypeError, ValueError, RecursionError):
        detail = "the metric cell of a row cannot be written as JSON"
        raise _cell_error(row, detail) from None
    return cell, None


def _check_args(metric, k, direction, space, max_work):
    """Refuse a bad argument; a missing argument is judged before an invalid one."""
    if metric is None:
        raise HoldoutError(
            "holdout_parameter_missing",
            "the metric has no default and None is not a metric name",
            names=("metric",),
        )
    if k is None:
        raise HoldoutError(
            "holdout_parameter_missing",
            "k has no default and None is not a count",
            names=("k",),
        )
    if direction is None and type(metric) is str and metric != "objective":
        raise HoldoutError(
            "holdout_parameter_missing",
            "a named metric has no recorded direction, so a direction must be given",
            names=("direction",),
        )
    if type(metric) is not str:
        raise HoldoutError(
            "holdout_parameter_invalid",
            "metric must be the name of a recorded metric, as a string",
            names=("metric",),
        )
    if type(k) is not int or k < 1:
        raise HoldoutError(
            "holdout_parameter_invalid",
            "k must be a positive integer",
            names=("k",),
        )
    if direction is not None:
        if type(direction) is not str or direction not in DIRECTIONS:
            raise HoldoutError(
                "holdout_parameter_invalid",
                "direction must be exactly maximize or minimize",
                names=("direction",),
            )
    if max_work is not None and (type(max_work) is not int or max_work < 1):
        raise HoldoutError(
            "holdout_parameter_invalid",
            "max_work must be a positive integer or None",
            names=("max_work",),
        )
    is_space = isinstance(space, (dict, StudySpec))
    if space is not None and not is_space:
        raise HoldoutError(
            "holdout_parameter_invalid",
            "space must be None, a recorded-space object or a StudySpec",
            names=("space",),
        )


def _direction_used(direction: Any, selection: Any, holdout: Any) -> str | None:
    """The direction of the call: the caller's word, or the two recorded ones."""
    if direction is not None:
        return direction
    recorded = (selection.direction, holdout.direction)
    if recorded[0] is None or recorded[0] != recorded[1]:
        raise HoldoutError(
            "holdout_parameter_missing",
            "direction is None and the two sources do not record the same direction",
            names=("direction",),
        )
    return recorded[0]


def _source_document(
    source: Any,
    excluded: dict,
    rows_eligible: int,
    vectors_eligible: int,
    ancestor_rows: int,
) -> dict:
    """One source's object: the counts, the exclusions and the recorded space hash."""
    reasons: dict = {}
    for reason in sorted(excluded):
        ids = sorted(excluded[reason])
        reasons[reason] = {"count": len(ids), "ids": ids}
    recorded = [row.space_hash for row in source.rows if row.space_hash is not None]
    return {
        "index": source.index,
        "rows": len(source.rows),
        "ancestor_rows": ancestor_rows,
        "rows_eligible": rows_eligible,
        "vectors_eligible": vectors_eligible,
        "rows_excluded": len(source.rows) - rows_eligible,
        "excluded": reasons,
        "space_hash": recorded[0] if recorded else None,
    }


def _input_digest(entries: list) -> str:
    """SHA-256 of the sorted per-row digests over the rows of both sources."""
    parts = []
    for index, trial_id, status, value, key in entries:
        text = _canonical([index, trial_id, status, value, key])
        parts.append(hashlib.sha256(text).digest())
    parts.sort()
    return hashlib.sha256(b"".join(parts)).hexdigest()


def holdout_compare(
    selection: Any,
    holdout: Any,
    *,
    metric: Any,
    direction: Any,
    k: Any,
    space: Any = None,
    max_work: Any = None,
) -> dict:
    """Return what ``holdout`` recorded for the best vectors of ``selection``.

    ``selection`` (source 0) and ``holdout`` (source 1) are each whatever
    ``_rows.read_source`` takes: a path to a complete result JSON or to a rows JSONL, a
    parsed result dict or a parsed list of rows. ``metric`` is ``"objective"`` or the
    exact name of a recorded metric, ``direction`` is ``"maximize"`` or ``"minimize"``
    (``None`` only for ``"objective"``, and then only when both sources record the same
    direction) and ``k`` is a positive integer, clipped to the eligible selection
    vector count. ``space`` is used only when no row and no result records one, and
    ``max_work`` is a positive integer bound on the admission figure; both default to
    ``None``.

    Returns the ``pineforge-hpo-holdout/v1`` document (see the module text). Raises
    ``HoldoutError`` for every refusal; nothing is returned for a refused call.
    """
    _check_args(metric, k, direction, space, max_work)
    try:
        selection_source = read_source(selection, 0)
    except RowsError as error:
        raise _from_rows_error(error, 0) from None
    try:
        holdout_source = read_source(holdout, 1)
    except RowsError as error:
        raise _from_rows_error(error, 1) from None
    try:
        shared_space = common_space([selection_source, holdout_source], space)
    except RowsError as error:
        raise _from_rows_error(error) from None
    used_direction = _direction_used(direction, selection_source, holdout_source)
    for source in (selection_source, holdout_source):
        try:
            check_context(source.rows)
        except RowsError as error:
            raise _from_rows_error(error) from None
    ancestors = (_ancestor_ids(selection), _ancestor_ids(holdout))
    selection_ids = {row.trial_id for row in selection_source.rows}
    repeated = sorted(t for t in ancestors[1] if t in selection_ids)
    if repeated:
        raise HoldoutError(
            "holdout_ancestor_rows",
            "the holdout source repeats a trial id of the selection source among its "
            "ancestor rows, so it may be reporting the selection study's own rows "
            f"(selection ancestor rows {len(ancestors[0])}, holdout ancestor rows "
            f"{len(ancestors[1])})",
            [(1, trial_id) for trial_id in repeated],
        )
    ordered = [(0, row) for row in selection_source.rows]
    ordered += [(1, row) for row in holdout_source.rows]
    ordered.sort(key=lambda item: (item[0], item[1].trial_id))
    keys: dict = {}
    for index, row in ordered:
        try:
            keys[(index, row.trial_id)] = vector_key(row.parameters, shared_space)
        except RowsError as error:
            raise HoldoutError(
                _CODES_BY_KIND[error.kind],
                error.detail,
                [(index, row.trial_id)],
                error.names,
            ) from None
    if metric != "objective":
        for source in (selection_source, holdout_source):
            if not source.rows:
                continue
            holders = [row for row in source.rows if metric in (row.metrics or {})]
            if not holders:
                raise HoldoutError(
                    "holdout_metric_unknown",
                    "no row of the "
                    f"{_SOURCE_WORDS[source.index]} source records the metric",
                    names=(metric,),
                )
    cells: dict = {}
    for index, row in ordered:
        cells[(index, row.trial_id)] = _metric_cell(row, metric)
    entries = []
    for index, row in ordered:
        where = (index, row.trial_id)
        entries.append((index, row.trial_id, row.status, cells[where][0], keys[where]))
    digest = _input_digest(entries)
    excluded: dict = {0: {}, 1: {}}
    eligible: dict = {0: [], 1: []}
    for index, row in ordered:
        where = (index, row.trial_id)
        if row.status != "ok":
            reason = f"status_{row.status}"
        elif metric != "objective" and cells[where][1] is None:
            metrics = row.metrics
            if metrics is None or metric not in metrics:
                reason = "metric_not_recorded"
            else:
                reason = "metric_unavailable"
        else:
            eligible[index].append((row.trial_id, cells[where][1], keys[where]))
            continue
        excluded[index].setdefault(reason, []).append(row.trial_id)
    if not eligible[0]:
        counts = ", ".join(
            f"{reason} {len(excluded[0][reason])}" for reason in sorted(excluded[0])
        )
        if selection_source.rows:
            detail = (
                "no eligible row remains in the selection source "
                f"({len(selection_source.rows)} rows read: {counts})"
            )
        else:
            detail = "no eligible row remains in the selection source (no rows read)"
        raise HoldoutError("holdout_no_eligible_rows", detail)
    vectors: dict = {0: {}, 1: {}}
    for index in (0, 1):
        groups: dict = {}
        for trial_id, value, key in sorted(eligible[index]):
            groups.setdefault(key, []).append((trial_id, value))
        conflicting = [key for key, rows in groups.items() if _differs(rows)]
        if conflicting:
            ids = [
                (index, trial_id)
                for trial_id, _, key in eligible[index]
                if key in conflicting
            ]
            raise HoldoutError(
                "holdout_duplicate_vector",
                "two eligible rows of one source share a parameter vector and disagree "
                f"on the metric value ({min(conflicting)})",
                ids,
            )
        for key, rows in groups.items():
            vectors[index][key] = rows[0]
    rows_read = len(selection_source.rows) + len(holdout_source.rows)
    n_sel = len(vectors[0])
    n_hol = len(vectors[1])
    selection_pairs = n_sel * (n_sel - 1) // 2
    holdout_pairs = n_hol * (n_hol - 1) // 2
    work = rows_read + selection_pairs + holdout_pairs
    if max_work is not None and work > max_work:
        raise HoldoutError(
            "holdout_resource_exceeded",
            f"the admission figure {work} (rows read {rows_read} + {selection_pairs} "
            f"pairs of {n_sel} selection vectors + {holdout_pairs} pairs of {n_hol} "
            f"holdout vectors) is above max_work {max_work}",
        )
    selection_order = _best_first(_vector_items(vectors[0]), used_direction)
    selection_rank = {
        key: position + 1 for position, (_, _, key) in enumerate(selection_order)
    }
    k_followed = min(k, n_sel)
    pool = {key: vectors[1][key] for key in vectors[1] if key in vectors[0]}
    pool_order = _best_first(_vector_items(pool), used_direction)
    pool_ranks = _mid_ranks([item[1] for item in pool_order], used_direction)
    holdout_rank = {
        item[2]: pool_ranks[position] for position, item in enumerate(pool_order)
    }
    cut = Fraction(len(pool) + 1, 2)
    followed: list = []
    unmatched: list = []
    matched: list = []
    for _, value, key in selection_order[:k_followed]:
        trial_id = vectors[0][key][0]
        if key in pool:
            holdout_trial_id, holdout_value = pool[key]
            difference = holdout_value - value
            if not math.isfinite(difference):
                raise HoldoutError(
                    "holdout_value_invalid",
                    "the difference of a matched followed vector is not finite",
                    [(0, trial_id), (1, holdout_trial_id)],
                )
            if used_direction == "maximize":
                improved = holdout_value > value
            else:
                improved = holdout_value < value
            followed.append(
                {
                    "selection_trial_id": trial_id,
                    "vector_key": key,
                    "selection_value": value,
                    "selection_rank": selection_rank[key],
                    "holdout_trial_id": holdout_trial_id,
                    "holdout_value": holdout_value,
                    "holdout_rank": _pair(holdout_rank[key]),
                    "difference": difference,
                    "improved": improved,
                }
            )
            matched.append((holdout_rank[key], value, holdout_value))
            continue
        holds = sorted(
            row.trial_id
            for row in holdout_source.rows
            if keys[(1, row.trial_id)] == key
        )
        unmatched.append(
            {
                "selection_trial_id": trial_id,
                "vector_key": key,
                "selection_value": value,
                "selection_rank": selection_rank[key],
                "reason": "holdout_row_not_eligible" if holds else "absent_in_holdout",
                "holdout_ids": holds,
            }
        )
    count = len(matched)
    if count:
        xs = _mid_ranks([entry[1] for entry in matched], used_direction)
        ys = _mid_ranks([entry[2] for entry in matched], used_direction)
        sum_x = sum(xs)
        sum_y = sum(ys)
        xy = count * sum(x * y for x, y in zip(xs, ys)) - sum_x * sum_y
        xx = count * sum(x * x for x in xs) - sum_x * sum_x
        yy = count * sum(y * y for y in ys) - sum_y * sum_y
        terms = {"xy": _pair(xy), "xx": _pair(xx), "yy": _pair(yy)}
        ranks = {"x": [_pair(x) for x in xs], "y": [_pair(y) for y in ys]}
        if count <= 2:
            spearman = None
            undefined = "fewer_than_three_matched"
        elif xx == 0:
            spearman = None
            undefined = "constant_selection_ranks"
        elif yy == 0:
            spearman = None
            undefined = "constant_holdout_ranks"
        else:
            root = math.sqrt(float(Fraction(xy * xy, xx * yy)))
            spearman = root if xy > 0 else (-root if xy < 0 else 0.0)
            undefined = None
    else:
        terms = {"xy": None, "xx": None, "yy": None}
        ranks = {"x": None, "y": None}
        spearman = None
        undefined = "no_matched_vectors"
    better = sum(1 for mid, _, _ in matched if mid <= cut)
    holdout_only = []
    for key, (only_trial_id, _) in vectors[1].items():
        if key not in vectors[0]:
            holdout_only.append(only_trial_id)
    holdout_only.sort()
    documents = [
        _source_document(
            selection_source,
            excluded[0],
            len(eligible[0]),
            n_sel,
            len(ancestors[0]),
        ),
        _source_document(
            holdout_source,
            excluded[1],
            len(eligible[1]),
            n_hol,
            len(ancestors[1]),
        ),
    ]
    return {
        "contract": CONTRACT,
        "status": "ok",
        "metric": metric,
        "direction": used_direction,
        "k": k,
        "k_followed": k_followed,
        "n_sel": n_sel,
        "n_hol": n_hol,
        "selection": documents[0],
        "holdout": documents[1],
        "followed": followed,
        "unmatched": unmatched,
        "n_matched": count,
        "pool": {
            "size": len(pool),
            "median_rank_cut": _pair(cut),
            "holdout_only": {"count": len(holdout_only), "ids": holdout_only},
        },
        "statistics": {
            "spearman": spearman,
            "spearman_terms": terms,
            "spearman_ranks": ranks,
            "spearman_undefined": undefined,
            "better_half": {
                "numerator": better,
                "denominator": count,
                "value": better / count if count else None,
            },
        },
        "algorithm": ALGORITHM,
        "input_sha256": digest,
        "admission": {
            "rows_read": rows_read,
            "selection_pairs": selection_pairs,
            "holdout_pairs": holdout_pairs,
            "work": work,
            "max_work": max_work,
        },
        "interpretation": INTERPRETATION,
    }
