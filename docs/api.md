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

The supported Python exports are defined by
[`pineforge_hpo.__all__`](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/python/pineforge_hpo/__init__.py).
Names beginning with an underscore are implementation details and are omitted
from this site.

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
