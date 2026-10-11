"""Neighbourhood stability of finished trials, read after the fact (version 1).

A candidate's neighbours are the parameter vectors one move away from it on a
single dimension: one lattice step, the other values, a fraction of the range
or a ratio, by the kind of the dimension. The module looks the neighbours up in
the rows it is given and reports how many of them score near the best row. It
evaluates nothing and starts nothing: a neighbour that has no row is returned
as a missing vector, and the caller may evaluate it and call again.

Rows are read by ``_rows`` and every row is checked against the recorded space.
Every refusal is a ``NeighbourhoodError`` with a module-local code and no
number. The result document is a pure function of the rows, the space, the
centres and the caller's rules: any row order gives the same bytes when it is
written as JSON with sorted keys. See ``docs/neighbourhood.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import math
from typing import Any, NamedTuple

from ._rows import (
    Row,
    RowsError,
    Source,
    check_context,
    common_space,
    read_source,
    vector_key,
)
from .study_spec import ParameterSpec, _finite_parameter_cardinality, _fused_grid_value

__all__ = [
    "CLASSES",
    "CODES",
    "CONTRACT",
    "INTERPRETATION",
    "NEVER_EVALUATED",
    "SCORED_INFEASIBLE",
    "SCORED_OK",
    "UNSCORABLE",
    "Dimension",
    "Moves",
    "NeighbourhoodError",
    "Study",
    "dimension",
    "name_key",
    "neighbourhood",
    "prepare",
    "top_ids",
]

CONTRACT = "pineforge-hpo-neighbourhood/v1"

CODES = (
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
)

SCORED_OK = "scored_ok"
SCORED_INFEASIBLE = "scored_infeasible"
UNSCORABLE = "unscorable"
NEVER_EVALUATED = "never_evaluated"
CLASSES = (SCORED_OK, SCORED_INFEASIBLE, UNSCORABLE, NEVER_EVALUATED)

INTERPRETATION = (
    "This describes the trials that were supplied and nothing else. "
    "It states no significance, confidence or probability. "
    "A neighbour that was never evaluated is not imputed, and nothing is said "
    "about it. "
    "The reference value is the best of many trials, so it is optimistic. "
    "With an adaptive sampler the neighbours that exist are not a random draw: "
    "the share near the best describes its stated neighbourhood only when "
    "coverage is 1. "
    "Every number is in-sample. "
    "Constraints, relations between parameters and whether two runs used the "
    "same data are not checked."
)

_SCORED_STATUSES = ("ok", "constraint_violation")
_DIRECTIONS = ("maximize", "minimize")
_BAND_MODES = ("absolute", "relative")


class NeighbourhoodError(Exception):
    """A refusal of the neighbourhood analysis; ``code`` names the refused rule.

    ``ids`` holds the offending rows as (source_index, trial_id) pairs,
    ascending; ``names`` holds offending dimension or parameter names. Either may
    be empty. No number of the analysis accompanies a refusal.
    """

    code: str
    detail: str
    ids: tuple
    names: tuple

    def __init__(
        self,
        code: str,
        detail: str,
        ids: Any = (),
        names: Any = (),
    ) -> None:
        if code not in CODES:
            raise ValueError(f"unknown neighbourhood error code: {code!r}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.ids = tuple(sorted(tuple(item) for item in ids))
        self.names = tuple(names)

    def __reduce__(self):
        return (type(self), (self.code, self.detail, self.ids, self.names))

    def document(self) -> dict:
        """The refusal as a JSON-ready document that carries no number."""
        return {
            "contract": CONTRACT,
            "status": "refused",
            "code": self.code,
            "detail": self.detail,
            "ids": [list(item) for item in self.ids],
            "names": list(self.names),
        }


def _refusal(name: str, detail: str, ids: Any = (), names: Any = ()):
    return NeighbourhoodError(f"neighbourhood_{name}", detail, ids, names)


def _from_rows_error(error: RowsError, index: int | None = None):
    """Map a reader error to the matching code; bare ids belong to ``index``."""
    ids = [item if isinstance(item, tuple) else (index, item) for item in error.ids]
    return NeighbourhoodError(
        f"neighbourhood_{error.kind}", error.detail, ids, error.names
    )


def _clip(value: Any, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def name_key(name: str) -> bytes:
    """Sort key of a dimension name: its UTF-8 bytes (byte-wise name order)."""
    return name.encode("utf-8", "surrogatepass")


def _row_id(row: Row) -> tuple:
    return (row.source_index, row.trial_id)


def _number(value: Any) -> float | None:
    """A finite int or float as a float; None for anything else (bool included)."""
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


class Moves(NamedTuple):
    """The neighbours of one value on one dimension, lower first, and the drops.

    ``bounds`` counts candidate moves outside the bounds or beyond the last
    lattice ordinal; ``same_as_centre`` counts moves that land on the value
    itself (a log integer rounding back, or a real step too small to move).
    """

    values: tuple
    bounds: int
    same_as_centre: int


def _within(candidates: Any, value: Any, low: Any, high: Any) -> Moves:
    kept = []
    bounds = same = 0
    for candidate in candidates:
        if candidate == value:
            same += 1
        elif low <= candidate <= high:
            kept.append(candidate + 0.0 if type(candidate) is float else candidate)
        else:
            bounds += 1
    return Moves(tuple(kept), bounds, same)


@dataclass(frozen=True)
class Dimension:
    """One search dimension of the recorded space, with its neighbour rule.

    ``rule`` names how a value moves: ``step`` (integer lattice), ``ratio_rounded``
    (log integer), ``ordinal`` (stepped real), ``fraction`` (continuous real),
    ``ratio`` (log real), ``other_value`` (boolean), ``other_choices``
    (categorical) or ``constant`` (exactly one legal value, no neighbours).
    ``last`` is the last lattice ordinal of a stepped real.
    """

    name: str
    kind: str
    rule: str
    low: Any = None
    high: Any = None
    step: Any = None
    log: bool = False
    choices: tuple = ()
    last: int | None = None

    @property
    def constant(self) -> bool:
        return self.rule == "constant"

    def decode(self, ordinal: int) -> float:
        """Value of a stepped real at ``ordinal``: one fused multiply-add.

        The last ordinal snaps to ``high`` when the product is above it (the
        study spec lets it exceed ``high`` by one representable step).
        """
        value = _fused_grid_value(ordinal, self.step, self.low)
        if ordinal == self.last and value > self.high:
            return self.high
        return value

    def ordinal(self, value: float) -> int:
        """Nearest lattice ordinal of a stepped-real value (continuation.py:228)."""
        return round((value - self.low) / self.step)

    def normalise(self, value: Any) -> Any:
        """The value as this dimension holds it; ValueError when it does not fit.

        The checks are those of the continuation loader for one parameter. A
        stepped real within four units in the last place of a lattice point is
        taken as that point, and -0.0 is taken as 0.0.
        """
        if self.kind == "integer":
            if (
                type(value) is int
                and self.low <= value <= self.high
                and (value - self.low) % self.step == 0
            ):
                return value
        elif self.kind == "real":
            if type(value) in (int, float):
                number = float(value)
                if self.low <= number <= self.high:
                    if self.step is None:
                        return number + 0.0
                    ordinal = self.ordinal(number)
                    if 0 <= ordinal <= self.last:
                        decoded = self.decode(ordinal)
                        slack = max(math.ulp(number), math.ulp(decoded)) * 4
                        if abs(decoded - number) <= slack:
                            return decoded + 0.0
        elif self.kind == "boolean":
            if type(value) is bool:
                return value
        else:
            for choice in self.choices:
                if type(value) is type(choice) and value == choice:
                    return choice
            if type(value) is int:
                for choice in self.choices:
                    if type(choice) is float and float(value) == choice:
                        return choice
        raise ValueError("the value does not fit the dimension")

    def neighbours(
        self,
        value: Any,
        fraction: float | None = None,
        ratio: float | None = None,
    ) -> Moves:
        """The values one move away from ``value`` and the moves that were dropped.

        ``value`` must be a normalised value of this dimension. Order: lower
        first; a categorical keeps its declared order. A rule that needs
        ``fraction`` or ``ratio`` raises ValueError when it is None. No log,
        exp or pow function is called: log integers use exact rational
        arithmetic, log reals use one binary64 multiplication or division.
        """
        rule = self.rule
        if rule == "constant":
            return Moves((), 0, 0)
        if rule == "step":
            return _within(
                (value - self.step, value + self.step), value, self.low, self.high
            )
        if rule == "ordinal":
            ordinal = self.ordinal(value)
            kept = []
            bounds = same = 0
            for target in (ordinal - 1, ordinal + 1):
                if not 0 <= target <= self.last:
                    bounds += 1
                    continue
                candidate = self.decode(target)
                if candidate == value:
                    same += 1
                else:
                    kept.append(candidate + 0.0)
            return Moves(tuple(kept), bounds, same)
        if rule == "fraction":
            if fraction is None:
                raise ValueError("this dimension needs a fraction")
            delta = fraction * (self.high - self.low)
            return _within((value - delta, value + delta), value, self.low, self.high)
        if rule == "ratio":
            if ratio is None:
                raise ValueError("this dimension needs a ratio")
            return _within((value / ratio, value * ratio), value, self.low, self.high)
        if rule == "ratio_rounded":
            if ratio is None:
                raise ValueError("this dimension needs a ratio")
            exact = Fraction(ratio)
            half = Fraction(1, 2)
            lower = math.floor(Fraction(value) / exact + half)
            upper = math.floor(Fraction(value) * exact + half)
            return _within((lower, upper), value, self.low, self.high)
        if rule == "other_value":
            return Moves((not value,), 0, 0)
        others = tuple(
            choice
            for choice in self.choices
            if not (type(choice) is type(value) and choice == value)
        )
        return Moves(others, 0, 0)

    def rank(self, value: Any) -> Any:
        """A sortable position of a value inside this dimension's own order."""
        if self.kind == "boolean":
            return int(value)
        if self.kind == "categorical":
            for position, choice in enumerate(self.choices):
                if type(value) is type(choice) and value == choice:
                    return position
            raise ValueError("the value is not a choice of the dimension")
        return value

    def sort_values(self, values: Any) -> list:
        """The values in this dimension's order: ascending, or declared order."""
        return sorted(values, key=self.rank)


def dimension(name: str, spec: Any) -> Dimension:
    """Build the dimension called ``name`` from its recorded-space entry.

    The entry is checked the way the study spec checks a dimension: integer
    bounds and a positive integer step with ``low <= high``; real bounds with an
    optional positive step; a log dimension has positive bounds, and a log
    integer step 1 while a log real has no step; a stepped real must have at
    most 2^53 ordinals. A dimension with exactly one legal value is constant.
    A categorical keeps the first of equal choices. Anything else raises
    ``NeighbourhoodError("neighbourhood_input_invalid")``.
    """

    def refuse(reason: str) -> NeighbourhoodError:
        return _refusal(
            "input_invalid", f"dimension {_clip(name)}: {reason}", names=(name,)
        )

    if not isinstance(spec, dict):
        raise refuse("the recorded entry must be an object")
    kind = spec.get("kind")
    log = spec.get("log", False)
    if kind in ("integer", "real") and type(log) is not bool:
        raise refuse("log must be a boolean")
    if kind == "integer":
        low, high, step = spec.get("low"), spec.get("high"), spec.get("step")
        if type(low) is not int or type(high) is not int:
            raise refuse("integer bounds must be integers")
        if type(step) is not int or step < 1:
            raise refuse("the integer step must be a positive integer")
        if low > high:
            raise refuse("low exceeds high")
        if log and (low < 1 or step != 1):
            raise refuse("a log integer needs positive bounds and step 1")
        if (high - low) // step == 0:
            rule = "constant"
        else:
            rule = "ratio_rounded" if log else "step"
        return Dimension(
            name=name, kind=kind, rule=rule, low=low, high=high, step=step, log=log
        )
    if kind == "real":
        low, high = _number(spec.get("low")), _number(spec.get("high"))
        if low is None or high is None:
            raise refuse("real bounds must be finite numbers")
        if low > high:
            raise refuse("low exceeds high")
        step = spec.get("step")
        if step is not None:
            step = _number(step)
            if step is None or step <= 0:
                raise refuse("the real step must be a positive finite number")
        if log and (low <= 0 or step is not None):
            raise refuse("a log real needs positive bounds and no step")
        last = None
        if low == high:
            rule = "constant"
            last = None if step is None else 0
        elif step is None:
            rule = "ratio" if log else "fraction"
        else:
            issues: list = []
            count = _finite_parameter_cardinality(
                ParameterSpec(kind="real", low=low, high=high, step=step),
                name,
                issues,
            )
            if count is None or issues:
                raise refuse("the stepped real has no usable lattice (over 2^53)")
            last = count - 1
            rule = "constant" if last == 0 else "ordinal"
        return Dimension(
            name=name,
            kind=kind,
            rule=rule,
            low=low,
            high=high,
            step=step,
            log=log,
            last=last,
        )
    if kind == "boolean":
        return Dimension(name=name, kind=kind, rule="other_value")
    if kind == "categorical":
        listed = spec.get("choices")
        if not isinstance(listed, list) or not listed:
            raise refuse("choices must be a non-empty list")
        choices: list = []
        seen: set = set()
        for choice in listed:
            if type(choice) not in (str, int, float, bool) or (
                type(choice) is float and not math.isfinite(choice)
            ):
                raise refuse("every choice must be a finite scalar")
            marker = (type(choice), choice)
            if marker not in seen:
                seen.add(marker)
                choices.append(choice)
        rule = "constant" if len(choices) == 1 else "other_choices"
        return Dimension(name=name, kind=kind, rule=rule, choices=tuple(choices))
    raise refuse("the kind must be integer, real, boolean or categorical")


def _direction(space: dict, sources: list) -> str:
    objective = space.get("objective")
    direction = objective.get("direction") if isinstance(objective, dict) else None
    if direction not in _DIRECTIONS:
        raise _refusal(
            "input_invalid",
            "the recorded objective has no direction of maximize or minimize",
            names=("direction",),
        )
    for source in sources:
        if source.direction is not None and source.direction != direction:
            raise _refusal(
                "space_mismatch",
                f"source {source.index} records a direction that differs from "
                "the space",
                names=("direction",),
            )
    return direction


def _digest(row: Row, key: str) -> bytes:
    text = json.dumps(
        [row.source_index, row.trial_id, row.status, row.objective, key],
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(text.encode("ascii")).digest()


class Study:
    """The rows of the supplied sources indexed by parameter vector.

    Built by ``prepare``. It holds the common recorded space, one ``Dimension``
    per parameter in byte-wise name order, the groups of rows that share a
    vector, the reference value and the input digest. ``bind`` adds the band so
    that ``cell`` can classify a vector. Treat every attribute as read-only.
    """

    def __init__(self, sources: list, passed_space: Any = None) -> None:
        rows = sorted((row for source in sources for row in source.rows), key=_row_id)
        try:
            self.space = common_space(sources, passed_space)
        except RowsError as error:
            raise _from_rows_error(error) from None
        self.direction = _direction(self.space, sources)
        try:
            check_context(rows)
        except RowsError as error:
            raise _from_rows_error(error) from None
        specs = self.space["parameters"]
        names = sorted(specs, key=name_key)
        self.dimensions = {name: dimension(name, specs[name]) for name in names}
        self.names = tuple(names)
        self.varying = tuple(n for n in names if not self.dimensions[n].constant)
        self.constants = tuple(n for n in names if self.dimensions[n].constant)
        self.rows = tuple(rows)
        self.row_count = len(rows)
        self.by_id = {_row_id(row): row for row in rows}
        self.summaries = [_summary(source) for source in sources]
        self.groups: dict = {}
        digests = []
        for row in rows:
            key = self.key_of(self.row_values(row))
            self.groups.setdefault(key, []).append(row)
            digests.append(_digest(row, key))
        self.input_sha256 = hashlib.sha256(b"".join(sorted(digests))).hexdigest()
        self.duplicate_rows = sum(len(group) - 1 for group in self.groups.values())
        self._check_duplicates()
        ok_rows = [row for row in rows if row.status == "ok"]
        if not ok_rows:
            raise _refusal(
                "no_feasible_rows",
                "no row has status ok, so there is no reference value",
            )
        pick = max if self.direction == "maximize" else min
        self.reference = pick(row.objective for row in ok_rows)
        self.reference_id = min(
            _row_id(row) for row in ok_rows if row.objective == self.reference
        )
        self.band: dict | None = None
        self.delta: float | None = None
        self.fraction: float | None = None
        self.ratio: float | None = None

    def key_of(self, values: dict) -> str:
        """Canonical key of a normalised parameter vector."""
        try:
            return vector_key(values, self.space)
        except RowsError as error:
            raise _from_rows_error(error) from None

    def row_values(self, row: Row) -> dict:
        """The normalised parameter vector of a row, or an input_invalid refusal."""
        parameters = row.parameters
        if (
            not isinstance(parameters, dict)
            or parameters.keys() != self.dimensions.keys()
        ):
            given = set(parameters) if isinstance(parameters, dict) else set()
            raise _refusal(
                "input_invalid",
                "the parameters of a row differ from the dimensions of the space",
                [_row_id(row)],
                sorted(given ^ set(self.dimensions)),
            )
        values = {}
        for name, dim in self.dimensions.items():
            try:
                values[name] = dim.normalise(parameters[name])
            except (ValueError, OverflowError):
                raise _refusal(
                    "input_invalid",
                    f"parameter {_clip(name)} is off the space or off its lattice",
                    [_row_id(row)],
                    (name,),
                ) from None
        return values

    def _check_duplicates(self) -> None:
        conflicting = []
        for group in self.groups.values():
            scored = [row for row in group if row.status in _SCORED_STATUSES]
            first = scored[0] if scored else None
            if any(
                (row.status, row.objective) != (first.status, first.objective)
                for row in scored[1:]
            ):
                conflicting.extend(_row_id(row) for row in scored)
        if conflicting:
            raise _refusal(
                "duplicate_conflict",
                "scored rows of one parameter vector disagree in status or objective",
                conflicting,
            )

    def bind(self, band: dict, fraction: float | None, ratio: float | None) -> None:
        """Set the band and the move rules; computes the near-best distance."""
        value = band["value"]
        delta = value if band["mode"] == "absolute" else value * abs(self.reference)
        if not math.isfinite(delta):
            raise _refusal(
                "parameter_invalid",
                "the band distance is not a finite number",
                names=("band",),
            )
        self.band = {"mode": band["mode"], "value": value, "delta": delta}
        self.delta = delta
        self.fraction = fraction
        self.ratio = ratio

    def gap(self, objective: float) -> float:
        """Distance from the reference value; one subtraction, never negative."""
        if self.direction == "maximize":
            return self.reference - objective
        return objective - self.reference

    def better(self, objective: float, other: float) -> bool:
        """Whether ``objective`` is strictly better than ``other``."""
        if self.direction == "maximize":
            return objective > other
        return objective < other

    def rank(self, name: str, value: Any) -> Any:
        return self.dimensions[name].rank(value)

    def sort_values(self, name: str, values: Any) -> list:
        return self.dimensions[name].sort_values(values)

    def cell(self, values: dict) -> dict:
        """Class, objective, gap, near flag and row ids of one full vector.

        Classes: ``scored_ok`` (status ok), ``scored_infeasible`` (constraint
        violation), ``unscorable`` (any other status; the recorded objective of
        the first row is shown, a pruned row's is a prefix value) and
        ``never_evaluated``. Scored rows of one vector agree exactly, so the
        first scored row speaks for the vector; otherwise the first row does.
        """
        if self.delta is None:
            raise ValueError("bind the band before classifying vectors")
        group = self.groups.get(self.key_of(values), ())
        rows = [[row.source_index, row.trial_id] for row in group]
        scored = [row for row in group if row.status in _SCORED_STATUSES]
        if scored:
            head = scored[0]
            if head.status == "ok":
                gap = self.gap(head.objective)
                return {
                    "class": SCORED_OK,
                    "objective": head.objective,
                    "gap": gap,
                    "near": gap <= self.delta,
                    "rows": rows,
                }
            return {
                "class": SCORED_INFEASIBLE,
                "objective": head.objective,
                "gap": None,
                "near": False,
                "rows": rows,
            }
        if group:
            return {
                "class": UNSCORABLE,
                "objective": group[0].objective,
                "gap": None,
                "near": False,
                "rows": rows,
            }
        return {
            "class": NEVER_EVALUATED,
            "objective": None,
            "gap": None,
            "near": False,
            "rows": rows,
        }


def _summary(source: Source) -> dict:
    hashes = [
        row.space_hash
        for row in sorted(source.rows, key=_row_id)
        if row.space_hash is not None
    ]
    return {
        "index": source.index,
        "rows": len(source.rows),
        "space_hash": hashes[0] if hashes else None,
    }


def prepare(sources: Any, *, space: Any = None) -> Study:
    """Read the sources and index their rows; see ``neighbourhood`` for refusals.

    ``sources`` is a non-empty list; an item is a path to a complete result JSON
    or a rows JSONL, a parsed result, or a parsed list of rows. The position in
    the list is the source index. ``space`` is a recorded-space dict or a
    ``StudySpec`` and is used only when no row or result records its space.
    """
    if not isinstance(sources, (list, tuple)) or not sources:
        raise _refusal(
            "input_invalid",
            "sources must be a non-empty list of result files, results or row lists",
            names=("sources",),
        )
    read = []
    for index, item in enumerate(sources):
        try:
            read.append(read_source(item, index))
        except RowsError as error:
            raise _from_rows_error(error, index) from None
    return Study(read, space)


def top_ids(sources: Any, k: int, *, space: Any = None) -> list:
    """The (source, trial_id) of the ``k`` best distinct parameter vectors.

    Only rows with status ok count. A vector is represented by its lowest
    (source, trial_id); best first, ties by the lower (source, trial_id). Fewer
    than ``k`` are returned when fewer vectors have an ok row. The sources are
    read and refused exactly as in ``neighbourhood``.
    """
    if type(k) is not int or k < 1:
        raise _refusal(
            "parameter_invalid", "k must be a positive integer", names=("k",)
        )
    study = prepare(sources, space=space)
    heads = []
    for group in study.groups.values():
        ok_rows = [row for row in group if row.status == "ok"]
        if ok_rows:
            heads.append(ok_rows[0])
    sign = -1.0 if study.direction == "maximize" else 1.0
    heads.sort(key=lambda row: (sign * row.objective, *_row_id(row)))
    return [_row_id(row) for row in heads[:k]]


def _check_band(band: Any) -> dict:
    if band is None:
        raise _refusal("parameter_missing", "no band was given", names=("band",))
    if not isinstance(band, dict) or set(band) != {"mode", "value"}:
        raise _refusal(
            "parameter_invalid",
            "the band must be an object with exactly the keys mode and value",
            names=("band",),
        )
    mode = band["mode"]
    value = _number(band["value"])
    if type(mode) is not str or mode not in _BAND_MODES:
        raise _refusal(
            "parameter_invalid",
            "the band mode must be absolute or relative",
            names=("band",),
        )
    if value is None or value < 0:
        raise _refusal(
            "parameter_invalid",
            "the band value must be a finite number of at least zero",
            names=("band",),
        )
    return {"mode": mode, "value": value + 0.0}


def _check_parameters(
    centres: Any,
    band: Any,
    fraction: Any,
    ratio: Any,
    pair_k: Any,
    max_work: Any,
) -> tuple:
    if centres is None or (isinstance(centres, (list, tuple)) and not centres):
        raise _refusal("parameter_missing", "no centres were given", names=("centres",))
    pairs = []
    if isinstance(centres, (list, tuple)):
        for centre in centres:
            if (
                isinstance(centre, (list, tuple))
                and len(centre) == 2
                and type(centre[0]) is int
                and type(centre[1]) is int
                and centre[0] >= 0
                and centre[1] >= 0
            ):
                pairs.append((centre[0], centre[1]))
    if not isinstance(centres, (list, tuple)) or len(pairs) != len(centres):
        raise _refusal(
            "parameter_invalid",
            "centres must be a list of (source, trial_id) pairs of non-negative "
            "integers",
            names=("centres",),
        )
    checked_band = _check_band(band)
    if fraction is not None:
        number = _number(fraction)
        if number is None or not 0.0 < number <= 1.0:
            raise _refusal(
                "parameter_invalid",
                "the fraction must be a number above 0 and at most 1",
                names=("fraction",),
            )
        fraction = number
    if ratio is not None:
        number = _number(ratio)
        if number is None or not number > 1.0:
            raise _refusal(
                "parameter_invalid",
                "the ratio must be a finite number above 1",
                names=("ratio",),
            )
        ratio = number
    if pair_k is None:
        raise _refusal("parameter_missing", "no pair_k was given", names=("pair_k",))
    if type(pair_k) is not int or pair_k < 0:
        raise _refusal(
            "parameter_invalid",
            "pair_k must be an integer of at least zero",
            names=("pair_k",),
        )
    if max_work is not None and (type(max_work) is not int or max_work < 1):
        raise _refusal(
            "parameter_invalid",
            "max_work must be a positive integer",
            names=("max_work",),
        )
    return pairs, checked_band, fraction, ratio, pair_k, max_work


def _require_varying(study: Study) -> None:
    if not study.varying:
        raise _refusal(
            "no_varying_dimension",
            "every dimension has exactly one legal value",
            names=study.names,
        )


def _require_rules(study: Study, fraction: Any, ratio: Any) -> None:
    absent = [
        name
        for name in study.varying
        if (study.dimensions[name].rule == "fraction" and fraction is None)
        or (study.dimensions[name].rule in ("ratio", "ratio_rounded") and ratio is None)
    ]
    if absent:
        raise _refusal(
            "parameter_missing",
            "a fraction (continuous real) or a ratio (log integer, log real) is "
            "needed for the dimensions in names",
            names=absent,
        )


def _centre_rows(study: Study, centres: list) -> list:
    seen: set = set()
    repeated, absent, unusable, rows = [], [], [], []
    for centre in centres:
        if centre in seen:
            repeated.append(centre)
            continue
        seen.add(centre)
        row = study.by_id.get(centre)
        if row is None:
            absent.append(centre)
        elif row.status != "ok":
            unusable.append(centre)
        else:
            rows.append(row)
    problems = []
    if absent:
        problems.append("absent from the rows")
    if unusable:
        problems.append("not a row with status ok")
    if repeated:
        problems.append("listed more than once")
    if problems:
        raise _refusal(
            "centre_invalid",
            "every centre must be a row with status ok, listed once ("
            + ", ".join(problems)
            + ")",
            absent + unusable + repeated,
        )
    return sorted(rows, key=_row_id)


def _admit(study: Study, lookups: int, max_work: int | None) -> None:
    work = study.row_count + lookups
    if max_work is not None and work > max_work:
        raise _refusal(
            "resource_exceeded",
            f"the work figure {work} (rows {study.row_count} plus lookups "
            f"{lookups}) is above max_work {max_work}",
            names=("max_work",),
        )


def _profile(study: Study, values: dict, moves: dict) -> dict:
    profile = {}
    for name in study.varying:
        cells = []
        for value in moves[name].values:
            vector = dict(values)
            vector[name] = value
            cells.append({"value": value, **study.cell(vector)})
        profile[name] = cells
    return profile


def _ratio(numerator: int, denominator: int) -> dict | None:
    if denominator == 0:
        return None
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator,
    }


def _centre_result(
    study: Study,
    row: Row,
    moves: dict,
    profile: dict,
    pair_maps: list,
) -> dict:
    counts = dict.fromkeys(
        (
            "geometric",
            SCORED_OK,
            SCORED_INFEASIBLE,
            UNSCORABLE,
            NEVER_EVALUATED,
            "near",
            "better_neighbours",
        ),
        0,
    )
    for cells in profile.values():
        for cell in cells:
            counts["geometric"] += 1
            counts[cell["class"]] += 1
            if cell["near"]:
                counts["near"] += 1
            if cell["class"] == SCORED_OK and study.better(
                cell["objective"], row.objective
            ):
                counts["better_neighbours"] += 1
    scored = counts[SCORED_OK] + counts[SCORED_INFEASIBLE]
    gap = study.gap(row.objective)
    return {
        "source": row.source_index,
        "trial_id": row.trial_id,
        "objective": row.objective,
        "centre_gap": gap,
        "centre_near": gap <= study.delta,
        "counts": counts,
        "dropped": {
            "bounds": sum(item.bounds for item in moves.values()),
            "same_as_centre": sum(item.same_as_centre for item in moves.values()),
        },
        "share_near_best": _ratio(counts["near"], scored),
        "coverage": _ratio(scored + counts[UNSCORABLE], counts["geometric"]),
        "profile": profile,
        "pair_maps": pair_maps,
    }


def _missing_vectors(study: Study, centres: list) -> list:
    """Never-evaluated vectors of every cell, deduplicated, in vector order."""
    found: dict = {}

    def note(vector: dict) -> None:
        order = tuple(study.rank(name, vector[name]) for name in study.names)
        found[order] = {name: vector[name] for name in study.names}

    for values, result in centres:
        for name, cells in result["profile"].items():
            for cell in cells:
                if cell["class"] == NEVER_EVALUATED:
                    note({**values, name: cell["value"]})
        for pair in result["pair_maps"]:
            for cell in pair["cells"]:
                if cell["class"] == NEVER_EVALUATED:
                    note({**values, **cell["values"]})
    return [found[order] for order in sorted(found)]


def neighbourhood(
    sources: Any,
    *,
    centres: Any,
    band: Any,
    fraction: Any = None,
    ratio: Any = None,
    pair_k: Any,
    max_work: Any = None,
    space: Any = None,
) -> dict:
    """Describe the neighbourhoods of the given centres from finished rows.

    ``sources`` is a non-empty list; an item is a path to a complete result JSON
    or a rows JSONL, a parsed result, or a parsed list of rows, and its position
    in the list is its source index. ``centres`` is a list of (source, trial_id)
    pairs, each a row with status ok (``top_ids`` returns such a list). ``band``
    is ``{"mode": "absolute" | "relative", "value": v}``: a scored ok neighbour
    is near the best iff its gap to the best ok objective is at most ``v`` or
    ``v`` times the absolute best. ``fraction`` (continuous reals) and ``ratio``
    (log dimensions) are needed only when a varying dimension uses them.
    ``pair_k`` must be passed: it has no default, and None refuses with
    ``neighbourhood_parameter_missing``. That many pair maps per centre are
    built from the pairs with the most not-near neighbours, and an explicit 0
    builds none. ``max_work`` bounds the rows plus the lookups. ``space`` is
    used only when no row or result records one. No default exists for the
    band, the fraction, the ratio, ``pair_k`` or ``max_work``.

    The result is a JSON-ready dict (see ``docs/neighbourhood.md``). Refusals
    raise ``NeighbourhoodError`` with one of ``CODES`` and, when several defects
    exist, report the first in this order: caller parameters, reading the
    sources, space and direction, context, dimensions, rows against the space,
    duplicate conflicts, no ok row, no varying dimension, missing fraction or
    ratio, a relative band at a zero reference, centres, work above
    ``max_work``.
    """
    centre_ids, band, fraction, ratio, pair_k, max_work = _check_parameters(
        centres, band, fraction, ratio, pair_k, max_work
    )
    study = prepare(sources, space=space)
    _require_varying(study)
    _require_rules(study, fraction, ratio)
    if band["mode"] == "relative" and study.reference == 0.0:
        raise _refusal(
            "band_degenerate",
            "a relative band needs a reference value other than zero",
        )
    study.bind(band, fraction, ratio)
    chosen = _centre_rows(study, centre_ids)
    values = [study.row_values(row) for row in chosen]
    moves = [
        {
            name: study.dimensions[name].neighbours(vector[name], fraction, ratio)
            for name in study.varying
        }
        for vector in values
    ]
    axis = sum(len(item.values) for centre in moves for item in centre.values())
    _admit(study, axis, max_work)
    profiles = [
        _profile(study, vector, centre) for vector, centre in zip(values, moves)
    ]
    maps: list = [[] for _ in chosen]
    pair_lookups = 0
    if pair_k:
        from . import sensitivity  # imported here: sensitivity imports this module

        plans = [
            sensitivity.plan_pairs(study, vector, profile, pair_k)
            for vector, profile in zip(values, profiles)
        ]
        pair_lookups = sum(plan.lookups for centre in plans for plan in centre)
        _admit(study, axis + pair_lookups, max_work)
        maps = [
            sensitivity.pair_maps(study, vector, centre)
            for vector, centre in zip(values, plans)
        ]
    results = [
        _centre_result(study, row, centre, profile, pair_maps)
        for row, centre, profile, pair_maps in zip(chosen, moves, profiles, maps)
    ]
    lookups = axis + pair_lookups
    return {
        "contract": CONTRACT,
        "status": "ok",
        "direction": study.direction,
        "reference": {
            "objective": study.reference,
            "source": study.reference_id[0],
            "trial_id": study.reference_id[1],
        },
        "band": dict(study.band),
        "rules": {
            "fraction": fraction,
            "ratio": ratio,
            "pair_k": pair_k,
            "by_dimension": {
                name: study.dimensions[name].rule for name in study.varying
            },
        },
        "sources": [dict(item) for item in study.summaries],
        "duplicate_rows": study.duplicate_rows,
        "constant_dimensions": list(study.constants),
        "missing_vectors": _missing_vectors(study, list(zip(values, results))),
        "admission": {
            "rows": study.row_count,
            "lookups": lookups,
            "work": study.row_count + lookups,
            "max_work": max_work,
        },
        "input_sha256": study.input_sha256,
        "interpretation": INTERPRETATION,
        "centres": results,
    }
