# pineforge-hpo

Native hyperparameter optimization for PineForge strategies.

`pineforge-hpo` compiles a PineScript strategy once, then runs parameter trials in a
parallel C++ hot loop. It supports exhaustive search, reproducible random search, dlib
global optimization, and a native Tree-structured Parzen Estimator (TPE). Objective
functions can combine PineForge backtest metrics with arithmetic expressions and
constraints.

> **Project status: alpha.** The single-strategy path supports Linux and macOS. The public
> C++ model includes portfolio observations and custom-objective
> contracts, but multiple-strategy shared-account execution is not implemented yet.

## Why pineforge-hpo?

- **No Python in the trial hot loop.** Python initializes the study and artifact; C++
  samples, backtests, scores, and schedules trials.
- **Compile once, override at runtime.** Search parameters use `strategy_set_input()` and
  supported `strategy(...)` settings use `strategy_set_override()`.
- **Backtest-native objectives.** Combine report metrics with `+`, `-`, `*`, `/`,
  comparisons, `min`, `max`, and `abs`.
- **Explicit coverage guarantees.** TPE can preserve a continuous space or operate without
  replacement over a finite stepped space.
- **Reproducible artifacts and studies.** Results record sampler identity, seed, artifact
  hash, search-space coverage, and the complete trial table.

```text
PineScript
    |
    | pineforge-codegen-oss (once)
    v
generated C++ + input manifest
    |
    | content-addressed build (once)
    v
strategy plugin + immutable OHLCV
    |
    | native C++ trials
    v
sampler -> runtime inputs/overrides -> backtest -> objective -> ask/tell
```

The HPO layer consumes the public PineForge C ABI. It is not a `BacktestEngine`
subclass, and it does not move transpiler or optimizer behavior into
`pineforge-engine`.

## Quick start

### Prerequisites

- Python 3.11 or newer;
- CMake 3.17 or newer and a C++17 compiler;
- Linux or macOS;
- Git and network access for the first dependency build.

Clone the repository, then initialize the two pinned integration dependencies:

```bash
git clone https://github.com/pineforge-4pass/pineforge-hpo.git
cd pineforge-hpo
git submodule update --init \
  external/pineforge-engine \
  external/pineforge-codegen-oss
```

The Python distribution exported by `pineforge-codegen-oss` is named
`pineforge-codegen`; its import module is `pineforge_codegen`.

The gitlinks are the compatibility baseline tested for the current HPO revision. Do not
replace them with each dependency's moving `main` branch in a release build. Updating a
gitlink requires the same native tests and nine-trial end-to-end study used by CI. The
submodules remain separate projects under their own licenses; nested engine corpus and
benchmark-asset submodules are not required by this quick start.

The first engine configure downloads Eigen when it is not installed. The first HPO
configure downloads the pinned dlib 20.0.1 archive and verifies its SHA-256; pass
`-DPINEFORGE_HPO_USE_SYSTEM_DLIB=ON` only when an exact 20.0.1 CMake package is already
installed.

From the `pineforge-hpo` repository root, the following block builds the engine and HPO
runner, installs both local Python packages, runs the test suite, and executes the bundled
nine-trial example:

```bash
export PINEFORGE_ENGINE_ROOT="$PWD/external/pineforge-engine"

cmake -S "$PINEFORGE_ENGINE_ROOT" -B "$PINEFORGE_ENGINE_ROOT/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DPINEFORGE_BUILD_TESTS=OFF \
  -DPINEFORGE_BUILD_TUTORIAL=OFF \
  -DPINEFORGE_BUILD_CORPUS_STRATEGIES=OFF
cmake --build "$PINEFORGE_ENGINE_ROOT/build" -j4

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e external/pineforge-codegen-oss
python -m pip install -e . --no-deps

cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DPINEFORGE_ENGINE_ROOT="$PINEFORGE_ENGINE_ROOT"
cmake --build build -j4
ctest --test-dir build --output-on-failure

pineforge-hpo run examples/single_strategy/study.json \
  --engine-root "$PINEFORGE_ENGINE_ROOT" \
  --native "$PWD/build/bin/pineforge-hpo-native" \
  --output build/quick-start-result.json
```

The command prints the complete JSON result. Check the compact summary with:

```bash
python - <<'PY'
import json

with open("build/quick-start-result.json", encoding="utf-8") as source:
    result = json.load(source)

for key in (
    "ok",
    "best_trial_id",
    "best_value",
    "trials_completed",
    "full_parameter_coverage",
):
    print(f"{key}: {result[key]}")
PY
```

You should see `ok: True`, `trials_completed: 9`, and
`full_parameter_coverage: True`. The first run transpiles and builds the strategy plugin;
later runs reuse its content-addressed cache when the Pine source, engine ABI, codegen,
compiler, flags, and dependencies have not changed.

To compile a Pine strategy without starting a study:

```bash
pineforge-hpo compile path/to/strategy.pine \
  --engine-root "$PINEFORGE_ENGINE_ROOT"
```

This returns the plugin, manifest, provenance, artifact key, and cache-hit state as JSON.
An existing validated plugin can also be referenced from a StudySpec, allowing the native
runner to be used without installing the transpiler. See the
[complete StudySpec reference](docs/study-spec.md).

## Define a study

Studies are strict JSON documents. Paths are resolved relative to the StudySpec file, not
the shell's current directory. The bundled example is
[`examples/single_strategy/study.json`](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/examples/single_strategy/study.json).

The three fields most commonly changed are the strategy search space, objective, and
sampler:

```json
{
  "search_space": {
    "Fast Length": {"kind": "integer", "low": 5, "high": 50, "step": 1},
    "Multiplier": {"kind": "real", "low": 0.5, "high": 3.0, "step": 0.1},
    "Use Filter": {"kind": "boolean"},
    "Mode": {"kind": "categorical", "choices": ["ema", "sma"]}
  },
  "objective": {
    "kind": "expression",
    "direction": "maximize",
    "expression": "metrics.all.net_profit_pct - 0.5 * metrics.equity.max_equity_drawdown_pct",
    "constraints": ["metrics.all.num_trades >= 30"],
    "nan_policy": "fail_trial",
    "division_by_zero": "fail_trial"
  },
  "sampler": {
    "kind": "tpe",
    "candidate_policy": "without_replacement",
    "seed": 20260718,
    "trials": 1000,
    "config": {"startup_trials": 200}
  }
}
```

This is an excerpt rather than a complete StudySpec. Strategy, dataset, and execution
objects are shown in the [schema documentation](docs/study-spec.md).

### Choose a sampler

| Sampler | Best fit | Important behavior |
| --- | --- | --- |
| `grid` | Small finite spaces and coverage baselines | Deterministically enumerates the Cartesian product. |
| `random` | Broad, inexpensive exploration | Seeded `std::mt19937_64`; supports continuous and log dimensions. |
| `dlib_global` | Model-based search over mixed numeric spaces | Native batched ask/tell through dlib global function search. |
| `tpe` | Adaptive refinement over mixed spaces | Native Parzen marginals, categorical probabilities, and constant-liar batches. |

TPE and dlib are adaptive: proposal order depends on both the seed and worker-sized batch
schedule. Compare optimizers under the same objective-evaluation budget and across
multiple seeds. For large `10^6`-`10^8` candidate domains, do not ask one TPE study to
enumerate the domain. Use an external coordinator to shard a finite grid or broad search,
then reserve TPE for bounded refinement. This runner does not currently provide distributed
partitioning, and its TPE deliberately retains its complete usable history.

### Choose a candidate policy

| Policy | Guarantee |
| --- | --- |
| `sampler_default` | Preserves the sampler's native behavior. Continuous real dimensions are allowed and TPE may repeat a vector. |
| `without_replacement` | Every complete vector is reserved at most once. The trial budget cannot exceed the finite-space cardinality. |
| `exhaustive` | Adds `trials == cardinality`, so every declared vector is attempted exactly once. |

Finite policies are implemented for `grid` and `tpe`. Every varying real dimension must
declare a positive `step`; `low == high` is a one-value dimension and needs no step.
Pending, completed, infeasible, failed, and abandoned candidates remain reserved, so
parallel workers cannot repeat them. A non-constant log-real dimension is continuous and
therefore cannot use a finite policy in StudySpec v1.

`exhaustive` guarantees parameter coverage. Equality with a successful grid-search optimum
also requires deterministic data and execution, plus a comparable result at every point.
See [ADR 0003](docs/adr/0003-finite-candidate-policies.md) for the exact lattice and
floating-point contract.

### Build an objective

Expression objectives can read `metrics.all.*`, `metrics.longs.*`,
`metrics.shorts.*`, `metrics.equity.*`, and selected report counters such as
`report.total_trades`. Expressions are parsed once before trials begin. Unknown metric
paths, non-finite final values, and configured division-by-zero failures cannot silently
become a winning trial.

Applications embedding the C++ library can instead implement
`ObjectiveFn<Observation>`. The generic objective contract is independent of a PineForge
report; the executable CLI currently resolves expression objectives only.

## Interfaces and API documentation

- **Python CLI:** `pineforge-hpo compile` and `pineforge-hpo run` initialize artifacts and
  studies.
- **Native CLI:** `build/bin/pineforge-hpo-native` runs a compiled plugin and normalized
  OHLCV directly. Use `--help` for its lower-level flags.
- **Python API:** `ArtifactBuilder`, `StudySpec`, validation models, and transpiler bridge
  are exported from `pineforge_hpo`.
- **C++ API:** public headers live under
  [`include/pineforge/hpo/`](https://github.com/pineforge-4pass/pineforge-hpo/tree/main/include/pineforge/hpo/).
  Build-tree consumers can link `PineForgeHPO::core` or
  `PineForgeHPO::engine_adapter`.

For an optimizer-only embedding with no PineForge engine checkout, configure with
`PINEFORGE_HPO_BUILD_ENGINE_ADAPTER=OFF` and
`PINEFORGE_HPO_BUILD_NATIVE_CLI=OFF`. PineForge HPO 0.1.x currently exposes CMake
build-tree targets; an installed CMake package is not published yet. Both adapter targets
default to off when PineForge HPO is included with `add_subdirectory()`. Core-only users
may leave both `external/` submodules uninitialized.

After GitHub Pages is enabled, the workflow publishes the
[C++ and Python API reference](https://pineforge-4pass.github.io/pineforge-hpo/). Until
then, [build the same site locally](docs/api.md). The reference is generated from the
public headers and Python facade on the repository's default branch.

## Architecture

Initialization and execution are deliberately separated:

1. `pineforge_codegen.transpile_full()` emits generated C++, input metadata, and
   `strategy(...)` parameters once.
2. `ArtifactBuilder` compiles and validates a content-addressed strategy plugin once.
3. `StrategyPlugin` and immutable OHLCV data are loaded once for the study.
4. Each trial creates a fresh strategy handle, applies inputs and overrides, runs the
   backtest, snapshots the required report fields, then frees report and handle in order.
5. The native scheduler evaluates worker-sized batches and feeds results back to adaptive
   samplers in deterministic trial-ID order.

The component boundaries, plugin lifecycle, objective abstraction, finite-space codec, and
future account-level execution model are documented in
[`docs/architecture.md`](docs/architecture.md).

## Benchmarks

The benchmark suite separates optimizer quality from proposal/feedback API overhead and records raw
per-seed results. It includes standard nonlinear functions, a mixed-type interaction
problem, and an independently exhaustively verified one-million-candidate discrete
problem. The native TPE comparison pins official Optuna in an isolated benchmark-only
environment; Optuna is not a runtime dependency.

Start with [`benchmarks/README.md`](benchmarks/README.md) for the benchmark protocol,
smoke/full commands, output schema, fairness rules, and guidance for contributing results.
The detailed native-versus-Optuna methodology lives in
[`benchmarks/optuna/README.md`](benchmarks/optuna/README.md), with the checked-in reference
run in [`results-2026-07-18.md`](benchmarks/optuna/results-2026-07-18.md).

Published numbers are evidence for the tested problems, budgets, seeds, and machine—not a
claim that one sampler dominates every backtest surface.

## Reproducibility

For a reproducible study, keep all of the following fixed:

- Pine source or precompiled artifact and its recorded artifact key;
- engine ABI/runtime, `pineforge-codegen-oss`, compiler target, and exact compile flags;
- OHLCV bytes, timeframes, timezone, fixed inputs, and strategy overrides;
- objective, constraints, search-space declaration, and candidate policy;
- sampler implementation/configuration, seed, worker count, and trial budget.

Generated strategies are compiled with the parity-critical
`-std=c++17 -O2 -ffp-contract=off -fPIC -shared` flags. Result JSON includes the HPO
version, sampler implementation identity, artifact key, complete trials, cardinality, and
coverage diagnostics. Preserve the result and artifact provenance together when reporting
a benchmark or bug.

## Current scope

Implemented today:

- one Pine strategy and one OHLCV dataset per executable study;
- integer, real, Boolean, categorical, stepped, and supported log dimensions;
- grid, seeded random, dlib global, and native TPE samplers;
- parallel thread execution over independent strategy handles;
- expression objectives, comparison constraints, runtime inputs, and runtime strategy
  overrides;
- content-addressed artifact compilation and provenance.

Not implemented yet:

- multiple-strategy shared cash/margin/order sequencing and portfolio CLI execution;
- durable study storage, resume/checkpointing, pruning, or distributed workers;
- conditional/hierarchical spaces, multi-objective Pareto optimization, and walk-forward
  orchestration;
- executable resolution of registered custom C++ objectives.

Independent strategy reports must not be described as a shared-account simulation. See
[`docs/plan.md`](docs/plan.md) for the staged roadmap.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the complete native/Python checks,
architecture boundaries, benchmark contribution rules, and pull-request checklist.
Repository administrators should also complete the one-time
[GitHub setup checklist](docs/repository-setup.md) after the initial public push.

Run the required local checks from the repository root:

```bash
cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DPINEFORGE_ENGINE_ROOT="$PWD/external/pineforge-engine"
cmake --build build -j4
ctest --test-dir build --output-on-failure
```

Please report bugs and propose features in the
[GitHub issue tracker](https://github.com/pineforge-4pass/pineforge-hpo/issues). Include a
minimal StudySpec, the complete result/provenance JSON, platform and compiler information,
and whether the behavior reproduces with one worker. Report suspected vulnerabilities
privately according to [SECURITY.md](SECURITY.md).

Additional design and validation material:

- [StudySpec v1](docs/study-spec.md)
- [Architecture](docs/architecture.md)
- [Implementation plan](docs/plan.md)
- [Native TPE design decision](docs/adr/0002-native-product-tpe.md)
- [Finite candidate-policy decision](docs/adr/0003-finite-candidate-policies.md)
- [Native TPE nonlinear validation](docs/tpe-validation.md)
- [Real-strategy validation](docs/real-strategy-validation.md)

## License

Original code in this repository is licensed under the
[Apache License 2.0](LICENSE). dlib 20.0.1 is used under the Boost Software License 1.0;
notices are recorded in [LEGAL.md](LEGAL.md) and
[THIRD_PARTY_LICENSES](https://github.com/pineforge-4pass/pineforge-hpo/tree/main/THIRD_PARTY_LICENSES/).

The optional PineScript bridge invokes the `pineforge-codegen` distribution from the
`pineforge-codegen-oss` repository. That separately distributed project has its own
source-available license and commercial-use terms; it is not relicensed under Apache-2.0.
Running `pineforge-hpo-native` with an already compiled strategy plugin does not require
the transpiler. See [LEGAL.md](LEGAL.md) for the dependency boundary.
