# Changelog

## 0.4.0

- Keep exact 0.3.0 full-history TPE by default for the entire study. The default
  history switch is unset (`null` in StudySpec/results means never switch).
  Opt into `--tpe-history-switch N` to bound native TPE history to 25 global elites,
  64 recent non-elites, and a seeded reservoir of 448 older non-elites. Cache numeric
  density tables, refit good models on elite changes and bad models every 32 completions,
  skip split construction on cache hits, retain live constant-liar overlays, and use eight
  draws after the switch. Outstanding and abandoned proposals, workers, and elapsed time
  never advance the switch. Add `--tpe-history-switch N` and StudySpec
  `sampler.config.history_switch`; record additive `tpe_history_switch` in results and
  TPE terminal records, plus `sampler_config.history_switch`. Schema version remains 1,
  and all existing terminal fields/types and billing counters remain unchanged.
  Record the
  `pineforge_product_tpe_v3_bounded` identity and `scale_ei_candidates` setting.
- Add `--tpe-bad-reservoir-size` (default 448, maximum 65,536) and the additive
  `sampler_config.bad_reservoir_size` result field. Exact proposals below the switch
  are an identity check, not evidence of long-budget quality equivalence.
- Make bounded TPE opt-in after measured long-budget refinement costs. With N=1,000,
  eight problems and ten seeds at 3k give geomean 1.036492 versus 0.3.0,
  with worst ratio 1.245558. Four problems and five seeds at 10k give geomean 1.031570,
  with worst
  ratio 1.091497 versus the benchmark-only full-history variant. This is about 3.65%
  worse per-problem median regret at 3k, not the superseded round-one 1.6% result.
  Recommend N=1,000 for 10k–1M studies when flat cost outweighs this quality tradeoff.
  Full-history default memory and ask cost remain history-growing.
  The completed-only transition changes reservoir initialization relative to that run;
  do not reuse its quality claims. Keep the reservoir, EI settings, and quality thresholds.
  See `benchmarks/scaling/final-2026-10-04.md` for prefix identity, all suites, and the
  measured 64D throughput limit. Larger switches preserve legacy behavior longer but
  retain its history-growing ask cost.
- Final 64D bounded asks cost 656.450/663.061 us at 100k/1M, above the 375-us budget.
  The synthetic W8 million-trial run completes in 919.82 s with 21,152 KiB peak RSS
  and every billing line strictly parsed. This is not a universal backtest throughput claim.
- Generate grid/random candidates lazily, use all workers for deadline-only adaptive studies,
  and accept `--max-trials 0 --max-wall-seconds S` without imposing a trial cap.
- Add native `--trials-out all|best-k|none`, `--best-k N` (default 10), and optional
  `--trials-file FILE` terminal-trial NDJSON. `all` remains the compatibility default;
  `best-k` and `none` bound resident trial retention independently of trial count.
- Keep `schema_version: 1` and every existing terminal-trial field/type unchanged, including
  all three billing bar counters. Progress and trials-file lines are now monotonic by trial
  ID, complete and flushed on cooperative stop, with bounded writer backpressure.
- Add result fields `trials_out`, `best_k`, `search_space_cardinality_overflow`, and, when
  dropping the full list, `summary.counts_by_status`, `summary.best_k`, and
  `summary.space_coverage`. In `none`, `trials` is empty; in `best-k` it contains retained
  feasible winners. Existing best/coverage/count fields describe the entire study.
- Permit default adaptive sampling when a finite Cartesian product exceeds uint64;
  cardinality is then null and the additive overflow flag is true. Exact finite policies
  still require representable cardinality. Exact ordinal coverage uses a dense bitset up to
  cardinality 100,000,000, with a disk-backed fallback above that bound. Disabled pruning
  retains no history; enabled pruning keeps
  the latest 1,024 observations per rung.
- Add timing-sidecar proposal/barrier, progress serialization/write/byte, and final JSON
  render/write/byte diagnostics; publish scaling and paired-quality evidence, a 100,000-trial
  native memory/time regression, worker replay, output parity, and stop-flush tests.
- Keep timeout archive insertion and progress enqueue atomic under backpressure. Bound
  progress readiness waits to 50 ms and fail explicitly after a two-second stopped-reader
  grace. Pipe records must fit `PIPE_BUF` and are rejected before writing otherwise;
  regular progress files and trials files remain unrestricted. Billing still uses received
  complete progress lines, never final-result counts following an I/O failure.
- Extend worker replay past the sampler switch to 3,000 trials and give the native
  quality suite a 300-second timeout to avoid spurious failures under benchmark load.
- Native `prepare` is reserved for a separately gated 0.4.x follow-up; the existing Python
  `prepare_run()` API remains available and unchanged.

## 0.3.1 — 2026-10-04

- Native `--syminfo FILE` applies the instrument lot-size grid: an optional `mincontract`
  (TradingView `syminfo.mincontract`, flat or wrapped) reaches the engine as the metadata
  keys `qty_step` and `mincontract`, after inputs and overrides and before mintick,
  pointvalue, timezone, and session. Percent-of-equity strategies no longer book sub-lot
  trades, so trial results match runs that apply the grid.
- `mincontract` absent or `null` changes nothing (no grid, no call). In the object that is
  read, any other value that is not a positive finite JSON number (zero, negative, string,
  boolean, array, object, or a number out of double range such as `1e999`) fails
  initialization (exit 1, `syminfo.mincontract must be a positive finite number`). Bare
  `NaN` and `Infinity` are not JSON and are rejected earlier by the parser (exit 1,
  `invalid JSON at byte N`).
- A plugin that lacks `strategy_set_syminfo_metadata` keeps working for every syminfo without
  `mincontract`; with one, each trial fails with a `trial_error` naming the key (exit 2, no
  feasible trial) instead of running without the grid.
- Add fake-plugin tests for the metadata keys, their order, the absent/null and invalid
  cases, and a plugin without the setter.

## 0.3.0 — 2026-10-03

- Decouple logical proposal batches from workers with `--batch-size`; defaults preserve
  the 0.1.x adaptive sequence and existing trial values.
- Add opt-in deterministic fixed-lag pipelining with `--batch-lag 1`, plus timing-only
  `--scheduler-stats` diagnostics.
- Add opt-in median and successive-halving prefix pruning with frozen earlier-batch cuts,
  partial `pruned` trial metrics, and cumulative compute counters.
- Share the existing compiled artifact, plugin, dataset, and compiled expressions for the
  entire study; reuse worker threads and serialize inputs once per trial across rungs.
- Forward batching/pruning settings from StudySpec and retain 0.2.0 progress, stop,
  symbol-info, recorded metrics, magnifier metering, and trial-timeout contracts.
- Add worker-count replay, lag, pruning, default-compatibility, and prefix API regressions;
  publish paired real-strategy measurements and replay methodology.
- Clear stale prefix report values on pruning-path engine/objective failures while keeping
  cumulative compute counters; discard queued trials when the coordinator unwinds.
- Reject lagged TPE without constant liar during StudySpec validation, document the
  whole-trial watchdog budget, and qualify unexplained default timing gaps as noise.

## 0.2.0 — unreleased

- U1: native `--syminfo FILE` applies optional mintick, pointvalue, exchange
  timezone, and session after inputs and overrides, in release-harness order.
  Flat and wrapped JSON are accepted; instrument numbers must be finite and
  positive, strings must be NUL-free, and omitted/empty string values keep defaults.
- U2: native `--progress-fd N` emits one flushed terminal-trial JSONL object through
  one writer, matching final `trials[]` entries. Non-blocking descriptors wait for
  writability on temporary backpressure. Consumers must drain pipes.
- U3: SIGTERM/SIGINT and native `--max-wall-seconds S` stop worker claims and
  adaptive batches cooperatively, including grid/random mid-batch. Final JSON
  includes completed trials and `stop_reason: cancelled` or `deadline`.
  Signal handlers restart interrupted blocking I/O, including final result writes.
- U4: repeatable native `--record-metric PATH` validates and retains additional
  report metrics under their expression names; unavailable values are `null`.
- U5: every trial `backtest` now includes `magnifier_sample_ticks_total`.
- U6: CMake supports installed engine prefixes, locating generated `version.h`
  under both `include/` and `build/include/`.
- U8: native `--trial-timeout-seconds T` records one `trial_timeout`, emits final
  JSON from terminal trials with `stop_reason: trial_timeout`, and `_exit(3)`
  without joining hung workers. The watchdog remains active if progress I/O fails.
  The study aborts rather than changing worker count.
- U9: public `pineforge_hpo.prepare_run(study_path, engine_root, cache_dir)` returns
  native argv and artifact JSON without launching, reusing CLI preparation.
- Existing trial statuses remain `ok`, `constraint_violation`, `objective_error`,
  `constraint_error`, `engine_error`, and `trial_error`. Existing stop reasons remain
  `trial_budget_reached`, `search_space_exhausted`, and `sampler_stopped`.
- Exit codes 0 (best feasible), 1 (initialization/I/O error), and 2 (no feasible
  trial) are unchanged; 3 denotes a trial timeout even with an earlier best trial.
  Cooperative stops and timeouts publish final JSON when result destinations remain
  writable, including empty tables for cooperative stops. Permanent progress I/O
  failures publish completed trials before exit 1; initialization or result-output
  failures may prevent publication.
- Add separate ASan/UBSan and TSan CMake presets and native-process contract tests.
  With no new flags, existing result fields and scoring/proposals remain unchanged
  except for the product version and the additive magnifier counter. No Python
  signal/progress/wall-limit forwarding or StudySpec timeout fields are added.

## 0.1.0

- Initial native single-strategy runner, artifact bridge, metric expressions,
  grid/random/TPE/dlib samplers, and finite candidate policies.
