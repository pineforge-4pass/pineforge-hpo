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
- [Stable failure codes and checked settings](failure-codes.md)
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
| `--symbol-feeds FILE` | Release-harness JSON index for fixed other-symbol reads. CSV paths are relative to the index; values are validated/hashed once and installed into each fresh trial. Also accepted by `space-info` and `warm-encode`; overrides work.json's `symbol_feeds`. String-input search requires engine/codegen >= 1.1.0 metadata; `input.symbol` dimensions are refused (D7). |
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

## Contract changes in 0.6.0

`run --warm-start FILE` auto-detects concatenated binary v2 blocks as well as
v0.5 JSON/JSONL. Binary validation checks every block's version, space hash,
column descriptors/counts, lengths and scalar domains before plugin/data loading.
Failures remain exit 4; finite exhaustion remains exit 5. Warm records never
appear on fd 3 or in the new trial file. Parent IDs remain uint64 and new IDs
follow their maximum, independent of block/row ordering.

| Control/API | Contract |
| --- | --- |
| `warm-encode --spec STUDY --input JSON --output WARM [--block-trials N]` | Native converter, also exposed by the Python CLI; converts complete result JSON, trial arrays or JSONL. `N` is positive; omit it for one block. No plugin, data or strategy compilation. |
| `space-info --spec STUDY --warm-start WARM` | Existing four-field response, using the read-only mapped loader and an exact tried-vector index. |
| Native `space-info ... --warm-details` | Adds `warm_trials`, `next_id`, `completed` and `feasible`, without hashing the payload. Add `--warm-digest` to request exact-input `source_sha256`. |
| Python `warm_start_metadata(study, file, native=...)` | JSON preflight or native binary preflight. Binary use needs the native executable, not a compiler or engine plugin. |
| Python `encode_warm_block(study, trials, sampler_state=None)` | Returns validated rows sorted by ID; the optional keyword-only checkpoint appends a state block. |
| Python `write_warm_block(stream, study, trials, sampler_state=None)` | Writes rows and optional state to a binary stream and returns its byte count. Concatenate chunks without separators. |
| C++ `TpeSampler::warm_start(shared_ptr<const WarmStartSource>, batch, state)` | A pristine sampler retains immutable shared rows, validates and reconstructs without proposal replay. A matching optional checkpoint restores exact state. |

`WarmStartSource` rows must have strictly increasing unique IDs, valid typed
parameters in search-space declaration order and optional finite feasible
objectives. The sampler validates before replacing its state; `reset()` releases
the source. The source owner must not modify mapped bytes for the sampler's lifetime.

New terminal records add `constraint_values`, in study declaration order:
each evaluated constraint expression's finite numeric result (usually 0/1), or
null when unavailable. Existing feasibility/TPE semantics do not change: only
finite `ok` objectives train, while all states reserve attempted points for finite
policies. Binary constraint columns have canonical expression order. Legacy
records without this array encode null columns, never invented evaluations.

The [precise format](study-spec.md) includes golden
vectors. There is no metrics/text/rung column. Binary warm input is incompatible
with active prefix pruning; legacy JSON continues to restore rung history.
With `trials_out=all`, binary ancestors are materialized only during final result
rendering as minimal JSON records, so that result can itself be a complete parent.
Avoid `all` when retaining the original binary plus new chunk records is sufficient.
The legacy 256-MiB cap applies only to JSON; binary supports up to `UINT32_MAX`
total rows across blocks and int32 indices for at most 2^31 grid/choice values.

## Contract changes in 0.7.0

TPE warm start never replays historical proposals. New result checkpoints preserve
exact continuation for the same sampler implementation and numerical build; legacy
row-only histories reconstruct deterministically instead. The existing batch argument
remains source-compatible but is ignored. See the continuation controls and sampler
rules for the explicit legacy compatibility change.

Full-history model construction and density evaluation parallelize independent
dimensions on a persistent worker pool once the history is large enough.
`TpeSamplerConfig::max_threads`, work.json `sampler.config.max_threads`, and native
`--tpe-max-threads` accept 0 (automatic) or 1..1024; automatic uses
`min(8, available CPUs)` with affinity/cgroup v1/v2 quota limits. Explicit limits
are also capped by available CPUs. Worker-creation failure falls back to serial.
Changing the limit never changes suggestions; it is excluded from checkpoint identity. Candidate
sampling remains serial, every dimension preserves its original arithmetic order,
and density ratios reduce in declaration order. No model-behavior flag or changed estimator default is required. The optional bounded estimator still has distinct model semantics.

## Contract changes in 0.5.0

### Continuation controls

| Surface | Contract |
| --- | --- |
| Python `run STUDY --warm-start FILE` | Validate the parent before compilation; use the study's trial budget for new work only. |
| Native `run ... --warm-start FILE` | Same input formats, checks, samplers and billing contract. |
| `space-info --spec STUDY [--warm-start FILE]` | Read-only JSON with `cardinality`, `tried`, `remaining`, `space_hash`; no compilation, plugin loading or dataset access. |
| Python `prepare_run(..., warm_start=FILE)` | Preflight, then return native argv and artifact metadata without launching. |
| C++ `TpeSampler::warm_start(history, replay_batch_size, sampler_state)` | Import candidates and optional finite objectives; return whether the optional checkpoint restored exact state. The legacy batch argument is retained but ignored in 0.7.0. |

`space-info` uses the count of **unique attempted vectors**, not successful observations.
`cardinality` and `remaining` are `null` for continuous or uint64-overflowing spaces.
It returns zero even when `remaining == 0`; a study run refuses such a parent.
Finite `without_replacement` continuation budgets cannot exceed `remaining`;
`exhaustive` continuation budgets must equal it. Invalid warm budgets fail with exit 4.
`load_study_spec(..., continuation=True)` defers those budget checks to warm preflight;
ordinary, non-continuation validation remains unchanged.

| Exit | Meaning |
| --- | --- |
| 0 | Success (a run has a feasible winner; space-info completed). |
| 1 | Existing ordinary initialization/I/O/StudySpec error. |
| 2 | Existing no-feasible-new-trial result. |
| 3 | Existing native hard trial-timeout exit; unchanged. |
| 4 | `warm-start incompatible: ...`: mismatched space/objective, unsupported sampler, unknown status or invalid/incomplete parent. |
| 5 | `space exhausted: ...`: every finite vector has been tried. |

### Result and terminal-trial fields

Every result and new terminal-trial line records `space_hash_version: 1`, `space_hash`
and declarative `space`. The hash covers parameter names, typed domains, bounds/steps,
ordered choices, log flags, objective expression, direction and sorted constraints.
Its exact portable canonical form is specified in [StudySpec](study-spec.md).
Compatibility is recomputed from recorded space, including for older hash versions.

A continuation result adds:

```json
"warm_start": {
  "source_sha256": "sha256 of the exact input bytes",
  "trials": 200,
  "completed": 200,
  "feasible": 190,
  "space_hash": "canonical current-space digest"
}
```

`completed` counts `ok` and `constraint_violation`; `feasible` counts `ok` trials.
A propagated nonfinite objective is serialized as `null` and does not train TPE.
Failed, infeasible, pruned and partial vectors count as tried; finite candidate
policies reserve them. TPE `sampler_default` may repeat parent vectors, like live TPE.
The existing requested/completed trial counters, `best_*`, and `trials` concern only
the **new job**, never the parent. Coverage concerns the union of parent and new work.
Warm history is never re-emitted on fd 3 or into the new `--trials-file`.

With `trials_out=all`, `warm_start_trials` separately preserves the full ancestor
history for another continuation. Summary/none results are not complete parents:
retain the original parent and concatenate its complete JSONL with the new job's
JSONL instead. A standalone `trials` array must likewise contain the entire intended
ancestry and each trial's recorded space. JSON inputs are capped at 256 MiB;
the v2 binary cap and minimal ancestor representation are described above.

Since 0.7.0, TPE results expose
`warm_start_model: "restored_sampler_state" | "rebuilt_history"`,
`replay_contract: "ordered_batches_v1"`, the separate
`continuation_contract: "sampler_checkpoint_v2"`, and `tpe_sampler_state` when no
candidates remain outstanding. `TpeSampler::sampler_state()` provides the same
opaque `PFHTPE2` checkpoint to C++ callers and refuses outstanding candidates.
The two MT19937-64 engines use explicit canonical 312-word arrays plus positions,
not implementation-specific standard-library stream operators. All-zero/degenerate
MT states are rejected. SHA-256 provides integrity only, not authenticity; warm
files/checkpoints must come from trusted sources.
Keep the complete history alongside this checkpoint; a checkpoint is not a trial store.
Matching configuration, seed, direction, typed history and numerical-build identity
restore exact RNG and bounded model state. Since 0.9.0, the enforced identity is the
portable revision-2 binary64 arithmetic contract: pinned CORE-MATH kernels, their
self-probe, nearest rounding, gradual underflow and the unfused-contraction canary.
Compiler brand, CPU vendor, libc and long-double format no longer participate.
Explicit IEEE FMA is used where required; implicit contraction, fast math, numerical
builtins and LTO are disabled on the numerical path. The sampler checks the supported
floating-point environment before initialization and each ask. Matching checkpoints
restore across supported Intel/AMD x86-64 with FMA3, aarch64 Linux and macOS arm64;
the application must still supply identical objective observations and ask/tell schedules.
Manual non-CMake builds report flags unavailable and refuse checkpoint restoration.
See the [cross-vendor proof and error analysis](portable-math.md).
Revision-1/v0.8.0 checkpoints rebuild ordered history, never mix old RNG/model state
with revision 2, and expose an explicit `warm_start_reason`. A mismatched arithmetic
contract or well-formed other checkpoint version likewise rebuilds
history instead of returning exit 4. A changed seed/configuration/history falls back to deterministic
reconstruction; malformed checkpoint bytes fail closed. Lag-one runner continuation
also reconstructs, as before. New batch sizes are accepted but only matching future
ask/tell schedules preserve the uninterrupted sequence.

**Legacy TPE compatibility:** row-only v0.5/v0.6 JSON/JSONL/binary histories no longer
replay proposals, even for complete matching lag-zero batches. Their next proposals
are deterministic reconstructed-history suggestions, not necessarily the old replay
stream. Numeric rejection draws and finite reservation retries depend on historical
models, so their exact RNG cursor cannot be inferred from winning terminal rows.
Grid/random behavior is unchanged. Complete result JSON and native warm-encode
preserve checkpoints; a JSONL trial stream or reduced result needs separately retained
complete rows and a matching revision-2 checkpoint to recover exact TPE continuation.
Older result checkpoints preserve useful history but do not provide revision-1 continuation
under 0.10.0.
`generated()` counts new proposals, `completed()` includes imported trainable
observations, and `outstanding()` starts at zero. New IDs follow the highest imported
ID, including failed/pruned trials. Import validates all observations before mutating
the sampler; failures leave it unchanged. `reset()` returns to the original empty study.

`dlib_global` continuation is not supported and fails closed with exit 4.

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
The runner queries `fpathconf(fd, _PC_PIPE_BUF)` for each progress pipe, falling back
to the platform's `PIPE_BUF` if no positive limit is available. Writes never exceed
that limit. One writer owns the descriptor and finishes every record, including
records larger than the atomic limit, before starting the next; workers cannot
interleave records. The caller must not share the progress destination with another
writer. There is no pipe record-size cap. Regular progress files and `--trials-file`
are also unrestricted. A broken destination is an I/O error. After an I/O failure,
final counts may exceed delivered lines and an oversized in-flight record may be
incomplete; only complete, successfully received progress lines are the billing record.

`schema_version` stays 1. Additive result fields are `trials_out`, `best_k`,
`search_space_cardinality_overflow`, the conditional `summary` block, and
`sampler_config.scale_ei_candidates` and `sampler_config.bad_reservoir_size`.
All prior result fields retain their types.
Top-level counts, best trial, and coverage describe the entire study. When the
Cartesian product exceeds uint64, default adaptive sampling accepts it and
reports finite=true/cardinality=null/overflow=true without claiming exact unique
coverage; grid and exact finite policies reject it.

TPE's identity changes to `pineforge_product_tpe_v3_bounded` (`_finite` for exact
finite policies). By default `--tpe-history-switch` is unset and TPE uses exact 0.3.0
full-history estimation for the whole study. Explicit `--tpe-history-switch N` opts
into bounded models at N completed usable observations: 25 elites plus 64 recent
non-elites and a seeded reservoir of 448 older
non-elites, 513-point numeric density tables,
good-model refits when elite IDs change, 32-completion bad-model refit epochs, and eight EI
draws bound history-dependent work. Unchanged elites and cached bad epochs skip rebuilding
the split as well as the models.
`--tpe-scale-ei-candidates N` changes the post-switch draws, capped by
`--tpe-ei-candidates`; record it for replay. Seed, explicit batch size, lag, and
pruning remain deterministic across worker counts. Enabled pruners now retain
the latest 1,024 values per rung.

The switch uses only successful finite `tell()` calls, not generated/pending/abandoned
candidates, wall time, or workers. Results add `tpe_history_switch` (positive integer
when opted in, null for never or non-TPE) and `sampler_config.history_switch`;
each TPE terminal record also adds `tpe_history_switch` (integer or null), including
in `--trials-file` and on the progress
stream. All existing schema-version-1 fields/types remain unchanged. Strict consumers
must allow this documented additive scalar; no metadata-only billing line is emitted.
StudySpec forwards `sampler.config.history_switch`. A larger override increases
full-history memory and ask cost before the switch.

The default retains every observation, with history-growing memory and ask cost.
An explicit N=1,000 is reasonable for 10k–1M studies requiring flat ask cost.
Historical 16D legacy snapshot asks cost 27,652 microseconds at 1,024
observations and 112,201 at 4,096; a larger override retains this growing cost.

Use `--batch-lag 1` to overlap sampling and execution, but it does not erase
serialized ask cost. The recent-only 64D result of 245 microseconds (about 65% of
the 375-microsecond W=8, 3-ms budget) is superseded and does not describe the
reservoir estimator. Its measured 64D ask limit and full billing throughput are
in the [final switch report](../benchmarks/scaling/final-2026-10-04.md).
The opted-in 64D probe costs 656.450/663.061 microseconds at 100k/1M history, above
the unchanged 375-microsecond budget; 64D can remain sampler-bound.

The opt-in's measured refinement cost is: at 3k, geomean 1.036492/worst
1.245558 versus 0.3.0; at 10k, geomean 1.031570/worst 1.091497 versus the full-history
benchmark variant. These are accepted opt-in tradeoffs, not evidence of quality parity.

`--tpe-bad-reservoir-size N` selects the older non-elite bound (default 448,
range 0–65,536). Its independent seeded RNG does not consume proposal RNG draws.
Zero recreates the recent-only bad-model retention, not the recommended refinement
configuration. Reservoir sampling happens as non-elites leave the recent window.

Opted-in TPE with bounded outputs retains fixed exact-prefix/model history, logical batches,
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
