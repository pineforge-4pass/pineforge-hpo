#!/usr/bin/env python3
"""Apples-to-apples PineForge native TPE versus Optuna TPESampler benchmark."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata as package_metadata
import itertools
import json
import math
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import optuna


PI = math.pi
EXPECTED_OPTUNA_VERSION = "4.9.0"
DEFAULT_SEEDS = (17, 41, 73, 109, 149)
SMOKE_SEEDS = (17,)
SMOKE_PROBLEMS = ("branin2", "mixed_log", "million_discrete")
SMOKE_TRIALS = 32
DEFAULT_STARTUP_TRIALS = 10
SMOKE_STARTUP_TRIALS = 8
CSV_SCHEMA_VERSION = 1
METADATA_SCHEMA = "pineforge-hpo.optuna-benchmark-metadata.v1"
NATIVE_IMPLEMENTATION = "pineforge_native"
OPTUNA_IMPLEMENTATION = "optuna_4.9.0"
EI_CANDIDATES = 24
GAMMA_FRACTION = 0.10
GAMMA_CAP = 25
PRIOR_WEIGHT = 1.0
BENCHMARK_PACKAGES = (
    "optuna",
    "numpy",
    "SQLAlchemy",
    "alembic",
    "greenlet",
    "Mako",
    "MarkupSafe",
    "PyYAML",
    "tqdm",
    "colorlog",
    "packaging",
    "typing_extensions",
)


@dataclass(frozen=True)
class Problem:
    name: str
    trials: int
    optimum: float
    ratio_floor: float
    known_optimum: dict[str, object]
    suggest: Callable[[optuna.Trial], dict[str, object]]
    objective: Callable[[dict[str, object]], float]


def branin(params: dict[str, object]) -> float:
    x1 = float(params["x1"])
    x2 = float(params["x2"])
    b = 5.1 / (4.0 * PI * PI)
    c = 5.0 / PI
    t = 1.0 / (8.0 * PI)
    return (
        (x2 - b * x1 * x1 + c * x1 - 6.0) ** 2 + 10.0 * (1.0 - t) * math.cos(x1) + 10.0
    )


def suggest_branin(trial: optuna.Trial) -> dict[str, object]:
    return {
        "x1": trial.suggest_float("x1", -5.0, 10.0),
        "x2": trial.suggest_float("x2", 0.0, 15.0),
    }


HARTMANN3_ALPHA = (1.0, 1.2, 3.0, 3.2)
HARTMANN3_A = (
    (3.0, 10.0, 30.0),
    (0.1, 10.0, 35.0),
    (3.0, 10.0, 30.0),
    (0.1, 10.0, 35.0),
)
HARTMANN3_P = (
    (0.3689, 0.1170, 0.2673),
    (0.4699, 0.4387, 0.7470),
    (0.1091, 0.8732, 0.5547),
    (0.0381, 0.5743, 0.8828),
)


def hartmann3(params: dict[str, object]) -> float:
    x = tuple(float(params[f"x{index}"]) for index in range(1, 4))
    result = 0.0
    for alpha, coefficients, center in zip(
        HARTMANN3_ALPHA, HARTMANN3_A, HARTMANN3_P, strict=True
    ):
        exponent = sum(
            coefficient * (value - target) ** 2
            for coefficient, value, target in zip(coefficients, x, center, strict=True)
        )
        result -= alpha * math.exp(-exponent)
    return result


def suggest_unit3(trial: optuna.Trial) -> dict[str, object]:
    return {
        f"x{index}": trial.suggest_float(f"x{index}", 0.0, 1.0) for index in range(1, 4)
    }


def rosenbrock6(params: dict[str, object]) -> float:
    x = tuple(float(params[f"x{index}"]) for index in range(1, 7))
    return sum(
        100.0 * (following - value * value) ** 2 + (1.0 - value) ** 2
        for value, following in zip(x, x[1:])
    )


def suggest_rosenbrock6(trial: optuna.Trial) -> dict[str, object]:
    return {
        f"x{index}": trial.suggest_float(f"x{index}", -2.048, 2.048)
        for index in range(1, 7)
    }


ROTATED_SHIFT = (3.1, -2.7, 1.9, -3.3, 2.4, -1.5)
ROTATIONS = (
    (0, 1, 0.41),
    (2, 5, -0.73),
    (1, 4, 1.07),
    (0, 3, -0.56),
    (2, 4, 0.92),
    (1, 5, -1.19),
    (3, 4, 0.67),
    (0, 2, -0.88),
    (3, 5, 1.31),
    (0, 4, 0.35),
    (1, 3, -0.64),
    (2, 5, 0.79),
)

MILLION_TARGET = (8, 1, 6, 3, 9, 4)


def rotated_rastrigin6(params: dict[str, object]) -> float:
    z = [float(params[f"x{index + 1}"]) - ROTATED_SHIFT[index] for index in range(6)]
    for first, second, angle in ROTATIONS:
        cosine = math.cos(angle)
        sine = math.sin(angle)
        old_first = z[first]
        old_second = z[second]
        z[first] = cosine * old_first - sine * old_second
        z[second] = sine * old_first + cosine * old_second
    return 60.0 + sum(value * value - 10.0 * math.cos(2.0 * PI * value) for value in z)


def suggest_rotated_rastrigin6(trial: optuna.Trial) -> dict[str, object]:
    return {
        f"x{index}": trial.suggest_float(f"x{index}", -5.12, 5.12)
        for index in range(1, 7)
    }


MIXED_BASINS = {
    "trend": (12.0, 80.0, 2e-5, 0.7, 0.55),
    "mean": (75.0, 120_000.0, 0.8, 2.3, 0.80),
    "breakout": (24.0, 900.0, 8e-3, 1.9, 0.35),
    "hybrid": (37.0, 3_000.0, 0.002, 1.4, 0.0),
    "carry": (88.0, 600_000.0, 2e-7, 0.4, 1.10),
    "noise": (5.0, 12.0, 20.0, 2.8, 2.50),
}


def mixed_log(params: dict[str, object]) -> float:
    center_depth, center_period, center_rate, center_leverage, offset = MIXED_BASINS[
        str(params["mode"])
    ]
    d = (int(params["depth"]) - center_depth) / 12.0
    p = math.log(int(params["period"]) / center_period)
    r = math.log(float(params["rate"]) / center_rate)
    leverage = float(params["leverage"])
    ell = (leverage - center_leverage) / 0.5
    disabled = 0.0 if bool(params["enabled"]) else 1.5
    return (
        offset
        + disabled
        + (d + 0.45 * p - 0.20 * ell) ** 2
        + 0.7 * (p - 0.55 * r) ** 2
        + 0.6 * (r + 0.35 * ell) ** 2
        + 0.45 * (ell - 0.25 * d) ** 2
        + 0.025 * (p * r) ** 2
        + 0.04 * (1.0 - math.cos(2.0 * PI * d))
    )


def suggest_mixed_log(trial: optuna.Trial) -> dict[str, object]:
    return {
        "mode": trial.suggest_categorical("mode", list(MIXED_BASINS)),
        "enabled": trial.suggest_categorical("enabled", [False, True]),
        "depth": trial.suggest_int("depth", 1, 96),
        "period": trial.suggest_int("period", 1, 1_000_000, log=True),
        "rate": trial.suggest_float("rate", 1e-8, 1e2, log=True),
        "leverage": trial.suggest_float("leverage", 0.2, 3.0, step=0.1),
    }


def circular_residue(value: int, modulus: int) -> int:
    residue = value % modulus
    return min(residue, modulus - residue)


def million_discrete_tuple(values: tuple[int, ...]) -> float:
    d = tuple(
        value - target for value, target in zip(values, MILLION_TARGET, strict=True)
    )
    rugged = (
        17 * circular_residue(3 * d[0] + 5 * d[1] + 7 * d[2] + 2 * d[3], 11) ** 2
        + 13 * circular_residue(2 * d[1] + 3 * d[2] + 5 * d[3] + 7 * d[4], 13) ** 2
        + 11 * circular_residue(5 * d[0] + 2 * d[2] + 3 * d[4] + 7 * d[5], 17) ** 2
        + 19
        * circular_residue(d[0] * d[3] + 2 * d[1] * d[4] + 3 * d[2] * d[5], 11) ** 2
        + 7 * circular_residue(d[0] * d[1] + d[2] * d[3] + d[4] * d[5], 13) ** 2
    )
    return float(100 * rugged + sum(value * value for value in d))


def million_discrete(params: dict[str, object]) -> float:
    values = tuple(int(params[f"x{index}"]) for index in range(1, 7))
    return million_discrete_tuple(values)


def suggest_million_discrete(trial: optuna.Trial) -> dict[str, object]:
    return {f"x{index}": trial.suggest_int(f"x{index}", 0, 9) for index in range(1, 7)}


PROBLEMS = (
    Problem(
        "branin2",
        128,
        0.39788735772973816,
        0.01,
        {"x1": -PI, "x2": 12.275},
        suggest_branin,
        branin,
    ),
    Problem(
        "hartmann3",
        192,
        -3.8627797869493365,
        0.01,
        {"x1": 0.11461292, "x2": 0.55564907, "x3": 0.85254695},
        suggest_unit3,
        hartmann3,
    ),
    Problem(
        "rosenbrock6",
        512,
        0.0,
        1.0,
        {f"x{index}": 1.0 for index in range(1, 7)},
        suggest_rosenbrock6,
        rosenbrock6,
    ),
    Problem(
        "rotated_rastrigin6",
        640,
        0.0,
        1.0,
        {f"x{index + 1}": value for index, value in enumerate(ROTATED_SHIFT)},
        suggest_rotated_rastrigin6,
        rotated_rastrigin6,
    ),
    Problem(
        "mixed_log",
        384,
        0.0,
        0.02,
        {
            "mode": "hybrid",
            "enabled": True,
            "depth": 37,
            "period": 3_000,
            "rate": 0.002,
            "leverage": 1.4,
        },
        suggest_mixed_log,
        mixed_log,
    ),
    Problem(
        "million_discrete",
        1_000,
        0.0,
        1.0,
        {f"x{index + 1}": value for index, value in enumerate(MILLION_TARGET)},
        suggest_million_discrete,
        million_discrete,
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--native", type=Path, required=True, help="native benchmark executable"
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "run a fast integration preset (one seed, three representative problems, "
            "32 trials each); explicit selection and budget options override the preset"
        ),
    )
    parser.add_argument(
        "--seeds",
        help="comma-separated fixed seeds; defaults to five paired seeds",
    )
    parser.add_argument(
        "--problems",
        help="comma-separated problem names; defaults to the full suite",
    )
    parser.add_argument(
        "--trials",
        type=int,
        help="override every selected problem's declared trial budget",
    )
    parser.add_argument(
        "--startup-trials",
        type=int,
        help="random startup trials for both TPE implementations (default: 10; smoke: 8)",
    )
    parser.add_argument("--output", type=Path, help="optional combined CSV result path")
    parser.add_argument(
        "--metadata-output",
        type=Path,
        help=(
            "optional JSON metadata path; defaults to <output>.metadata.json when --output "
            "is provided"
        ),
    )
    return parser.parse_args()


def run_native(
    executable: Path, problem: Problem, seed: int, startup_trials: int
) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(executable),
            "--problem",
            problem.name,
            "--seed",
            str(seed),
            "--trials",
            str(problem.trials),
            "--startup-trials",
            str(startup_trials),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{problem.name}: native benchmark did not emit one JSON object"
        ) from error
    required_fields = {
        "implementation",
        "problem",
        "seed",
        "trials",
        "startup_trials",
        "best_value",
        "optimum",
        "best_regret",
        "sampler_ns",
        "objective_ns",
        "validation_ns",
        "wall_ns",
        "unique_candidates",
        "duplicate_candidates",
        "exact_optimum_hits",
        "best_hamming",
        "best_l1",
    }
    if not isinstance(result, dict) or set(result) != required_fields:
        missing = (
            required_fields - set(result)
            if isinstance(result, dict)
            else required_fields
        )
        extra = set(result) - required_fields if isinstance(result, dict) else set()
        raise RuntimeError(
            f"{problem.name}: native result schema drift; missing={sorted(missing)}, "
            f"extra={sorted(extra)}"
        )
    expected_identity = {
        "implementation": NATIVE_IMPLEMENTATION,
        "problem": problem.name,
        "seed": seed,
        "trials": problem.trials,
        "startup_trials": startup_trials,
    }
    for field, expected in expected_identity.items():
        if result[field] != expected:
            raise RuntimeError(
                f"{problem.name}: native {field} drift; expected {expected!r}, "
                f"found {result[field]!r}"
            )
    for field in ("best_value", "optimum", "best_regret"):
        if not math.isfinite(float(result[field])):
            raise RuntimeError(f"{problem.name}: native {field} was not finite")
    if not math.isclose(
        float(result["optimum"]), problem.optimum, rel_tol=0.0, abs_tol=1e-12
    ):
        raise RuntimeError(
            f"{problem.name}: native and Python declared optima disagree"
        )
    return result


def run_optuna(problem: Problem, seed: int, startup_trials: int) -> dict[str, object]:
    # In pinned Optuna 4.9.0, the default gamma and prior weight match the
    # native constants above. Passing those deprecated keywords would only add
    # warnings; the version gate in main() prevents a silent default change.
    sampler = optuna.samplers.TPESampler(
        seed=seed,
        n_startup_trials=startup_trials,
        n_ei_candidates=EI_CANDIDATES,
        multivariate=False,
        group=False,
        constant_liar=False,
    )
    study = optuna.create_study(
        direction="minimize", sampler=sampler, pruner=optuna.pruners.NopPruner()
    )
    sampler_ns = 0
    objective_ns = 0
    unique_candidates: set[tuple[int, ...]] = set()
    exact_optimum_hits = 0
    best_observed = math.inf
    best_hamming = -1
    best_l1 = -1
    is_million_discrete = problem.name == "million_discrete"
    wall_start = time.perf_counter_ns()
    for _ in range(problem.trials):
        ask_start = time.perf_counter_ns()
        trial = study.ask()
        params = problem.suggest(trial)
        sampler_ns += time.perf_counter_ns() - ask_start

        objective_start = time.perf_counter_ns()
        value = problem.objective(params)
        objective_ns += time.perf_counter_ns() - objective_start
        if not math.isfinite(value):
            raise RuntimeError(f"{problem.name}: Optuna objective was not finite")
        if is_million_discrete:
            values = tuple(int(params[f"x{index}"]) for index in range(1, 7))
            unique_candidates.add(values)
            hamming = sum(
                value != target
                for value, target in zip(values, MILLION_TARGET, strict=True)
            )
            l1 = sum(
                abs(value - target)
                for value, target in zip(values, MILLION_TARGET, strict=True)
            )
            if hamming == 0:
                exact_optimum_hits += 1
            if value < best_observed or (
                value == best_observed
                and (
                    best_hamming < 0
                    or hamming < best_hamming
                    or (hamming == best_hamming and l1 < best_l1)
                )
            ):
                best_observed = value
                best_hamming = hamming
                best_l1 = l1

        tell_start = time.perf_counter_ns()
        study.tell(trial, value)
        sampler_ns += time.perf_counter_ns() - tell_start
    wall_ns = time.perf_counter_ns() - wall_start
    best = float(study.best_value)
    return {
        "implementation": OPTUNA_IMPLEMENTATION,
        "problem": problem.name,
        "seed": seed,
        "trials": problem.trials,
        "startup_trials": startup_trials,
        "best_value": best,
        "optimum": problem.optimum,
        "best_regret": max(0.0, best - problem.optimum),
        "sampler_ns": sampler_ns,
        "objective_ns": objective_ns,
        "validation_ns": 0,
        "wall_ns": wall_ns,
        "unique_candidates": len(unique_candidates) if is_million_discrete else -1,
        "duplicate_candidates": (
            problem.trials - len(unique_candidates) if is_million_discrete else -1
        ),
        "exact_optimum_hits": exact_optimum_hits,
        "best_hamming": best_hamming,
        "best_l1": best_l1,
    }


def validate_million_space() -> tuple[float, int, tuple[int, ...], float, float]:
    start = time.perf_counter()
    best = math.inf
    best_count = 0
    best_candidate: tuple[int, ...] = ()
    second_best = math.inf
    for candidate in itertools.product(range(10), repeat=6):
        value = million_discrete_tuple(candidate)
        if value < best:
            second_best = best
            best = value
            best_count = 1
            best_candidate = candidate
        elif value == best:
            best_count += 1
        elif value < second_best:
            second_best = value
    elapsed = time.perf_counter() - start
    if best != 0.0 or best_count != 1 or best_candidate != MILLION_TARGET:
        raise RuntimeError(
            "million_discrete full enumeration did not confirm the declared unique optimum"
        )
    return best, best_count, best_candidate, second_best, elapsed


def median(rows: list[dict[str, object]], key: str) -> float:
    return statistics.median(float(row[key]) for row in rows)


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_value(repository: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def dependency_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in BENCHMARK_PACKAGES:
        try:
            versions[package] = package_metadata.version(package)
        except package_metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    return versions


def source_snapshot(repository: Path) -> dict[str, object]:
    """Fingerprint every source file that defines or builds this comparison."""
    paths = [
        repository / "VERSION",
        repository / "CMakeLists.txt",
        repository / "benchmarks/optuna/CMakeLists.txt",
        repository / "benchmarks/optuna/native_benchmark.cpp",
        repository / "benchmarks/optuna/requirements.txt",
        repository / "benchmarks/optuna/run_benchmark.py",
    ]
    paths.extend((repository / "include/pineforge/hpo").glob("*.hpp"))
    paths.extend((repository / "src/core").glob("*.cpp"))
    unique_paths = sorted(
        set(paths), key=lambda path: path.relative_to(repository).as_posix()
    )

    entries: list[dict[str, object]] = []
    aggregate = hashlib.sha256()
    for path in unique_paths:
        if not path.is_file():
            raise FileNotFoundError(f"source snapshot input not found: {path}")
        relative_path = path.relative_to(repository).as_posix()
        file_hash = sha256_file(path)
        size_bytes = path.stat().st_size
        entries.append(
            {
                "path": relative_path,
                "sha256": file_hash,
                "size_bytes": size_bytes,
            }
        )
        aggregate.update(relative_path.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(file_hash.encode("ascii"))
        aggregate.update(b"\0")
        aggregate.update(str(size_bytes).encode("ascii"))
        aggregate.update(b"\n")
    return {
        "algorithm": "sha256(relative_path\\0sha256\\0size_bytes\\n)",
        "aggregate_sha256": aggregate.hexdigest(),
        "files": entries,
    }


def portable_path(path: Path, repository: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repository).as_posix()
    except ValueError:
        return str(resolved)


def default_metadata_path(csv_path: Path) -> Path:
    return csv_path.with_suffix(csv_path.suffix + ".metadata.json")


def write_metadata(path: Path, metadata: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        json.dump(metadata, output, indent=2, sort_keys=True)
        output.write("\n")


def benchmark_metadata(
    *,
    profile: str,
    native_path: Path,
    seeds: tuple[int, ...],
    problems: tuple[Problem, ...],
    startup_trials: int,
    rows: list[dict[str, object]],
    output_path: Path | None,
    million_validation: dict[str, object] | None,
    driver_wall_seconds: float,
) -> dict[str, object]:
    repository = Path(__file__).resolve().parents[2]
    configuration = {
        "profile": profile,
        "direction": "minimize",
        "schedule": "sequential_ask_objective_tell",
        "seeds": list(seeds),
        "problems": [
            {
                "name": problem.name,
                "trials": problem.trials,
                "declared_optimum": problem.optimum,
            }
            for problem in problems
        ],
        "tpe": {
            "startup_trials": startup_trials,
            "ei_candidates": EI_CANDIDATES,
            "gamma_fraction": GAMMA_FRACTION,
            "gamma_cap": GAMMA_CAP,
            "prior_weight": PRIOR_WEIGHT,
            "independent_marginals": True,
            "constant_liar": False,
        },
    }
    canonical_configuration = json.dumps(
        configuration, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    repository_top = git_value(repository, "rev-parse", "--show-toplevel")
    if repository_top is not None and Path(repository_top).resolve() == repository:
        revision = git_value(repository, "rev-parse", "HEAD")
        status = git_value(repository, "status", "--porcelain")
    else:
        revision = None
        status = None
    csv_artifact: dict[str, object] | None = None
    if output_path is not None:
        csv_artifact = {
            "path": portable_path(output_path, repository),
            "sha256": sha256_file(output_path),
            "rows": len(rows),
        }
    return {
        "schema": METADATA_SCHEMA,
        "csv_schema_version": CSV_SCHEMA_VERSION,
        "suite": "native_tpe_vs_optuna",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "configuration_sha256": hashlib.sha256(canonical_configuration).hexdigest(),
        "configuration": configuration,
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "dependencies": dependency_versions(),
            "native_executable": portable_path(native_path, repository),
            "native_executable_sha256": sha256_file(native_path),
            "repository_revision": revision,
            "repository_dirty": None if status is None else bool(status),
            "source_snapshot": source_snapshot(repository),
        },
        "validation": {
            "declared_optima_checked_in_both_implementations": True,
            "million_discrete_full_enumeration": million_validation,
        },
        "artifacts": {"csv": csv_artifact},
        "execution": {
            # Python implementation/version are recorded above. Keep the command
            # replayable without publishing a contributor's home-directory path.
            "command": ["python", *sys.argv],
            "driver_wall_seconds": driver_wall_seconds,
        },
    }


def print_results(
    rows: list[dict[str, object]], seeds: tuple[int, ...], problems: tuple[Problem, ...]
) -> None:
    print("\nNative C++ TPE versus Optuna 4.9.0 TPESampler")
    print("same problem/search space/seed/trial budget; sequential minimize")
    print(
        f"{'problem':<22} {'seed':>6} {'trials':>7} {'native regret':>15} "
        f"{'Optuna regret':>15} {'native/Optuna':>15}"
    )
    for problem in problems:
        for seed in seeds:
            native = next(
                row
                for row in rows
                if row["problem"] == problem.name
                and row["seed"] == seed
                and row["implementation"] == NATIVE_IMPLEMENTATION
            )
            optuna_row = next(
                row
                for row in rows
                if row["problem"] == problem.name
                and row["seed"] == seed
                and row["implementation"] == OPTUNA_IMPLEMENTATION
            )
            ratio = (float(native["best_regret"]) + problem.ratio_floor) / (
                float(optuna_row["best_regret"]) + problem.ratio_floor
            )
            print(
                f"{problem.name:<22} {seed:>6} {problem.trials:>7} "
                f"{float(native['best_regret']):>15.6e} "
                f"{float(optuna_row['best_regret']):>15.6e} {ratio:>15.3f}"
            )

    print("\nMedian quality and public-API sampler overhead")
    print(
        f"{'problem':<22} {'native regret':>15} {'Optuna regret':>15} "
        f"{'N/O':>8} {'native us/trial':>17} {'Optuna us/trial':>17} {'wins N/O/T':>12}"
    )
    all_log_ratios: list[float] = []
    native_total_wins = 0
    optuna_total_wins = 0
    ties = 0
    for problem in problems:
        native_rows = [
            row
            for row in rows
            if row["problem"] == problem.name
            and row["implementation"] == NATIVE_IMPLEMENTATION
        ]
        optuna_rows = [
            row
            for row in rows
            if row["problem"] == problem.name
            and row["implementation"] == OPTUNA_IMPLEMENTATION
        ]
        native_regret = median(native_rows, "best_regret")
        optuna_regret = median(optuna_rows, "best_regret")
        ratio = (native_regret + problem.ratio_floor) / (
            optuna_regret + problem.ratio_floor
        )
        native_us = median(native_rows, "sampler_ns") / problem.trials / 1_000.0
        optuna_us = median(optuna_rows, "sampler_ns") / problem.trials / 1_000.0
        native_wins = 0
        optuna_wins = 0
        problem_ties = 0
        for seed in seeds:
            native = next(row for row in native_rows if row["seed"] == seed)
            optuna_row = next(row for row in optuna_rows if row["seed"] == seed)
            native_value = float(native["best_regret"])
            optuna_value = float(optuna_row["best_regret"])
            tolerance = 1e-12 * max(1.0, abs(native_value), abs(optuna_value))
            if native_value + tolerance < optuna_value:
                native_wins += 1
            elif optuna_value + tolerance < native_value:
                optuna_wins += 1
            else:
                problem_ties += 1
            paired_ratio = (native_value + problem.ratio_floor) / (
                optuna_value + problem.ratio_floor
            )
            all_log_ratios.append(math.log(min(100.0, max(0.01, paired_ratio))))
        native_total_wins += native_wins
        optuna_total_wins += optuna_wins
        ties += problem_ties
        print(
            f"{problem.name:<22} {native_regret:>15.6e} {optuna_regret:>15.6e} "
            f"{ratio:>8.3f} {native_us:>17.2f} {optuna_us:>17.2f} "
            f"{native_wins:>3}/{optuna_wins}/{problem_ties:<3}"
        )
    geometric_ratio = math.exp(statistics.mean(all_log_ratios))
    print(
        f"\naggregate paired wins native/Optuna/tie: "
        f"{native_total_wins}/{optuna_total_wins}/{ties}; "
        f"geometric paired regret ratio native/Optuna: {geometric_ratio:.3f}"
    )
    print(
        "Timing is public ask/suggest/tell API overhead. It includes Python/Optuna Study and "
        "storage overhead versus native C++ runtime; it is not a pure estimator-kernel benchmark."
    )

    if not any(problem.name == "million_discrete" for problem in problems):
        return

    print("\nMillion-discrete coverage and optimum-distance diagnostics")
    print(
        f"{'seed':>6} {'native unique':>14} {'Optuna unique':>14} "
        f"{'native dup':>12} {'Optuna dup':>12} {'native H/L':>12} "
        f"{'Optuna H/L':>12} {'exact hits N/O':>16}"
    )
    million_rows = [row for row in rows if row["problem"] == "million_discrete"]
    for seed in seeds:
        native = next(
            row
            for row in million_rows
            if row["seed"] == seed and row["implementation"] == NATIVE_IMPLEMENTATION
        )
        optuna_row = next(
            row
            for row in million_rows
            if row["seed"] == seed and row["implementation"] == OPTUNA_IMPLEMENTATION
        )
        print(
            f"{seed:>6} {int(native['unique_candidates']):>14} "
            f"{int(optuna_row['unique_candidates']):>14} "
            f"{int(native['duplicate_candidates']):>12} "
            f"{int(optuna_row['duplicate_candidates']):>12} "
            f"{int(native['best_hamming']):>5}/{int(native['best_l1']):<6} "
            f"{int(optuna_row['best_hamming']):>5}/{int(optuna_row['best_l1']):<6} "
            f"{int(native['exact_optimum_hits']):>7}/{int(optuna_row['exact_optimum_hits']):<7}"
        )

    print("\nMillion-discrete implementation summaries")
    for implementation, label in (
        (NATIVE_IMPLEMENTATION, "native"),
        (OPTUNA_IMPLEMENTATION, "Optuna"),
    ):
        implementation_rows = [
            row for row in million_rows if row["implementation"] == implementation
        ]
        unique_values = [int(row["unique_candidates"]) for row in implementation_rows]
        duplicate_values = [
            int(row["duplicate_candidates"]) for row in implementation_rows
        ]
        hamming_values = [int(row["best_hamming"]) for row in implementation_rows]
        l1_values = [int(row["best_l1"]) for row in implementation_rows]
        hit_trials = sum(int(row["exact_optimum_hits"]) for row in implementation_rows)
        hit_runs = sum(
            int(row["exact_optimum_hits"]) > 0 for row in implementation_rows
        )
        median_unique = statistics.median(unique_values)
        print(
            f"{label}: unique median {median_unique:.0f} "
            f"[{min(unique_values)}, {max(unique_values)}], duplicates median "
            f"{statistics.median(duplicate_values):.0f} "
            f"[{min(duplicate_values)}, {max(duplicate_values)}], median coverage "
            f"{100.0 * median_unique / 1_000_000.0:.4f}%, best Hamming median/range "
            f"{statistics.median(hamming_values):.0f}/"
            f"[{min(hamming_values)}, {max(hamming_values)}], best L1 median/range "
            f"{statistics.median(l1_values):.0f}/[{min(l1_values)}, {max(l1_values)}], "
            f"exact optimum {hit_trials} trial hits in {hit_runs}/{len(seeds)} runs"
        )


def main() -> int:
    args = parse_args()
    if optuna.__version__ != EXPECTED_OPTUNA_VERSION:
        raise RuntimeError(
            f"expected Optuna {EXPECTED_OPTUNA_VERSION}, found {optuna.__version__}; "
            "use the pinned requirements.txt"
        )
    native_path = args.native.resolve()
    if not native_path.is_file():
        raise FileNotFoundError(f"native benchmark executable not found: {native_path}")
    if (
        args.output is not None
        and args.metadata_output is not None
        and args.output.resolve() == args.metadata_output.resolve()
    ):
        raise ValueError("--metadata-output must not overwrite --output")
    seed_text = args.seeds or ",".join(
        str(seed) for seed in (SMOKE_SEEDS if args.smoke else DEFAULT_SEEDS)
    )
    seeds = tuple(int(value) for value in seed_text.split(",") if value)
    if not seeds:
        raise ValueError("at least one seed is required")
    if len(set(seeds)) != len(seeds):
        raise ValueError("--seeds must not contain duplicates")
    if any(seed < 0 or seed > 2**64 - 1 for seed in seeds):
        raise ValueError("--seeds entries must be unsigned 64-bit integers")
    startup_trials = args.startup_trials
    if startup_trials is None:
        startup_trials = SMOKE_STARTUP_TRIALS if args.smoke else DEFAULT_STARTUP_TRIALS
    if startup_trials <= 0:
        raise ValueError("--startup-trials must be positive")
    if args.trials is not None and args.trials <= 0:
        raise ValueError("--trials must be positive")
    problem_by_name = {problem.name: problem for problem in PROBLEMS}
    problem_text = args.problems or ",".join(
        SMOKE_PROBLEMS if args.smoke else tuple(problem_by_name)
    )
    requested_names = tuple(value for value in problem_text.split(",") if value)
    unknown_names = [name for name in requested_names if name not in problem_by_name]
    if not requested_names or unknown_names:
        raise ValueError(
            "--problems must contain known names; unknown: " + ", ".join(unknown_names)
        )
    if len(set(requested_names)) != len(requested_names):
        raise ValueError("--problems must not contain duplicates")
    problems = tuple(problem_by_name[name] for name in requested_names)
    trial_override = (
        args.trials
        if args.trials is not None
        else (SMOKE_TRIALS if args.smoke else None)
    )
    if trial_override is not None:
        problems = tuple(
            replace(problem, trials=trial_override) for problem in problems
        )
    profile = "smoke" if args.smoke else "full"
    if not args.smoke and any(
        value is not None
        for value in (args.seeds, args.problems, args.trials, args.startup_trials)
    ):
        profile = "custom"
    optuna.logging.set_verbosity(optuna.logging.ERROR)
    for problem in problems:
        known_value = problem.objective(problem.known_optimum)
        if not math.isclose(known_value, problem.optimum, rel_tol=0.0, abs_tol=2e-5):
            raise RuntimeError(
                f"{problem.name}: Python objective and declared optimum disagree: {known_value}"
            )

    million_validation: dict[str, object] | None = None
    if any(problem.name == "million_discrete" for problem in problems):
        validation = validate_million_space()
        best, best_count, best_candidate, second_best, enumeration_seconds = validation
        million_validation = {
            "candidate_count": 1_000_000,
            "minimum": best,
            "minimizer": list(best_candidate),
            "multiplicity": best_count,
            "second_best": second_best,
            "wall_seconds": enumeration_seconds,
        }
        print(
            "million_discrete exhaustive validation: 1,000,000/1,000,000 candidates, "
            f"minimum={best:.0f}, minimizer={best_candidate}, multiplicity={best_count}, "
            f"second-best={second_best:.0f}, wall={enumeration_seconds:.2f}s"
        )

    rows: list[dict[str, object]] = []
    benchmark_start = time.perf_counter()
    print(
        f"environment: Optuna {optuna.__version__}, Python {platform.python_version()}, "
        f"native C++ executable {native_path}, profile={profile}, "
        f"startup_trials={startup_trials}"
    )
    for problem in problems:
        for seed in seeds:
            rows.append(run_native(native_path, problem, seed, startup_trials))
            rows.append(run_optuna(problem, seed, startup_trials))
    print_results(rows, seeds, problems)
    driver_wall_seconds = time.perf_counter() - benchmark_start
    print(f"complete driver wall time: {driver_wall_seconds:.2f} s")
    if args.output is not None:
        write_csv(args.output, rows)
        print(f"raw CSV: {args.output.resolve()}")
    metadata_path = args.metadata_output
    if metadata_path is None and args.output is not None:
        metadata_path = default_metadata_path(args.output)
    if metadata_path is not None:
        metadata = benchmark_metadata(
            profile=profile,
            native_path=native_path,
            seeds=seeds,
            problems=problems,
            startup_trials=startup_trials,
            rows=rows,
            output_path=args.output,
            million_validation=million_validation,
            driver_wall_seconds=driver_wall_seconds,
        )
        write_metadata(metadata_path, metadata)
        print(f"run metadata: {metadata_path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
