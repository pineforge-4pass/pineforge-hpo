# G1 Sobol wiring: the shared seams

Status: source only, written 2026-10-09 on `ar/methods-g1-integrated`. The Sobol engine, mapper,
sampler, continuation code and the numeric identity helper were written by their own lanes; this
note records the places where they meet the shared files (the command line, the root and test build
files, the Python route, the licence inputs) and the decisions taken there. Nothing was compiled,
configured, run, imported or syntax-checked; every change and every test named below is unexecuted
and confirmed only by the proof listed at the end. This note is internal and public-safe. It makes
no quality claim for the sampler, promises no cost outcome and claims no cross-architecture
equality.

The sampler itself is described in `docs/internal/methods-sobol-native.md`; the numeric identity,
its binding and its limits are in `docs/internal/sobol-identity.md`. This note does not repeat
either.

## What the wiring changed

| Area | Change |
|---|---|
| `src/cli/main.cpp` | `--sampler sobol` and `--sobol-scramble digital_shift\|none`; refusals before any plugin, dataset or trial; descriptor and identity built right after the space hash; complete-result parent admission; a dedicated with-replacement branch; part accounting for every terminal trial; the additive `sobol` result block; implementation name; no `search_space_exhausted` stop reason; help text |
| `CMakeLists.txt` | the engine, mapper, sampler and identity units in the core library; the marked identity block; the option `PINEFORGE_HPO_REQUIRE_SOBOL_IDENTITY`; a marker pair around the return-statistics block |
| `tests/CMakeLists.txt` | engine, mapper, continuation, identity, oracle, table-generation, licence and command-line tests; the identity build test beside the return-statistics one |
| `tests/test_return_stats_identity.py` | the real-core scenario works on a root that already contains the Sobol block (below) |
| `python/pineforge_hpo/study_spec.py`, `cli.py` | kind `sobol`, `config.scramble`, exact unsigned 64-bit seed, no history preflight |
| `NOTICE`, `THIRD_PARTY_LICENSES/sobol_joe_kuo.txt`, `pyproject.toml`, `LEGAL.md` | attribution, unchanged licence copy, wheel metadata entry, dependency record |
| docs | `docs/study-spec.md`, `docs/api.md`, `README.md`, `CHANGELOG.md`, `docs/batching.md` (the public sections), this note, the identity note |

## Decisions

1. **Selection and refusals.** An invalid `--sobol-scramble` value is the registered
   `hpo_study_spec_invalid` (reason `sampler`); the option given with another sampler is a usage
   error. The budget-or-wall requirement covers Sobol. A non-default candidate policy is a usage
   error. Column ceiling, unrepresentable counts and log-integer limits are tested by a probe
   sampler before the plugin is loaded.
2. **Numeric identity: one call, in a fixed order.** The identity is requested only when the probe
   reports `uses_floating_point()`, before the plugin, the dataset, any output file or any parent.
   `require_sobol_numeric_identity()` in `main.cpp` makes exactly one call,
   `sobol_numeric_build_identity()`, in namespace `pineforge::hpo` (declared in
   `include/pineforge/hpo/sobol_identity.hpp`). That function runs
   `detail::require_portable_environment()` first and only then looks at the bound flag, so an
   unsupported environment is reported as `hpo_portable_math_unavailable` even in an unbound
   build, and an unbound build then refuses with `hpo_toolchain_unavailable` (reason
   `native_runner`, the exact unbound reason in the text). The caller does not read
   `sobol_numeric_identity_bound()` first, which would reverse that order; it only rejects an empty
   string or a string without the `portable-sobol-v1;` prefix as `hpo_invariant`. A discrete-only
   space passes no identity and never reaches the function. `main.cpp` does not include
   `numeric_build.hpp`, so no macro-dependent inline function is compiled into a second command-line
   unit.
3. **Parents.** Sobol warm start goes through `admit_sobol_parent` only: no generic loader, no
   finite-space exhaustion test, no ID-overflow test, no reseeding, no parent exclusion. An
   incomplete parent is refused with the registered warm-start code (exit 4). The progress writer
   and the ID-available limit stay consistent with the admitted `next_id`.
4. **Proposer.** `SobolSampler(space, seed, scramble, first_index, max_trials)`; the trial ID is the
   raw index; the draw is with replacement. With patience on, an external stop is observed at
   evaluation completion (the 0.12.0 precedence).
5. **Accounting.** `TrialArchive::add` feeds `SobolPartAccumulator::observe(trial_id)` for every
   terminal record before retention, under the archive's existing lock, so timeout rendering,
   best-k and none are all counted.
6. **Output.** The implementation name is `pineforge_sobol_gray64_joe_kuo_d6_v1`; the `sobol`
   block follows `return_stats` and precedes `candidate_list`; the `search_space_exhausted` stop
   reason is skipped for Sobol. Wall-only Sobol runs are not capped by the space cardinality;
   random still is (unchanged).
7. **Build.** The Sobol engine, mapper, sampler and identity units are core sources (the generated
   table `.inc` is included by the engine). The root passes the strict recipe `-fno-fast-math -ffp-contract=off -frounding-math -fno-builtin
   -fno-lto` to `pfh_sobol_identity` explicitly (it is also the helper's default) and names the
   consumers `pineforge_hpo_engine_adapter` and `pineforge-hpo-native` (whichever exist); their
   dependency on the generation target is unconditional, so every supported build constructs its
   own generated header. The helper writes the per-source properties of the Sobol units; nothing
   else in the root sets a flag for them. Strict warnings (`-Wall -Wextra -Wpedantic`) are on the
   core target and on the identity test; the generated header reaches `sobol_identity.cpp` alone.
8. **Marker pairs.** `# PFH-RETURN-STATS-IDENTITY-BEGIN/END` and `# PFH-SOBOL-IDENTITY-BEGIN/END`
   delimit the two identity blocks. They carry no meaning for CMake; the build tests cut a block to
   build a baseline, and the return-statistics test needs the mark because the real-core
   scenario previously appended its own block to a root that now already contains one (a second
   `pfh_return_stats_identity(TARGET pineforge_hpo_core ...)` call fails at configure time).
   `scenario_real_core` now cuts the block for its baseline and, when the root is already
   integrated, appends only what the old integration text adds beyond the root's own.
9. **Python.** `sampler.kind: sobol` with `config.scramble` as the only key (default
   `digital_shift`); unknown keys and other values are refused; the seed up to 2^64-1 stays exact
   and travels as its decimal string; a non-default policy is refused; a pruner stays allowed.
   `prepare_run` skips the whole history preflight for Sobol: native admission is the only
   authority, and Python never parses or re-serializes a parent.
10. **Licence and packaging.** `THIRD_PARTY_LICENSES/sobol_joe_kuo.txt` is a byte copy of
    `third_party/sobol_joe_kuo/LICENSE` (SHA-256
    `9d10226b50eeb34be0ab06bfa3392c7bd1f04bf602f9af4343295d1fd003d0e3`); `NOTICE` carries the
    attribution, the no-endorsement sentence and the citation request; `pyproject.toml` lists the
    copy under `license-files`; `LEGAL.md` records the dependency. The product licence is unchanged.
    The source distribution carries `third_party/sobol_joe_kuo/**`, the generated `.inc` and the
    engine sources; the wheel is the pure-Python package and carries the licence as metadata only.

## The shared-helper seam

The runtime helpers the mapper and the identity unit call are inline functions with external
linkage in headers that the TPE units also include, so the copy that runs is not necessarily the one
a Sobol unit compiled. The integration closes this by conservative coupling, not by changing the TPE
units: the identity descriptor also carries the generated compile command and the source digest of
`src/core/search_space.cpp` and `src/core/tpe_sampler.cpp` (the providers). Their options, the TPE
flag text and the TPE checkpoint identity keep their bytes; the Sobol identity moves when a
provider's command or source moves. The runtime probe stays a sanity check and no actual-flags
coverage is claimed beyond this binding. The full rule, the include-closure test that keeps the
provider list complete and the limits are in `docs/internal/sobol-identity.md`.

## Seams still open

1. **Effective command versus emitted descriptor.** As for the return-statistics identity, the
   compilation database is what CMake intends to run. `scenario_real_core` in
   `tests/test_sobol_identity.py` deletes the object of `sobol_mapper.cpp`, of both providers and of
   one math C unit, rebuilds the real target verbosely and requires the normalized executed command
   to equal the database entry that the descriptor was built from. It has not run; the other units
   rest on their database entry alone, and no identity claim stands before this comparison has
   passed on the supported build.
2. **Blast radius.** The generation target runs `cmake -P` before every build of the core library,
   the adapter and the command line; a defect in the script would break ordinary builds, not only
   Sobol. It is unconditional by design, so the first build of the supported configuration is part
   of the proof.
3. **Unbound builds.** Explicit `CMAKE_EXPORT_COMPILE_COMMANDS=OFF`, a multi-configuration
   generator and the other fail-closed reasons leave the capability unbound; a compiler launcher,
   a response file and a CMake older than 3.19 are covered only by the synthetic-database tests.
   Discrete-only Sobol runs and ordinary runs are byte-identical in a bound and an unbound build
   (pinned in the command-line test).
4. **Ledger of first-compile risks.** The `SobolAdmission::history` move into the shared warm
   history, the `sh -c` quoting of the float-reference generation step (UNIX only), the include
   directories of the Sobol test fixtures and the generated header's per-source include directory
   are the likeliest first-configure defects.
5. **The ordinal set.** `TrialArchive` still inserts every Sobol row into the ordinal set on a
   finite space (a bitmap up to 1e8 points, the existing disk-backed set above that): unchanged
   behaviour, not measured for 2^64-index runs.
6. **Doxygen.** `docs/internal`, the touched public headers and the identity header (which
   includes a header that exists only after a build) are swept by the zero-warning API build,
   which has not run.

## Proof routes (all unexecuted)

Configure the proof build with `-DPINEFORGE_HPO_REQUIRE_SOBOL_IDENTITY=ON` and
`-DPINEFORGE_HPO_REQUIRE_RETURN_STATS_IDENTITY=ON`, so that an unbound main build fails the tests
instead of passing them.

* Engine, table, mapper: `pineforge_hpo_sobol_oracle64_generate` then `pineforge_hpo_sobol_engine`,
  `pineforge_hpo_sobol_table_generation`, `pineforge_hpo_sobol_mapper_reference_generate` then
  `pineforge_hpo_sobol_mapper`, `pineforge_hpo_sobol_continuation`. A one-ulp mismatch against the
  mapper reference is a finding, never a tolerance to loosen.
* Identity: `pineforge_hpo_sobol_identity` (read back from the linked build) and
  `pineforge_hpo_sobol_identity_build` (synthetic databases, fixture projects on the Makefile and
  Ninja generators, copies of the real project, provider flag, source and relocation cases, the
  include-closure scan). The return-statistics identity tests run in the same build and must still
  pass unchanged.
* Licence and packaging: `pineforge_hpo_sobol_licence`, then
  `PFH_SOBOL_SDIST=... PFH_SOBOL_WHEEL=... python3 tests/test_sobol_licence.py .` on built packages.
* Command line, fake plugin: `pineforge_hpo_sobol_cli`. Pinned release bytes are mandatory in the
  actual proof: run it with `PFH_SOBOL_PROOF=1`, `PFH_SOBOL_REFERENCE` and
  `PFH_SOBOL_REFERENCE_SHA256` set (the same release for the return-statistics and candidate-list
  command-line tests). A proof run that skips the comparison is not a proof.
* Python: `python3 -m unittest discover -s tests/python -v`.
* Real engine, by hand: `tests/test_sobol_e2e.py` (a real compiled strategy, workers 1, 4 and 16,
  continuation, interop with the statistics block).
* Each build pair that is claimed equal needs its own measured-pair qualification; x86-64 and
  aarch64 are proven separately.

## Not claimed

The identity names a build and is not an attestation; the canary and the probe are sanity checks
of the running process. No equality across architectures, compilers or flag changes is claimed
without a listed pair. No quality, coverage or cost claim is made for the sampler. Selected-window
mode, any new public action and any change of the product licence are outside this wiring.
