"""Shared reader for finished HPO trial rows.

One reader serves the post-hoc analysis modules. A source is a complete result
object, a rows JSONL file or a list of rows, and every row is checked the way
``continuation`` checks warm-start rows. Each refusal is a ``RowsError`` whose
``kind`` names the rule that failed. Nothing here evaluates a trial, writes a
file or keeps state: the only side effect is reading the one file named to
``read_source``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from itertools import chain
import json
import math
import os
from typing import Any, Iterable

from .continuation import (
    _dump,
    _scalar,
    canonical_space as _canonical_space,
    recorded_space,
)

__all__ = [
    "KINDS",
    "STATUSES",
    "Row",
    "RowsError",
    "Source",
    "canonical_space",
    "check_context",
    "check_unique_ids",
    "common_space",
    "loads_strict",
    "read_source",
    "validate_row",
    "vector_key",
]

KINDS = (
    "input_invalid",
    "rows_incomplete",
    "space_missing",
    "space_mismatch",
    "context_mismatch",
)
STATUSES = (
    "ok",
    "constraint_violation",
    "objective_error",
    "constraint_error",
    "engine_error",
    "trial_error",
    "trial_timeout",
    "pruned",
    "partial",
)

_ID_LIMIT = (1 << 64) - 1
_DIRECTIONS = ("maximize", "minimize")
_COUNTERS = (
    "input_bars_processed",
    "script_bars_processed",
    "input_tf_seconds",
    "script_tf_seconds",
)


class RowsError(Exception):
    """A rows source cannot be read or compared; ``kind`` names the refused rule.

    ``ids`` holds offending trial ids, or (source_index, trial_id) pairs when the
    rows may come from several sources, ascending; ``names`` holds offending
    dimension, metric or field names. Either may be empty.
    """

    kind: str
    detail: str
    ids: tuple
    names: tuple

    def __init__(
        self,
        kind: str,
        detail: str,
        ids: Iterable = (),
        names: Iterable = (),
    ) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown rows error kind: {kind!r}")
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.ids = tuple(sorted(ids))
        self.names = tuple(names)

    def __reduce__(self):
        return (type(self), (self.kind, self.detail, self.ids, self.names))


def _clip(value: Any, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _reject_duplicate_keys(pairs: list) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise RowsError("input_invalid", f"duplicate JSON key {_clip(repr(key))}")
        result[key] = value
    return result


def _reject_constant(token: str) -> None:
    raise RowsError("input_invalid", f"non-finite JSON number {token} is not allowed")


def _parse_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise RowsError(
            "input_invalid", f"JSON number {_clip(token)} overflows to infinity"
        )
    return value


def _parse_int(token: str) -> int:
    value = int(token)
    if len(token) > 300:
        try:
            float(value)
        except OverflowError:
            raise RowsError(
                "input_invalid", f"JSON number {_clip(token)} overflows to infinity"
            ) from None
    return value


def _loads(text: str) -> Any:
    return json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
        parse_float=_parse_float,
        parse_int=_parse_int,
    )


def _parse(text: str, line: int | None = None) -> Any:
    """Parse strictly; ``line`` (1-based) names a JSONL line in the refusal."""
    where = "" if line is None else f"line {line}: "
    try:
        return _loads(text)
    except RowsError as error:
        if line is None:
            raise
        raise RowsError(error.kind, where + error.detail) from None
    except json.JSONDecodeError as error:
        place = (
            f"line {error.lineno} column {error.colno}"
            if line is None
            else f"column {error.colno}"
        )
        raise RowsError(
            "input_invalid", f"{where}invalid JSON ({error.msg}) at {place}"
        ) from None
    except (ValueError, RecursionError) as error:
        raise RowsError(
            "input_invalid", f"{where}unreadable JSON ({_clip(error)})"
        ) from None


def loads_strict(text: str) -> Any:
    """``json.loads`` refusing duplicate keys, NaN/Infinity tokens and overflow.

    Float tokens that overflow to infinity, and integer tokens too large for a
    binary64, raise ``RowsError("input_invalid")`` like every other defect.
    """
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError:
            raise RowsError("input_invalid", "the text is not valid UTF-8") from None
    if not isinstance(text, str):
        raise RowsError("input_invalid", "the text to parse must be a string")
    return _parse(text)


@dataclass(frozen=True)
class Row:
    """One validated trial row; ``raw`` is the parsed object it came from.

    ``parameters``, ``metrics``, ``backtest`` and ``space`` are None when the row
    has no such key. Treat every dict as read-only.
    """

    source_index: int
    trial_id: int
    status: str
    feasible: bool
    objective: float | None
    parameters: dict | None
    metrics: dict | None
    backtest: dict | None
    space_hash: str | None
    space: dict | None
    raw: dict


@dataclass(frozen=True)
class Source:
    """The rows of one result or rows file, in file order, read-only.

    ``space`` is the result's own recorded space (None for a rows-only source);
    ``direction`` is ``maximize`` or ``minimize`` when the result or a space
    records it, else None.
    """

    index: int
    rows: tuple
    space: dict | None
    direction: str | None


def _row_key(row: Row) -> tuple:
    return (row.source_index, row.trial_id)


def validate_row(raw: dict, source_index: int) -> Row:
    """Check one row object (continuation.py:352-371) and return it as a Row."""
    if type(source_index) is not int or source_index < 0:
        raise RowsError(
            "input_invalid", "the source index must be a non-negative integer"
        )
    if not isinstance(raw, dict):
        raise RowsError("input_invalid", "a row must be a JSON object")
    identifier = raw.get("trial_id")
    ids = (identifier,) if type(identifier) is int else ()

    def refuse(detail: str) -> RowsError:
        return RowsError("input_invalid", detail, ids)

    if type(identifier) is not int or not 0 <= identifier < _ID_LIMIT:
        raise refuse("trial_id must be an integer from 0 to 2^64 - 2")
    status = raw.get("status")
    if type(status) is not str or status not in STATUSES:
        raise refuse(f"unknown trial status {_clip(repr(status))}")
    feasible = raw.get("feasible")
    if type(feasible) is not bool or feasible != (status == "ok"):
        raise refuse("feasible must be a boolean equal to (status is ok)")
    if "objective" not in raw:
        raise refuse("the row has no objective")
    value = raw["objective"]
    objective = None
    if value is not None:
        if type(value) not in (int, float):
            raise refuse("objective must be a finite number or null")
        try:
            objective = float(value)
        except OverflowError:
            raise refuse("objective must be a finite number or null") from None
        if not math.isfinite(objective):
            raise refuse("objective must be a finite number or null")
    if objective is None and status in ("ok", "constraint_violation"):
        raise refuse(f"a {status} row needs a finite objective")
    members = {}
    for key in ("parameters", "metrics", "backtest", "space"):
        if key in raw and not isinstance(raw[key], dict):
            raise refuse(f"{key} must be an object when it is present")
        members[key] = raw.get(key)
    space_hash = raw.get("space_hash")
    if "space_hash" in raw and type(space_hash) is not str:
        raise refuse("space_hash must be a string when it is present")
    return Row(
        source_index,
        identifier,
        status,
        feasible,
        objective,
        members["parameters"],
        members["metrics"],
        members["backtest"],
        space_hash,
        members["space"],
        raw,
    )


def check_unique_ids(rows: Iterable[Row]) -> None:
    """Refuse a trial id that repeats inside one source (continuation.py:358-359).

    Rows are keyed by (source_index, trial_id), so rows of several sources may be
    passed together; the repeated ids are plain trial ids when every row belongs
    to one source and (source_index, trial_id) pairs otherwise.
    """
    seen: set = set()
    repeated: set = set()
    sources: set = set()
    for row in rows:
        key = _row_key(row)
        sources.add(row.source_index)
        if key in seen:
            repeated.add(key)
        seen.add(key)
    if repeated:
        ids = (
            [trial_id for _, trial_id in repeated]
            if len(sources) == 1
            else list(repeated)
        )
        raise RowsError("input_invalid", "a trial id repeats inside one source", ids)


def _build_rows(labelled: Iterable, index: int) -> tuple:
    rows = []
    for label, raw in labelled:
        try:
            rows.append(validate_row(raw, index))
        except RowsError as error:
            raise RowsError(
                error.kind, f"{label}: {error.detail}", error.ids, error.names
            ) from None
    check_unique_ids(rows)
    return tuple(rows)


def _space_direction(space: Any) -> str | None:
    objective = space.get("objective") if isinstance(space, dict) else None
    direction = objective.get("direction") if isinstance(objective, dict) else None
    return direction if direction in _DIRECTIONS else None


def _common_direction(rows: tuple) -> str | None:
    found = {_space_direction(row.space) for row in rows if row.space is not None}
    found.discard(None)
    return found.pop() if len(found) == 1 else None


def _rows_source(index: int, labelled: Iterable) -> Source:
    rows = _build_rows(labelled, index)
    return Source(index, rows, None, _common_direction(rows))


def _array_source(array: list, index: int) -> Source:
    return _rows_source(
        index, ((f"rows[{position}]", raw) for position, raw in enumerate(array))
    )


def _jsonl_rows(numbered: Iterable):
    for number, line in numbered:
        yield f"line {number}", _parse(line, number)


def _result_direction(result: dict, space: dict | None) -> str | None:
    recorded = _space_direction(space)
    if "direction" not in result:
        return recorded
    direction = result["direction"]
    if type(direction) is not str or direction not in _DIRECTIONS:
        raise RowsError(
            "input_invalid", "the result direction must be maximize or minimize"
        )
    if recorded is not None and recorded != direction:
        raise RowsError(
            "input_invalid",
            "the result direction differs from the direction of its space",
        )
    return direction


def _result_source(result: dict, index: int) -> Source:
    """A complete result: rows are warm_start_trials + trials (continuation.py:271-289)."""
    if result.get("trials_out", "all") != "all":
        raise RowsError(
            "rows_incomplete",
            "the result keeps only some of its trials (trials_out is not all)",
        )
    own = result.get("trials")
    inherited = result.get("warm_start_trials", [])
    if not isinstance(own, list) or not isinstance(inherited, list):
        raise RowsError(
            "input_invalid",
            "a result needs a trials array and, when present, a warm_start_trials array",
        )
    if "trials_completed" in result and (
        type(result["trials_completed"]) is not int
        or result["trials_completed"] != len(own)
    ):
        raise RowsError(
            "rows_incomplete",
            "trials_completed does not equal the number of trials in the result",
        )
    if "warm_start" in result:
        warm = result["warm_start"]
        count = warm.get("trials") if isinstance(warm, dict) else None
        if type(count) is not int or count != len(inherited):
            raise RowsError(
                "rows_incomplete",
                "warm_start.trials does not equal the number of ancestor rows in the result",
            )
    space = result.get("space")
    if "space" in result and not isinstance(space, dict):
        raise RowsError(
            "input_invalid", "the result space must be an object when it is present"
        )
    direction = _result_direction(result, space)
    labelled = chain(
        ((f"warm_start_trials[{position}]", raw) for position, raw in enumerate(inherited)),
        ((f"trials[{position}]", raw) for position, raw in enumerate(own)),
    )
    return Source(index, _build_rows(labelled, index), space, direction)


def _from_value(value: Any, index: int, line: int | None) -> Source:
    if isinstance(value, dict) and ("trials" in value or "trials_out" in value):
        return _result_source(value, index)
    if isinstance(value, list):
        return _array_source(value, index)
    place = "the document" if line is None else f"line {line}"
    if not isinstance(value, dict):
        raise RowsError(
            "input_invalid",
            f"{place} must hold a result, a list of rows or a row object",
        )
    return _rows_source(index, [(place, value)])


def _content_lines(text: str):
    """Yield (line number, line) for each non-blank line, splitting on LF only."""
    start = 0
    number = 0
    while start <= len(text):
        stop = text.find("\n", start)
        if stop < 0:
            stop = len(text)
        number += 1
        line = text[start:stop]
        if line.strip(" \t\r"):
            yield number, line
        start = stop + 1


def _is_one_value(line: str) -> bool:
    """Whether a line is one complete JSON value, judged by syntax alone."""
    try:
        json.loads(line)
    except json.JSONDecodeError:
        return False
    except (ValueError, RecursionError):
        return True
    return True


def _source_from_text(text: str, index: int) -> Source:
    """Read a source from its whole text; the layout of the text never matters."""
    lines = _content_lines(text)
    first = next(lines, None)
    if first is None:
        return Source(index, (), None, None)
    second = next(lines, None)
    if second is None:
        return _from_value(_parse(first[1], first[0]), index, first[0])
    if _is_one_value(first[1]):
        return _rows_source(index, _jsonl_rows(chain((first, second), lines)))
    return _from_value(_parse(text), index, None)


def _file_source(path: Any, index: int) -> Source:
    try:
        with open(path, "rb") as stream:
            content = stream.read()
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise RowsError(
            "input_invalid", f"cannot read {_clip(os.fspath(path), 120)}: {reason}"
        ) from None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise RowsError("input_invalid", "the source file is not valid UTF-8") from None
    return _source_from_text(text, index)


def read_source(source: Any, index: int = 0) -> Source:
    """Read one source into a Source whose rows are validated and in file order.

    ``source`` is a path to a result JSON or a rows JSONL, an already-parsed
    result dict, or an already-parsed list of rows. A result must keep every
    trial (``trials_out`` all or absent) and its counts must agree with its rows,
    else ``rows_incomplete``. A JSONL file holds one row object per non-blank line;
    a bad line is refused with its 1-based line number. A source with no rows is
    returned empty. Rows from several sources are told apart by ``index``.
    """
    if type(index) is not int or index < 0:
        raise RowsError(
            "input_invalid", "the source index must be a non-negative integer"
        )
    if isinstance(source, dict):
        return _result_source(source, index)
    if isinstance(source, list):
        return _array_source(source, index)
    if isinstance(source, (str, os.PathLike)):
        return _file_source(source, index)
    raise RowsError(
        "input_invalid", "a source must be a path, a result object or a list of rows"
    )


def canonical_space(space: dict) -> str:
    """Canonical v1 JSON of a recorded space (the form of continuation.py:143-181)."""
    try:
        return _canonical_space(space)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
        raise RowsError(
            "input_invalid",
            f"the recorded space is malformed ({_clip(type(error).__name__)}: {_clip(error)})",
        ) from None


def _passed(space: Any) -> dict:
    if isinstance(space, dict):
        return space
    try:
        return recorded_space(space)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RowsError(
            "input_invalid", "the passed space is neither a recorded space nor a StudySpec"
        ) from None


def _differing_names(expected: str, others: Iterable[str]) -> list:
    base = json.loads(expected)["parameters"]
    names: set = set()
    for other in others:
        parameters = json.loads(other)["parameters"]
        for name in base.keys() | parameters.keys():
            if base.get(name) != parameters.get(name):
                names.add(name)
    return sorted(names)


def common_space(sources: Iterable[Source], passed_space: Any = None) -> dict:
    """Return the one recorded space shared by every row and result of ``sources``.

    A row's own ``space`` and a result's ``space`` must all have the same canonical
    form (which includes the objective, its direction and its constraints), else
    ``space_mismatch`` naming the dimensions that differ and the offending rows.
    When nothing records a space, ``passed_space`` (a recorded-space dict or a
    StudySpec) is used; with none passed the call is refused as ``space_missing``.
    A passed space is not compared with recorded ones. Rows or sources that record
    no space are not checked against it.
    """
    holders = []
    for source in sources:
        if source.space is not None:
            holders.append((source.index, -1, source.space))
        holders.extend(
            (row.source_index, row.trial_id, row.space)
            for row in source.rows
            if row.space is not None
        )
    if not holders:
        if passed_space is None:
            raise RowsError(
                "space_missing",
                "no row or result records its space and none was passed",
            )
        space = _passed(passed_space)
        canonical_space(space)
        return copy.deepcopy(space)
    forms: dict = {}

    def form(space: Any) -> str:
        try:
            text = json.dumps(space, sort_keys=True)
        except (TypeError, ValueError):
            return canonical_space(space)
        if text not in forms:
            forms[text] = canonical_space(space)
        return forms[text]

    reference = min(holders, key=lambda holder: holder[:2])
    expected = form(reference[2])
    apart = [holder for holder in holders if form(holder[2]) != expected]
    if apart:
        involved = sorted({reference[0], *(holder[0] for holder in apart)})
        raise RowsError(
            "space_mismatch",
            "rows or results record different spaces "
            f"(sources {', '.join(str(number) for number in involved)})",
            [holder[:2] for holder in apart if holder[1] >= 0],
            _differing_names(expected, {form(holder[2]) for holder in apart}),
        )
    return copy.deepcopy(reference[2])


def _counters(row: Row) -> tuple:
    return tuple(row.backtest.get(name) for name in _COUNTERS)


def check_context(rows: Iterable[Row]) -> None:
    """Refuse rows whose scoring context visibly differs (necessary, not sufficient).

    Among rows with status ``ok`` that carry a ``backtest`` object, the tuple
    (input_bars_processed, script_bars_processed, input_tf_seconds,
    script_tf_seconds) must be equal; every row that carries a ``space_hash`` must
    carry the same one. Equal counters and hashes do not prove the same data,
    window, fixed inputs or strategy (study-spec.md:1018-1020). The refusal names
    the rows as (source_index, trial_id) pairs, starting from the lowest pair.
    """
    rows = list(rows)
    scored = [
        (row, _counters(row))
        for row in rows
        if row.status == "ok" and row.backtest is not None
    ]
    if scored:
        reference, expected = min(scored, key=lambda pair: _row_key(pair[0]))
        apart = [row for row, counters in scored if counters != expected]
        if apart:
            differing = [
                name
                for position, name in enumerate(_COUNTERS)
                if any(counters[position] != expected[position] for _, counters in scored)
            ]
            raise RowsError(
                "context_mismatch",
                "scored rows disagree on their backtest bar and timeframe counters",
                [_row_key(row) for row in [reference, *apart]],
                sorted(differing),
            )
    hashed = [row for row in rows if row.space_hash is not None]
    if hashed:
        reference = min(hashed, key=_row_key)
        apart = [row for row in hashed if row.space_hash != reference.space_hash]
        if apart:
            raise RowsError(
                "context_mismatch",
                "rows record different space_hash values",
                [_row_key(row) for row in [reference, *apart]],
                ("space_hash",),
            )


def _typed(name: str, value: Any, dimension: Any) -> list:
    """The typed scalar of continuation.py:119-130 for one parameter value."""
    kind = dimension.get("kind") if isinstance(dimension, dict) else None
    try:
        if kind == "integer":
            valid = type(value) is int
        elif kind == "real":
            valid = type(value) in (int, float)
            if valid:
                value = float(value)
        elif kind == "boolean":
            valid = type(value) is bool
        elif kind == "categorical":
            choices = dimension["choices"]
            valid = any(
                type(value) is type(choice) and value == choice for choice in choices
            )
            if not valid and type(value) is int:
                for choice in choices:
                    if type(choice) is float and float(value) == choice:
                        value = float(value)
                        valid = True
                        break
        else:
            valid = False
        if not valid:
            raise ValueError("the value does not fit the dimension kind")
        return _scalar(value)
    except (ValueError, KeyError, TypeError, OverflowError):
        raise RowsError(
            "input_invalid",
            f"parameter {_clip(repr(name))} does not fit its dimension",
            names=(name,),
        ) from None


def vector_key(parameters: dict, space: dict) -> str:
    """Canonical JSON key of a parameter vector (continuation.py:112-140, 363).

    Each value becomes a typed scalar by its dimension's kind in ``space``; reals
    are binary64 hex with -0.0 taken as +0.0 and an int in a real dimension taken
    as the same real. Equality is exact: no tolerance and no lattice snapping.
    """
    dimensions = space.get("parameters") if isinstance(space, dict) else None
    if not isinstance(dimensions, dict):
        raise RowsError("input_invalid", "the space has no parameters object")
    if not isinstance(parameters, dict) or parameters.keys() != dimensions.keys():
        given = set(parameters) if isinstance(parameters, dict) else set()
        raise RowsError(
            "input_invalid",
            "the parameter names differ from the space dimensions",
            names=sorted(given ^ set(dimensions)),
        )
    return _dump(
        {
            name: _typed(name, parameters[name], dimension)
            for name, dimension in dimensions.items()
        }
    )
