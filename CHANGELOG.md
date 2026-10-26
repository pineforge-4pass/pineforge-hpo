# Changelog

## Unreleased

- REFUSAL FIX, not a feature: hpo trials refuse in the engine's jailed profile; this makes the existing engine C ABI path reachable.
  Preserve inherited arena fd 5, preflight native admission before workers, and install the
  engine's bounded-memory default tables on fresh trial and selected-window handles.
  With the arena variable present, `--output` is also forwarded to the native runner (its
  trial spool needs a writable parent), and on Linux a present arena that is not a sealed
  memfd is now refused at startup (engine admission rule) where 1.7.0 ran with disk-backed
  tables, including when `PF_SCRATCH_ARENA_JAILED` is unset or 0.
- Follow-up to the walk-forward foundations: test hardening and documentation fixes; an unreadable
  planner helper no longer fails an ordinary artifact build (no `window_plan` block is published);
  ancestry refusals now identify the offending path, mode (octal) and uid.
- Add native selected-window planning over an immutable `DatasetView`, with bounded input,
  typed planner refusals, plan validation and retained rows/counts for OOS evaluation.
- Produce and publish source- and plugin-bound request-feed inventories when codegen
  supports them, include the producer schema in cache identity, and add pure Python
  selected-study inventory/timeframe admission with typed refusals.
- Add optional selected-window/observer plugin bindings with embedded engine revision admission
  and a fresh-handle OOS evaluation helper that checks callback identities and plan counts before
  returning a full owning report snapshot.
- Add a native planner-helper loader with verified private snapshots, engine ABI pins,
  a frozen golden self-test, and a typed C planner wrapper that borrows OHLCV rows in place.
- Record optional planner-helper identity in additive `window_plan` artifact metadata,
  outside the compile keys. Add Python hash verification, explicit-path relocation,
  locked source-cache enrichment and typed `hpo_walkforward_unavailable` refusals.
- Add immutable `DatasetView` ranges and a view-based trial-execution seam; const members make
  `DatasetView` and therefore `TrialExecutor` no longer assignable. The ordinary native CLI still
  uses the whole dataset. Split admission, study execution and publication so sequential native
  studies release their samplers, archives, workers and anonymous trial spools on return, and
  return an owned best candidate.
- Require status `ok`, feasibility and a finite objective for best-trial selection and best-k
  retention, including `summary.best_k` and retained winner rows in timeout results, with the
  existing direction and lowest-trial-ID tie rule. A post-scoring `trial_error` stays in all-mode
  `trials`, `--trials-file` / `--progress-fd` output and `counts_by_status`.
- Accept color-typed input manifests as packed `0xAARRGGBB` integers in `0..4294967295`,
  including integer sweeps, while preserving legacy string-typed color inputs.

## 1.7.0 (2026-10-25)

- Final 1.7.0 release (PEP 440 `1.7.0`, tag `v1.7.0`), paired with `pineforge-engine` v1.7.0 and
  `pineforge-codegen` 1.7.0 (tags `v1.7.0`). The `transpile` extra keeps
  `pineforge-codegen>=1.1.0,<2`; install the lockstep codegen with
  `pineforge-codegen==1.7.0`. The optional submodule gitlinks in
  `external/pineforge-engine` (`ed9e08e`) and `external/pineforge-codegen-oss` (`3218bb9`) pin
  the `v1.7.0` release commits.
- The trial snapshot now copies captured trades by the engine report's `trades_len` and refuses
  a report whose `trades_len` differs from `total_trades` with `hpo_report_invalid`. Engine
  1.7.0 defines `total_trades` as the logical closed-trade count and `trades_len` as the
  resident length of the trades array. On the owning-array route that HPO uses (it installs no
  report sink) the two are equal, so results are unchanged; a sink or resident-window report
  would now be refused instead of being copied past the end of the array. Covered by
  `test_trades_snapshot` in `tests/test_engine_adapter.cpp`.
- Bound `--trials-out all` retention in the native process with an anonymous spool independent
  of ordered streams. The Python `run` wrapper still reads the child's stdout into memory.
- The default `--trials-out all` needs a writable directory for two anonymous files
  (`--output`'s parent if writable, else non-empty `TMPDIR`, else `/tmp`), with no retry elsewhere
  after selection. Both files are unlinked at creation, but the spool holds the whole trial array
  on disk until the process exits. On read-only root filesystems, `--output` in a writable place
  and `--trials-out none|best-k` are remedies for the native CLI only. The Python `run` wrapper
  forwards neither `--output` nor `--trials-out`, so for `pineforge-hpo run` only a writable
  `TMPDIR` (or `/tmp`) helps.
- Attribute result destination write failures to the output file or stdout without marking
  the terminal trial spool failed; retain `hpo_output_io_failed` and the existing exit codes.
- Add native `--ohlcv-bin` and StudySpec `ohlcv_bin` for shared read-only mapped chart bars.
  Validate the complete `pf_bar_t/v1` sidecar and mapped file SHA-256 by default (`full`);
  `--verify-bin sidecar` / dataset `verify_bin: "sidecar"` skips only the digest comparison.
  Refuse non-regular sidecars before reading. Publish mapped files by rename/`os.replace`,
  never by rewriting or truncating them in place.
- The public Python API adds `DatasetSpec.ohlcv_bin` and `DatasetSpec.verify_bin`;
  `DatasetSpec.ohlcv` is now `Path | None`, with exactly one of `ohlcv` / `ohlcv_bin` set.
- Add C++ `Dataset::load_bin`, `BinaryVerification`, and `Dataset::verify_unchanged`.
  `Dataset::bars()` now throws `std::logic_error` for mapped input; use `data()` and `size()`.
  The mapped-file guard compares `st_dev`, `st_ino`, `st_size`, `st_mtim` and `st_ctim` against
  their load-time values, using full seconds+nanoseconds timestamps. Changes to device, inode,
  size or mtime are refused. When only ctime moves (chmod, chown, xattr, an extra hard link,
  or rename-over), re-hash the held mapped bytes against the sidecar digest parsed at load and
  accept only equality; without a digest a ctime change is refused. Rename-over never reopens
  the replacement path.
  In Sidecar mode the bytes were not compared to the digest at load, so even a metadata-only
  change can refuse if the bytes never matched it.
  The run's results are computed only from the bytes verified at load: a change that the mapped
  bytes show is refused (the held mapping is re-hashed against the sidecar digest when ctime moved).
  On platforms where a private read-only mapping does not show later in-place writes (observed on
  macOS), the run keeps using the original verified bytes and is accepted. This also applies to a
  same-size rewrite followed by exact mtime restoration (`touch -r`, `os.utime` with `ns=`):
  POSIX writes and mtime updates advance ctime.
  Residual: a rewrite that is fully restored (content and mtime) before the end-of-run re-hash can
  go undetected if trials read the changed pages in between; the guard covers changes still present
  at the end of the run, and the scenario needs a deliberate local writer.
  Detection resolution is the filesystem's timestamp granularity: coarse ctime can miss a
  rewrite within one tick that keeps size, mtime and ctime unchanged.

## 1.6.3 (2026-10-23)

- Final 1.6.3 release (PEP 440 `1.6.3`, tag `v1.6.3`), paired with `pineforge-engine` v1.6.3 and
  `pineforge-codegen` 1.6.3 (tags `v1.6.3`; the C ABI version `PF_ABI_VERSION` remains 4). The
  `transpile` extra keeps `pineforge-codegen>=1.1.0,<2`; install the lockstep codegen with
  `pineforge-codegen==1.6.3`. The optional submodule gitlinks in
  `external/pineforge-engine` (`e799ac8`) and `external/pineforge-codegen-oss` (`3ac6503`) pin
  the `v1.6.3` release commits.
- Since 1.6.2, the pinned engine changes are CI-only or test-only: run on the GCP fleet
  (#474), require full verify and parity green for release (#479), split the PR wall from
  full-event verification (#480), and test fixes (#476/#478); there is no runtime or ABI change.
  The pinned codegen changes are CI and docs only: hosted runners (#237), npm publish by
  operator dispatch only (#238), and docs (#239/#240). HPO's own code and behaviour are
  unchanged since 1.6.2; its only commit since v1.6.2 is
  `ci: run every hpo job on GitHub-hosted runners (#69)`.

## 1.6.2 (2026-10-21)

- Final 1.6.2 release (PEP 440 `1.6.2`, tag `v1.6.2`), paired with `pineforge-engine` v1.6.2 and
  `pineforge-codegen` 1.6.2 (tags `v1.6.2`; the C ABI version `PF_ABI_VERSION` remains 4). The
  `transpile` extra keeps `pineforge-codegen>=1.1.0,<2`; install the lockstep codegen with
  `pineforge-codegen==1.6.2` (a pre-release is not selected by default, so `1.6.2rc1` needed
  `--pre` or `pineforge-codegen==1.6.2rc1`). The optional submodule gitlinks in
  `external/pineforge-engine` (`dfe5f36`) and `external/pineforge-codegen-oss` (`f0786ea`) pin
  the `v1.6.2` release commits.
- The pinned pair carries the engine's selected-window contract revision `1.3` declared with the
  EN-115 completeness proof (#412), EN-115 ownership probe fixes (#446) and the ownership
  manifest-controls test sharded (#445), and codegen's generated-C++ allowlist checker
  `pf-cpp-allowlist` (CR-164 S1, #210), plus CI-only changes in both.
- Since 1.6.2rc1, the licensor entity in `LICENSE`, `NOTICE`, `PYPI_README.md` and `README.md`
  is exactly PINEFORGE LLC, pinned by `tests/python/test_license_entity.py` (#63), and CI skips
  the PR suite on draft pull requests and cancels only superseded pull-request runs (#66).
  HPO's own code and behaviour are unchanged since 1.6.1.

## 1.6.2rc1 (2026-10-20)

- Re-pin to engine and codegen v1.6.2-rc.1 (gitlinks: `external/pineforge-engine` `7bd065c`,
  `external/pineforge-codegen-oss` `9fbf5e6`; the C ABI version `PF_ABI_VERSION` remains 4), HPO
  version 1.6.2rc1 (PEP 440; the engine and codegen tags are spelled v1.6.2-rc.1). The `transpile`
  extra keeps `pineforge-codegen>=1.1.0,<2`: a pre-release is not selected by default, so install
  the lockstep codegen with `--pre` or `pineforge-codegen==1.6.2rc1`. The pinned pair carries the
  engine's selected-window contract revision `1.3` declared with the EN-115 completeness proof
  (#412), EN-115 ownership probe fixes (#446) and the ownership manifest-controls test sharded
  (#445), and codegen's generated-C++ allowlist checker `pf-cpp-allowlist` (CR-164 S1, #210), plus
  CI-only changes in both; HPO's own code and behaviour are unchanged since 1.6.1.

## 1.6.1 (2026-10-18)

- Final 1.6.1 release (PEP 440 `1.6.1`, tag `v1.6.1`), paired with `pineforge-engine` v1.6.1 and
  `pineforge-codegen` 1.6.1 (tags `v1.6.1`; the C ABI version `PF_ABI_VERSION` remains 4). The
  `transpile` extra keeps `pineforge-codegen>=1.1.0,<2`; install the lockstep codegen with
  `pineforge-codegen==1.6.1` (a pre-release is not selected by default, so `1.6.1rc1` needed
  `--pre` or `pineforge-codegen==1.6.1rc1`). The optional submodule gitlinks in
  `external/pineforge-engine` (`f37c8b5`) and `external/pineforge-codegen-oss` (`52d74d6`) pin
  the `v1.6.1` release commits.
- The pinned pair carries the engine's selected-window contract revision `1.3-parity` (parity
  with the 1.4.0 reference, no completeness claim) and codegen's timezone-only calendar built-ins
  and `array.from` element types; HPO's own code and behaviour are unchanged since 1.6.0.

## 1.6.1rc1 (2026-10-17)

- Re-pin to engine and codegen v1.6.1-rc.1 (gitlinks: `external/pineforge-engine` `196592d`,
  `external/pineforge-codegen-oss` `74fe4cd`; the C ABI version `PF_ABI_VERSION` remains 4), HPO
  version 1.6.1rc1 (PEP 440; the engine and codegen tags are spelled v1.6.1-rc.1). The `transpile`
  extra keeps `pineforge-codegen>=1.1.0,<2`: a pre-release is not selected by default, so install
  the lockstep codegen with `--pre` or `pineforge-codegen==1.6.1rc1`. The pinned pair carries the
  engine's selected-window contract revision `1.3-parity` (parity with the 1.4.0 reference, no
  completeness claim), and codegen's timezone-only calendar built-ins and `array.from` element
  types; HPO's own code and behaviour are unchanged since 1.6.0.

## 1.6.0 (2026-10-17)

- Final 1.6.0 release (PEP 440 `1.6.0`, tag `v1.6.0`), paired with `pineforge-engine` v1.6.0 and
  `pineforge-codegen` 1.6.0 (tags `v1.6.0`; the C ABI version `PF_ABI_VERSION` remains 4). The
  `transpile` extra keeps `pineforge-codegen>=1.1.0,<2`; install the lockstep codegen with
  `pineforge-codegen==1.6.0` (a pre-release is not selected by default, so `1.6.0rc1` needed
  `--pre` or `pineforge-codegen==1.6.0rc1`). The optional submodule gitlinks in
  `external/pineforge-engine` (`1e48cfd`) and `external/pineforge-codegen-oss` (`7300166`) pin
  the `v1.6.0` release commits. As an approved exception to "the release-candidate tree plus
  documentation", HPO 1.6.0 also ships #48 (comments, docstrings and docs only) and #49 (release
  plumbing: the `notify-hub` job, `scripts/notify_release_hub.py`, `tests/release`; no product
  code).
- Add a synthetic proof route for the native `--pbo-blocks` moments (tests and examples only; the
  production binary, its options and its result bytes are unchanged). `examples/pbo_proof/` holds a
  deterministic 2,048-bar series with its generator and a 25-trial grid. A separate test-only
  executable, `pineforge-hpo-native-proof`, writes each trial's original full-window equity curve; an
  independent exact-rational checker re-derives every block's count, sum and centred moment from those
  curves and compares them with the emitted values, seven controls each reach their own named
  refusal, and the PBO module is then run on the same native rows against the exact oracle. It proves
  mechanics on synthetic data only, not market behaviour. PBO on real data remains unproven.
- Accept release candidates in the form `1.6.0rcN` in `VERSION`, the CMake project
  version, and the failure-catalog diff, taken against the last final release.
  The wheel and sdist keep the full pre-release version string.
- Replace stale `UNEXECUTED` test-first status notes across comments, docstrings, Markdown and
  TSV headers with what the sealed 48-stage proof recorded in `CHANGELOG.md` and the internal
  wire notes covers, keeping its limits (recorded builds and controls only; no quality, cost or
  unmeasured cross-build equality claim; no recorded proof result for the by-hand
  compiled-strategy / canonical-curve routes; `docs/pareto.md` and its tests remain unnamed by any
  recorded proof run; the native-proof PBO run uses synthetic data only and PBO on real data
  remains unproven), and update one stale `v1.2.0` artifact label in
  `tests/test_symbol_feeds_e2e.py` to `v1.6.0` (`v1.6.0-rc.1` in #48, then `v1.6.0`; comments,
  docstrings, documentation and one test print string only; product code is unchanged).
- Add the `notify-hub` release-workflow job, `scripts/notify_release_hub.py`,
  `docs/release-chain.md`, `docs/repository-setup.md` updates and the `tests/release/` contract
  suite: after the `package` job uploads the wheel, sdist, `hpo_failure_codes.json` and
  `hpo_failure_codes_diff.json` (or on a `workflow_dispatch` replay of an existing published tag),
  verify the tagged `VERSION`, release flags and all four public assets before sending
  `repository_dispatch` `hpo-release` (`{version, prerelease, run_id}`, canonical PEP 440
  spelling) to `pineforge-4pass/pineforge-release` with a scoped GitHub App token.

## 1.6.0rc1 (2026-10-14)

- Re-pin to engine and codegen v1.6.0-rc.1 (gitlinks), HPO version 1.6.0rc1 (PEP 440; the engine and codegen tags are spelled v1.6.0-rc.1). The transpile extra keeps pineforge-codegen>=1.1.0,<2: a pre-release is not selected by default, so install the lockstep codegen with --pre or pineforge-codegen==1.6.0rc1.
- Add a synthetic proof route for the native `--pbo-blocks` moments (tests and examples only; the
  production binary, its options and its result bytes are unchanged). `examples/pbo_proof/` holds a
  deterministic 2,048-bar series with its generator and a 25-trial grid. A separate test-only
  executable, `pineforge-hpo-native-proof`, writes each trial's original full-window equity curve; an
  independent exact-rational checker re-derives every block's count, sum and centred moment from those
  curves and compares them with the emitted values, seven controls each reach their own named
  refusal, and the PBO module is then run on the same native rows against the exact oracle. It proves
  mechanics on synthetic data only, not market behaviour. PBO on real data remains unproven.
- Accept release candidates in the form `1.6.0rcN` in `VERSION`, the CMake project
  version, and the failure-catalog diff, taken against the last final release.
  The wheel and sdist keep the full pre-release version string.

## 0.15.0 (2026-10-13)

- Add `pineforge_hpo.walkforward.walkforward`, a post hoc analysis of finished per-window studies
  (contract `pineforge-hpo-walkforward/v1`, algorithm `exact_per_window_v1`): it selects the best row
  of each supplied window, reports the previous window's selected parameter vector in the next
  window's rows with its rank or the reason it is absent, and lists the selected values per dimension;
  it runs no window, evaluates nothing and writes no file. v1 analyses finished studies; native
  in-loop walk-forward comes later.

- Add the post hoc `dsr` module (`pineforge_hpo.dsr`, contract `pineforge-hpo-dsr/v1`): a library
  function that reads the recorded `returns.<series>.{sharpe_per_period,count,skew,kurt_raw,status}`
  statistics of a finished study and returns the deflated Sharpe ratio of one selected trial under
  a trial count the caller states (at least the number of rows read) or, by default, the number of
  rows read (every trial in the record, warm-start ancestors included), with module-local `dsr_*`
  refusal codes, no command-line flag and no native field.
  v1 analyses finished studies; native in-loop walk-forward comes later.

- Add `pineforge_hpo.holdout` (`holdout_compare`), a post hoc module that joins the
  finished rows of a selection study with those of a second finished study by exact
  parameter vector and reports, for the best selection vectors, what the second study
  recorded (contract `pineforge-hpo-holdout/v1`); it splits no data, runs no trial and
  makes no claim about the data either study used.
  v1 analyses finished studies; native in-loop walk-forward comes later.

- Add the native-only `--pbo-blocks S` flag for full-window bar-return moments. Completed `ok`
  and `constraint_violation` trials carry `block_returns`, and the result declares their shared
  `return_blocks` layout and numeric build identity. Bad-data payloads publish status only;
  failed and pruned trials carry none. Runs that request neither the flag nor return statistics
  keep their existing result bytes.
  PBO on real data remains unproven.
- The return-statistics numeric build identity value changed because its hashed reducer source
  grew to include return blocks. Evidence bound to the old value applies to its own version;
  the new value requires its own validation.

## 0.14.0 (2026-10-13)

- Strategy compilation has no time limit by default; `compile_timeout_seconds` still
  sets an explicit limit. The artifact lock wait still stops after 120 seconds with an
  error that names the stuck lock and identifies a lock that never frees as a deadlock
  to report.
- Remove test and CI time limits; only the CI platform bound applies. Product behavior is unchanged.
- Add the opt-in `rolling` schedule (StudySpec `execution.schedule: "rolling"`, native
  `--schedule rolling`). Whenever a trial finishes, the runner feeds its result back and at once
  proposes a new trial into the freed worker slot, instead of waiting for a batch to end, so every
  worker stays busy. A rolling result is not byte-replayable: it names
  `replay_contract: "rolling_completion_v1"` (never `ordered_batches_v1`) and carries the study-level
  `completion_order` and `completed_before`, a log of what each proposal saw, from which every
  proposal can be audited and re-derived. Pruner cuts and the patience count follow the same order,
  and a patience stop feeds back at most `workers - 1` further trials. A rolling result carries no
  TPE checkpoint, so a continuation rebuilds its history from the rows. `batch_size` and
  `batch_lag` are refused together with `rolling`, and TPE needs `constant_liar` enabled. The default
  `batches` schedule is unchanged and keeps the bytes of 0.12.0; the byte-replay rules of
  `docs/batching.md` do not apply to a rolling study.
- Move the pinned gitlinks to engine v1.5.0 (`b802cee`) and codegen v1.5.0
  (`52101f9`); the C ABI version `PF_ABI_VERSION` remains 4.
- Accept an enum input's categorical choice in either of two forms: the member's tag (for
  example `Side.long`) or the member's ordinal, the index of that member in the enum
  declaration. The rule applies when the manifest lists an enum input's `options`, which
  codegen 1.4.0 and later write as the members' string tags. A tag is accepted when it is
  exactly one of the manifest options and reaches the engine unchanged, as it did before;
  a bare member name, a tag that is not in `options` and any other string are refused. An
  ordinal is accepted when the receipt's `option_values` declares it. An enum input whose
  manifest lists no `options` (codegen before 1.4.0) is unchanged. When the receipt cannot
  be read, an ordinal choice is refused rather than checked against the positions in
  `options`, which are the declaration indices only for a fully listed enum.
- Load the compiled strategy plugin inside the HPO process only for an enum input whose
  manifest lists `options` and for which the study gives ordinal choices, to read the
  settings receipt's `option_values` with the same user rights as the native runner that
  later loads and runs it. A cached or precompiled artifact was not loaded in-process
  before, so this is a new in-process load on those paths; a tag choice needs no load.
- Lift the cap of the `transpile` extra that 0.13.2 set below codegen 1.4: the extra now
  requires `pineforge-codegen` >= 1.1.0 and < 2, because both enum choice forms are
  accepted with codegen 1.4 and later.

## 0.13.2 (2026-10-12)

The `transpile` extra is capped below codegen 1.4 until HPO supports the `Enum.member` enum options of codegen 1.4 and 1.5; no code change.

## 0.13.1 (2026-10-12)

Re-release of 0.13.0 with the version file corrected; no code change.

## 0.13.0 (2026-10-11)

- Add the `sobol` sampler (native `--sampler sobol [--sobol-scramble digital_shift|none]`; StudySpec
  `sampler.kind: "sobol"` with `sampler.config.scramble`). It issues the 64-bit Gray-code Sobol
  sequence (Joe-Kuo criterion-6 direction numbers for dimensions 2 to 1024, vendored verbatim with
  their BSD licence in `NOTICE` and `THIRD_PARTY_LICENSES/sobol_joe_kuo.txt`) over the name-sorted
  varying dimensions, with an optional seeded 64-bit digital shift. The trial ID is the sequence
  index; sampling is with replacement (duplicates are distinct occurrences, nothing is excluded,
  reseeded or skipped, and a finite space never stops the run early). A top-level `sobol` object
  carries the descriptor, its identity and the part's index range. `--warm-start` accepts only a
  complete Sobol result with a matching identity and verifies every parent row against the
  generator; other histories are refused with exit 4. Spaces with a floating-point column need a
  build whose Sobol numeric identity is bound and are refused before any trial otherwise. Runs
  without the new options keep their bytes. No quality advantage is claimed. A sealed 48-stage run
  has executed the sampler (manifest
  sha256 ee8f77c684bd7c38a9231029b5a99c2cd36f9a596ddf726c5845869d4813390c).

- Add the `returns.{bar,monthly}.{count,skipped,periods_per_year,mean,std,sharpe_per_period,
  skew,kurt_raw,status}` metrics (contract `pineforge-hpo-return-stats/v1`, legacy mode). They are
  computed from the engine's ordered equity curve while the report is alive, only for the series an
  objective, constraint or `--record-metric` names; with none named there is no reduction, no new
  object and the result bytes are unchanged. A requested run adds those names to each trial's
  `metrics` and one result-level `return_stats` object with its own build identity on every
  sampler path (independent of the TPE checkpoint identity). A trial without a report has every
  requested metric null. A build whose statistics identity cannot be bound (multi-configuration
  generator, compiler launcher, explicit compile-database OFF, CMake older than 3.19, ...) still
  builds and runs every ordinary study unchanged, and refuses a request for these metrics before
  any trial with `hpo_toolchain_unavailable` (reason `native_runner`) naming the exact unbound
  reason. The default build now also writes `compile_commands.json` into the build directory (one
  extra file). A sealed 48-stage run has executed these metrics (manifest
  sha256 ee8f77c684bd7c38a9231029b5a99c2cd36f9a596ddf726c5845869d4813390c).
- `--sampler candidates`: an explicitly given `--max-trials 0` is refused (N is at least 1); the
  budget may still be omitted, and zero keeps its "no cap" meaning for every other sampler.

- Add the `candidates` sampler (native `--sampler candidates --candidates FILE`; StudySpec
  `sampler.kind: "candidates"` with `sampler.config.candidates_file`). It evaluates an ordered
  JSONL list of complete parameter vectors, preserving duplicates with trial ID equal to the
  zero-based position. The whole list is validated before any plugin, dataset or trial work
  (initial input caps 50,000 occurrences, 32 MiB, 64 KiB per line; they bound input only). A
  final `candidate_list` object reports source and list SHA-256, evaluated and scored counts,
  `complete` (every position terminal, not every score valid) and missing ranges. A candidate
  result can seed TPE as rebuilt history through the existing loader. Candidate policy other
  than `sampler_default`, a pruner, positive patience and warm start are refused. Runs that do
  not use the new flags keep their bytes. A sealed 48-stage run has executed the sampler (manifest
  sha256 ee8f77c684bd7c38a9231029b5a99c2cd36f9a596ddf726c5845869d4813390c).

## 0.12.0 (2026-10-08)

- Add opt-in `--no-improvement-trials N` to native and Python `run`. Count ordered
  terminal trials after the first feasible finite result, resetting only on a
  strictly better feasible objective. Continuation resets count and reference best
  to the new part. Absent/zero stays off with unchanged output/checkpoint formats.
- On `no_improvement`, finish and report all already-proposed batches; tail
  improvements still affect final best. Enabled results include part-local
  `early_stop` metadata. Watchdog/output failures and observed cancellation/deadline
  keep priority. Explicit batch size is required for worker-independent stop trials.
- Move the patience-enabled coordinator's final cancellation/deadline observation
  to evaluation completion, before checkpoint serialization and worker/output cleanup.
- Validation note: the initial app-shaped plateau had 201 finite candidates, all
  tried before the trigger. It proves patience arithmetic and precedence on a real
  compiled strategy, not proposal behavior in a real app study.
- Open validation items: comparison with the actual deployed executable, and
  downstream app parser acceptance of `no_improvement` and `early_stop`.

## 0.11.0 (2026-10-07)

- License changed to the PineForge Source License 1.2.
  Releases up to 0.10.0 remain under Apache-2.0.

## 0.10.0 (2026-10-06)

- Strategies stopped by an empty `runtime.error()` are no longer scored as successful
  trials. A non-empty engine error text, a non-empty failure code, or failed run status
  rejects the partial report before objective evaluation. Best trials and existing goldens
  may therefore move for studies containing these previously silent failures.
- Add `failure_code`, `failure_args`, and `failure_origin` after `error` in terminal trial
  objects, including progress, trial files, and retained results. Forward engine codes
  verbatim, validate scalar argument objects, and cap diagnostics at 4 KiB on a UTF-8
  boundary. Optional getters preserve compatibility with older plugins and the exact ABI
  version check remains unchanged.
- Prefer checked strategy creation and input/override setters when exported. Rejected
  settings fail closed with engine-owned metadata; unsupported settings use
  `setting_unsupported`, and ambiguous status-only refusals omit a reason rather than
  inventing one. Values outside declared minval/maxval ranges or options, previously
  accepted by legacy setters, now fail trials and may move best trials. Older plugins
  retain the legacy setter fallback. Building the adapter requires engine headers >= 1.1.0.
- Add typed C++ failure metadata and Python exception `.code`, `.failure_args`, and `.origin`
  without changing public call signatures, `BaseException.args`, or human-readable
  diagnostics. `HpoError` is a catchable exception base. Initialization
  and process errors now print one structured failure document on stdout, retaining exit
  codes and stderr diagnostics.
- Preserve completed-trial publication to writable stdout and `--output` after progress-I/O
  failures. Add a top-level `failure` object (`origin`, `code`, `args`, `exit_code`) to the
  ordinary result document; retain exit 1, or exit 3 and timeout trials for the watchdog.
  Scheduler statistics continue to be written after a progress-I/O failure when requested.
- Publish `hpo_failure_codes.json` and its checked version/hash diff in the Python package
  and release assets. CI checks the unreleased diff; release packaging stamps its target
  version without changing the catalog bytes. Pin the diff baseline explicitly so checks
  continue to pass after release tagging.
  Release stamping requires advancing the baseline to the previous merged release.
- Parse symbol feeds once, including non-seekable inputs, and reserve exact retained
  capacity for bars and close times without changing feed values or fingerprints.

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
