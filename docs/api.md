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
- [Deterministic batching and prefix pruning](batching.md)
- [Finite candidate policies](adr/0003-finite-candidate-policies.md)
- [Benchmark suite and reproducibility protocol](../benchmarks/README.md)

## C++ API

The reusable C++ surface is in the \ref pineforge::hpo namespace and is split
into two conceptual libraries:

- **Core:** \ref pineforge::hpo::SearchSpace, samplers, candidate types,
  objective expressions, constraints, generic objective functions, and
  coordinator-owned \ref pineforge::hpo::Pruner cut snapshots.
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
| `--progress-fd N` | An inherited writable descriptor receives one complete, flushed JSONL object per terminal trial, serialized by one writer in increasing trial-ID order. Objects use the unchanged terminal-trial schema regardless of final retention mode. Consumers must drain a pipe concurrently. |
| `--max-wall-seconds S` | Positive finite study wall cap, measured from native initialization. SIGTERM/SIGINT and the cap stop new worker claims and adaptive batches; in-flight trials can finish. |
| `--record-metric PATH` | Repeatable extra metric path, validated before the first trial. Keys preserve expression spelling, including aliases, and unavailable values are `null`. |
| `--trial-timeout-seconds T` | Positive finite per-trial wall cap, starting at worker claim. On the first expiry, record one `trial_timeout`, flush progress and final JSON from terminal trials, and `_exit(3)` without joining any hung worker. Other in-flight/unstarted trials are excluded. |

Since 0.3.1, `--syminfo FILE` also reads an optional `mincontract`, the instrument lot-size
grid (TradingView `syminfo.mincontract`). A finite number above zero is applied before
`mintick` as the engine metadata keys `qty_step` (order quantities are floored to the grid)
and `mincontract`; `null` or absent means no grid and no call. In the object that is read,
any other value (zero, negative, string, boolean, array, object, or a number outside the
double range such as `1e999`) fails initialization with
`syminfo.mincontract must be a positive finite number` (exit 1). A plugin without the
optional `strategy_set_syminfo_metadata` export keeps working for every syminfo that has no
`mincontract`; with one, each trial fails with a `trial_error` that names the key and the
run exits 2 because no trial is feasible.

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
3. Cooperative stops and timeouts publish final JSON to stdout and `--output`
when configured, provided those result destinations remain writable.
`trials_completed` counts terminal records, including the timeout record.
Cancellation/deadline can validly return an empty table and exit 2.
Non-blocking progress descriptors are supported: temporary backpressure waits
for writability and retries instead of cancelling the study. A permanent progress
I/O failure stops new claims, publishes the completed trials to the writable result
destinations, then exits 1 with the I/O diagnostic on stderr. Initialization or
result-destination failures can prevent final JSON publication.
Progress I/O errors do not disable timeout protection: if an in-flight trial hangs,
the watchdog still emits final JSON and exits 3, with the I/O diagnostic on stderr.

The native schema remains version 1. CMake accepts an installed engine
prefix as well as a source/build tree, finding `pineforge/version.h` under either
`include/` or `build/include/`.

## Contract changes in 0.4.0

These controls are native `run` flags, not new StudySpec fields or Python wrapper
pass-throughs. Existing callers retain `--trials-out all` by default.

- `--trials-out all` retains every terminal record in `trials`, as before. This
  compatibility mode intentionally uses memory proportional to study size.
- `--trials-out best-k --best-k N` retains at most N feasible, finite-objective
  winners in `trials`; ties prefer the lower trial ID. Records are emitted in
  trial-ID order, not objective order. N defaults to 10 and must be 1–1,000,000.
- `--trials-out none` emits `trials: []`. Both bounded modes include `summary`
  with `counts_by_status`, `best_k` (full terminal-record objects), and
  `space_coverage` (finite/cardinality/overflow/unique/full/exhaustive facts).
- `--trials-file FILE` optionally writes every full terminal record as flushed
  NDJSON, independently of retention, with or without fd progress.
- `--max-trials 0 --max-wall-seconds 3600` runs adaptive sampling without a trial
  cap. `trials_requested: 0` denotes this deadline-only budget. In-flight work
  finishes cooperatively; a wall cap is not a hard interrupt of engine execution.

The fd progress stream is the complete billing record, not the retained final
list. Each line still has `trial_id`, `status`, `feasible`, `objective`,
`parameters`, `total_trades`, `net_profit`, `backtest`, `metrics`, and `error`,
plus the existing optional pruning object. The `backtest` counters remain
`input_bars_processed`, `script_bars_processed`, and
`magnifier_sample_ticks_total`, with unchanged names and types. No terminal-line
field is removed, renamed, or added. IDs are unique and increasing; unstarted
proposals can leave gaps. SIGTERM/deadline drains terminal lines before final
output. Progress pipes are nonblocking, with 50-ms readiness checks and a two-second
grace for a stalled reader after SIGTERM, deadline, or trial timeout. Expiring that
grace is an explicit I/O error, not successful delivery; trial timeouts still exit 3.
To preserve complete lines even on that error path, pipe records must fit the
descriptor's `PIPE_BUF` atomic-write bound (4,096 bytes on the measured Linux host).
Oversized records are rejected before any bytes of that record are written. Regular
progress files and `--trials-file` have no such line-size limit. A broken destination
is also an I/O error. After either failure, final counts may exceed delivered lines;
only the successfully received progress lines are the billing record.

`schema_version` stays 1. Additive result fields are `trials_out`, `best_k`,
`search_space_cardinality_overflow`, the conditional `summary` block, and
`sampler_config.scale_ei_candidates` and `sampler_config.bad_reservoir_size`.
All prior result fields retain their types.
Top-level counts, best trial, and coverage describe the entire study. When the
Cartesian product exceeds uint64, default adaptive sampling accepts it and
reports finite=true/cardinality=null/overflow=true without claiming exact unique
coverage; grid and exact finite policies reject it.

TPE's identity changes to `pineforge_product_tpe_v3_bounded` (`_finite` for exact
finite policies). Its default first 1,000 proposals keep full-history estimation.
Thereafter 25 elites plus 64 recent non-elites and a seeded reservoir of 448 older
non-elites, 513-point numeric density tables,
good-model refits when elite IDs change, 32-completion bad-model refit epochs, and eight EI
draws bound history-dependent work. Unchanged elites and cached bad epochs skip rebuilding
the split as well as the models.
`--tpe-scale-ei-candidates N` changes the post-warm-up draws, capped by
`--tpe-ei-candidates`; record it for replay. Seed, explicit batch size, lag, and
pruning remain deterministic across worker counts. Enabled pruners now retain
the latest 1,024 values per rung.

Use `--batch-lag 1` to overlap sampling and execution. The original recent-only
64D profile used about 65% of the W=8, 3-ms trial budget (245/375 microseconds).
That is not a guarantee for the reservoir estimator: its loaded review profile
uses about 669 microseconds/ask, exceeding that budget. History cost is flat,
but 64D sampling can still be the bottleneck; see the scaling review report.

`--tpe-bad-reservoir-size N` selects the older non-elite bound (default 448,
range 0–65,536). Its independent seeded RNG does not consume proposal RNG draws.
Zero recreates the recent-only bad-model retention, not the recommended refinement
configuration. Reservoir sampling happens as non-elites leave the recent window.

Bounded modes retain fixed warm-up/model history, outstanding logical batches,
a bounded writer queue, and best-k, independent of total trials. Exact finite
coverage uses a dense bitset for cardinalities up to 100,000,000 (at most 12.5 MB
per index); larger spaces use a temporary disk index growing with unique attempts.
Dataset/engine state still depends on
bars and workers. `all` and dlib's optimizer history are not covered by the
bounded-memory TPE guarantee.

The timing-only `--scheduler-stats` sidecar adds `proposal_seconds`,
`barrier_seconds`, `progress_serialization_seconds`, `progress_write_seconds`,
`progress_bytes`, `final_json_bytes`, `final_json_render_seconds`, and
`final_json_write_seconds`. These are not replay or billing fields. See
[scaling evidence](../benchmarks/scaling/README.md). Native `prepare` remains
a separately gated 0.4.x follow-up; Python `prepare_run()` remains unchanged.

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
