# Implementation plan

## Current position

Milestones 0-3 have produced a working single-strategy MVP. It accepts one Pine
strategy and one OHLCV dataset, compiles or reuses a native plugin, runs grid,
seeded-random, adaptive dlib global, or native TPE search, applies runtime
inputs and strategy overrides, evaluates expressions/constraints, and emits the
best trial plus a complete trial table. TPE and grid also support explicit
finite `without_replacement` and `exhaustive` candidate policies.

Portfolio-facing data types and the generic custom C++ objective contract are
present. Account aggregation, portfolio studies, persistence, and
multi-objective search are not implemented.

Release 0.3.0 adds worker-independent logical batches, fixed-lag pipelining,
and deterministic prefix pruning. Release 0.2.0 supplies terminal progress,
cooperative cancellation/deadlines, and watchdog timeouts. Both new scheduling
modes remain opt-in; see [batching measurements](batching.md).

## Principles

- Transpile and compile each compatible strategy artifact once, never per trial.
- Keep Python in initialization/orchestration and the trial hot loop in C++.
- Keep artifact compilation, execution, objective evaluation, and sampling
  behind separate contracts.
- Treat single-strategy search and account-level portfolio search as separate
  evaluators sharing lower-level infrastructure.
- Seed every stochastic sampler and preserve its seed in study output.
- Never describe independent completed-report aggregation as shared-account
  execution.

## Milestone 0 — contracts and repository boundary: complete

Implemented:

- Apache-2.0 repository license at the initial release and an explicit
  separate-codegen boundary; releases up to and including v0.10.0 remain available
  under Apache-2.0. Starting with v0.11.0, original HPO code uses the
  [PineForge Source License 1.2](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/LICENSE);
- dependency-free JSON StudySpec loader for `single_strategy`;
- `Candidate`, `TrialContext`, `ObjectiveResult`, expression policy, and status
  contracts;
- `StrategyArtifact` manifest/provenance schema v1;
- reusable `PineForgeHPO::core` and `PineForgeHPO::engine_adapter` targets;
- repository-local Python bridge to the separately distributed transpiler:
  repository `pineforge-codegen-oss`, distribution `pineforge-codegen`, module
  `pineforge_codegen`.

The native layer does not depend on `BacktestEngine` internals or a third-party
storage library. Its dlib optimizer dependency is isolated behind a PIMPL.

## Milestone 1 — PineScript to cached StrategyArtifact: complete

Implemented:

- one-pass `transpile_full()` bridge and structured diagnostics;
- input-manifest and literal strategy-parameter capture;
- Linux `.so` and macOS `.dylib` compilation;
- parity-critical `-ffp-contract=off` and engine force-load linking;
- preliminary request index that can skip repeat transpilation;
- final content artifact key including the generated C++ hash;
- per-key process lock and atomic cache publication;
- manifest/provenance, generated-C++, and plugin hash validation;
- required-symbol loading and engine ABI validation before publication;
- Python `compile` orchestration surface.

Covered by tests:

- identical source/toolchain produces a cache hit without another transpile or
  native compile;
- changed source produces a different request/artifact;
- Pine and native compiler failures remain distinguishable;
- canonical flags and platform-specific force-load behavior are present;
- invalid/non-loadable plugins fail validation.

Follow-up hardening:

- exercise more compiler/SDK combinations in CI;
- add cache inspection, eviction, and size controls;
- decide whether Windows strategy compilation is in scope.

## Milestone 2 — deterministic native executor: complete

Implemented:

- immutable CSV OHLCV dataset loader;
- local-scope dynamic strategy-plugin loader and ABI checks;
- fresh strategy handle for every trial;
- fixed `strategy_set_input()` and `strategy_set_override()` application;
- chart-timeframe, timezone, and magnifier configuration;
- engine last-error propagation;
- owning report snapshots with typed metric lookup and equity curves;
- report-before-handle teardown order;
- thread-safe independent calls over one plugin and one immutable dataset.

Covered by native tests:

- dataset parsing and validation;
- plugin symbol/ABI failures;
- runtime inputs and overrides reach distinct fresh handles;
- report data survives resource cleanup;
- engine failures remain trial-local.

The example fixture is also compared against the canonical
`pineforge-engine/scripts/run_strategy.py` harness. The selected candidate's
trade count, net profit, and maximum equity drawdown are identical.

Remaining executor work belongs to later modes: process workers, cancellation,
timeouts, shared-memory datasets, and worker recovery.

## Milestone 3 — single-strategy HPO MVP: complete

Implemented:

- integer, stepped/continuous real, boolean, and categorical dimensions;
- fixed inputs and fixed strategy overrides;
- deterministic exhaustive `GridSampler`;
- deterministic seeded `RandomSampler`;
- adaptive dlib `global_function_search` ask/tell sampler;
- native single-objective TPE with mixed typed dimensions and constant-liar
  batch proposals;
- a canonical mixed-radix finite-space codec, stepped-real cardinality checks,
  and atomic no-repeat/exhaustive candidate policies for TPE and grid;
- arithmetic/comparison metric expressions compiled once;
- `min`, `max`, `abs`, constraints, and explicit division/non-finite policies;
- bounded native worker threads;
- maximize/minimize direction;
- best feasible trial and complete JSON trial table;
- Python `run` orchestration over StudySpec and the native executable.

Executable scope:

```text
one strategy
one OHLCV dataset
grid, seeded random, dlib global, or native TPE
expression objective
sequential or threaded trials
```

Near-term hardening:

- stabilize result JSON as a documented schema;
- expand StudySpec/CLI negative-path coverage;
- add CI matrix coverage for Python package, C++ Release/Debug, and sanitizers;
- measure scaling and deterministic ordering at larger worker counts.

## Milestone 4 — adaptive optimizer slices complete; persistence next

Implemented:

- accepted dlib decision record with BSL-1.0 boundary;
- pinned dlib 20.0.1 FetchContent plus exact system-package option;
- mixed continuous/discrete `DlibGlobalSampler` encoding;
- multiple outstanding requests with ordered deterministic batch feedback;
- minimize direction and abandoned failed/infeasible requests;
- real third-party-strategy benchmark against seeded random search;
- native TPE with bounded numeric Parzen mixtures, prior-smoothed categorical
  marginals, and explicit ask/tell/abandon lifecycle;
- strict typed StudySpec config for TPE startup, EI candidate, gamma, prior, and
  constant-liar controls;
- finite-policy StudySpec validation, deterministic remaining-candidate
  fallback, pending/failed reservation semantics, and coverage provenance.

Still planned:

- crash-safe checkpoint/resume;
- immutable study, artifact, and dataset provenance in persisted state;
- resumable pruning/cancellation state across process restarts;
- additional CMA-ES/evolutionary evaluation only when study requirements justify
  another optimizer;
- result/query tools for long-running studies.

External optimizer evaluation criteria:

- conditional and categorical spaces;
- constraints and multi-objective support;
- parallel ask/tell behavior;
- deterministic seeding;
- checkpointing and resumption;
- license, release activity, binary size, and integration complexity.

dlib global search and single-objective TPE are the current adaptive native
optimizers. The runner now provides prefix pruning independently of their
estimators; durable storage, distributed coordination, and Pareto study
features remain future work.

## Milestone 5 — multiple-strategy portfolio MVP: planned

Already implemented as contracts:

- `PortfolioObservation`;
- account-equity, allocation, and sleeve-summary types;
- observation-independent `ObjectiveFn<Observation>`;
- `PortfolioObjectiveFn` alias for application-provided custom C++ objectives;
- report snapshots that retain sleeve equity curves.

Still required:

- multiple source/artifact inputs;
- hierarchical candidates for strategy, market, parameters, and allocations;
- aligned sleeve return/equity series;
- independent-curve account aggregator;
- requested-field projection into `PortfolioObservation`;
- registered custom-objective lookup and configuration;
- process-worker execution for cross-plugin safety;
- portfolio constraints such as weights, concentration, turnover, and minimum
  market/strategy coverage;
- portfolio StudySpec parser and CLI output.

Exit tests will need to prove:

- account equity reconciles with sleeve weights;
- custom objectives receive only declared observations;
- strategy ordering cannot change deterministic output;
- process-worker crashes fail only their candidate and remain resumable.

## Milestone 6 — performance and advanced studies: planned

Potential work, driven by profiling and real study requirements:

- metric-only engine reporting when trades/equity are unnecessary;
- shared-memory datasets across process workers;
- additional Bayesian/CMA-ES/evolutionary sampler adapters;
- multi-fidelity evaluation and pruning;
- walk-forward and cross-market robustness objectives;
- multi-objective directions and Pareto-front reporting;
- conditional/hierarchical spaces.

## Deferred — true shared-account execution

Independent strategy reports can support approximate sleeve/equity aggregation,
but cannot reproduce shared cash, margin, order admission, synchronized fills,
or cross-strategy order ordering. True shared-account semantics require a
strategy signal/order-intent ABI and one portfolio broker/ledger. It must remain
a separate execution mode from independent-curve aggregation.

## Open decisions

1. Persistence format and storage backend.
2. Portfolio MVP: independent-curve aggregation scope and accounting rules.
3. Process-worker protocol and shared dataset transport.
4. Windows artifact compiler and native-runner support.
