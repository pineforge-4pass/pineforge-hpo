"""Post hoc walk-forward analysis of finished per-window studies.

``walkforward(windows, sources, *, metric, direction, space=None, max_work=None)``
reads the caller's ordered plan and one finished study per supplied window. It runs
no window, evaluates nothing, writes no file, keeps no state and changes no native
result. v1 analyses finished studies; native in-loop walk-forward comes later.

Contract ``pineforge-hpo-walkforward/v1``, algorithm ``exact_per_window_v1``:

Only ``ok`` rows with finite metric cells compete. Other statuses are excluded as
``status_<status>``, absent cells as ``metric_not_recorded`` and null cells as
``metric_unavailable``, in that order. An invalid cell on an ok row, or a non-finite
or JSON-unwritable cell on any row, refuses the call. Numeric cells are binary64;
booleans are not numbers. The metric is ``objective`` or an exact recorded name.

Selection minimises ``(sign * value, trial_id)``, with sign -1.0 for maximize and
+1.0 for minimize. A supplied window with no eligible row has no selection; none
anywhere refuses. Missing sources appear in ``never_run`` in plan order. Gaps
between consecutive supplied windows appear in ``chain_breaks``.

For adjacent supplied windows whose first has a selection, its exact parameter
key is looked up in the target's rows. Every such pair appears, including a target
without eligible rows. Eligible matches must have one value; unequal values refuse,
otherwise the lowest id is reported with ``row_found`` true. Its mid-rank is
``better + (equal + 1) / 2`` among target eligible rows. Without an eligible match,
``row_found`` is false, numeric fields are null, and the reason is that of the
lowest-id matching row, or ``no_row``. When ``row_found`` is true, parameters belong
to the matching row; when false, they are the source window's selected parameters
(the vector looked up). No value is inferred.

Stability lists selected parameters as recorded, in plan order, with distinct
typed-scalar counts and exact numeric minimum and maximum for both integer and real
dimensions, keeping the first recorded value on equality. Boolean and categorical
extremes are null. Real identity equates integer and real spellings and signed zeros.
There is no tolerance, averaging, interpolation, threshold or robustness verdict.

The shared reader checks each supplied study and its context separately. Recorded
spaces and row space hashes must agree across windows; bar and timeframe counters
are compared only inside a window. Top-level result hashes are not compared. For
objective, the recorded common space's direction is checked first, then source
directions; a passed space's direction is not compared.
Hash-mismatch ``ids`` ascend: one id per differing row, so an id that occurs in
several windows is listed once for each.

Refusals raise ``WalkforwardError`` with its refused ``document()``. The first
defect decides: missing options before invalid options; unknown labels; no source;
reading and each window's context; recorded spaces then row hashes; direction;
metric name; cells; every row's parameters; carried duplicates; no eligible row;
admission figure. The figure is rows read + sum e(e-1)/2 + sum target eligible
counts over carry-over pairs, bounded only by a caller-supplied ``max_work``.

The document and digest ignore row and mapping insertion order. The digest is
SHA-256 of sorted raw per-row SHA-256 digests of compact, key-sorted ASCII JSON
``[window label, trial_id, status, cell]``. It covers every status, normalises numeric
cells to binary64, writes absent/null as null, and preserves other JSON cells on
excluded rows. It omits metric name, direction and plan position. Listings follow
the plan, dimensions follow code-point name order, exclusion status reasons precede the
missing and null metric reasons, and ids ascend. ``interpretation`` is fixed.
"""

from __future__ import annotations

from collections.abc import Mapping
import copy
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Iterable

from ._rows import (
    Row,
    RowsError,
    Source,
    canonical_space,
    check_context,
    common_space,
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
    "WalkforwardError",
    "walkforward",
]

CONTRACT = "pineforge-hpo-walkforward/v1"
ALGORITHM = "exact_per_window_v1"
DIRECTIONS = ("maximize", "minimize")
CODES = (
    "walkforward_parameter_missing",
    "walkforward_parameter_invalid",
    "walkforward_window_unknown",
    "walkforward_no_supplied_sources",
    "walkforward_input_invalid",
    "walkforward_rows_incomplete",
    "walkforward_space_missing",
    "walkforward_space_mismatch",
    "walkforward_context_mismatch",
    "walkforward_direction_mismatch",
    "walkforward_metric_unknown",
    "walkforward_duplicate_vector",
    "walkforward_no_eligible_rows",
    "walkforward_resource_exceeded",
)

INTERPRETATION = (
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


class WalkforwardError(Exception):
    """A contract refusal, with ascending trial ``ids`` and ordered ``names``."""

    def __init__(
        self, code: str, detail: str, ids: Iterable = (), names: Iterable = ()
    ) -> None:
        if code not in CODES:
            raise ValueError(f"unknown walkforward refusal code: {code!r}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.ids = tuple(sorted(ids))
        self.names = tuple(names)

    def __reduce__(self):
        return (type(self), (self.code, self.detail, self.ids, self.names))

    def document(self) -> dict:
        """Render the refused document; a refused call returns no analysis."""
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


def _reader_error(error: RowsError, labels: Iterable = ()) -> WalkforwardError:
    ids = [item[1] if isinstance(item, tuple) else item for item in error.ids]
    return WalkforwardError(
        "walkforward_" + error.kind, error.detail, ids, (*labels, *error.names)
    )


def _options(windows, sources, metric, direction, space, max_work) -> None:
    required = {
        "windows": windows,
        "sources": sources,
        "metric": metric,
        "direction": direction,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise WalkforwardError(
            "walkforward_parameter_missing",
            "required arguments are None",
            names=missing,
        )
    invalid = []
    repeated = []
    if not isinstance(windows, (list, tuple)) or not windows:
        invalid.append("windows")
    elif any(type(label) is not str or not label for label in windows):
        invalid.append("windows")
    else:
        seen = set()
        for label in windows:
            if label in seen and label not in repeated:
                repeated.append(label)
            seen.add(label)
        if repeated:
            invalid.append("windows")
    if not isinstance(sources, Mapping):
        invalid.append("sources")
    if type(metric) is not str:
        invalid.append("metric")
    if type(direction) is not str or direction not in DIRECTIONS:
        invalid.append("direction")
    if space is not None and not isinstance(space, (dict, StudySpec)):
        invalid.append("space")
    if max_work is not None and (type(max_work) is not int or max_work < 1):
        invalid.append("max_work")
    if invalid:
        raise WalkforwardError(
            "walkforward_parameter_invalid",
            "invalid plan or options",
            names=(*invalid, *repeated),
        )


def _holders(loaded: list[Source]) -> list:
    holders = []
    for source in loaded:
        if source.space is not None:
            holders.append((source.index, -1, source.space))
        holders.extend(
            (source.index, row.trial_id, row.space)
            for row in source.rows
            if row.space is not None
        )
    return sorted(holders, key=lambda holder: holder[:2])


def _space(loaded: list[Source], windows: list, passed: Any) -> tuple:
    holders = _holders(loaded)
    try:
        shared = common_space(loaded, passed)
    except RowsError as error:
        if error.kind == "space_mismatch":
            expected = canonical_space(holders[0][2])
            involved = {holders[0][0]}
            involved.update(
                index
                for index, _, recorded in holders
                if canonical_space(recorded) != expected
            )
            labels = [windows[index] for index in sorted(involved)]
        elif error.kind == "input_invalid":
            labels = []
            for index, _, recorded in holders:
                try:
                    canonical_space(recorded)
                except RowsError:
                    labels = [windows[index]]
                    break
        else:
            labels = []
        raise _reader_error(error, labels) from None
    hashed = sorted(
        (row for source in loaded for row in source.rows if row.space_hash is not None),
        key=lambda row: (row.source_index, row.trial_id),
    )
    if hashed:
        reference = hashed[0]
        apart = [row for row in hashed if row.space_hash != reference.space_hash]
        if apart:
            indices = {reference.source_index, *(row.source_index for row in apart)}
            raise WalkforwardError(
                "walkforward_space_mismatch",
                "rows of supplied windows record different space_hash values",
                [row.trial_id for row in apart],
                [*(windows[index] for index in sorted(indices)), "space_hash"],
            )
    return shared, bool(holders)


def _direction(shared, recorded, loaded, windows, metric, direction) -> None:
    if metric != "objective":
        return
    if recorded:
        own = shared["objective"]["direction"]
        if own in DIRECTIONS and own != direction:
            raise WalkforwardError(
                "walkforward_direction_mismatch",
                "the common recorded space's objective direction differs",
                names=("direction",),
            )
    for source in loaded:
        if source.direction is not None and source.direction != direction:
            raise WalkforwardError(
                "walkforward_direction_mismatch",
                "the source's recorded direction differs",
                names=(windows[source.index], "direction"),
            )


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


@dataclass
class _Entry:
    row: Row
    value: Any
    reason: str | None
    key: str = ""


@dataclass
class _Window:
    label: str
    index: int
    entries: list[_Entry]
    eligible: list[_Entry]
    selected: _Entry | None = None


def _cells(source: Source, label: str, metric: str, digests: list) -> _Window:
    entries = []
    invalid = []
    for row in sorted(source.rows, key=lambda row: row.trial_id):
        present = metric == "objective" or (
            row.metrics is not None and metric in row.metrics
        )
        cell = (
            row.objective
            if metric == "objective"
            else row.metrics[metric]
            if present
            else None
        )
        try:
            if type(cell) in (int, float):
                cell = float(cell)
                if not math.isfinite(cell):
                    raise ValueError("non-finite cell")
            elif row.status == "ok" and cell is not None:
                raise ValueError("cell is not a number")
            digest = hashlib.sha256(
                _canonical([label, row.trial_id, row.status, cell])
            ).digest()
        except (TypeError, ValueError, OverflowError, RecursionError):
            invalid.append(row.trial_id)
            continue
        digests.append(digest)
        if row.status != "ok":
            reason = "status_" + row.status
        elif not present:
            reason = "metric_not_recorded"
        elif cell is None:
            reason = "metric_unavailable"
        else:
            reason = None
        entries.append(_Entry(row, cell, reason))
    if invalid:
        raise WalkforwardError(
            "walkforward_input_invalid",
            "a metric cell is invalid or cannot be written as JSON",
            invalid,
            (label, metric),
        )
    return _Window(
        label,
        source.index,
        entries,
        [entry for entry in entries if entry.reason is None],
    )


def _per_window(window: _Window) -> dict:
    reasons = {}
    for entry in window.entries:
        if entry.reason is not None:
            reasons.setdefault(entry.reason, []).append(entry.row.trial_id)
    ordered = sorted(reason for reason in reasons if reason.startswith("status_"))
    ordered.extend(
        reason
        for reason in ("metric_not_recorded", "metric_unavailable")
        if reason in reasons
    )
    selected = window.selected
    return {
        "window": window.label,
        "rows_read": len(window.entries),
        "rows_eligible": len(window.eligible),
        "rows_excluded": len(window.entries) - len(window.eligible),
        "excluded": {
            reason: {"count": len(reasons[reason]), "ids": reasons[reason]}
            for reason in ordered
        },
        "selected": None
        if selected is None
        else {
            "trial_id": selected.row.trial_id,
            "value": selected.value,
            "parameters": copy.deepcopy(selected.row.parameters),
        },
        "no_eligible_rows": selected is None,
    }


def _matches(source: _Window, target: _Window) -> list[_Entry]:
    return [entry for entry in target.entries if entry.key == source.selected.key]


def _carry(source: _Window, target: _Window, direction: str) -> dict:
    carried = source.selected
    matches = _matches(source, target)
    eligible = [entry for entry in matches if entry.reason is None]
    found = eligible[0] if eligible else None
    result = {
        "from": source.label,
        "to": target.label,
        "vector_key": carried.key,
        "parameters": copy.deepcopy(
            found.row.parameters if found is not None else carried.row.parameters
        ),
        "row_found": found is not None,
        "trial_id": None,
        "value": None,
        "better": None,
        "equal": None,
        "rank_mid": None,
        "reason": None
        if found is not None
        else matches[0].reason
        if matches
        else "no_row",
    }
    if found is not None:
        better = sum(
            entry.value > found.value
            if direction == "maximize"
            else entry.value < found.value
            for entry in target.eligible
        )
        equal = sum(entry.value == found.value for entry in target.eligible)
        result.update(
            trial_id=found.row.trial_id,
            value=found.value,
            better=better,
            equal=equal,
            rank_mid=better + (equal + 1) / 2,
        )
    return result


def _stability(states: list[_Window], shared: dict) -> dict:
    selected = [state for state in states if state.selected is not None]
    dimensions = {}
    for name in sorted(shared["parameters"]):
        kind = shared["parameters"][name]["kind"]
        values = [state.selected.row.parameters[name] for state in selected]
        identities = {tuple(json.loads(state.selected.key)[name]) for state in selected}
        dimensions[name] = {
            "kind": kind,
            "values": copy.deepcopy(values),
            "distinct": len(identities),
            "minimum": min(values) if kind in ("integer", "real") else None,
            "maximum": max(values) if kind in ("integer", "real") else None,
        }
    return {
        "windows": [state.label for state in selected],
        "windows_without_selection": [
            state.label for state in states if state.selected is None
        ],
        "dimensions": dimensions,
    }


def walkforward(
    windows: Any,
    sources: Any,
    *,
    metric: str,
    direction: str,
    space: Any = None,
    max_work: int | None = None,
) -> dict:
    """Summarise finished studies in plan order; raise ``WalkforwardError`` on refusal.

    ``windows`` is a non-empty list or tuple of distinct non-empty labels, and
    ``sources`` maps supplied labels to complete results, rows lists or file paths.
    ``metric`` and ``direction`` are required; there is no inferred direction.
    ``space`` is used only when no source records one. ``max_work`` bounds the
    declared admission figure, with equality admitted and None unbounded.
    """
    _options(windows, sources, metric, direction, space, max_work)
    windows = list(windows)
    unknown = [label for label in sources if label not in windows]
    if unknown:
        raise WalkforwardError(
            "walkforward_window_unknown",
            "sources holds labels absent from the plan",
            names=sorted(unknown, key=lambda label: (str(label), type(label).__name__)),
        )
    supplied = [label for label in windows if label in sources]
    if not supplied:
        raise WalkforwardError(
            "walkforward_no_supplied_sources", "no plan window has a supplied source"
        )
    loaded = []
    for index, label in enumerate(windows):
        if label not in sources:
            continue
        try:
            source = read_source(sources[label], index)
            check_context(source.rows)
        except RowsError as error:
            raise _reader_error(error, (label,)) from None
        loaded.append(source)
    shared, recorded = _space(loaded, windows, space)
    _direction(shared, recorded, loaded, windows, metric, direction)
    rows_read = sum(len(source.rows) for source in loaded)
    if (
        rows_read
        and metric != "objective"
        and not any(
            row.metrics is not None and metric in row.metrics
            for source in loaded
            for row in source.rows
        )
    ):
        raise WalkforwardError(
            "walkforward_metric_unknown",
            "the metric name is recorded in no row's metrics object",
            names=(metric,),
        )
    digests = []
    states = [
        _cells(source, windows[source.index], metric, digests) for source in loaded
    ]
    for state in states:
        for entry in state.entries:
            try:
                entry.key = vector_key(entry.row.parameters, shared)
            except RowsError as error:
                raise WalkforwardError(
                    "walkforward_input_invalid",
                    error.detail,
                    (entry.row.trial_id,),
                    (state.label, *error.names),
                ) from None
    sign = -1.0 if direction == "maximize" else 1.0
    for state in states:
        if state.eligible:
            state.selected = min(
                state.eligible,
                key=lambda entry: (sign * entry.value, entry.row.trial_id),
            )
    pairs = []
    breaks = []
    for source, target in zip(states, states[1:]):
        if target.index != source.index + 1:
            breaks.append(
                {
                    "from": source.label,
                    "to": target.label,
                    "skipped": windows[source.index + 1 : target.index],
                }
            )
        elif source.selected is not None:
            pairs.append((source, target))
    for source, target in pairs:
        eligible = [entry for entry in _matches(source, target) if entry.reason is None]
        if eligible and any(entry.value != eligible[0].value for entry in eligible):
            raise WalkforwardError(
                "walkforward_duplicate_vector",
                "eligible rows carrying the selected vector record different values",
                [entry.row.trial_id for entry in eligible],
                (target.label,),
            )
    rows_eligible = sum(len(state.eligible) for state in states)
    if not rows_eligible:
        raise WalkforwardError(
            "walkforward_no_eligible_rows", "no supplied window holds an eligible row"
        )
    work = (
        rows_read
        + sum(len(state.eligible) * (len(state.eligible) - 1) // 2 for state in states)
        + sum(len(target.eligible) for _, target in pairs)
    )
    if max_work is not None and work > max_work:
        raise WalkforwardError(
            "walkforward_resource_exceeded",
            f"the admission figure {work} is above max_work {max_work}",
        )
    return {
        "contract": CONTRACT,
        "status": "ok",
        "metric": metric,
        "direction": direction,
        "windows": windows,
        "supplied": supplied,
        "never_run": [label for label in windows if label not in sources],
        "rows_read": rows_read,
        "rows_eligible": rows_eligible,
        "rows_excluded": rows_read - rows_eligible,
        "per_window": [_per_window(state) for state in states],
        "carry_over": [_carry(source, target, direction) for source, target in pairs],
        "chain_breaks": breaks,
        "stability": _stability(states, shared),
        "work": work,
        "algorithm": ALGORITHM,
        "input_sha256": hashlib.sha256(b"".join(sorted(digests))).hexdigest(),
        "interpretation": INTERPRETATION,
    }
