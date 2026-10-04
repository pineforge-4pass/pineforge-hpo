"""Command-line orchestration for PineScript artifacts and native HPO studies."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence

from . import __version__
from .artifact import ArtifactBuildError, ArtifactBuilder, StrategyArtifact
from .continuation import (
    SPACE_EXHAUSTED_EXIT,
    WARM_START_EXIT,
    SpaceExhaustedError,
    WarmStartError,
    cardinality,
    space_info,
    is_binary_warm,
    warm_start_metadata,
)
from .study_spec import (
    ParameterSpec,
    StudySpec,
    StudySpecError,
    TpeSamplerConfig,
    load_study_spec,
)


class CliError(RuntimeError):
    """A user-facing CLI configuration or execution error."""


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_engine_root(value: str | None) -> Path:
    if value:
        resolved = Path(value).expanduser().resolve()
        if (resolved / "include" / "pineforge" / "pineforge.h").is_file():
            return resolved
        raise CliError(f"--engine-root is not a pineforge-engine tree: {resolved}")
    environment = os.environ.get("PINEFORGE_ENGINE_ROOT")
    if environment:
        resolved = Path(environment).expanduser().resolve()
        if (resolved / "include" / "pineforge" / "pineforge.h").is_file():
            return resolved
        raise CliError(
            f"PINEFORGE_ENGINE_ROOT is not a pineforge-engine tree: {resolved}"
        )
    root = _repository_root()
    for candidate in (
        root / "external" / "pineforge-engine",
        root.parent / "pineforge-engine",
    ):
        resolved = candidate.resolve()
        if (resolved / "include" / "pineforge" / "pineforge.h").is_file():
            return resolved
    raise CliError(
        "pineforge-engine was not found; initialize external/pineforge-engine, "
        "pass --engine-root, or set PINEFORGE_ENGINE_ROOT"
    )


def _resolve_native(value: str | None) -> Path:
    if value:
        resolved = Path(value).expanduser().resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
        raise CliError(f"--native is not an executable file: {resolved}")
    environment = os.environ.get("PINEFORGE_HPO_NATIVE")
    if environment:
        resolved = Path(environment).expanduser().resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
        raise CliError(f"PINEFORGE_HPO_NATIVE is not executable: {resolved}")
    root = _repository_root()
    candidates = (
        root / "build" / "bin" / "pineforge-hpo-native",
        root / "build-release" / "bin" / "pineforge-hpo-native",
        root / "build-mvp" / "bin" / "pineforge-hpo-native",
    )
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    from_path = shutil.which("pineforge-hpo-native")
    if from_path:
        return Path(from_path).resolve()
    raise CliError(
        "pineforge-hpo-native was not found; build the CMake target, pass --native, "
        "or set PINEFORGE_HPO_NATIVE"
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _abi_scalar(value: object, *, where: str) -> str:
    if value is None:
        raise CliError(f"{where} cannot be null")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format(value, ".17g")
    if isinstance(value, (str, int)):
        return str(value)
    raise CliError(f"{where} must be a JSON scalar")


def _artifact_json(artifact: StrategyArtifact) -> dict[str, Any]:
    return {
        "artifact_key": artifact.artifact_key,
        "request_key": artifact.request_key,
        "plugin": str(artifact.plugin_path),
        "generated_cpp": str(artifact.generated_cpp_path),
        "manifest": str(artifact.manifest_path),
        "provenance": str(artifact.provenance_path),
        "cache_hit": artifact.cache_hit,
    }


def _precompiled_artifact(
    path: Path,
) -> tuple[dict[str, Any], tuple[Mapping[str, Any], ...]]:
    manifest_path = path.parent / "manifest.json"
    if not manifest_path.is_file():
        raise CliError(
            "a precompiled strategy requires its adjacent manifest.json so "
            "input names and types can be validated"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CliError(
            f"cannot read artifact manifest {manifest_path}: {error}"
        ) from error
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
        raise CliError(f"unsupported artifact manifest: {manifest_path}")
    expected_sha256 = manifest.get("plugin_sha256")
    actual_sha256 = _file_sha256(path)
    if expected_sha256 != actual_sha256:
        raise CliError(f"precompiled plugin hash does not match {manifest_path}")
    raw_inputs = manifest.get("inputs")
    if not isinstance(raw_inputs, list) or not all(
        isinstance(item, Mapping) for item in raw_inputs
    ):
        raise CliError(f"artifact manifest has an invalid input list: {manifest_path}")
    artifact_key = manifest.get("artifact_key")
    request_key = manifest.get("request_key")
    if not isinstance(artifact_key, str) or len(artifact_key) != 64:
        raise CliError(
            f"artifact manifest has an invalid artifact key: {manifest_path}"
        )
    provenance_path = path.parent / "provenance.json"
    generated_cpp_path = path.parent / "generated.cpp"
    artifact = {
        "artifact_key": artifact_key,
        "request_key": request_key if isinstance(request_key, str) else None,
        "plugin": str(path),
        "generated_cpp": (
            str(generated_cpp_path) if generated_cpp_path.is_file() else None
        ),
        "manifest": str(manifest_path),
        "provenance": str(provenance_path) if provenance_path.is_file() else None,
        "cache_hit": True,
    }
    return artifact, tuple(dict(item) for item in raw_inputs)


def _same_scalar(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _value_matches_input_type(value: object, input_type: str) -> bool:
    if input_type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if input_type == "float":
        return _is_number(value)
    if input_type == "bool":
        return isinstance(value, bool)
    if input_type in {"string", "source"}:
        return isinstance(value, str)
    if input_type == "enum":
        return value is not None and isinstance(value, (str, int, float, bool))
    return False


def _validate_value_against_manifest(
    value: object, manifest: Mapping[str, Any], *, where: str
) -> None:
    input_type = manifest.get("type")
    if not isinstance(input_type, str) or not _value_matches_input_type(
        value, input_type
    ):
        raise CliError(f"{where} is incompatible with Pine input type {input_type!r}")
    if _is_number(value):
        minimum = manifest.get("min")
        maximum = manifest.get("max")
        if _is_number(minimum) and value < minimum:
            raise CliError(f"{where} is below the Pine input minimum {minimum}")
        if _is_number(maximum) and value > maximum:
            raise CliError(f"{where} is above the Pine input maximum {maximum}")
    options = manifest.get("options")
    if isinstance(options, list) and not any(
        _same_scalar(value, option) for option in options
    ):
        raise CliError(f"{where} is not one of the Pine input options")


def _validate_strategy_overrides(study: StudySpec) -> None:
    overrides = study.strategy.strategy_overrides
    numeric = {"initial_capital", "commission_value", "default_qty_value"}
    integer = {"pyramiding", "slippage"}
    boolean = {"process_orders_on_close", "calc_on_order_fills"}
    enum_values = {
        "default_qty_type": {
            "fixed",
            "strategy.fixed",
            "0",
            "percent_of_equity",
            "strategy.percent_of_equity",
            "1",
            "cash",
            "strategy.cash",
            "2",
        },
        "commission_type": {
            "percent",
            "strategy.commission.percent",
            "0",
            "cash_per_order",
            "strategy.commission.cash_per_order",
            "1",
            "cash_per_contract",
            "strategy.commission.cash_per_contract",
            "2",
        },
        "close_entries_rule": {"FIFO", "fifo", "ANY", "any", "0", "1"},
    }
    allowed = numeric | integer | boolean | set(enum_values)
    unknown = sorted(set(overrides) - allowed)
    if unknown:
        raise CliError("unsupported strategy overrides: " + ", ".join(unknown))
    for name, value in overrides.items():
        text = _abi_scalar(value, where=f"strategy_overrides.{name}")
        if name in numeric:
            try:
                parsed = float(text)
            except ValueError as error:
                raise CliError(f"strategy_overrides.{name} must be numeric") from error
            if not math.isfinite(parsed):
                raise CliError(f"strategy_overrides.{name} must be finite")
            if name in {"initial_capital", "default_qty_value"} and parsed <= 0:
                raise CliError(f"strategy_overrides.{name} must be positive")
            if name == "commission_value" and parsed < 0:
                raise CliError(
                    "strategy_overrides.commission_value must be non-negative"
                )
        elif name in integer:
            if re.fullmatch(r"[+-]?\d+", text) is None:
                raise CliError(f"strategy_overrides.{name} must be an integer")
            if int(text) < 0:
                raise CliError(f"strategy_overrides.{name} must be non-negative")
        elif name in boolean and text not in {"true", "false", "1", "0"}:
            raise CliError(f"strategy_overrides.{name} must be true/false or 1/0")
        elif name in enum_values and text not in enum_values[name]:
            raise CliError(f"strategy_overrides.{name} has an unsupported value")


def _validate_manifest_inputs(
    study: StudySpec, inputs: Sequence[Mapping[str, Any]]
) -> None:
    by_title: dict[str, Mapping[str, Any]] = {}
    for item in inputs:
        title = item.get("title")
        if not isinstance(title, str) or not title:
            raise CliError("artifact input manifest contains an invalid title")
        if title in by_title:
            raise CliError(
                f"artifact input manifest contains duplicate title {title!r}"
            )
        by_title[title] = item
    requested = set(study.strategy.search_space) | set(study.strategy.fixed_inputs)
    unknown = sorted(requested - set(by_title))
    if unknown:
        raise CliError(
            "StudySpec references inputs not emitted by pineforge-codegen-oss: "
            + ", ".join(unknown)
        )
    for name, parameter in study.strategy.search_space.items():
        manifest = by_title[name]
        input_type = manifest.get("type")
        if parameter.kind == "categorical":
            for index, choice in enumerate(parameter.choices):
                _validate_value_against_manifest(
                    choice, manifest, where=f"search_space.{name}.choices[{index}]"
                )
            continue
        expected_kind = {
            "int": "integer",
            "float": "real",
            "bool": "boolean",
        }.get(input_type)
        if expected_kind != parameter.kind:
            raise CliError(
                f"search_space.{name} kind {parameter.kind!r} is incompatible "
                f"with Pine input type {input_type!r}"
            )
        if parameter.low is not None:
            _validate_value_against_manifest(
                parameter.low, manifest, where=f"search_space.{name}.low"
            )
        if parameter.high is not None:
            _validate_value_against_manifest(
                parameter.high, manifest, where=f"search_space.{name}.high"
            )
    for name, value in study.strategy.fixed_inputs.items():
        _validate_value_against_manifest(
            value, by_title[name], where=f"fixed_inputs.{name}"
        )
    _validate_strategy_overrides(study)


def _add_dimension(command: list[str], name: str, parameter: ParameterSpec) -> None:
    if parameter.kind == "integer":
        if parameter.log:
            command.extend(
                (
                    "--log-int-dim",
                    name,
                    str(parameter.low),
                    str(parameter.high),
                )
            )
            return
        command.extend(
            (
                "--int-dim",
                name,
                str(parameter.low),
                str(parameter.high),
                str(parameter.step),
            )
        )
        return
    if parameter.kind == "real":
        if parameter.log:
            command.extend(
                (
                    "--log-real-dim",
                    name,
                    str(parameter.low),
                    str(parameter.high),
                )
            )
            return
        command.extend(
            (
                "--real-dim",
                name,
                str(parameter.low),
                str(parameter.high),
                "continuous" if parameter.step is None else str(parameter.step),
            )
        )
        return
    if parameter.kind == "boolean":
        command.extend(("--bool-dim", name))
        return
    if parameter.kind == "categorical":
        serialized: set[str] = set()
        for index, choice in enumerate(parameter.choices):
            value = _abi_scalar(choice, where=f"search_space.{name}.choices[{index}]")
            if value in serialized:
                raise CliError(
                    f"search_space.{name}: choices collide after strategy ABI serialization"
                )
            serialized.add(value)
            if isinstance(choice, bool):
                option = "--categorical-bool-choice"
            elif isinstance(choice, int):
                option = "--categorical-int-choice"
            elif isinstance(choice, float):
                option = "--categorical-real-choice"
            else:
                option = "--categorical-choice"
            command.extend((option, name, value))
        return
    raise CliError(f"search_space.{name}: unsupported kind {parameter.kind!r}")


def _native_command(
    study: StudySpec,
    *,
    native: Path,
    plugin: Path,
    artifact_key: str,
) -> list[str]:
    if study.objective.kind != "expression" or not study.objective.expression:
        raise CliError(
            "the executable MVP supports objective.kind=expression; custom C++ "
            "objectives use the ObjectiveFn API"
        )
    if study.sampler.kind not in {"grid", "random", "dlib_global", "tpe"}:
        raise CliError(
            "the executable supports sampler.kind=grid, random, dlib_global, or tpe"
        )
    if study.sampler.kind == "dlib_global" and study.sampler.seed > 2_147_483_647:
        raise CliError(
            "sampler.seed must be <= 2147483647 for portable dlib_global seeding"
        )
    if study.execution.isolation == "processes":
        raise CliError("process isolation is reserved for the portfolio milestone")
    if study.execution.timeout_seconds is not None:
        raise CliError("execution.timeout_seconds is not implemented in the MVP")
    if study.execution.fail_fast:
        raise CliError("execution.fail_fast is not implemented in the MVP")
    if len(study.strategy.dataset_ids) != 1:
        raise CliError("the executable MVP requires exactly one dataset per strategy")
    if len(study.datasets) != 1:
        raise CliError("the executable MVP requires exactly one dataset per study")
    _validate_strategy_overrides(study)

    datasets = {dataset.id: dataset for dataset in study.datasets}
    dataset = datasets[study.strategy.dataset_ids[0]]
    command = [
        str(native),
        "run",
        "--strategy",
        str(plugin),
        "--ohlcv",
        str(dataset.ohlcv),
        "--objective",
        study.objective.expression,
        "--sampler",
        study.sampler.kind,
        "--candidate-policy",
        study.sampler.candidate_policy,
        "--max-trials",
        str(study.sampler.trials),
        "--seed",
        str(study.sampler.seed),
        "--workers",
        str(study.execution.workers),
        "--direction",
        study.objective.direction,
        "--input-tf",
        dataset.input_tf,
        "--script-tf",
        dataset.script_tf,
        "--chart-timezone",
        dataset.chart_timezone,
        "--artifact-key",
        artifact_key,
        "--division-by-zero",
        "ieee" if study.objective.division_by_zero == "signed_infinity" else "reject",
        "--non-finite",
        "allow" if study.objective.nan_policy == "propagate" else "reject",
    ]
    if study.execution.batch_size is not None:
        command.extend(("--batch-size", str(study.execution.batch_size)))
    if study.execution.batch_lag:
        command.extend(("--batch-lag", str(study.execution.batch_lag)))
    if study.execution.pruner != "none":
        command.extend(
            (
                "--pruner",
                study.execution.pruner,
                "--pruner-rungs",
                ",".join(map(str, study.execution.pruner_rungs)),
                "--pruner-eta",
                str(study.execution.pruner_eta),
            )
        )
    if study.sampler.kind == "tpe":
        if not isinstance(study.sampler.config, TpeSamplerConfig):
            raise CliError("sampler.kind=tpe requires a valid typed sampler.config")
        config = study.sampler.config
        if config.history_switch is not None:
            command.extend(("--tpe-history-switch", str(config.history_switch)))
        command.extend(
            (
                "--tpe-startup-trials",
                str(config.startup_trials),
                "--tpe-ei-candidates",
                str(config.ei_candidates),
                "--tpe-gamma-fraction",
                str(config.gamma_fraction),
                "--tpe-gamma-cap",
                str(config.gamma_cap),
                "--tpe-prior-weight",
                str(config.prior_weight),
                "--tpe-constant-liar",
                str(config.constant_liar).lower(),
            )
        )
    for expression in study.objective.constraints:
        command.extend(("--constraint", expression))
    for name, parameter in study.strategy.search_space.items():
        if (
            study.sampler.kind == "grid"
            and parameter.kind == "real"
            and parameter.step is None
        ):
            raise CliError(f"search_space.{name}: grid sampling a real requires step")
        _add_dimension(command, name, parameter)
    for name, value in study.strategy.fixed_inputs.items():
        command.extend(
            ("--fixed-input", name, _abi_scalar(value, where=f"fixed_inputs.{name}"))
        )
    for name, value in study.strategy.strategy_overrides.items():
        command.extend(
            (
                "--strategy-override",
                name,
                _abi_scalar(value, where=f"strategy_overrides.{name}"),
            )
        )
    return command


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _builder(args: argparse.Namespace, engine_root: Path) -> ArtifactBuilder:
    return ArtifactBuilder(
        engine_root=engine_root,
        cache_dir=args.cache_dir,
        compiler=args.compiler,
        eigen_include=args.eigen_include,
    )


def _compile(args: argparse.Namespace) -> int:
    source_path = Path(args.source).expanduser().resolve()
    try:
        source = source_path.read_text(encoding="utf-8")
    except OSError as error:
        raise CliError(f"cannot read Pine source {source_path}: {error}") from error
    artifact = _builder(args, _resolve_engine_root(args.engine_root)).build(
        source, filename=str(source_path)
    )
    document = {"ok": True, **_artifact_json(artifact)}
    print(json.dumps(document, indent=2, sort_keys=True, allow_nan=False))
    return 0


def prepare_run(
    study_path: str | Path,
    engine_root: str | Path | None = None,
    cache_dir: str | Path | None = None,
    *,
    native: str | Path | None = None,
    compiler: str | None = None,
    eigen_include: str | Path | None = None,
    warm_start: str | Path | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Validate/build once and return native argv and artifact JSON without launching."""
    if warm_start is not None:
        preflight = load_study_spec(study_path, continuation=True)
        if preflight.sampler.kind == "dlib_global":
            raise WarmStartError(
                "warm-start incompatible: dlib_global is not supported"
            )
        history = warm_start_metadata(preflight, warm_start, native=native)
        if preflight.execution.pruner != "none" and is_binary_warm(warm_start):
            raise WarmStartError(
                "warm-start incompatible: binary history has no pruning rungs"
            )
        count = cardinality(preflight)
        remaining = None if count is None else count - history["tried"]
        if remaining == 0:
            raise SpaceExhaustedError(
                "space exhausted: every parameter vector was tried"
            )
        if preflight.sampler.candidate_policy != "sampler_default" and (
            preflight.sampler.trials > remaining
            or (
                preflight.sampler.candidate_policy == "exhaustive"
                and preflight.sampler.trials != remaining
            )
        ):
            raise WarmStartError(
                "warm-start incompatible: finite budget does not fit remaining space"
            )
        if preflight.sampler.trials > (1 << 64) - 1 - history["next_id"]:
            raise WarmStartError(
                "warm-start incompatible: new trial budget would overflow trial IDs"
            )
    study = load_study_spec(
        study_path, require_files=True, continuation=warm_start is not None
    )
    artifact: dict[str, Any]
    if study.strategy.source is not None:
        source_path = study.strategy.source
        try:
            source = source_path.read_text(encoding="utf-8")
        except OSError as error:
            raise CliError(f"cannot read Pine source {source_path}: {error}") from error
        built = ArtifactBuilder(
            engine_root=_resolve_engine_root(
                None if engine_root is None else str(engine_root)
            ),
            cache_dir=cache_dir,
            compiler=compiler,
            eigen_include=eigen_include,
        ).build(source, filename=str(source_path))
        _validate_manifest_inputs(study, built.inputs)
        artifact = _artifact_json(built)
    else:
        assert study.strategy.artifact is not None
        artifact, inputs = _precompiled_artifact(study.strategy.artifact)
        _validate_manifest_inputs(study, inputs)

    native_path = _resolve_native(None if native is None else str(native))
    command = _native_command(
        study,
        native=native_path,
        plugin=Path(artifact["plugin"]),
        artifact_key=str(artifact["artifact_key"]),
    )
    if warm_start is not None:
        command.extend(("--warm-start", str(Path(warm_start).expanduser().resolve())))
    return command, artifact


def _run(args: argparse.Namespace) -> int:
    study = load_study_spec(args.study, continuation=args.warm_start is not None)
    command, artifact = prepare_run(
        study.spec_path,
        args.engine_root,
        args.cache_dir,
        native=args.native,
        compiler=args.compiler,
        eigen_include=args.eigen_include,
        warm_start=args.warm_start,
    )
    if args.progress_fd is not None:
        command.extend(("--progress-fd", str(args.progress_fd)))
    if args.trials_file:
        command.extend(
            ("--trials-file", str(Path(args.trials_file).expanduser().resolve()))
        )
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            **(
                {"pass_fds": (args.progress_fd,)}
                if args.progress_fd is not None
                else {}
            ),
        )
    except OSError as error:
        raise CliError(f"cannot start native runner: {error}") from error
    if completed.returncode in {WARM_START_EXIT, SPACE_EXHAUSTED_EXIT}:
        print(completed.stderr.strip(), file=sys.stderr)
        return completed.returncode
    if completed.returncode not in {0, 2}:
        detail = (
            completed.stderr.strip() or completed.stdout.strip() or "no diagnostics"
        )
        raise CliError(
            f"native runner failed with exit {completed.returncode}: {detail}"
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise CliError(f"native runner returned invalid JSON: {error}") from error
    if not isinstance(result, dict):
        raise CliError("native runner returned a non-object JSON result")

    dataset_id = study.strategy.dataset_ids[0]
    result.update(
        {
            "study": str(study.spec_path),
            "strategy_id": study.strategy.id,
            "dataset_id": dataset_id,
            "artifact": artifact,
        }
    )
    if args.output:
        _write_json(Path(args.output), result)
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return completed.returncode


def _space_info(args: argparse.Namespace) -> int:
    study = load_study_spec(args.spec, continuation=True)
    print(
        json.dumps(
            space_info(study, args.warm_start, native=args.native),
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


def _warm_encode(args: argparse.Namespace) -> int:
    command = [
        str(_resolve_native(args.native)),
        "warm-encode",
        "--spec",
        args.spec,
        "--input",
        args.input,
        "--output",
        args.output,
    ]
    if args.block_trials is not None:
        if args.block_trials <= 0:
            raise CliError("--block-trials must be positive")
        command.extend(("--block-trials", str(args.block_trials)))
    return subprocess.run(command, check=False).returncode


def _add_build_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--engine-root", help="pineforge-engine source/build tree")
    parser.add_argument("--cache-dir", help="content-addressed artifact cache")
    parser.add_argument("--compiler", help="C++ compiler path")
    parser.add_argument("--eigen-include", help="Eigen include directory")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pineforge-hpo",
        description="Compile PineScript strategies and run native PineForge HPO studies.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    compile_parser = commands.add_parser(
        "compile", help="build/cache a strategy plugin"
    )
    compile_parser.add_argument("source", help="PineScript strategy source")
    _add_build_options(compile_parser)
    compile_parser.set_defaults(handler=_compile)

    run_parser = commands.add_parser("run", help="execute a StudySpec v1 JSON file")
    run_parser.add_argument("study", help="StudySpec JSON path")
    run_parser.add_argument("--native", help="pineforge-hpo-native executable")
    run_parser.add_argument("--output", help="write the final JSON result atomically")
    run_parser.add_argument(
        "--warm-start",
        help="binary v2 blocks, parent fd-3 JSONL or complete result JSON",
    )
    run_parser.add_argument(
        "--progress-fd", type=int, help="terminal new trials descriptor"
    )
    run_parser.add_argument("--trials-file", help="write only new trials as JSONL")
    _add_build_options(run_parser)
    run_parser.set_defaults(handler=_run)
    info_parser = commands.add_parser(
        "space-info", help="inspect coverage without execution"
    )
    info_parser.add_argument("--spec", required=True, help="StudySpec JSON path")
    info_parser.add_argument("--native", help="native binary loader for v2 input")
    info_parser.add_argument(
        "--warm-start",
        help="binary v2 blocks, parent fd-3 JSONL or complete result JSON",
    )
    info_parser.set_defaults(handler=_space_info)
    encode_parser = commands.add_parser(
        "warm-encode", help="encode JSON/JSONL history as v2"
    )
    encode_parser.add_argument("--spec", required=True, help="StudySpec JSON path")
    encode_parser.add_argument(
        "--input", required=True, help="complete JSON/JSONL history"
    )
    encode_parser.add_argument(
        "--output", required=True, help="binary warm output path"
    )
    encode_parser.add_argument(
        "--block-trials", type=int, help="maximum rows per output block"
    )
    encode_parser.add_argument("--native", help="pineforge-hpo-native executable")
    encode_parser.set_defaults(handler=_warm_encode)
    return parser


def _format_artifact_error(error: ArtifactBuildError) -> str:
    detail = f"artifact {error.stage} failed: {error}"
    if error.diagnostics:
        diagnostics = "; ".join(
            f"{item.severity}: {item.message}" for item in error.diagnostics
        )
        detail += f" ({diagnostics})"
    elif error.stderr.strip():
        detail += f" ({error.stderr.strip()})"
    return detail


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except WarmStartError as error:
        print(f"pineforge-hpo: {error}", file=sys.stderr)
        return WARM_START_EXIT
    except SpaceExhaustedError as error:
        print(f"pineforge-hpo: {error}", file=sys.stderr)
        return SPACE_EXHAUSTED_EXIT
    except ArtifactBuildError as error:
        print(f"pineforge-hpo: {_format_artifact_error(error)}", file=sys.stderr)
    except (CliError, StudySpecError) as error:
        print(f"pineforge-hpo: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
