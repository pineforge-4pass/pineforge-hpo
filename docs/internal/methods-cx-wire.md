# C and legacy X wiring lane

Status: source only, written 2026-10-09 on `ar/methods-cx-integrated`. The base is the composed
head `005e68d8e0ab8e1667f8f992bcc5ade28169ef9b` (tree `921e3a75e433788f5d2ef4bb6120c99c4a9e27ec`):
the clean C integration, the X reducer and the first identity helper. Nothing was compiled,
configured, run, imported or syntax-checked; every change and every test below is unexecuted.
The identity helper was then repaired by its own lane (v2 identity bound to the generated compile
command, no duplicate contract constant, `identity_bound` and an unbound reason) and cherry-picked
here; the follow-up in this note completes the shared seam on top of it. This note is internal and
public-safe. It promises no cost outcome, no memory bound and no cross-architecture equality.

## What this lane changed

| Area | Change |
|---|---|
| `src/cli/main.cpp` | explicit `--max-trials 0` refused for the candidates sampler; request derivation from expression identifiers; unbound-identity refusal before any plugin, dataset, output file or trial; executor configuration; result-level `return_stats` object with the reducer's contract and rate constants and an empty-identity guard; both headers with a contract `static_assert`; help lines |
| `include/pineforge/hpo/trial_executor.hpp`, `src/engine_adapter/trial_executor.cpp` | two request flags, nine stored numbers per series, in-place reduction before the report is released, metric resolution, `require_return_stats_identity()` and the contraction probe at construction, contract `static_assert` |
| `CMakeLists.txt`, `tests/CMakeLists.txt` | reducer in the core library, identity helper call with the engine adapter and the command line as unconditional consumers, `PINEFORGE_HPO_REQUIRE_RETURN_STATS_IDENTITY`, unit, headers, identity, integration and command-line tests registered |
| tests | `test_return_stats_cli.py` (v2 prefix, bound-build probe), `test_return_stats_headers.cpp`, `test_return_stats_integration.py`, `test_return_stats_e2e.py`, `real_strategy_curve.cpp`, `tests/python/test_return_stats_route.py` (v2 prefix, unbound relay), updated candidates fixture |
| docs | `docs/return-stats.md`, `docs/study-spec.md`, `docs/api.md`, `README.md`, `CHANGELOG.md`, this note |

The metric path helper is `ReportSnapshot::metric()` in `trial_executor.cpp` (it already served
every metric name and the `--record-metric` and expression validation), extended in place. No
other metric helper exists. Python needed no source change: expressions are forwarded verbatim
and the native result is relayed whole; `tests/python/test_return_stats_route.py` pins that.

## Decisions

1. **Request.** A series is requested exactly when an objective, constraint or recorded metric
   names one of its nine fields. Nothing named means two false flags, no canary, no reduction, no
   stored value, no `return_stats` object, and no new key in any row. A near-miss name is an
   unknown metric (the names are matched exactly).
2. **Reduction point.** `copy_report` calls the reducer while the C-ABI report is alive. The view
   reads `time_ms` and `equity` of `pf_equity_point_t` in place by offset; nothing is copied or
   retained. The engine error check precedes any use of the report. A fresh strategy handle is
   still created per execution; prefix executions (pruning rungs) reduce their own prefix curve
   like every other metric.
3. **Missing report.** An engine-error row never reaches the reducer, so every requested metric,
   status included, stays NaN, which the row publishes as null.
4. **Canary.** `return_stats_contraction_free()` runs once in the executor constructor, only when
   a series is requested. A failure throws the existing `hpo_invariant` (class internal): the build
   violated an arithmetic invariant of the reducer. This stays the right code (AR, 2026-10-09).
5. **Unbound identity.** `require_return_stats_identity()` (declared in `trial_executor.hpp`,
   defined in the adapter) throws the registered `hpo_toolchain_unavailable` with
   `args.reason = native_runner` when `return_stats_identity_bound()` is false or the identity is
   empty. The catalogue's reason vocabulary is closed, so the exact unbound reason (for example
   `compile_database_disabled`, `multi_config_generator`, `compiler_launcher`) is in the
   diagnostic text, not in `args`; a dedicated reason would be a catalogue addition for AR. The
   command line calls it right after the request is derived and before the plugin, the dataset,
   the progress files or any trial, so an unbound build refuses before any work; the executor
   constructor calls it again for library callers. A run that requests nothing never reaches it,
   so ordinary runs and builds are unchanged. `render_results` additionally refuses to write a
   `return_stats` object with an empty identity (`hpo_invariant`).
6. **Identity and constants.** The result-level object carries the contract and the fixed
   risk-free rate from the reducer header (`kReturnStatsContract`, `kReturnStatsRiskFreeAnnual`;
   no duplicate literal) and `return_stats_numeric_build_identity()` from the identity header,
   on every sampler path, unrelated to the TPE `numeric_build_identity`. The identity header no
   longer defines `kReturnStatsContract`, so `main.cpp` and the adapter include both; each asserts
   at compile time that the contract bound into the identity equals the reducer's constant.
7. **Build.** The consumers are `pineforge_hpo_engine_adapter` and `pineforge-hpo-native`
   (whichever exist); their dependency on the generation target is unconditional, not gated on a
   test having passed, so a supported build constructs its own `return_stats_identity_generated.hpp`
   before compiling them. The default build writes `compile_commands.json` (accepted as one extra
   build file; an explicit OFF is respected and leaves the identity unbound). The option
   `PINEFORGE_HPO_REQUIRE_RETURN_STATS_IDENTITY` turns an unbound main build into a test failure
   for proof runs.
6. **Explicit zero budget.** `--max-trials` presence is tracked apart from the old zero sentinel
   (`Options::max_trials_given`). Only the candidates admission looks at it, after the whole list
   is validated and before any plugin, dataset or trial work.

## Seams still open

1. **Effective command versus emitted descriptor (required proof, not yet run).** The compilation
   database is what CMake intends to run; it is not proof of what the build invoked.
   `tests/test_return_stats_integration.py` deletes the real reducer object, rebuilds the real
   `pineforge_hpo_core` target verbosely, normalizes the executed command with its own
   implementation of the documented rule and requires that it equals the emitted descriptor
   token for token and equals the database entry. It must run on the actual core target of the
   supported build before any identity claim.
2. **Blast radius.** The generation target runs `cmake -P` before every build of its consumers; a
   defect in that script would break ordinary builds (the engine adapter and the command line),
   not only statistics. It is unconditional by design; the first build of the supported
   configuration is therefore part of the proof.
3. **Unbound capability coverage.** Explicit OFF and the multi-configuration generator are covered
   by fresh trees in the integration test (the multi-configuration case needs Ninja); a compiler
   launcher, a response file and a CMake older than 3.19 are covered only by the identity
   helper's own synthetic-database tests.
4. **Per-source properties.** The helper writes the reducer's compile options itself and refuses
   a source that already carries some; nobody may add per-source options to `return_stats.cpp`
   elsewhere in the build.
5. **Doxygen.** `docs/internal`, the touched public headers and the identity header (which includes
   a header that exists only after a build) are swept by the zero-warning API build, which has
   not run.
6. **Pruned and prefix rows** report the statistics of their own curve, as pinned; who may count
   them is a future consumer's decision, not this feature's.

## For the future Sobol sampler

The statistics are sampler independent by construction: request derivation, the executor and the
`return_stats` object do not look at the sampler. A Sobol path needs only its own sampler wiring
(allow-list entry, settings refusals, implementation string, proposer, any coverage block gated
on the sampler, see `docs/internal/methods-c-wire.md`) and then reports the same statistics
object and the same identity with no further change. It must not reuse the TPE checkpoint
identity for statistics, and any selected-window mode stays gated on the frozen window interface.

## Proof routes (all unexecuted)

* Unit: `pineforge_hpo_return_stats` (analytic, degenerate, month boundary, overflow, status
  invariant, a differential sweep), `pineforge_hpo_candidate_list`.
* Identity: `pineforge_hpo_return_stats_identity` (the helper's own tests) and
  `pineforge_hpo_return_stats_headers` (both headers in one translation unit, contract
  `static_assert`, the real build's identity read back against its own descriptor and the reducer's
  real per-source flags in the generated command).
* Integration (serial, long): `pineforge_hpo_return_stats_integration` (the executed reducer
  command versus the descriptor and the database; an explicit-OFF tree refuses a request with the
  registered diagnostic before the plugin, a bogus plugin path and any output file while ordinary
  runs equal the bound build's bytes; a multi-configuration tree reports its reason).
* Command line, fake plugin: `pineforge_hpo_return_stats_cli` (requested versus OFF, closed-form
  values on the fake two-point curve, null and status combinations, workers 1, 2, 4, 8 and a
  repeat, all four samplers and candidates, terminal error, deadline, timeout and cancel paths,
  candidate result as a TPE parent, process timezone independence) and
  `pineforge_hpo_candidate_list_cli` (explicit zero budget).
* Python: `python3 -m unittest discover -s tests/python -v`.
* Real engine, by hand: `tests/test_return_stats_e2e.py` (canonical curve versus an exact
  rational reference, a binary64 mirror of the pinned order and the engine's own Sharpe ratios on
  the named common domain and scale; workers; the Python route; pinned release bytes; added time
  per trial at about 8,760 and 2,000,000 script bars against identical OFF trials).
* Pinned release bytes are mandatory in the actual proof: run the command-line test with
  `PFH_RETURN_STATS_PROOF=1`, `PFH_RETURN_STATS_REFERENCE` and `PFH_RETURN_STATS_REFERENCE_SHA256`.
  A proof run that skips that comparison is not a proof. Configure the proof build with
`-DPINEFORGE_HPO_REQUIRE_RETURN_STATS_IDENTITY=ON` so that an unbound main build fails the tests
instead of skipping them.

## Not claimed

No cost claim until both timing cases are measured. The identity names a build and is not an
attestation. The probe is a sanity check. Cross-architecture equality is claimed only for pairs
that evidence lists. Selected-window mode, block returns, ancestor merging and eligibility for any
deflated-Sharpe step are outside this feature.
