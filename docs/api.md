# PineForge HPO API Reference

PineForge HPO is a C++17 optimization engine with a dependency-free Python
configuration and artifact-build bridge. This site documents the public API,
the executable StudySpec contract, and the design decisions behind the native
samplers.

> **Project status:** `0.x` alpha. The public interfaces are usable, but source
> and binary compatibility are not guaranteed until a `1.0` release.

## Start here

- [README and quick start](../README.md)
- [StudySpec v1](study-spec.md)
- [Architecture](architecture.md)
- [Finite candidate policies](adr/0003-finite-candidate-policies.md)
- [Benchmark suite and reproducibility protocol](../benchmarks/README.md)

## C++ API

The reusable C++ surface is in the \ref pineforge::hpo namespace and is split
into two conceptual libraries:

- **Core:** \ref pineforge::hpo::SearchSpace, samplers, candidate types,
  objective expressions, constraints, and generic objective functions.
- **Engine adapter:** \ref pineforge::hpo::Dataset,
  \ref pineforge::hpo::StrategyPlugin, \ref pineforge::hpo::TrialExecutor, and
  detached report snapshots.
- **Portfolio contracts:** \ref pineforge::hpo::PortfolioObservation and
  related account-level observation types. These are extension contracts; the
  current CLI does not yet run multi-strategy portfolio studies.

Use the **Namespaces**, **Classes**, and **Files** entries in the navigation
tree for the complete generated reference.

## Python API

The `pineforge_hpo` package provides the control-plane API:

- \ref pineforge_hpo::study_spec::StudySpec "StudySpec" and
  \ref pineforge_hpo::study_spec::load_study_spec "load_study_spec()" validate
  an executable study before the native process starts.
- \ref pineforge_hpo::artifact::ArtifactBuilder "ArtifactBuilder" and
  \ref pineforge_hpo::artifact::build_strategy_artifact
  "build_strategy_artifact()" transpile and compile each unique strategy
  artifact once and publish it into a content-addressed cache.
- \ref pineforge_hpo::transpile::transpile_source "transpile_source()" exposes
  the one-pass `pineforge-codegen-oss` bridge with structured diagnostics.
- `pineforge_hpo.prepare_run(study_path, engine_root, cache_dir)` returns
  `(native_argv, artifact_json)` after the same artifact and manifest validation
  used by the CLI, without starting a native child. Optional keyword-only
  `native`, `compiler`, and `eigen_include` select existing CLI overrides.

The supported Python exports are defined by
[`pineforge_hpo.__all__`](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/python/pineforge_hpo/__init__.py).
Names beginning with an underscore are implementation details and are omitted
from this site.

## Native runner controls

Version 0.2.0 adds the following native `run` flags; they are not StudySpec fields
or Python CLI pass-throughs:

| Flag | Semantics |
| --- | --- |
| `--syminfo FILE` | Flat or wrapped instrument JSON. Optional positive finite `mintick`/`pointvalue` and NUL-free `timezone`/`session` strings are applied after inputs and overrides, in harness order. Empty strings keep defaults; unrelated catalog keys are ignored. |
| `--progress-fd N` | An inherited writable descriptor receives one flushed JSONL object per terminal trial, serialized by one writer. Objects equal final `trials[]` entries; completion order may differ from final trial-ID order. Consumers must drain a pipe concurrently. |
| `--max-wall-seconds S` | Positive finite study wall cap, measured from native initialization. SIGTERM/SIGINT and the cap stop new worker claims and adaptive batches; in-flight trials can finish. |
| `--record-metric PATH` | Repeatable extra metric path, validated before the first trial. Keys preserve expression spelling, including aliases, and unavailable values are `null`. |
| `--trial-timeout-seconds T` | Positive finite per-trial wall cap, starting at worker claim. On the first expiry, record one `trial_timeout`, flush progress and final JSON from terminal trials, and `_exit(3)` without joining any hung worker. Other in-flight/unstarted trials are excluded. |

Each trial's `backtest` object additionally contains
`magnifier_sample_ticks_total`, copied from the detached report or zero when
unavailable. Symbol info is also available to C++ consumers through
`BacktestConfiguration::symbol_info`, `SymbolInfo`, and
`StrategyPlugin::set_symbol_info()`; unrequested optional setters are not called.

Trial statuses are `ok`, `constraint_violation`, `objective_error`,
`constraint_error`, `engine_error`, `trial_error`, and `trial_timeout`. Stop reasons
are `trial_budget_reached`, `search_space_exhausted`, `sampler_stopped`,
`cancelled` (SIGTERM/SIGINT), `deadline` (study cap), and `trial_timeout`.

Exit codes remain 0 (a feasible best trial), 1 (initialization/I/O failure), and
2 (no feasible trial); 3 denotes a trial timeout regardless of earlier feasible
results. A timeout may therefore have `ok: true` and a best trial while exiting
3. All stop paths publish final JSON to stdout and `--output` when configured.
`trials_completed` counts terminal records, including the timeout record.
Cancellation/deadline can validly return an empty table and exit 2.
Progress I/O errors do not disable timeout protection: if an in-flight trial hangs,
the watchdog still emits final JSON and exits 3, with the I/O diagnostic on stderr.

The native schema remains version 1. Without the new flags, all existing result
fields and proposal/scoring behavior are preserved, apart from the product
version and the additive magnifier counter. CMake accepts an installed engine
prefix as well as a source/build tree, finding `pineforge/version.h` under either
`include/` or `build/include/`.

## Compatibility boundaries

- PineForge HPO consumes compiled PineForge strategy plugins and the public
  engine C ABI; it is not a `BacktestEngine` subclass.
- The direct PineScript flow calls `pineforge-codegen-oss` once before the
  native trial loop. The transpiler is optional when a compiled plugin is
  supplied.
- A fresh strategy handle is created for every trial. Plugins and immutable
  OHLCV data may be shared across workers; strategy handles may not.
- `PortfolioObservation` describes account-level objective inputs, but
  aggregating independent completed reports is not a shared-account
  simulation.

See [LEGAL.md](../LEGAL.md) for the Apache-2.0 project boundary and optional
dependency licenses.

## Build this site locally

Install Doxygen 1.9.8 or newer, then run from the repository root:

```bash
rm -rf build/docs
mkdir -p build/docs
PROJECT_NUMBER="$(cat VERSION)" doxygen Doxyfile
python3 docs/pages/validate_generated_site.py build/docs/html
```

Open `build/docs/html/index.html`. The validation step removes build-machine
paths and rejects broken local links. The same commands are executed by
`.github/workflows/docs-pages.yml`; the workflow publishes only from `main`
and validates documentation changes on pull requests.
