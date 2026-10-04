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
- CMake 3.20 or newer (the projects require 3.17; `ctest --test-dir` needs 3.20) and a
  C++17 compiler;
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

The gitlinks pin the releases this HPO revision is tested with: engine v1.0.0 (`5718c5d`,
C ABI 4) and codegen v1.0.0 (`5bf595b`). With them, all eight `ctest` suites and the
nine-trial example below pass (checked on Linux arm64 and macOS arm64, 2026-09-30).
From 1.0.0 on, codegen X.Y.Z is supported only with engine vX.Y.Z: a mismatched pair may
still compile, but equal `PF_ABI_VERSION` values do not guarantee a compatible C++ source
layout or the same behavior. The optional `transpile` extra admits any `pineforge-codegen`
1.x, so install the release that matches your engine (`pineforge-codegen==1.0.0` for the
gitlinks). The native adapter reads the equity statistics by their engine 1.0 names, so it
needs engine 1.x headers. Regenerate a precompiled plugin referenced from a StudySpec with
codegen 1.0.0 and rebuild it against engine v1.0.0: plugins from engine v0.13.x or earlier
(ABI 3 or lower) are refused with an ABI mismatch, while plugins from 1.0 prereleases or
development builds also report ABI 4 and load without an error although they do not pair
with v1.0.0. C++ code that reads
`ReportSnapshot::metrics.equity.sharpe_tv` or `sortino_tv` must use `sharpe_monthly` or
`sortino_monthly`.

Compared with the previous gitlinks (engine `7bff706`, codegen `cefeec8`), 3 of the 9
example trials report different metrics: since v0.13.0 the engine reports a position still
open after the final bar as a range-end close, which counts as a trade. The best trial is
unchanged.

Do not replace the gitlinks with
each dependency's moving `main` branch in a release build. Updating a gitlink requires the
same native tests and nine-trial end-to-end study used by CI. The submodules remain
separate projects under their own licenses; nested engine corpus and benchmark-asset
submodules are not required by this quick start.

The first engine configure downloads Eigen when it is not installed. The first HPO
configure downloads the pinned dlib 20.0.1 archive and verifies its SHA-256; pass
`-DPINEFORGE_HPO_USE_SYSTEM_DLIB=ON` only when an exact 20.0.1 CMake package is already
installed. If `pineforge-hpo` reports that the Eigen include directory was not found (for
example, the engine configure reused an Eigen fetched by another build tree), pass
`--eigen-include <dir>` or set `EIGEN3_INCLUDE_DIR`, where `<dir>` is the directory that
contains `Eigen/Core`.

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

## Native runner 0.2.0

The native executable adds optional app-integration controls without changing
candidate ordering or scoring for runs that omit them:

| Flag | Contract |
| --- | --- |
| `--syminfo FILE` | Apply the instrument lot grid (`mincontract`), then `mintick`, `pointvalue`, `timezone`, and `session`, after inputs and overrides, in that order. Accept a flat JSON object or `{"syminfo": {...}}`; omitted values keep engine defaults. |
| `--progress-fd N` | Write and flush one JSONL object per terminal trial to an inherited writable descriptor. A single writer emits exactly the objects in the final `trials[]`, in completion order. Blocking and non-blocking descriptors are supported; drain pipes while the process runs. |
| `--max-wall-seconds S` | Positive, finite study wall limit in seconds, including native initialization. Stop taking candidates cooperatively, including inside grid/random batches. |
| `--record-metric PATH` | Repeatable additional report-metric path. Validate before execution and preserve the path spelling in each trial's `metrics`; unavailable values are JSON `null`. |
| `--trial-timeout-seconds T` | Positive, finite per-trial wall cap. The first timeout records one `trial_timeout`, publishes the completed/timeout trials, and exits immediately without joining workers. |

For example, append these controls to a native `run` command:

```bash
--syminfo symbol.json --progress-fd 3 --max-wall-seconds 600 \
--trial-timeout-seconds 20 --record-metric metrics.all.profit_factor \
--output result.json 3>trials.jsonl
```

Numeric symbol values must be finite and positive; timezone/session values must
be strings without embedded NULs. Empty strings and omitted fields are no-ops;
other catalog fields are ignored. Symbol timezone is distinct from chart timezone.
The four setters mirror the pinned engine 1.0.0 release harness.

`mincontract` (since 0.3.1) is the instrument's lot-size grid (TradingView
`syminfo.mincontract`). Pass the catalog object as is: a number is applied to the
engine as the metadata keys `qty_step` (the engine floors order quantities to this
grid, so a percent-of-equity strategy no longer books sub-lot trades) and
`mincontract` (what `syminfo.mincontract` reads in the script), before `mintick`;
absent or `null` means no grid and no call. In the object that is read, any other
value (zero, negative, string, boolean, array, object, or a number outside the double
range such as `1e999` or `1e-400`) is an initialization error with the message
`syminfo.mincontract must be a positive finite number` (exit 1, no trial runs). Bare
`NaN` and `Infinity` are not JSON: the parser rejects them first (exit 1,
`invalid JSON at byte N`, which does not name the key).

Applying the grid needs the plugin to export `strategy_set_syminfo_metadata`, which the
pinned engine v1.0.0 and v1.0.1 both do (declared in `pineforge.h`; the engine honours the
`qty_step` key). A plugin without it keeps working for every syminfo that has no
`mincontract`; with one, each trial fails with a `trial_error` that names the key and the
run exits 2 because no trial is feasible, instead of running without the grid.

SIGTERM and SIGINT produce cooperative `cancelled` stops; a wall limit produces
`deadline`. In-flight trials may finish, but unstarted candidates are not reported.
Use the per-trial cap to bound a non-returning engine call. All trials now include
`backtest.magnifier_sample_ticks_total`, including zero when no report is available.

The existing statuses `ok`, `constraint_violation`, `objective_error`,
`constraint_error`, `engine_error`, and `trial_error` remain unchanged;
`trial_timeout` is added. The `stop_reason` set is `trial_budget_reached`,
`search_space_exhausted`, `sampler_stopped`, `cancelled`, `deadline`, and
`trial_timeout`. Exit codes are **0** when a best feasible trial exists, **1** for
initialization/I/O errors, **2** when no trial is feasible, and **3** for a trial
timeout even if an earlier trial was feasible. Cooperative stops, timeouts, and
permanent progress I/O failures write final JSON to stdout and `--output` when
those destinations remain writable; a progress I/O failure then exits 1. Timeout
output includes exactly one timed-out trial and excludes other still-running
trials. Initialization or result-output failures can prevent final publication.

These controls are native-only. StudySpec's reserved `timeout_seconds` remains
rejected, and the Python CLI does not relay signals or progress descriptors.
Launchers can use public `pineforge_hpo.prepare_run(study_path, engine_root,
cache_dir)` to obtain `(native_argv, artifact_json)` without starting the native
process, then append native flags and execute directly. Optional keyword arguments
`native`, `compiler`, and `eigen_include` preserve existing CLI overrides.

CMake accepts either an engine source/build tree or an installed release prefix,
for example `-DPINEFORGE_ENGINE_ROOT=/opt/pineforge`. Generated `version.h` is
found in either `include/` or `build/include/`.

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

TPE and dlib are adaptive: proposal order depends on the seed, logical batch size, and
fixed feedback lag, not worker timing. Set `execution.batch_size` explicitly to replay
across worker counts; its default remains the worker count for compatibility. Compare
optimizers under the same objective-evaluation budget and across
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

Metric names follow the engine's report structs. Engine 1.0 renamed the monthly Sharpe
and Sortino ratios, so they are `metrics.equity.sharpe_monthly` and
`metrics.equity.sortino_monthly`; the pre-1.0 names `metrics.equity.sharpe_tv` and
`metrics.equity.sortino_tv` remain accepted aliases, so existing StudySpecs keep working.

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

The `API documentation` workflow publishes the
[C++ and Python API reference](https://pineforge-4pass.github.io/pineforge-hpo/) to GitHub
Pages; [build the same site locally](docs/api.md) to preview changes. The reference is
generated from the public headers and Python facade on the repository's default branch.

## Architecture

Initialization and execution are deliberately separated:

1. `pineforge_codegen.transpile_full()` emits generated C++, input metadata, and
   `strategy(...)` parameters once.
2. `ArtifactBuilder` compiles and validates a content-addressed strategy plugin once.
3. `StrategyPlugin` and immutable OHLCV data are loaded once for the study.
4. Each trial creates a fresh strategy handle, applies inputs and overrides, runs the
   backtest, snapshots the required report fields, then frees report and handle in order.
5. A persistent thread pool executes logical batches. The coordinator proposes and feeds
   back results in deterministic trial-ID order, with optional fixed-lag pipelining and
   deterministic prefix pruning.

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
- sampler implementation/configuration, seed, batch size/lag, pruning policy, and trial budget.

With the default batch size, the worker count still selects the logical batch size.
With an explicit batch size, changing only workers leaves `trials[]` byte-identical for
completed studies on the same deterministic artifact, data, runtime, and native build.
Wall-deadline, cancellation, and timeout truncation are intentionally outside this guarantee.
See [batching and pruning measurements](docs/batching.md) for the replay proof, metering
contract, quality tradeoffs, and opt-in flags. `--tpe-history-switch N` (default `1000`)
uses the exact 0.3.0 full-history estimator below `N` completed usable observations,
then switches to 25 elites, 64 recent non-elites, and 448 seeded older non-elites.
The switch never depends on elapsed time, worker count, pending or abandoned proposals.
StudySpec forwards `sampler.config.history_switch`; results and TPE terminal records
persist `tpe_history_switch`. An override can preserve a longer exact prefix, at the
cost of full-history startup memory and quadratic study work before the switch.

The default preserves a 1,000-observation full-history prefix; it is not selected
by a per-dimension latency budget. Larger overrides retain legacy behavior longer,
but ask cost grows with history: historical 16D snapshot probes measured 27,652
microseconds at 1,024 observations and 112,201 at 4,096. Identity below the switch
is a replay guarantee, not evidence of long-budget quality parity.

**The 0.4.0 release gate remains blocked.** At 3,000 trials (eight problems, ten seeds),
the completed-count switch has median-regret-ratio geomean 1.036492 versus 0.3.0:
about **3.65% worse**, with rotated ellipsoid 20D at 1.245558. At 10,000 (four
problems, five seeds), the geomean is 1.031570 versus the full-history benchmark
variant, with worst ratio 1.091497. These miss the required 1.02/1.03 geomeans.
The earlier "about 1.6% worse median regret at 3k than 0.3.0 in exchange for flat
ask cost" result describes the superseded issued-count transition, not this default.
The reservoir, EI draws, and test thresholds are not tuned to recover that number.
The flat 64D ask probe measures 656.450/663.061 microseconds at 100k/1M history,
above the 375-microsecond W=8 budget; 64D sampling remains a known bottleneck.
See the [final switch evidence and all gates](benchmarks/scaling/final-2026-10-04.md).

Generated strategies are compiled with the parity-critical
`-std=c++17 -O2 -ffp-contract=off -fPIC -shared` flags; with Clang, the builder also passes
`-fbracket-depth=1024`, which engine 1.0 requires for deeply nested generated C++. Result
JSON includes the HPO version, sampler implementation identity, artifact key, trials
selected by `--trials-out`, cardinality, and coverage diagnostics. Preserve result and
artifact provenance together when reporting a benchmark or bug.

## Current scope

Implemented today:

- one Pine strategy and one OHLCV dataset per executable study;
- integer, real, Boolean, categorical, stepped, and supported log dimensions;
- grid, seeded random, dlib global, and native TPE samplers;
- parallel thread execution over independent strategy handles;
- worker-independent logical batches, optional fixed-lag pipelining, and prefix pruning;
- expression objectives, comparison constraints, runtime inputs, and runtime strategy
  overrides;
- content-addressed artifact compilation and provenance.

Not implemented yet:

- multiple-strategy shared cash/margin/order sequencing and portfolio CLI execution;
- durable study storage, resume/checkpointing, or distributed workers;
- conditional/hierarchical spaces, multi-objective Pareto optimization, and walk-forward
  orchestration;
- executable resolution of registered custom C++ objectives.

Independent strategy reports must not be described as a shared-account simulation. See
[`docs/plan.md`](docs/plan.md) for the staged roadmap.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the complete native/Python checks,
architecture boundaries, benchmark contribution rules, and pull-request checklist.
Repository settings that cannot be committed as files are listed in the
[GitHub setup checklist](docs/repository-setup.md).

Run the required local checks from the repository root:

```bash
cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DPINEFORGE_ENGINE_ROOT="$PWD/external/pineforge-engine"
cmake --build build -j4
ctest --test-dir build --output-on-failure
```

CMake 3.21+ also supports the `release`, `asan` (ASan/UBSan), and `tsan`
(ThreadSanitizer) presets. Use separate sanitizer builds:

```bash
cmake --preset asan && cmake --build --preset asan -j4 && ctest --preset asan
cmake --preset tsan && cmake --build --preset tsan -j4 && ctest --preset tsan
```

To include the installed-prefix gate, configure with
`-DPINEFORGE_HPO_TEST_ENGINE_BUILD="$PWD/external/pineforge-engine/build"`
after building the pinned engine. The test performs `cmake --install` into a
temporary prefix and configures/builds HPO against that prefix.

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
