# C and legacy X wiring lane

Status: source only, written 2026-10-09 on `ar/methods-cx-integrated`. The base is the composed
head `005e68d8e0ab8e1667f8f992bcc5ade28169ef9b` (tree `921e3a75e433788f5d2ef4bb6120c99c4a9e27ec`):
the clean C integration, the X reducer and the first identity helper. Nothing was compiled,
configured, run, imported or syntax-checked; every change and every test below is unexecuted.
This note is internal and public-safe. It promises no cost outcome, no memory bound and no
cross-architecture equality.

## What this lane changed

| Area | Change |
|---|---|
| `src/cli/main.cpp` | explicit `--max-trials 0` refused for the candidates sampler; request derivation from expression identifiers; executor configuration; result-level `return_stats` object; help lines |
| `include/pineforge/hpo/trial_executor.hpp`, `src/engine_adapter/trial_executor.cpp` | two request flags, nine stored numbers per series, in-place reduction before the report is released, metric resolution, contraction probe at construction |
| `CMakeLists.txt`, `tests/CMakeLists.txt` | reducer in the core library, identity helper call, unit, identity and command-line tests registered |
| tests | `test_return_stats_cli.py`, `test_return_stats_e2e.py`, `real_strategy_curve.cpp`, `tests/python/test_return_stats_route.py`, updated candidates fixture |
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
   violated an arithmetic invariant of the reducer. `hpo_portable_math_unavailable` has the
   closest requirement name (`no_fast_math`) but its catalogue text is specific to TPE, so it was
   not reused. If AR prefers a dedicated code, that is a catalogue addition (a new code plus its
   documentation entry), not a bare string in the source.
5. **Identity.** The result-level object carries `return_stats_numeric_build_identity()` from the
   identity helper's public header, on every sampler path, in addition to and unrelated to the
   TPE `numeric_build_identity`. The helper is called once from the root build with the reducer
   source, the reducer header, the contract string and per-source options.
6. **Explicit zero budget.** `--max-trials` presence is tracked apart from the old zero sentinel
   (`Options::max_trials_given`). Only the candidates admission looks at it, after the whole list
   is validated and before any plugin, dataset or trial work.

## Seams reported, not edited

1. **Duplicate contract constant (needs the identity or reducer author).**
   `include/pineforge/hpo/return_stats.hpp` and `include/pineforge/hpo/return_stats_identity.hpp`
   both define `inline constexpr std::string_view kReturnStatsContract` in `pineforge::hpo`.
   A translation unit that includes both fails to compile (redefinition). This lane keeps them
   apart: `trial_executor.cpp` includes the reducer header only, `main.cpp` the identity header only,
   and `trial_executor.hpp` neither. A one-line fix on either side (the identity header including
   the reducer header, or dropping its own constant) removes the constraint; until then no
   translation unit may use both.
2. **Pending identity delta.** The identity lane is repairing the binding of the real effective
   flags. This lane uses only the public accessors (`return_stats_numeric_build_identity`,
   `return_stats_contract`) and the CMake entry point `pfh_return_stats_identity` with its
   documented arguments. The `SOURCE_OPTIONS` list in `CMakeLists.txt`
   (`-fno-fast-math -ffp-contract=off -frounding-math -fno-builtin -fno-lto`) is the reducer
   author's recommended consistent set and is the value AR must confirm with the repaired helper;
   the identity test is registered under its existing name and may need the repaired expectations.
3. **Per-source properties.** The helper refuses a reducer source that already carries compile
   options, and writes them itself. Nobody may add per-source options to `return_stats.cpp`
   elsewhere in the build.
4. **Doxygen.** `docs/internal` and the new headers are swept by the zero-warning API build. The
   notes avoid angle brackets and commands outside code, but that build has not run.

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
* Identity: `pineforge_hpo_return_stats_identity` (build-system binding only).
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
  A proof run that skips that comparison is not a proof.

## Not claimed

No cost claim until both timing cases are measured. The identity names a build and is not an
attestation. The probe is a sanity check. Cross-architecture equality is claimed only for pairs
that evidence lists. Selected-window mode, block returns, ancestor merging and eligibility for any
deflated-Sharpe step are outside this feature.
