# C integration lane: native CLI and Python route

Status: source only, written 2026-10-09 on `ar/methods-c-wire` (base `e9639a51`, which is the
native leaf `ar/methods-c-native`). Nothing was compiled, configured, run or imported: every C++
and Python change below, and every test, is **unexecuted** until the proof phase. Binding
semantics are the AR pin `methods-c-contract.pin.md`. This note is internal. It promises no
cost outcome, no whole-process memory bound and no universal warm-import behavior.

## What changed

| Area | Change |
|---|---|
| `src/cli/candidate_list.hpp` | Signed-zero correction: no `-0.0` normalization; accepted values equal the importer's bit for bit (`list_sha256` still ignores a zero's sign through `candidate_key`; `source_sha256` distinguishes the bytes). Finite-lattice canonicalization and refusals are unchanged. |
| `src/cli/main.cpp` | `--sampler candidates`, `--candidates FILE`; flag refusals at parse time; whole-file admission in `run()`; cursor proposer; coverage inside `TrialArchive`; additive `candidate_list` block; `sampler_implementation`; natural stop reason. |
| `python/pineforge_hpo/study_spec.py`, `cli.py` | `sampler.kind: "candidates"`, `sampler.config.candidates_file`; native argv; warm-start refusal. |
| `tests/CMakeLists.txt` | `pineforge_hpo_candidate_list` (unit) and `pineforge_hpo_candidate_list_cli` (black box). |
| Docs | `docs/study-spec.md`, `docs/api.md`, `README.md`, `CHANGELOG.md` (Unreleased; no version change). |

## Hooks as implemented (main.cpp line numbers are those of the new file; grep the names)

1. **Parse.** `Options::candidates` / `candidate_list`; `--sampler` accepts `candidates`;
   `--candidates FILE`; after the `--objective` check,
   `require_candidate_list_settings(sampler==candidates, file given, candidate_policy, pruner,
   no_improvement_trials, warm_start given)`: missing partner flag, non-default policy, a pruner,
   positive patience are `hpo_cli_usage` (exit 1); warm start is `hpo_warm_start_rejected`
   (exit 4). Nothing has been read yet.
2. **Admission.** In `run()`, immediately after the grid check and before symbol feeds, plugin
   and dataset: `load_candidate_list(path, space, fixed_inputs)`, then
   `require_candidate_list_budget(options.max_trials, N)`, then `options.max_trials = N`. Setting
   `max_trials` makes `worker_count`, `submit_batch` and `trials_requested` correct unchanged.
   `validate_search_input_kinds` (a manifest read, not a plugin load) still runs first.
3. **Proposer.** A `CandidateListCursor` is the `evaluate_batches` proposer; feedback is a no-op.
   `evaluate_candidate` and the fresh-handle path are untouched; trial ID is the position.
4. **Coverage.** `TrialArchive` owns a `std::optional<CandidateCoverage>`, built from the list
   size and recorded at the top of `add()`. `add()` runs only under `RunState::mutex_`
   (`finish()` and the single timeout record in `check_timeouts()`), `finish()` returns early once
   `timed_out_` is set, and a skipped candidate never reaches the archive, so a position is
   recorded at most once and never for an unclaimed vector. It is independent of `--trials-out`.
5. **Rendering.** `render_results`: `sampler_implementation` =
   `pineforge_candidate_list_v1`; `stop_reason` skips the `search_space_exhausted` branch for this
   sampler so a complete list reads `trial_budget_reached` at N (a requested stop reason, i.e.
   cancelled, deadline, trial_timeout, keeps precedence exactly as before); the
   `candidate_list` object is appended after `early_stop` and only when the archive has coverage,
   in both the normal and the watchdog render. The `search_space_exhausted`, `unique_candidates_attempted`
   and `exhaustive_equivalent` fields keep their meaning: they describe the vectors tried.
6. **Python.** `_parse_sampler(value, base_dir, issues)` resolves `candidates_file` with
   `_resolve_path` (relative to the study file, like `ohlcv`); a pruner other than `none` and a
   non-default `candidate_policy` are spec issues; `require_files` checks existence only and does
   not echo the path; `_native_command` appends `--candidates`; `prepare_run` refuses
   `warm_start` for this kind before building anything. `sampler.trials` stays required and
   positive and is always forwarded as `--max-trials`, so native admission is the only place that
   compares it with N. Python never reads, counts, validates or rewrites the list or any history.

## Scope boundaries kept

No sampler math or RNG change, no engine/codegen pin change, no new failure code or reason, no
version/tag, no change to rows of other samplers, no resume or merge. Runs that use none of the
new flags take no new branch: the only shared-path edits are the `Options` fields, the
`TrialArchive` optional (empty), the `sampler_implementation` early return, the `stop_reason`
condition (`&& sampler != "candidates"`) and the guarded block, so their bytes must not move; the
literal comparison against a binary built at `d2f83326` is in
`tests/test_candidate_list_cli.py` (`PFH_CANDIDATE_LIST_REFERENCE`, `..._SHA256`).

## Tests written first (all unexecuted)

* `tests/test_candidate_list.cpp` (unit): now compares accepted values by binary64 bits against the
  real importer, including signed-zero spaces (continuous and finite), and checks propagation
  through the cursor and the strategy-ABI text (`-0` versus `0`).
* `tests/test_candidate_list_cli.py` (shipped CLI, fake plugin): order, positional ids, duplicates,
  independent `list_sha256` recomputation, absent/zero/exact budget, an admission table (about 30
  cases) with typed codes and no path/value leak and no trial work started, file types, exact
  limits, flag conflicts and exit 4, admission-before-plugin ordering, worker 1/2/4/8 x lag 0/1 at a
  fixed batch, progress/trials-file equality, best-k/none coverage, constraint and engine-error
  rows, complete-means-terminal, SIGTERM, deadline and trial-timeout coverage, C to TPE
  (JSON and NDJSON parents, summary parent refused), raw-text signed zero, off-path metadata, and the
  optional literal reference comparison.
* `tests/python/test_candidates.py`: schema, path resolution, refusals, argv, relay of native
  results and failures, warm-start refusal, patience left to native.
* `tests/test_candidate_list_e2e.py` (manual, real compiled Pine, like `test_warm_start_e2e.py`):
  every distinct vector's `total_trades` and four aggregate metrics against
  `external/pineforge-engine/docker/run_json.py`, value for value, plus the Python route's argv,
  identity and coverage. HPO captures no trades, so no per-trade data is invented.

## Integration needs for a later X/Sobol sampler (not edited here)

Seams a second new sampler must touch, in the order this lane touched them:

1. `main.cpp` `Options` and the `--sampler` allow-list and message; a `require_*_settings`-style
   helper if it refuses flags (policy, pruner, patience, warm start), called after the
   `--objective` check. The existing rule "finite candidate policies only for tpe and grid" and
   the "`--max-trials` or `--max-wall-seconds` required" rule list samplers by name and need an
   entry if the new sampler is budgeted.
2. `run()` admission point: anything that can be refused without the plugin or dataset belongs
   right after the grid check; it must set `options.max_trials` before `worker_count` is computed.
3. The proposer contract of `evaluate_batches`: `propose()` runs on the coordinator thread,
   `candidate.id` must be unique and is the trial ID, `feedback` runs in ID order per batch.
4. Result keys: `sampler_implementation`, `continuation_contract`, `stop_reason` precedence and
   any new top-level block must be gated on the sampler and appended after `early_stop`, so other
   samplers' bytes do not move. `append_process_failure` inserts before the last `}`, so a new
   block need not be last.
5. Python: `study_spec.py` (`_parse_sampler` kind set and message, the config branch, the
   `_validate_candidate_policy` unsupported-kind set, a pruner issue if refused),
   `cli.py` (`_native_command` kind set and branch, the `prepare_run` warm-start refusal list). The
   existing study-spec test that asserts the kind list message needs the new name.
6. Failure registry: reuse `sampler`, `search_space`, `input`; a new `reason` would need
   `python/pineforge_hpo/hpo_failure_codes.json`, `docs/failure-codes.md` and
   `tests/test_failure_codes.py`.
7. CMake: tests only (`tests/CMakeLists.txt`); the root `CMakeLists.txt` lists `main.cpp` and the
   headers are included relatively, so nothing there changes.
8. Docs: the `Sampler` section and the coverage table in `docs/study-spec.md`, one additive
   sentence in `docs/api.md`, the README sampler table and `CHANGELOG.md`.
9. Reusable test machinery: the helpers and the reference-binary comparison in
   `tests/test_candidate_list_cli.py`; a coverage tracker is needed only if the sampler can leave
   proposed positions without a terminal row (a Sobol or deterministic-sequence sampler that
   runs a prefix of an infinite sequence needs none).

## Open questions (for AR/TOP)

1. `unevaluated_ranges` is the key name used (the pin says "missing-position ranges").
2. `search_space_exhausted: true` is still reported for a list that covers a whole finite space,
   with `stop_reason: trial_budget_reached`; say so if the boolean should also be suppressed.
3. A failed candidate-list parent (partial coverage) imports with ID gaps closed only by the
   importer's own rules; the missing positions are not reserved.
4. Python checks only that the list file exists; a missing file is therefore a spec issue
   (before the artifact build) while every content problem is a native admission failure after
   the artifact build.
5. Resolved in the integrated lane: `--max-trials` presence is tracked separately from the
   pre-existing zero sentinel, so an explicit `--max-trials 0` is refused for this sampler
   (`hpo_study_spec_invalid`, reason `sampler`) and keeps its meaning for every other sampler.
