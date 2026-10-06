# Changelog

## 0.9.0 — 2026-10-05

- Vendor MIT-licensed CORE-MATH binary64 `log`, `log1p`, `exp`, `expm1`, `cos` and `erfc`.
  TPE proposal arithmetic no longer calls host transcendental functions or uses long double.
  Use compensated binary64 pairs for finite-grid coordinates; retain the contraction canary,
  disabled fast math/contraction and explicit correctly-rounded IEEE FMA/square root.
- Bump TPE's numerical algorithm revision to 2 and replace host-libm identity with a
  portable-kernel self-probe. Revision-1/v0.8.0 checkpoints rebuild ordered objective history
  with an explicit reason, never import or mix old sampler state. Regenerate serial goldens
  for the portable revision; preserve grid/random release compatibility.
- Intentionally stop binding the compiler-flags hash in `numeric_build_identity`:
  the strict semantic contract, contraction canaries and portable-kernel self-probe
  permit cross-compiler/vendor restoration. Arbitrary numerical compiler modifications
  remain unsupported; a missing generated flags header still prevents restoration.
- Add eight-space serial/threaded cross-vendor and Intel warm-restore proof tooling,
  MPFR accuracy checks and independent-build proposal-only performance measurements.
  Document the supported arithmetic environment, numerical bounds and proof receipts.
- Harden trusted input-kind metadata: require an actual symbol-kind transpile canary,
  refuse markers contradicting known pre-1.1.0 codegen versions, and cross-check precompiled
  manifests against adjacent provenance. Document external builders' trust obligations.
- Include bounded ASan quarantine options in the `asan` test preset so it works as-is.

## 0.8.0 — 2026-10-05

- Add fixed other-symbol `request.security` feeds: CLI `--symbol-feeds` and
  StudySpec `symbol_feeds` accept the release harness's JSON index. Native initialization
  validates/hashes CSV values once; every fresh trial installs the same immutable facts,
  bars and close times through the public C ABI. Refusals occur before any trial output.
- Record `applied_runtime.symbol_feeds` and `runtime_sha256`, including exact symbol
  keys, installed facts, timeframe, bar count, first/last open and value hash. Warm JSON,
  JSONL and v2 block headers refuse changed/added/removed feeds. Feedless v2 bytes remain
  unchanged; feedless grid/random result content remains unchanged except the release
  version marker. TPE additionally gains the numeric-identity provenance fields below.
- Refuse search dimensions naming `input.symbol` (D7); fixed symbol inputs remain supported.
  Require engine/codegen >= 1.1.0, pin both optional submodules to v1.2.0, and fail closed
  for ambiguous string-input metadata. Add real-artifact D7 refusal and BTCUSDT 4h /
  BINANCE:ETHUSDT 240 + 1D compiled-strategy C-ABI equality gates to Linux CI.
- Preserve `input.source`/`input.enum` and symbol-free string/timeframe search dimensions.
  The artifact builder stamps `input_kind_schema: 1` from codegen's modern `requests` result;
  both frontends trust only that marker, never another input's kind or package version.
  Unstamped string searches require rebuilding; pre-stamp artifact caches rebuild once.
  Ignore unrelated duplicate titles and refuse unknown manifest input types consistently.
- Key independent serial goldens by double/long-double precision and require a matching
  golden in both CI jobs; print the full identity. Non-CI unavailable goldens remain visible
  CTest skips. Matching checkpoints with an unavailable flags hash
  always rebuild. Probe 4,096 inputs per libm function, cached once per process with
  floating-point environment/errno preserved. Hash custom CMake configuration flags too.
- Pin the Linux aarch64 serial golden and name the precision key on golden mismatches.
  Differentially interpose all 22 probed libm functions across eight search-space shapes,
  requiring every proposal-changing perturbation to change the numeric identity.
- Scope runtime-libm identity to functions used by the configured search space, excluding
  long-double log1p for non-log dimensions. Expose `numeric_build_identity` and compared
  `parent_numeric_build_identity` in TPE results and warm provenance.
- Tolerate unreadable optional native input manifests; support large required manifests
  with clear file diagnostics. Accept `--symbol-feeds` in `warm-encode`/`space-info`,
  clarify malformed warm headers, and align common feed errors with the engine harness.
- Repair the no-replay manual build recipe and clarify that other checkpoint versions rebuild.

Feed-extended warm-v2 headers use flags bit 0 and are refused by readers before 0.8.0.
The sampler algorithm and RNG consumption do not change; the expanded numerical-build
identity intentionally rebuilds older sampler checkpoints without historical proposal replay.

## 0.7.0 — 2026-10-05

- Derive checkpoint numerical identity from an in-sampler contraction canary,
  runtime double/long-double libm probe hash, sampler algorithm revision and a
  hash of generated compilation options/definitions, rather than declaring contraction.
  Valid other checkpoint versions rebuild; malformed envelopes remain rejected.
- TPE now compiles with `-ffp-contract=off`: fresh streams change versus 0.6.0 on
  arm64/FMA targets; x86-64 builds without FMA retain the previous stream.

- Remove historical TPE proposal replay from all continuation paths. Imported
  observations never generate historical proposals, for any history count/batch/seed.
- Add checksummed `tpe_sampler_state` and optional `PFHSTATE` warm-v2 blocks.
  `PFHTPE2` serializes both MT19937-64 engines canonically (312 words + position),
  independently of libc++/libstdc++ stream formats; zero/degenerate MT states are refused.
  Enforce compiler/stdlib/target/floating-point numerical-build identity in the
  signature. A foreign build or different/shorter history rebuilds, never exits 4
  merely because its valid checkpoint is incompatible. Checksums are integrity,
  not authenticity: warm inputs must be trusted.
- Full-history checkpoints are independent of history count. Bounded checkpoints
  include retained/cached model IDs, so their size depends on configured retention.
  At the switch boundary stale full-history model ID lists are omitted; the 16-MiB
  payload cap still applies. Matching checkpoints preserve partial-batch continuation.
- Keep `replay_contract: "ordered_batches_v1"` for fresh-run ordering and add
  `continuation_contract: "sampler_checkpoint_v2"` for TPE (the unshipped PR initially
  changed `replay_contract` to `sampler_checkpoint_v1`; this is now separated).
  Replace `warm_start_model: "replayed_batches"` with `"restored_sampler_state"`;
  `"rebuilt_history"` remains. Add `TpeSampler::sampler_state()` and the optional
  sampler-state argument/source overload to `warm_start()`.
- Fit/score independent dimensions with internal persistent threads, preserving
  serial RNG draws, per-dimension arithmetic and declaration-order reductions.
  Add C++/work.json `max_threads` and `--tpe-max-threads`: 0 defaults to
  min(8, available CPUs), capped by affinity and Linux cgroup v1/v2 quota. Explicit
  thread limits never change results. Failed thread creation falls back to serial.
  `WarmStartSource` implementations must support concurrent const reads.
  `history_switch` remains an owner opt-in; no estimator default changes.

### Breaking changes

Row-only v0.5/v0.6 TPE parents no longer reproduce the uninterrupted proposal stream,
including complete matching lag-zero batches. They rebuild deterministically. JSONL
trial streams alone can never give exact TPE continuation; keep a separately saved
matching checkpoint with the complete history. Model-dependent rejection/retry draws
prevent reconstructing the exact RNG cursor from terminal rows without replay.
Grid/random continuation semantics are unchanged. `PFHSTATE`-extended warm files are
rejected by readers <=0.6; omit state blocks for a compatible row-only export.
The C++ warm-start interface, config and result fields change: this is 0.7.0, not a
patch release.

This explicitly withdraws these v0.5/v0.6 promises (v0.6.0 commit `6fc5b1fe`):

- `docs/study-spec.md:799`: “TPE first attempts to replay the ordered parent as complete
  lag-zero ask/tell batches with the current seed/configuration. Every proposal and ID
  must match. On success it preserves the RNG, density caches and bounded reservoir
  state, so its first new proposal (and subsequent proposals under the same
  batching/results) equals the long run.”
- CHANGELOG 0.5.0: “Recover uninterrupted TPE proposals at matching complete lag-zero
  batch boundaries; expose deterministic reconstruction when replay is not possible.”
- `include/pineforge/hpo/sampler.hpp:214`: “IDs continue after the largest imported ID.
  Returns true when complete lag-zero batches replay exactly, preserving the
  uninterrupted RNG/model state; otherwise rebuilds history with a continuation_seed()
  RNG. replay_batch_size == 0 disables replay.”
- `docs/api.md:219`: “TPE results expose `warm_start_model: "replayed_batches" |
  "rebuilt_history"`.”

## 0.6.0 — 2026-10-04

- Add exact, sampler-only binary warm format v2: independently self-describing,
  concatenable little-endian columnar blocks. Retain every earlier attempted trial,
  uint64 IDs, typed grid/choice indices, float64 parameters/objectives/constraints
  and uint8 states; omit metrics, text and JSON. Keep v0.5 JSON/JSONL inputs for
  this release, exit codes 4/5, space identity and new-trials-only progress billing.
- Map binary columns read-only and keep collision-free tried-vector row indices.
  TPE retains shared observation references rather than per-trial candidate maps,
  including exact batch replay, bounded reservoirs and full-history reconstruction.
- Add `warm-encode --spec --input --output [--block-trials]`, a dependency-free
  Python ingest writer, native/Python byte-for-byte golden vectors, corruption
  tests, cross-version bitwise continuation and real-strategy gates. Binary input
  currently requires `execution.pruner=none`; JSON retains pruning-rung history.
- Add additive `constraint_values` to new terminal records, with null for unavailable
  evaluations. Binary columns use canonical parameter-name and constraint-expression
  order; legacy records without constraint values encode canonical nulls.
- Add an 8-GiB-cgroup resource benchmark for 100k–2M histories at 5/32 inputs.

## 0.5.0 — 2026-10-04

- Add `--warm-start FILE` study continuation from terminal-trial JSONL, complete
  result JSON or a trial array. Import history without emitting/billing old trials;
  continue IDs after the parent's maximum and record exact source SHA-256/counts.
- Add read-only `space-info --spec STUDY [--warm-start FILE]`, portable canonical
  space-hash v1 and declarative recorded space. Identity ignores package/compiler/pin
  bumps; older hash versions are recomputed from recorded space. Refuse incompatible
  parents with exit 4 and finite exhaustion with exit 5; timeout exit 3 is unchanged.
- Restore TPE observations, startup quota, bounded history, finite reservations and
  pruning rung history. Recover uninterrupted TPE proposals at matching complete
  lag-zero batch boundaries; expose deterministic reconstruction when replay is not
  possible. Grid retains untried ordinal order; random derives a continuation stream
  and excludes parent points.
- Cover native/Python contracts, hash goldens, billing, replay at batches 2/5/8 and
  200 + 200 versus 400 finite-set/proposal equivalence.

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
  grace. Query each pipe's atomic limit with `fpathconf`, falling back to platform
  `PIPE_BUF`, and write larger records in bounded chunks through the single ordered
  writer instead of rejecting normal records on macOS. Complete cooperative-stop
  delivery is unchanged; an I/O failure can leave an oversized record incomplete.
  Billing still uses received complete progress lines, never incomplete records or
  final-result counts following an I/O failure.
- Exercise progress records larger than the platform's real pipe atomic limit under
  nonblocking backpressure. Keep 3,000-trial output/pruner/worker replay tests and
  their 1,000-observation switch, but disable artificial per-rung sleeps only in
  the high-volume output fixture to avoid macOS timeouts. Smaller batching tests
  retain variable delays to exercise out-of-order worker completion. Quality
  thresholds and CTest timeouts are unchanged.
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
