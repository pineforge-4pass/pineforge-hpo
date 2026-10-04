"""Dependency-free StudySpec v1 JSON loader and validator."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
import json
import math
from pathlib import Path
from typing import Any, Mapping

JsonScalar = str | int | float | bool | None


@dataclass(frozen=True)
class ValidationIssue:
    """One path-qualified StudySpec validation failure."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class StudySpecError(ValueError):
    """Aggregate error containing every issue found in one StudySpec."""

    def __init__(self, issues: list[ValidationIssue] | tuple[ValidationIssue, ...]):
        self.issues = tuple(issues)
        super().__init__("; ".join(str(issue) for issue in self.issues))


@dataclass(frozen=True)
class ParameterSpec:
    """Normalized integer, real, Boolean, or categorical search dimension."""

    kind: str
    low: int | float | None = None
    high: int | float | None = None
    step: int | float | None = None
    log: bool = False
    choices: tuple[JsonScalar, ...] = ()


@dataclass(frozen=True)
class StrategySpec:
    """One Pine source or compiled artifact and its runtime inputs."""

    id: str
    source: Path | None
    artifact: Path | None
    dataset_ids: tuple[str, ...]
    fixed_inputs: Mapping[str, JsonScalar]
    strategy_overrides: Mapping[str, JsonScalar]
    search_space: Mapping[str, ParameterSpec]


@dataclass(frozen=True)
class DatasetSpec:
    """One OHLCV source and its PineForge timeframe interpretation."""

    id: str
    ohlcv: Path
    input_tf: str
    script_tf: str
    chart_timezone: str


@dataclass(frozen=True)
class ObjectiveSpec:
    """Objective direction, expression or registration, and constraints."""

    kind: str
    direction: str
    expression: str | None = None
    name: str | None = None
    constraints: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    config: Mapping[str, Any] = field(default_factory=dict)
    nan_policy: str = "fail_trial"
    division_by_zero: str = "fail_trial"


@dataclass(frozen=True)
class TpeSamplerConfig:
    """Validated native TPE model and batching controls."""

    startup_trials: int = 10
    ei_candidates: int = 24
    gamma_fraction: float = 0.10
    gamma_cap: int = 25
    prior_weight: float = 1.0
    constant_liar: bool = True
    history_switch: int = 1000


@dataclass(frozen=True)
class SamplerSpec:
    """Sampler selection, deterministic seed, budget, and candidate policy."""

    kind: str
    seed: int
    trials: int
    candidate_policy: str = "sampler_default"
    config: TpeSamplerConfig | None = None


@dataclass(frozen=True)
class ExecutionSpec:
    """Native trial worker and failure-policy configuration."""

    workers: int
    isolation: str
    timeout_seconds: float | None = None
    fail_fast: bool = False
    batch_size: int | None = None
    batch_lag: int = 0
    pruner: str = "none"
    pruner_rungs: tuple[float, ...] = (0.25, 0.5)
    pruner_eta: int = 2


@dataclass(frozen=True)
class StudySpec:
    """Fully validated and path-resolved executable StudySpec v1."""

    schema_version: int
    mode: str
    strategy: StrategySpec
    datasets: tuple[DatasetSpec, ...]
    objective: ObjectiveSpec
    sampler: SamplerSpec
    execution: ExecutionSpec
    spec_path: Path

    @property
    def search_space(self) -> Mapping[str, ParameterSpec]:
        """Return the selected strategy's tunable input dimensions."""

        return self.strategy.search_space

    @property
    def fixed_inputs(self) -> Mapping[str, JsonScalar]:
        """Return Pine inputs applied unchanged to every trial."""

        return self.strategy.fixed_inputs

    @property
    def overrides(self) -> Mapping[str, JsonScalar]:
        """Return runtime ``strategy(...)`` overrides applied to every trial."""

        return self.strategy.strategy_overrides

    @classmethod
    def from_json(cls, path: str | Path, *, require_files: bool = False) -> "StudySpec":
        """Load a StudySpec with the same validation as :func:`load_study_spec`."""

        return load_study_spec(path, require_files=require_files)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StudySpecError([ValidationIssue("$", f"duplicate JSON key {key!r}")])
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise StudySpecError(
        [ValidationIssue("$", f"non-finite JSON number {value!r} is not allowed")]
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _check_unknown(
    value: Mapping[str, Any],
    allowed: set[str],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    for key in value:
        if key not in allowed:
            issues.append(ValidationIssue(f"{path}.{key}", "unknown field"))


def _object(value: Any, path: str, issues: list[ValidationIssue]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        issues.append(ValidationIssue(path, "must be an object"))
        return {}
    return dict(value)


def _nonempty_string(value: Any, path: str, issues: list[ValidationIssue]) -> str:
    if not isinstance(value, str) or not value.strip():
        issues.append(ValidationIssue(path, "must be a non-empty string"))
        return ""
    return value


def _resolve_path(
    value: Any, path: str, base_dir: Path, issues: list[ValidationIssue]
) -> Path | None:
    text = _nonempty_string(value, path, issues)
    if not text:
        return None
    candidate = Path(text).expanduser()
    return (candidate if candidate.is_absolute() else base_dir / candidate).resolve()


def _scalar_map(
    value: Any, path: str, issues: list[ValidationIssue]
) -> dict[str, JsonScalar]:
    result: dict[str, JsonScalar] = {}
    raw = _object(value, path, issues)
    for key, item in raw.items():
        if not isinstance(key, str) or not key:
            issues.append(ValidationIssue(path, "keys must be non-empty strings"))
            continue
        if not _is_scalar(item) or (
            isinstance(item, float) and not math.isfinite(item)
        ):
            issues.append(
                ValidationIssue(f"{path}.{key}", "must be a finite JSON scalar")
            )
            continue
        result[key] = item
    return result


def _parse_parameter(
    value: Any, path: str, issues: list[ValidationIssue]
) -> ParameterSpec:
    raw = _object(value, path, issues)
    kind = _nonempty_string(raw.get("kind"), f"{path}.kind", issues)
    common = {"kind", "low", "high", "step", "log", "choices"}
    _check_unknown(raw, common, path, issues)

    if kind == "integer":
        low = raw.get("low")
        high = raw.get("high")
        step = raw.get("step", 1)
        log = raw.get("log", False)
        if not _is_int(low):
            issues.append(ValidationIssue(f"{path}.low", "must be an integer"))
        if not _is_int(high):
            issues.append(ValidationIssue(f"{path}.high", "must be an integer"))
        if not _is_int(step) or step <= 0:
            issues.append(ValidationIssue(f"{path}.step", "must be a positive integer"))
        if not isinstance(log, bool):
            issues.append(ValidationIssue(f"{path}.log", "must be a boolean"))
            log = False
        if _is_int(low) and _is_int(high) and low > high:
            issues.append(ValidationIssue(path, "low must not exceed high"))
        if log:
            if _is_int(low) and low <= 0:
                issues.append(
                    ValidationIssue(f"{path}.low", "must be positive for log sampling")
                )
            if _is_int(high) and high <= 0:
                issues.append(
                    ValidationIssue(f"{path}.high", "must be positive for log sampling")
                )
            if _is_int(step) and step > 0 and step != 1:
                issues.append(
                    ValidationIssue(
                        f"{path}.step", "must equal 1 for log integer sampling"
                    )
                )
        return ParameterSpec(
            kind=kind,
            low=low if _is_int(low) else None,
            high=high if _is_int(high) else None,
            step=step if _is_int(step) and step > 0 else None,
            log=log,
        )

    if kind == "real":
        low = raw.get("low")
        high = raw.get("high")
        step = raw.get("step")
        log = raw.get("log", False)
        if not _is_number(low):
            issues.append(ValidationIssue(f"{path}.low", "must be a finite number"))
        if not _is_number(high):
            issues.append(ValidationIssue(f"{path}.high", "must be a finite number"))
        if step is not None and (not _is_number(step) or step <= 0):
            issues.append(
                ValidationIssue(f"{path}.step", "must be a positive finite number")
            )
        if not isinstance(log, bool):
            issues.append(ValidationIssue(f"{path}.log", "must be a boolean"))
            log = False
        if _is_number(low) and _is_number(high) and low > high:
            issues.append(ValidationIssue(path, "low must not exceed high"))
        if log:
            if _is_number(low) and low <= 0:
                issues.append(
                    ValidationIssue(f"{path}.low", "must be positive for log sampling")
                )
            if _is_number(high) and high <= 0:
                issues.append(
                    ValidationIssue(f"{path}.high", "must be positive for log sampling")
                )
            if step is not None:
                issues.append(
                    ValidationIssue(
                        f"{path}.step", "is not supported for log real sampling"
                    )
                )
        return ParameterSpec(
            kind=kind,
            low=low if _is_number(low) else None,
            high=high if _is_number(high) else None,
            step=step if step is not None and _is_number(step) and step > 0 else None,
            log=log,
        )

    if kind == "categorical":
        if "log" in raw:
            issues.append(
                ValidationIssue(
                    f"{path}.log", "is only supported for integer and real dimensions"
                )
            )
        choices = raw.get("choices")
        normalized: list[JsonScalar] = []
        if not isinstance(choices, list) or not choices:
            issues.append(
                ValidationIssue(f"{path}.choices", "must be a non-empty array")
            )
        else:
            for index, choice in enumerate(choices):
                if not _is_scalar(choice) or (
                    isinstance(choice, float) and not math.isfinite(choice)
                ):
                    issues.append(
                        ValidationIssue(
                            f"{path}.choices[{index}]", "must be a finite JSON scalar"
                        )
                    )
                else:
                    normalized.append(choice)
        return ParameterSpec(kind=kind, choices=tuple(normalized))

    if kind == "boolean":
        if "log" in raw:
            issues.append(
                ValidationIssue(
                    f"{path}.log", "is only supported for integer and real dimensions"
                )
            )
        return ParameterSpec(kind=kind)

    if kind:
        issues.append(
            ValidationIssue(
                path + ".kind", "must be integer, real, boolean, or categorical"
            )
        )
    return ParameterSpec(kind=kind)


def _parse_search_space(
    value: Any, path: str, issues: list[ValidationIssue]
) -> dict[str, ParameterSpec]:
    raw = _object(value, path, issues)
    if not raw:
        issues.append(ValidationIssue(path, "must contain at least one tunable input"))
    output: dict[str, ParameterSpec] = {}
    for name, parameter in raw.items():
        if not isinstance(name, str) or not name:
            issues.append(
                ValidationIssue(path, "parameter names must be non-empty strings")
            )
            continue
        output[name] = _parse_parameter(parameter, f"{path}.{name}", issues)
    return output


def _parse_strategy(
    value: Any, base_dir: Path, issues: list[ValidationIssue]
) -> StrategySpec:
    path = "$.strategies[0]"
    raw = _object(value, path, issues)
    _check_unknown(
        raw,
        {
            "id",
            "source",
            "artifact",
            "datasets",
            "fixed_inputs",
            "strategy_overrides",
            "search_space",
        },
        path,
        issues,
    )
    strategy_id = _nonempty_string(raw.get("id"), f"{path}.id", issues)
    has_source = "source" in raw
    has_artifact = "artifact" in raw
    if has_source == has_artifact:
        issues.append(
            ValidationIssue(path, "must define exactly one of source or artifact")
        )
    source = (
        _resolve_path(raw.get("source"), f"{path}.source", base_dir, issues)
        if has_source
        else None
    )
    artifact = (
        _resolve_path(raw.get("artifact"), f"{path}.artifact", base_dir, issues)
        if has_artifact
        else None
    )

    dataset_ids_raw = raw.get("datasets")
    dataset_ids: list[str] = []
    if not isinstance(dataset_ids_raw, list) or not dataset_ids_raw:
        issues.append(ValidationIssue(f"{path}.datasets", "must be a non-empty array"))
    else:
        for index, dataset_id in enumerate(dataset_ids_raw):
            normalized = _nonempty_string(
                dataset_id, f"{path}.datasets[{index}]", issues
            )
            if normalized:
                dataset_ids.append(normalized)
        if len(set(dataset_ids)) != len(dataset_ids):
            issues.append(
                ValidationIssue(f"{path}.datasets", "must not contain duplicates")
            )

    fixed = _scalar_map(raw.get("fixed_inputs", {}), f"{path}.fixed_inputs", issues)
    overrides = _scalar_map(
        raw.get("strategy_overrides", {}), f"{path}.strategy_overrides", issues
    )
    search = _parse_search_space(
        raw.get("search_space"), f"{path}.search_space", issues
    )
    overlap = sorted(set(fixed) & set(search))
    if overlap:
        issues.append(
            ValidationIssue(
                path, "inputs cannot be both fixed and tunable: " + ", ".join(overlap)
            )
        )
    return StrategySpec(
        id=strategy_id,
        source=source,
        artifact=artifact,
        dataset_ids=tuple(dataset_ids),
        fixed_inputs=fixed,
        strategy_overrides=overrides,
        search_space=search,
    )


def _parse_dataset(
    value: Any, index: int, base_dir: Path, issues: list[ValidationIssue]
) -> DatasetSpec:
    path = f"$.datasets[{index}]"
    raw = _object(value, path, issues)
    _check_unknown(
        raw, {"id", "ohlcv", "input_tf", "script_tf", "chart_timezone"}, path, issues
    )
    dataset_id = _nonempty_string(raw.get("id"), f"{path}.id", issues)
    ohlcv = _resolve_path(raw.get("ohlcv"), f"{path}.ohlcv", base_dir, issues)
    input_tf = _nonempty_string(raw.get("input_tf"), f"{path}.input_tf", issues)
    script_tf_raw = raw.get("script_tf", input_tf)
    if not isinstance(script_tf_raw, str):
        issues.append(ValidationIssue(f"{path}.script_tf", "must be a string"))
        script_tf_raw = ""
    chart_timezone = _nonempty_string(
        raw.get("chart_timezone", "UTC"), f"{path}.chart_timezone", issues
    )
    return DatasetSpec(
        id=dataset_id,
        ohlcv=ohlcv or (base_dir / "<invalid>"),
        input_tf=input_tf,
        script_tf=script_tf_raw,
        chart_timezone=chart_timezone,
    )


def _parse_objective(value: Any, issues: list[ValidationIssue]) -> ObjectiveSpec:
    path = "$.objective"
    raw = _object(value, path, issues)
    _check_unknown(
        raw,
        {
            "kind",
            "direction",
            "expression",
            "name",
            "constraints",
            "requires",
            "config",
            "nan_policy",
            "division_by_zero",
        },
        path,
        issues,
    )
    kind = _nonempty_string(raw.get("kind"), f"{path}.kind", issues)
    direction = _nonempty_string(raw.get("direction"), f"{path}.direction", issues)
    if direction not in {"maximize", "minimize"}:
        issues.append(
            ValidationIssue(f"{path}.direction", "must be maximize or minimize")
        )

    expression: str | None = None
    name: str | None = None
    if kind == "expression":
        expression = _nonempty_string(
            raw.get("expression"), f"{path}.expression", issues
        )
    elif kind == "registered":
        name = _nonempty_string(raw.get("name"), f"{path}.name", issues)
    elif kind:
        issues.append(
            ValidationIssue(f"{path}.kind", "must be expression or registered")
        )

    constraints_raw = raw.get("constraints", [])
    constraints: list[str] = []
    if not isinstance(constraints_raw, list):
        issues.append(ValidationIssue(f"{path}.constraints", "must be an array"))
    else:
        for index, constraint in enumerate(constraints_raw):
            item = _nonempty_string(constraint, f"{path}.constraints[{index}]", issues)
            if item:
                constraints.append(item)

    requires_raw = raw.get("requires", [])
    requires: list[str] = []
    if not isinstance(requires_raw, list):
        issues.append(ValidationIssue(f"{path}.requires", "must be an array"))
    else:
        for index, requirement in enumerate(requires_raw):
            item = _nonempty_string(requirement, f"{path}.requires[{index}]", issues)
            if item:
                requires.append(item)

    config = _object(raw.get("config", {}), f"{path}.config", issues)
    nan_policy = raw.get("nan_policy", "fail_trial")
    if nan_policy not in {"fail_trial", "reject", "propagate"}:
        issues.append(
            ValidationIssue(
                f"{path}.nan_policy", "must be fail_trial, reject, or propagate"
            )
        )
    division_by_zero = raw.get("division_by_zero", "fail_trial")
    if division_by_zero not in {"fail_trial", "reject", "signed_infinity"}:
        issues.append(
            ValidationIssue(
                f"{path}.division_by_zero",
                "must be fail_trial, reject, or signed_infinity",
            )
        )
    return ObjectiveSpec(
        kind=kind,
        direction=direction,
        expression=expression,
        name=name,
        constraints=tuple(constraints),
        requires=tuple(requires),
        config=config,
        nan_policy=str(nan_policy),
        division_by_zero=str(division_by_zero),
    )


def _parse_sampler(value: Any, issues: list[ValidationIssue]) -> SamplerSpec:
    path = "$.sampler"
    raw = _object(value, path, issues)
    _check_unknown(
        raw, {"kind", "seed", "trials", "candidate_policy", "config"}, path, issues
    )
    kind = _nonempty_string(raw.get("kind"), f"{path}.kind", issues)
    if kind and kind not in {"grid", "random", "dlib_global", "tpe"}:
        issues.append(
            ValidationIssue(f"{path}.kind", "must be grid, random, dlib_global, or tpe")
        )
    seed = raw.get("seed")
    trials = raw.get("trials")
    if not _is_int(seed) or seed < 0:
        issues.append(ValidationIssue(f"{path}.seed", "must be a non-negative integer"))
        seed = 0
    elif kind == "dlib_global" and seed > 2_147_483_647:
        issues.append(
            ValidationIssue(f"{path}.seed", "must be <= 2147483647 for dlib_global")
        )
    if not _is_int(trials) or trials <= 0:
        issues.append(ValidationIssue(f"{path}.trials", "must be a positive integer"))
        trials = 1
    candidate_policy = raw.get("candidate_policy", "sampler_default")
    if not isinstance(candidate_policy, str) or candidate_policy not in {
        "sampler_default",
        "without_replacement",
        "exhaustive",
    }:
        issues.append(
            ValidationIssue(
                f"{path}.candidate_policy",
                "must be sampler_default, without_replacement, or exhaustive",
            )
        )
        candidate_policy = "sampler_default"
    raw_config = _object(raw.get("config", {}), f"{path}.config", issues)
    config: TpeSamplerConfig | None = None
    if kind == "tpe":
        config_path = f"{path}.config"
        _check_unknown(
            raw_config,
            {
                "startup_trials",
                "ei_candidates",
                "gamma_fraction",
                "gamma_cap",
                "prior_weight",
                "constant_liar",
                "history_switch",
            },
            config_path,
            issues,
        )

        startup_trials = raw_config.get("startup_trials", 10)
        if not _is_int(startup_trials) or startup_trials <= 0:
            issues.append(
                ValidationIssue(
                    f"{config_path}.startup_trials", "must be a positive integer"
                )
            )
            startup_trials = 10

        ei_candidates = raw_config.get("ei_candidates", 24)
        if (
            not _is_int(ei_candidates)
            or ei_candidates <= 0
            or ei_candidates > 1_000_000
        ):
            issues.append(
                ValidationIssue(
                    f"{config_path}.ei_candidates",
                    "must be an integer between 1 and 1000000",
                )
            )
            ei_candidates = 24

        gamma_fraction = raw_config.get("gamma_fraction", 0.10)
        if not _is_number(gamma_fraction) or gamma_fraction <= 0 or gamma_fraction > 1:
            issues.append(
                ValidationIssue(
                    f"{config_path}.gamma_fraction",
                    "must be a finite number greater than 0 and at most 1",
                )
            )
            gamma_fraction = 0.10

        gamma_cap = raw_config.get("gamma_cap", 25)
        if not _is_int(gamma_cap) or gamma_cap <= 0:
            issues.append(
                ValidationIssue(
                    f"{config_path}.gamma_cap", "must be a positive integer"
                )
            )
            gamma_cap = 25

        prior_weight = raw_config.get("prior_weight", 1.0)
        if not _is_number(prior_weight) or prior_weight <= 0:
            issues.append(
                ValidationIssue(
                    f"{config_path}.prior_weight",
                    "must be a finite number greater than 0",
                )
            )
            prior_weight = 1.0

        constant_liar = raw_config.get("constant_liar", True)
        if not isinstance(constant_liar, bool):
            issues.append(
                ValidationIssue(f"{config_path}.constant_liar", "must be a boolean")
            )
            constant_liar = True

        history_switch = raw_config.get("history_switch", 1000)
        if not _is_int(history_switch) or not 0 < history_switch <= 2**64 - 1:
            issues.append(
                ValidationIssue(f"{config_path}.history_switch", "must be a positive uint64")
            )
            history_switch = 1000

        config = TpeSamplerConfig(
            startup_trials=startup_trials,
            ei_candidates=ei_candidates,
            gamma_fraction=float(gamma_fraction),
            gamma_cap=gamma_cap,
            prior_weight=float(prior_weight),
            constant_liar=constant_liar,
            history_switch=history_switch,
        )
    elif raw_config:
        issues.append(
            ValidationIssue(f"{path}.config", "is reserved and must be empty")
        )
    return SamplerSpec(
        kind=kind,
        seed=seed,
        trials=trials,
        candidate_policy=str(candidate_policy),
        config=config,
    )


_UINT64_MAX = (1 << 64) - 1
_MAX_EXACT_REAL_ORDINALS = 1 << 53


def _fused_grid_value(index: int, step: float, low: float) -> float:
    fma = getattr(math, "fma", None)
    if fma is not None:
        try:
            return float(fma(float(index), step, low))
        except OverflowError:
            exact_sign = Fraction(index) * Fraction.from_float(
                step
            ) + Fraction.from_float(low)
            return -math.inf if exact_sign < 0 else math.inf
    # math.fma is unavailable on some supported Python 3.11/3.12 builds.
    # Fraction performs the multiply-add exactly before binary64 conversion.
    exact = Fraction(index) * Fraction.from_float(step) + Fraction.from_float(low)
    try:
        return float(exact)
    except OverflowError:
        return -math.inf if exact < 0 else math.inf


def _finite_parameter_cardinality(
    parameter: ParameterSpec,
    path: str,
    issues: list[ValidationIssue],
) -> int | None:
    if parameter.kind == "integer":
        if (
            not _is_int(parameter.low)
            or not _is_int(parameter.high)
            or not _is_int(parameter.step)
            or parameter.step <= 0
            or parameter.low > parameter.high
        ):
            return None
        return (parameter.high - parameter.low) // parameter.step + 1

    if parameter.kind == "real":
        if not _is_number(parameter.low) or not _is_number(parameter.high):
            return None
        low = float(parameter.low)
        high = float(parameter.high)
        if low > high:
            return None
        if low == high:
            return 1
        if parameter.log:
            issues.append(
                ValidationIssue(
                    path,
                    "non-constant log real is not supported by finite candidate policies",
                )
            )
            return None
        if parameter.step is None:
            issues.append(
                ValidationIssue(
                    f"{path}.step",
                    "is required for a varying real under a finite candidate policy",
                )
            )
            return None
        if not _is_number(parameter.step) or parameter.step <= 0:
            return None

        step = float(parameter.step)
        span = high - low
        scaled = span / step if math.isfinite(span) else high / step - low / step
        if not math.isfinite(scaled) or scaled < 0:
            issues.append(ValidationIssue(path, "finite cardinality exceeds uint64"))
            return None
        floored = math.floor(scaled)
        if floored >= _UINT64_MAX:
            issues.append(ValidationIssue(path, "finite cardinality exceeds uint64"))
            return None

        last_index = floored
        next_index = last_index + 1
        decoded_next = _fused_grid_value(next_index, step, low)
        if math.isfinite(decoded_next) and decoded_next <= math.nextafter(
            high, math.inf
        ):
            last_index = next_index
        if last_index == _UINT64_MAX:
            issues.append(ValidationIssue(path, "finite cardinality exceeds uint64"))
            return None
        count = last_index + 1
        if count > _MAX_EXACT_REAL_ORDINALS:
            issues.append(
                ValidationIssue(
                    path,
                    "finite stepped-real cardinality must not exceed 2^53",
                )
            )
            return None
        return count

    if parameter.kind == "boolean":
        return 2

    if parameter.kind == "categorical":
        return len(parameter.choices) if parameter.choices else None

    return None


def _validate_candidate_policy(
    strategy: StrategySpec,
    sampler: SamplerSpec,
    issues: list[ValidationIssue],
) -> None:
    if sampler.candidate_policy == "sampler_default":
        return

    if sampler.kind not in {"grid", "tpe"}:
        if sampler.kind in {"random", "dlib_global"}:
            issues.append(
                ValidationIssue(
                    "$.sampler.candidate_policy",
                    "finite candidate policies are only supported by grid and tpe",
                )
            )
        return

    cardinality = 1
    cardinality_valid = True
    for name, parameter in strategy.search_space.items():
        count = _finite_parameter_cardinality(
            parameter,
            f"$.strategies[0].search_space.{name}",
            issues,
        )
        if count is None:
            cardinality_valid = False
            continue
        if count > _UINT64_MAX or cardinality > _UINT64_MAX // count:
            issues.append(
                ValidationIssue(
                    "$.strategies[0].search_space",
                    "finite cardinality exceeds uint64",
                )
            )
            cardinality_valid = False
            break
        cardinality *= count

    if not cardinality_valid:
        return

    if (
        sampler.candidate_policy == "without_replacement"
        and sampler.trials > cardinality
    ):
        issues.append(
            ValidationIssue(
                "$.sampler.trials",
                "must not exceed finite search-space cardinality "
                f"{cardinality} when candidate_policy=without_replacement",
            )
        )
    elif sampler.candidate_policy == "exhaustive" and sampler.trials != cardinality:
        issues.append(
            ValidationIssue(
                "$.sampler.trials",
                "must equal finite search-space cardinality "
                f"{cardinality} when candidate_policy=exhaustive",
            )
        )


def _parse_execution(value: Any, issues: list[ValidationIssue]) -> ExecutionSpec:
    path = "$.execution"
    raw = _object(value, path, issues)
    _check_unknown(
        raw,
        {
            "workers",
            "isolation",
            "timeout_seconds",
            "fail_fast",
            "batch_size",
            "batch_lag",
            "pruner",
            "pruner_rungs",
            "pruner_eta",
        },
        path,
        issues,
    )
    workers = raw.get("workers")
    if not _is_int(workers) or workers <= 0:
        issues.append(ValidationIssue(f"{path}.workers", "must be a positive integer"))
        workers = 1
    isolation = _nonempty_string(raw.get("isolation"), f"{path}.isolation", issues)
    if isolation not in {"sequential", "threads", "processes"}:
        issues.append(
            ValidationIssue(
                f"{path}.isolation", "must be sequential, threads, or processes"
            )
        )
    if isolation == "sequential" and workers != 1:
        issues.append(ValidationIssue(path, "sequential isolation requires workers=1"))
    timeout = raw.get("timeout_seconds")
    if timeout is not None and (not _is_number(timeout) or timeout <= 0):
        issues.append(ValidationIssue(f"{path}.timeout_seconds", "must be positive"))
        timeout = None
    fail_fast = raw.get("fail_fast", False)
    if not isinstance(fail_fast, bool):
        issues.append(ValidationIssue(f"{path}.fail_fast", "must be a boolean"))
        fail_fast = False
    batch_size = raw.get("batch_size")
    if batch_size is not None and (
        not _is_int(batch_size) or not 1 <= batch_size <= 1000000
    ):
        issues.append(
            ValidationIssue(f"{path}.batch_size", "must be an integer in [1, 1000000]")
        )
        batch_size = None
    batch_lag = raw.get("batch_lag", 0)
    if not _is_int(batch_lag) or batch_lag not in (0, 1):
        issues.append(ValidationIssue(f"{path}.batch_lag", "must be 0 or 1"))
        batch_lag = 0
    pruner = raw.get("pruner", "none")
    if pruner not in ("none", "median", "halving"):
        issues.append(
            ValidationIssue(f"{path}.pruner", "must be none, median, or halving")
        )
        pruner = "none"
    rungs = raw.get("pruner_rungs", [0.25, 0.5])
    if (
        not isinstance(rungs, list)
        or not rungs
        or any(not _is_number(rung) or not 0 < rung < 1 for rung in rungs)
        or any(left >= right for left, right in zip(rungs, rungs[1:]))
    ):
        issues.append(
            ValidationIssue(f"{path}.pruner_rungs", "must increase strictly in (0, 1)")
        )
        rungs = [0.25, 0.5]
    eta = raw.get("pruner_eta", 2)
    if not _is_int(eta) or not 2 <= eta <= 4294967295:
        issues.append(
            ValidationIssue(
                f"{path}.pruner_eta", "must be an integer in [2, 4294967295]"
            )
        )
        eta = 2
    return ExecutionSpec(
        workers=workers,
        isolation=isolation,
        timeout_seconds=float(timeout) if timeout is not None else None,
        fail_fast=fail_fast,
        batch_size=batch_size,
        batch_lag=batch_lag,
        pruner=pruner,
        pruner_rungs=tuple(float(rung) for rung in rungs),
        pruner_eta=eta,
    )


def load_study_spec(path: str | Path, *, require_files: bool = False) -> StudySpec:
    """Load StudySpec v1 and resolve every filesystem path relative to its JSON file."""

    spec_path = Path(path).expanduser().resolve()
    try:
        source = spec_path.read_text(encoding="utf-8")
    except OSError as error:
        raise StudySpecError(
            [ValidationIssue("$", f"cannot read {spec_path}: {error}")]
        ) from error
    try:
        document = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except StudySpecError:
        raise
    except json.JSONDecodeError as error:
        raise StudySpecError(
            [
                ValidationIssue(
                    "$",
                    f"invalid JSON at line {error.lineno}, "
                    f"column {error.colno}: {error.msg}",
                )
            ]
        ) from error

    issues: list[ValidationIssue] = []
    root = _object(document, "$", issues)
    _check_unknown(
        root,
        {
            "schema_version",
            "mode",
            "strategies",
            "datasets",
            "objective",
            "sampler",
            "execution",
        },
        "$",
        issues,
    )
    schema_version = root.get("schema_version")
    if schema_version != 1:
        issues.append(ValidationIssue("$.schema_version", "must equal 1"))
    mode = root.get("mode")
    if mode != "single_strategy":
        issues.append(
            ValidationIssue("$.mode", "this loader currently supports single_strategy")
        )

    strategies = root.get("strategies")
    strategy_raw: Any = {}
    if not isinstance(strategies, list) or len(strategies) != 1:
        issues.append(
            ValidationIssue("$.strategies", "must contain exactly one strategy")
        )
        if isinstance(strategies, list) and strategies:
            strategy_raw = strategies[0]
    else:
        strategy_raw = strategies[0]
    strategy = _parse_strategy(strategy_raw, spec_path.parent, issues)

    datasets_raw = root.get("datasets")
    datasets: list[DatasetSpec] = []
    if not isinstance(datasets_raw, list) or not datasets_raw:
        issues.append(ValidationIssue("$.datasets", "must be a non-empty array"))
    else:
        datasets = [
            _parse_dataset(value, index, spec_path.parent, issues)
            for index, value in enumerate(datasets_raw)
        ]
    dataset_ids = [dataset.id for dataset in datasets if dataset.id]
    if len(set(dataset_ids)) != len(dataset_ids):
        issues.append(ValidationIssue("$.datasets", "dataset ids must be unique"))
    unknown_dataset_ids = sorted(set(strategy.dataset_ids) - set(dataset_ids))
    if unknown_dataset_ids:
        issues.append(
            ValidationIssue(
                "$.strategies[0].datasets",
                "unknown dataset ids: " + ", ".join(unknown_dataset_ids),
            )
        )

    objective = _parse_objective(root.get("objective"), issues)
    sampler = _parse_sampler(root.get("sampler"), issues)
    _validate_candidate_policy(strategy, sampler, issues)
    execution = _parse_execution(root.get("execution"), issues)

    if (
        sampler.kind == "tpe"
        and execution.batch_lag == 1
        and sampler.config is not None
        and not sampler.config.constant_liar
    ):
        issues.append(
            ValidationIssue(
                "$.execution.batch_lag",
                "lag-one TPE requires sampler.config.constant_liar=true",
            )
        )

    if require_files:
        strategy_path = strategy.source or strategy.artifact
        if strategy_path is not None and not strategy_path.is_file():
            issues.append(
                ValidationIssue(
                    "$.strategies[0]", f"file does not exist: {strategy_path}"
                )
            )
        for index, dataset in enumerate(datasets):
            if not dataset.ohlcv.is_file():
                issues.append(
                    ValidationIssue(
                        f"$.datasets[{index}].ohlcv",
                        f"file does not exist: {dataset.ohlcv}",
                    )
                )

    if issues:
        raise StudySpecError(issues)
    return StudySpec(
        schema_version=1,
        mode="single_strategy",
        strategy=strategy,
        datasets=tuple(datasets),
        objective=objective,
        sampler=sampler,
        execution=execution,
        spec_path=spec_path,
    )


__all__ = [
    "DatasetSpec",
    "ExecutionSpec",
    "ObjectiveSpec",
    "ParameterSpec",
    "SamplerSpec",
    "StrategySpec",
    "StudySpec",
    "StudySpecError",
    "TpeSamplerConfig",
    "ValidationIssue",
    "load_study_spec",
]
