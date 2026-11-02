"""Internal, pure validation of declared symbol domains and data identities."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import re
from typing import AbstractSet, Mapping, NoReturn, Sequence

from .cli import _cli_error
from .study_spec import ParameterSpec


@dataclass(frozen=True)
class SymbolFacts:
    """Declared instrument facts; these do not attest to an engine's report unit."""

    tickerid: str
    type: str
    currency: str
    mintick: float
    pointvalue: float
    mincontract: float
    timezone: str
    session: str


@dataclass(frozen=True)
class DatasetDeclaration:
    """Identity and interpretation of a tape, independent of its filesystem path."""

    id: str
    symbol: str
    input_tf: str
    script_tf: str
    chart_timezone: str
    syminfo: SymbolFacts


@dataclass(frozen=True)
class SymbolBinding:
    """A role and an explicit choice-to-dataset map.

    Pairs retain duplicate keys until validation; converting an unvalidated JSON object
    straight to a dict would lose that evidence.
    """

    input_title: str
    role: str
    datasets: tuple[tuple[object, str], ...]


@dataclass(frozen=True)
class ResolvedSymbolBinding:
    """Validated datasets in effective categorical choice order."""

    input_title: str
    role: str
    choices: tuple[str, ...]
    datasets: tuple[DatasetDeclaration, ...]


def _invalid(reason: str, message: str) -> NoReturn:
    raise _cli_error("hpo_study_spec_invalid", {"reason": reason}, message)


def _text(value: object, where: str, reason: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        _invalid(reason, f"{where}: must be nonempty text without control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _invalid(reason, f"{where}: must be valid UTF-8")
    return value


def _timeframe(value: str, where: str) -> str:
    _text(value, where, "backtest")
    if value in ("D", "W", "M", "S"):
        value = "1" + value
    if re.fullmatch(r"[1-9][0-9]*[DWMS]?", value) is None:
        _invalid(
            "backtest",
            f"{where}: must be whole minutes or a positive D/W/M/S timeframe",
        )
    return value


def _dataset(declaration: DatasetDeclaration) -> DatasetDeclaration:
    where = f"datasets.{declaration.id}"
    _text(declaration.id, f"{where}.id", "input_symbol")
    _text(declaration.symbol, f"{where}.symbol", "input_symbol")
    facts = declaration.syminfo
    _text(facts.tickerid, f"{where}.syminfo.tickerid", "input_symbol")
    if facts.tickerid != declaration.symbol:
        _invalid(
            "input_symbol",
            f"{where}.syminfo.tickerid {facts.tickerid!r} differs from "
            f"declared symbol {declaration.symbol!r}",
        )
    for name in ("type", "currency", "timezone", "session"):
        _text(getattr(facts, name), f"{where}.syminfo.{name}", "backtest")
    for name in ("mintick", "pointvalue", "mincontract"):
        number = getattr(facts, name)
        try:
            valid = (
                type(number) in (int, float) and math.isfinite(number) and number > 0
            )
        except OverflowError:
            valid = False
        if not valid:
            _invalid("backtest", f"{where}.syminfo.{name}: must be finite and positive")
    _text(declaration.chart_timezone, f"{where}.chart_timezone", "backtest")
    if declaration.chart_timezone != facts.timezone:
        _invalid("backtest", f"{where}.chart_timezone differs from syminfo.timezone")
    return replace(
        declaration,
        input_tf=_timeframe(declaration.input_tf, f"{where}.input_tf"),
        script_tf=_timeframe(declaration.script_tf, f"{where}.script_tf"),
    )


def validate_symbol_bindings(
    bindings: Sequence[SymbolBinding],
    *,
    search_space: Mapping[str, ParameterSpec],
    referenced_dataset_ids: Sequence[str],
    datasets: Sequence[DatasetDeclaration],
) -> tuple[ResolvedSymbolBinding, ...]:
    """Check a complete effective domain without loading data or sampling candidates.

    The caller supplies the effective search space after its ordinary input validation.
    This component neither admits new StudySpec fields nor bypasses manifest validation.
    Additional dataset declarations are checked but need not be binding targets.
    """

    declarations: dict[str, DatasetDeclaration] = {}
    for declaration in datasets:
        normalized = _dataset(declaration)
        if normalized.id in declarations:
            _invalid(
                "input_symbol", f"datasets: duplicate dataset id {normalized.id!r}"
            )
        declarations[normalized.id] = normalized

    references: set[str] = set()
    for dataset_id in referenced_dataset_ids:
        _text(dataset_id, "strategy.datasets", "input_symbol")
        if dataset_id in references:
            _invalid(
                "input_symbol", f"strategy.datasets: duplicates include {dataset_id!r}"
            )
        if dataset_id not in declarations:
            _invalid(
                "input_symbol", f"strategy.datasets: unknown dataset {dataset_id!r}"
            )
        references.add(dataset_id)

    titles: set[str] = set()
    resolved: list[ResolvedSymbolBinding] = []
    for binding in bindings:
        where = f"symbol_bindings.{binding.input_title}"
        _text(binding.input_title, where, "input_symbol")
        if binding.input_title in titles:
            _invalid("input_symbol", f"{where}: duplicate binding input")
        titles.add(binding.input_title)
        if binding.role not in ("chart", "request"):
            _invalid("input_symbol", f"{where}.role: must be chart or request")
        parameter = search_space.get(binding.input_title)
        if parameter is None or parameter.kind != "categorical":
            _invalid(
                "input_symbol",
                f"{where}: requires an effective categorical search input",
            )
        if not parameter.choices:
            _invalid("input_symbol", f"{where}.choices: domain must not be empty")
        choices: list[str] = []
        seen: set[str] = set()
        for choice in parameter.choices:
            if not isinstance(choice, str):
                _invalid(
                    "input_symbol",
                    f"{where}.choices: symbol choice {choice!r} is not a string",
                )
            _text(choice, f"{where}.choices", "input_symbol")
            if choice in seen:
                _invalid(
                    "input_symbol", f"{where}.choices: duplicate choice {choice!r}"
                )
            seen.add(choice)
            choices.append(choice)

        mapping: dict[str, str] = {}
        targets: set[str] = set()
        for choice, dataset_id in binding.datasets:
            _text(choice, f"{where}.datasets choice", "input_symbol")
            _text(dataset_id, f"{where}.datasets[{choice!r}]", "input_symbol")
            if choice in mapping:
                _invalid("input_symbol", f"{where}.datasets: duplicate key {choice!r}")
            mapping[choice] = dataset_id
        if set(mapping) != seen:
            _invalid(
                "input_symbol",
                f"{where}.datasets: keys must equal choices exactly; "
                f"missing={sorted(seen - set(mapping))!r}, extra={sorted(set(mapping) - seen)!r}",
            )
        selected: list[DatasetDeclaration] = []
        for choice in choices:
            dataset_id = mapping[choice]
            if dataset_id in targets:
                _invalid(
                    "input_symbol", f"{where}.datasets: duplicate target {dataset_id!r}"
                )
            targets.add(dataset_id)
            if dataset_id not in declarations:
                _invalid(
                    "input_symbol",
                    f"{where}.datasets[{choice!r}]: unknown dataset {dataset_id!r}",
                )
            if dataset_id not in references:
                _invalid(
                    "input_symbol",
                    f"{where}.datasets[{choice!r}]: unreferenced dataset {dataset_id!r}",
                )
            declaration = declarations[dataset_id]
            if declaration.symbol != choice:
                _invalid(
                    "input_symbol",
                    f"{where}.datasets[{choice!r}]: dataset {dataset_id!r} declares "
                    f"symbol {declaration.symbol!r}",
                )
            selected.append(declaration)
        resolved.append(
            ResolvedSymbolBinding(
                binding.input_title, binding.role, tuple(choices), tuple(selected)
            )
        )
    if bindings and sum(binding.role == "chart" for binding in bindings) != 1:
        _invalid("input_symbol", "symbol_bindings: exactly one chart role is required")
    return tuple(resolved)


def validate_bound_policy(
    bindings: Sequence[ResolvedSymbolBinding],
    *,
    pruner: str,
    alignment_mode: str,
    approved_alignment_modes: AbstractSet[str],
) -> None:
    """Refuse undecided policy combinations; callers must supply all approved modes."""

    if not bindings:
        return
    if pruner != "none":
        _invalid(
            "pruner", f"execution.pruner {pruner!r}: undecided policy for bound data"
        )
    currencies: dict[str, str] = {}
    for binding in bindings:
        for declaration in binding.datasets:
            currencies[declaration.id] = declaration.syminfo.currency
    if len(set(currencies.values())) > 1:
        _invalid(
            "objective",
            f"objective: undecided quote currency ranking policy for datasets {currencies!r}",
        )
    if alignment_mode not in approved_alignment_modes:
        _invalid(
            "backtest",
            f"data_comparison.alignment {alignment_mode!r}: missing approval in approved modes",
        )
