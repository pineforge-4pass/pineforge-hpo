# AGENTS.md — pineforge-hpo

> Project memory for coding agents. Keep this file terse and concrete.
> CLAUDE.md is this file's twin: same body, keep the two in step.

## What this repo is

Native hyperparameter optimization for PineForge strategies: a Pine strategy is compiled once, then parameter
trials run in a parallel C++ hot loop. It pairs with `pineforge-codegen-oss` (Pine transpiler, used once per
artifact) and `pineforge-engine` (public C ABI), both pinned optional submodules under `external/`; from 1.0.0
on, codegen X.Y.Z is supported only with engine vX.Y.Z. Boundary rules: see Product boundary below.

## Build

```bash
git submodule update --init external/pineforge-engine external/pineforge-codegen-oss
cmake -S external/pineforge-engine -B external/pineforge-engine/build -DCMAKE_BUILD_TYPE=Release -DPINEFORGE_BUILD_TESTS=OFF -DPINEFORGE_BUILD_TUTORIAL=OFF
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DPINEFORGE_ENGINE_ROOT="$PWD/external/pineforge-engine" -DPINEFORGE_HPO_BUILD_EXAMPLES=ON
cmake --build build -j4
cmake --preset asan && cmake --build --preset asan -j4   # presets (CMake 3.21+): release, asan, tsan
python3 -m venv .venv && source .venv/bin/activate && python -m pip install -e '.[dev]'
python -m pip install -e external/pineforge-codegen-oss && python -m pip wheel . --no-deps --wheel-dir dist
rm -rf build/docs && mkdir -p build/docs && PROJECT_NUMBER="$(tr -d '\n' < VERSION)" doxygen Doxyfile && test ! -s build/docs/doxygen-warnings.log && python3 docs/pages/validate_generated_site.py build/docs/html
```

## Layout

- `include/pineforge/hpo/` public C++ headers (Doxygen input); `src/core/` samplers, search space, objectives, pruner, portable math, builds without the engine
- `src/engine_adapter/` strategy-plugin loading and trial execution over the PineForge C ABI; `src/cli/` the `pineforge-hpo-native` runner
- `python/pineforge_hpo/` StudySpec, artifact build, `pineforge-hpo` CLI, post-hoc analysis modules
- `tests/` ctest sources and native-CLI scripts; `tests/python/` unittest suite; `tests/release/`, `tests/workflows/` CI contracts
- `scripts/` CI, release and verification helpers; `benchmarks/` suites outside the normal build; `examples/` small studies
- `docs/` design notes and ADRs; every `.md` under it is Doxygen input and must build without warnings
- `external/` pinned engine and codegen submodules; `third_party/` vendored sources; `cmake/` helper modules

## Testing

Check names are job names in `.github/workflows/` (design: `docs/ci.md`). On pull requests `ci.yml` and `benchmark-smoke.yml` run only `authz`.

| Check (job name) | Pull request, `gke-pr.yml` | Push to main, dispatch | What it checks |
|---|---|---|---|
| `admit` | yes | `authz` in `ci-post.yml` | identity admission; docs-only classification |
| `workflow-lint`, `Light`, `Release notification contract` | yes | yes | actionlint, `tests/workflows`, `tests/release/pr_admission.py check`; license entity, release pins; `tests/release` unittest, ruff on the release scripts |
| `Native (ubuntu-latest)`, `Native (macos-latest)` | core-only tree | full tree | build, `scripts/gen_catalog_diff.py --check`, ctest, C++ API example |
| `Python 3.11`, `Python 3.14` | yes | yes | `tests/python` unittest, wheel build, ruff (3.14) |
| `HPO memory growth gate` | yes | yes | full build, `scripts/ci_memory_gate.py` (child RSS growth); no ctest |
| `Native TPE vs Optuna` | yes | `benchmark-smoke-post.yml` | benchmark smoke profile |
| `Build API reference` | yes | `docs-pages.yml`, path-filtered | zero-warning Doxygen; `Deploy GitHub Pages` follows on main |
| `build-gate` | **REQUIRED** | `ci-post.yml`; `benchmark-check` in `benchmark-smoke-post.yml` | aggregate of every job in its own workflow |

- Docs-only PRs (root README/CONTRIBUTING/CHANGELOG/SECURITY/LICENSE/AGENTS.md/CLAUDE.md, or `docs/**` .md/.rst/.txt; `docs_only()` in `tests/release/pr_admission.py`) skip every PR job except `admit`, `workflow-lint`, `Light`, `Build API reference`; `build-gate` expects those skips.
- On push to main and dispatch, `Native` also runs all ctest (adapter + CLI), including the required v0.12.0 default-schedule byte comparison, and the cross-vendor portable-math proof, and on Linux the Quick Start study, the symbol-feed/D7 equality gate and the v0.8.0 and v0.9.0 baseline contract gates. PR CI never runs the engine-adapter or native-CLI ctest tests: for changes under `src/cli` or `src/engine_adapter` run `ctest --test-dir build -R <name>` on the full build.
- Local default-schedule check: `-DPINEFORGE_HPO_V012_NATIVE=<binary built from v0.12.0>` supplies the baseline for `pineforge_hpo_rolling_default_v012`; without it, the default `PINEFORGE_HPO_REQUIRE_V012_BASELINE=OFF` permits a skip. Set `-DPINEFORGE_HPO_REQUIRE_V012_BASELINE=ON` to fail on a missing baseline at runtime.
- Release: `release.yml` on `release: published`: `package` (tag equals `v` + `VERSION`, catalog stamp, `python -m build`, asset upload), then `Notify release hub`; dispatch with a tag replays only the notification. No tag-push trigger. Schedule: no workflow has one.

**Local-only** (no workflow runs these; run the one matching your change, once, on the final candidate):
- Sanitizers, for threads, shared buffers or plugin/handle lifetimes: `ctest --preset asan` after the asan build above; tsan: `cmake --preset tsan && cmake --build --preset tsan -j4 && ctest --preset tsan`.
- `scripts/verify_portable_math.sh <release|proof|sanitizers|docs|contract|performance|optuna>`, for portable math, TPE arithmetic or numeric-identity changes.
- Engine integration: compare a fixed candidate against the canonical PineForge harness and require identical metrics/trades for the selected fixture. CI does this only on push to main; by hand, for the feature you changed, against `external/pineforge-engine/docker/run_json.py`: `python3 tests/test_<warm_start|candidate_list|sobol|return_stats>_e2e.py --native build/bin/pineforge-hpo-native --output <dir>`.
- Oracles, for the matching `python/pineforge_hpo` analysis module: `python3 tests/g2_oracles/<pbo|pareto|neighbourhood>_oracle.py`, `python3 tests/g3_oracles/<holdout|dsr|walkforward>_oracle.py`.
- CMake inputs no workflow sets: `-DPINEFORGE_HPO_TEST_ENGINE_BUILD="$PWD/external/pineforge-engine/build"` (installed-prefix gate: install or engine-gitlink changes), `-DPINEFORGE_HPO_PBO_PROOF_PLUGIN=<plugin>` (PBO native proof: block path).
- Environment inputs no workflow sets (the tests skip without them): `PFH_<NAME>_REFERENCE=<pinned reference binary>` plus `PFH_<NAME>_REFERENCE_SHA256` for `CANDIDATE_LIST`, `NO_IMPROVEMENT`, `PBO`, `RETURN_STATS`, `SOBOL` (literal byte comparison, when default output could change); `PFH_SOBOL_SDIST` and `PFH_SOBOL_WHEEL` with `python3 tests/test_sobol_licence.py .` (package contents, for packaging or `NOTICE` changes).
- Benchmarks beyond the smoke profile (`benchmarks/README.md`): only to back a performance or quality claim. For sampler or benchmark-protocol changes, run the reproducible smoke profile documented in `benchmarks/README.md` and retain its CSV metadata sidecar while reviewing the result. Ad-hoc benchmark artifacts stay under ignored build directories. A small raw evidence set may be committed only under `benchmarks/optuna/evidence/` when it is tied to a reviewed published result and includes its metadata sidecar plus verified hashes.

**Rules**
- During development run only the tests that touch your change (`ctest --test-dir build -R <name> --output-on-failure`; `python3 -m unittest discover -s tests/python -p 'test_<module>.py' -v`; `ruff check python tests/python benchmarks` and `ruff format --check python tests/python benchmarks` for Python). The whole suite runs once, on the final candidate: the PR's CI.
- Every test has one specific target; no duplicates (same target: merge, in a separate reviewed change). A new test only for a target no existing test covers. Never delete or skip a test whose target is unique.
- No extra verification layers on top of the repo's gates: no home-made kits, receipt packs, second reads, reruns.

**The probe rule.** What a Pine script computes is a parity probe, not a CI test.
A check of what a Pine script computes (script plus data gives expected values or trades) is a probe in the parity campaign's population: crypto data goes to the public corpus, any other data to the private set; the campaign's parity sweep measures it against TradingView.
The evidence for a behaviour fix is a probe in that population, measured by a sweep, not a new CI test.
A new test in CI is a real unit test with one target, no duplicates.
Migration: an existing test leaves CI only after its probe is admitted and measured in a parity sweep; nothing is removed before that.
Tests that skip in CI can never fail there: say so in the PR.
Kernel and library unit tests (ctest, the optimizer's own algorithms) are a separate class: they stay in CI, one target each, never probe candidates.

## Product boundary

- This repository is a consumer of PineForge strategy plugins and the public
  PineForge C ABI.
- `external/pineforge-engine` and `external/pineforge-codegen-oss` are pinned
  optional git submodules. Do not advance their gitlinks without the full
  compatibility and end-to-end gates.
- Do not implement HPO as a `BacktestEngine` subclass.
- Do not move transpiler implementation into this repository.
- A direct PineScript UX may invoke a one-time `pineforge-codegen-oss` bridge
  through its `pineforge_codegen` Python module; the trial hot loop remains
  native C++.

## Correctness invariants

- Transpile and compile each unique strategy artifact once, not once per trial.
- Load each strategy plugin once per worker process.
- Create a fresh strategy handle for every trial.
- Apply `strategy_set_input` and `strategy_set_override` before running.
- Check `strategy_get_last_error` before scoring a report.
- Free the report before freeing the strategy handle.
- Treat OHLCV input as immutable shared data.
- Include codegen, engine ABI/runtime, compiler, target, and exact flags in the
  artifact cache identity.
- Preserve `-ffp-contract=off` in generated-strategy compilation.

## Architecture invariants

- Keep artifact compilation, trial execution, objective evaluation, and
  sampling behind separate interfaces.
- A report-based metric expression is one objective adapter, not the objective
  abstraction itself.
- Multiple-strategy objectives consume an explicitly requested account or
  portfolio observation and need not materialize all report fields.
- Do not call independent strategy-report aggregation a shared-account
  simulation. Shared cash/margin/order sequencing requires a separate execution
  contract.
- Do not add a third-party optimizer or storage dependency until the choice is
  recorded in the plan or an ADR.

## Language and style

- C++17, four-space indentation, 100-column limit.
- Use fixed-width integer types at ABI and persistence boundaries.
- Prefer typed IDs and enums in the hot path; resolve configuration strings
  during study initialization.
- Seed all stochastic samplers and persist the seed with every study.
- Bump `kTpeAlgorithmRevision` in `src/core/tpe_algorithm.hpp` for every change to
  TPE numerical arithmetic or RNG consumption; revalidate checkpoint/golden contracts.
