"""Portable search-space identity and read-only continuation preflight."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import struct
from typing import Any

from .study_spec import (
    StudySpec,
    _finite_parameter_cardinality,
    _fused_grid_value,
    _reject_duplicate_keys,
    _reject_json_constant,
    _parse_objective,
    _parse_search_space,
    load_study_spec,
)

SPACE_HASH_VERSION = 1
WARM_START_EXIT = 4
SPACE_EXHAUSTED_EXIT = 5
_UINT64_MAX = (1 << 64) - 1
_STATUSES = {
    "ok",
    "constraint_violation",
    "engine_error",
    "objective_error",
    "constraint_error",
    "trial_error",
    "trial_timeout",
    "pruned",
    "partial",
}


class WarmStartError(ValueError):
    """A parent cannot safely seed the requested study (exit 4)."""


class SpaceExhaustedError(ValueError):
    """Every finite parameter vector has already been tried (exit 5)."""


@dataclass(frozen=True)
class WarmHistory:
    """Validated parent history; no trial execution or billing occurs here."""

    source_sha256: str
    trials: tuple[dict[str, Any], ...]
    tried: frozenset[str]
    completed: int
    feasible: int
    next_id: int


def recorded_space(study: StudySpec) -> dict[str, Any]:
    """Return the version-independent declarative space recorded in results."""
    parameters = {}
    for name, parameter in study.search_space.items():
        value: dict[str, Any] = {"kind": parameter.kind}
        if parameter.kind in {"integer", "real"}:
            value.update(
                low=parameter.low,
                high=parameter.high,
                step=parameter.step,
                log=parameter.log,
            )
        elif parameter.kind == "categorical":
            value["choices"] = list(parameter.choices)
        parameters[name] = value
    if study.objective.kind != "expression":
        raise ValueError("space identity requires an expression objective")
    return {
        "parameters": parameters,
        "objective": {
            "expression": study.objective.expression,
            "direction": study.objective.direction,
            "constraints": list(study.objective.constraints),
        },
    }


def _real_bits(value: float) -> str:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite real")
    return struct.pack(">d", 0.0 if number == 0.0 else number).hex()


def _scalar(value: Any) -> list[Any]:
    if isinstance(value, bool):
        return ["boolean", value]
    if isinstance(value, int):
        if not -(1 << 63) <= value < (1 << 63):
            raise ValueError("integer outside int64")
        return ["integer", str(value)]
    if isinstance(value, float):
        return ["real", _real_bits(value)]
    if isinstance(value, str):
        return ["string", value]
    raise ValueError("expected scalar parameter")


def _dump(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_space(space: dict[str, Any]) -> str:
    """Return canonical v1 JSON; numeric identities are independent of formatting."""
    parameters = {}
    for name, source in space["parameters"].items():
        kind = source["kind"]
        parameter = {"kind": kind}
        if kind in {"integer", "real"}:
            for key in ("low", "high", "step"):
                value = source[key]
                if value is None:
                    parameter[key] = None
                elif kind == "integer":
                    if type(value) is not int:
                        raise ValueError("expected integer bound/step")
                    parameter[key] = str(value)
                else:
                    if type(value) not in {int, float}:
                        raise ValueError("expected real bound/step")
                    parameter[key] = _real_bits(value)
            if type(source["log"]) is not bool:
                raise ValueError("log must be boolean")
            parameter["log"] = source["log"]
        elif kind == "categorical":
            parameter["choices"] = [_scalar(choice) for choice in source["choices"]]
        elif kind != "boolean":
            raise ValueError("unknown parameter kind")
        parameters[name] = parameter
    objective = space["objective"]
    return _dump(
        {
            "space_hash_version": SPACE_HASH_VERSION,
            "parameters": parameters,
            "objective": {
                "expression": objective["expression"],
                "direction": objective["direction"],
                "constraints": sorted(objective["constraints"]),
            },
        }
    )


def space_hash(study: StudySpec) -> str:
    """SHA-256 of canonical v1 JSON, independent of package versions and pins."""
    return hashlib.sha256(canonical_space(recorded_space(study)).encode()).hexdigest()


def cardinality(study: StudySpec) -> int | None:
    """Exact uint64 cardinality, or None for continuous/overflowing spaces."""
    total = 1
    for name, parameter in study.search_space.items():
        issues = []
        count = _finite_parameter_cardinality(parameter, name, issues)
        if count is None or issues or total > _UINT64_MAX // count:
            return None
        total *= count
    return total


def _loads(text: str) -> Any:
    return json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_json_constant,
    )


def _parameters(study: StudySpec, values: Any) -> dict[str, Any]:
    if not isinstance(values, dict) or values.keys() != study.search_space.keys():
        raise ValueError("parameter names differ")
    normalized = {}
    for name, parameter in study.search_space.items():
        value = values[name]
        valid = False
        if parameter.kind == "integer":
            valid = (
                type(value) is int
                and parameter.low <= value <= parameter.high
                and (value - parameter.low) % parameter.step == 0
            )
        elif parameter.kind == "real":
            valid = type(value) in {int, float} and math.isfinite(value)
            if valid:
                value = float(value)
                valid = parameter.low <= value <= parameter.high
                if valid and parameter.step is not None:
                    ordinal = round((value - parameter.low) / parameter.step)
                    decoded = _fused_grid_value(
                        ordinal, float(parameter.step), float(parameter.low)
                    )
                    valid = (
                        ordinal >= 0
                        and abs(decoded - value)
                        <= max(math.ulp(value), math.ulp(decoded)) * 4
                    )
                    if valid:
                        value = decoded
        elif parameter.kind == "boolean":
            valid = type(value) is bool
        else:
            valid = any(
                type(value) is type(choice) and value == choice
                for choice in parameter.choices
            )
            if not valid and type(value) is int:
                for choice in parameter.choices:
                    if type(choice) is float and float(value) == choice:
                        value = float(value)
                        valid = True
                        break
        if not valid:
            raise ValueError(f"invalid trial parameter {name!r}")
        normalized[name] = value
    return normalized


def load_warm_start(study: StudySpec, path: str | Path) -> WarmHistory:
    """Auto-detect fd-3 JSONL, a complete result object, or its trials array."""
    try:
        source = Path(path).expanduser().resolve()
        content = source.read_bytes()
        if len(content) > 256 * 1024 * 1024:
            raise ValueError("warm-start document exceeds 256 MiB")
        text = content.decode("utf-8")
        try:
            document = _loads(text)
        except json.JSONDecodeError:
            document = None
        parent_space = None
        if isinstance(document, dict) and "trials" in document:
            if document.get("trials_out", "all") != "all":
                raise ValueError("summary/none result is not a complete trial history")
            own_trials = document["trials"]
            inherited = document.get("warm_start_trials", [])
            if "trials_completed" in document and (
                type(document["trials_completed"]) is not int
                or document["trials_completed"] != len(own_trials)
            ):
                raise ValueError("result does not contain all completed trials")
            if "warm_start" in document and (
                type(document["warm_start"]["trials"]) is not int
                or document["warm_start"]["trials"] != len(inherited)
            ):
                raise ValueError("result is missing ancestor trials")
            trials = inherited + own_trials
            parent_space = document.get("space")
            if parent_space is None and "study_spec" in document:
                embedded = document["study_spec"]
                issues = []
                parameters = _parse_search_space(
                    embedded["strategies"][0]["search_space"],
                    "$.strategies[0].search_space",
                    issues,
                )
                objective = _parse_objective(embedded["objective"], issues)
                if issues:
                    raise ValueError("invalid legacy study_spec space or objective")
                parent_space = recorded_space(
                    replace(
                        study,
                        strategy=replace(study.strategy, search_space=parameters),
                        objective=objective,
                    )
                )
            if parent_space is None and "study" in document:
                parent_spec = Path(document["study"])
                if not parent_spec.is_absolute():
                    parent_spec = source.parent / parent_spec
                parent_space = recorded_space(
                    load_study_spec(parent_spec, continuation=True)
                )
        elif isinstance(document, list):
            trials = document
        else:
            trials = [_loads(line) for line in text.splitlines() if line.strip()]
        if not isinstance(trials, list) or not trials:
            raise ValueError("parent has no trials")
        expected = canonical_space(recorded_space(study))
        expected_hash = hashlib.sha256(expected.encode()).hexdigest()

        def verify_hash(owner):
            version = owner.get("space_hash_version")
            if version is not None and type(version) is not int:
                raise ValueError("space_hash_version must be an integer")
            if (
                version == SPACE_HASH_VERSION
                and "space_hash" in owner
                and owner["space_hash"] != expected_hash
            ):
                raise ValueError("recorded space_hash does not match space")

        if parent_space is not None and canonical_space(parent_space) != expected:
            raise ValueError("search space or objective differs (space_hash)")
        if parent_space is not None:
            verify_hash(document)
        tried = set()
        identifiers = set()
        completed = feasible = next_id = 0
        for trial in trials:
            space = trial.get("space", parent_space)
            if space is None:
                raise ValueError(
                    "missing recorded space; compatibility cannot be proven"
                )
            if canonical_space(space) != expected:
                raise ValueError("search space or objective differs (space_hash)")
            verify_hash(trial)
            status = trial["status"]
            if status not in _STATUSES:
                raise ValueError(f"unknown trial status: {status}")
            identifier = trial["trial_id"]
            if type(identifier) is not int or not 0 <= identifier < _UINT64_MAX:
                raise ValueError("expected uint64 trial_id with a continuation ID")
            if identifier in identifiers:
                raise ValueError("duplicate trial_id")
            identifiers.add(identifier)
            next_id = max(next_id, identifier + 1)
            values = _parameters(study, trial["parameters"])
            tried.add(_dump({name: _scalar(value) for name, value in values.items()}))
            is_feasible = trial["feasible"]
            objective = trial["objective"]
            if type(is_feasible) is not bool or is_feasible != (status == "ok"):
                raise ValueError("inconsistent status/feasibility/objective")
            if objective is not None and (
                type(objective) not in {int, float} or not math.isfinite(objective)
            ):
                raise ValueError("objective must be finite or null")
            for score in trial.get("pruning", {}).get("rung_scores", []):
                if score is not None and (
                    type(score) not in {int, float} or not math.isfinite(score)
                ):
                    raise ValueError("invalid pruning rung score")
            completed += status in {"ok", "constraint_violation"}
            feasible += is_feasible
        return WarmHistory(
            hashlib.sha256(content).hexdigest(),
            tuple(sorted(trials, key=lambda trial: trial["trial_id"])),
            frozenset(tried),
            completed,
            feasible,
            next_id,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
    ) as error:
        raise WarmStartError(f"warm-start incompatible: {error}") from error


def space_info(
    study: StudySpec, warm_start: str | Path | None = None
) -> dict[str, Any]:
    """Inspect coverage without compiling a strategy, loading data, or drawing candidates."""
    count = cardinality(study)
    tried = 0 if warm_start is None else len(load_warm_start(study, warm_start).tried)
    return {
        "cardinality": count,
        "tried": tried,
        "remaining": None if count is None else count - tried,
        "space_hash": space_hash(study),
    }
