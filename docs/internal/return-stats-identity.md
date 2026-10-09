# Return-statistics build identity

The return-statistics reducer reports one result-level object when return statistics are
requested. That object carries `numeric_build_identity`, an opaque identifier of the build that
computed the statistics. This note describes how the identifier is produced, what it binds, and
how to wire it into the root build. Contract string: `pineforge-hpo-return-stats/v1`.

The identity is independent of every sampler. Grid, random, TPE and candidate-list runs all
report the same value for the same build. It is never derived from, combined with, or compared
to a TPE checkpoint identity (`numeric_build_identity()` in the sampler core), and it shares no
file, macro or generated flag hash with it.

## What the identity binds

| Field | Where it comes from |
|---|---|
| Contract string | the `CONTRACT` argument of the CMake helper |
| Reducer source digest | SHA-256 of every listed source and header, hashed at build time |
| Compiler identity and version | `CMAKE_CXX_COMPILER_ID` and `CMAKE_CXX_COMPILER_VERSION`, or explicit arguments |
| Compiler banner and target triple | first line of the compiler's version output and its machine triple |
| Translation-unit flags | `CMAKE_CXX_FLAGS`, the active configuration's flags, target-level options, per-source options |
| Definitions | target-level and per-source definitions |
| Language settings | `CXX_STANDARD`, `CXX_EXTENSIONS`, position-independent code, interprocedural optimization |
| Platform | system name, processor, macOS architectures and deployment target |
| Build configuration | the active configuration name |

The identity is the SHA-256 digest of the descriptor text, prefixed
`pineforge-hpo-return-stats-build/v1:sha256:`. The descriptor is retained next to it so a proof
can compare it with the real compile commands.

## What the identity does not claim

- It names a build. It does not attest that two builds produce equal arithmetic, and it says
  nothing about other architectures. Cross-architecture equality is claimed only for the pairs a
  proof actually tested.
- It contains no canary result. A compiled arithmetic canary, where one exists, is a
  fail-closed sanity check of its own; one canary does not attest all arithmetic.
- It does not replace the repeat proofs, the worker-count invariance proofs (1, 2, 4 and 8
  workers), the measured-pair qualification, or the independent analytic and high-precision
  reference cases.
- It binds compile-time inputs of the reducer translation units only. Link-time libraries, the
  runtime floating-point environment and the operating system's math library are not bound.

## C++ interface

`include/pineforge/hpo/return_stats_identity.hpp` is header-only and includes the generated
header `return_stats_identity_generated.hpp`. Building a translation unit that includes it
without the helper's include directory fails with a missing-file error, so the identity can
never silently read as unavailable.

```cpp
#include <pineforge/hpo/return_stats_identity.hpp>

namespace pineforge::hpo {
inline constexpr std::string_view kReturnStatsContract = "pineforge-hpo-return-stats/v1";
constexpr std::string_view return_stats_numeric_build_identity() noexcept;  // opaque identity
constexpr std::string_view return_stats_identity_descriptor() noexcept;     // exact descriptor
constexpr std::string_view return_stats_source_digest() noexcept;           // source digest
constexpr std::string_view return_stats_contract() noexcept;                // bound contract
}
```

A `static_assert` ties the contract bound at configure time to `kReturnStatsContract`, so a
build configured for another contract does not compile. The header includes no sampler,
checkpoint or TPE header.

## CMake interface

```cmake
include(cmake/ReturnStatsIdentity.cmake)
pfh_return_stats_identity(
    TARGET <target>                   # target that compiles the reducer sources
    SOURCES <file>...                 # reducer translation units
    [HEADERS <file>...]               # headers whose bytes change the reducer
    CONTRACT <contract>               # statistics contract string
    [SOURCE_OPTIONS <option>...]      # per-source options
    [SOURCE_DEFINITIONS <define>...]  # per-source definitions
    [CONSUMERS <target>...]           # targets that include return_stats_identity.hpp
    [COMPILER_ID <id>] [COMPILER_VERSION <version>] [COMPILER_BANNER <text>]
    [SOURCE_ROOT <dir>] [OUTPUT_DIR <dir>])
```

The helper creates the custom target `<target>_return_stats_identity`. Each consumer gets a
dependency on it and the per-configuration output directory as a private include directory.
That target runs `cmake/GenerateReturnStatsIdentity.cmake` on every build. The script hashes the
listed files and rewrites its three outputs only when their bytes change, so an unchanged build
does not recompile its consumers. A source edit changes the identity without a reconfigure.

Outputs in the per-configuration directory (`PFH_RETURN_STATS_IDENTITY_DIRECTORY`):

| File | Content |
|---|---|
| `return_stats_identity.descriptor.txt` | the exact descriptor; its SHA-256 is the identity |
| `return_stats_identity.txt` | the identity, one line |
| `return_stats_identity_generated.hpp` | constants read by the public header |

The path of the generated header is not an input of the digest, and the helper refuses any
listed source or header inside the output directory or named like the generated header, so the
identity cannot hash itself.

### Per-source flag handling

The helper writes only per-source properties of `SOURCES`: `COMPILE_OPTIONS`,
`COMPILE_DEFINITIONS`, `SKIP_UNITY_BUILD_INCLUSION` and `SKIP_PRECOMPILE_HEADERS`. It never
calls `target_compile_options`, `target_compile_definitions` or `add_compile_options`, and it
never assigns `CMAKE_CXX_FLAGS`, so the target-wide flags that the checked-in numeric flag
recipe hashes for the TPE checkpoint identity stay byte-identical. It refuses a source that
already carries `COMPILE_FLAGS`, `COMPILE_OPTIONS` or `COMPILE_DEFINITIONS`, because such flags
could not be bound; pass them through `SOURCE_OPTIONS` and `SOURCE_DEFINITIONS`.

Call the helper after the last assignment to `CMAKE_CXX_FLAGS` and its per-configuration
variants, and do not touch the properties of `SOURCES` afterwards. Target-level options and
definitions are read when the build system is generated, so they may still change after the
call, and options of linked targets are included.

## Descriptor format

UTF-8, LF line endings, no absolute path. Lines appear in this order:

```text
pineforge-hpo-return-stats-identity/v1
contract=...                         scalar lines: contract, configuration, compiler.id,
configuration=...                    compiler.version, compiler.banner, compiler.target,
...                                  system.name, system.processor, osx.architectures,
                                     osx.deployment_target, language.standard,
                                     language.extensions, position.independent, interprocedural
flag cxx TOKEN                       tokens of CMAKE_CXX_FLAGS
flag config TOKEN                    tokens of the active configuration's flags
flag target TOKEN                    target-level options, first occurrence kept
flag source TOKEN                    SOURCE_OPTIONS
define target NAME[=VALUE]           target-level definitions, first occurrence kept
define source NAME[=VALUE]           SOURCE_DEFINITIONS
source RELATIVE_PATH sha256=HEX      reducer sources, sorted by name
header RELATIVE_PATH sha256=HEX      reducer headers, sorted by name
source.digest=HEX                    SHA-256 of the source and header lines above
```

File names are relative to `SOURCE_ROOT` (default `PROJECT_SOURCE_DIR`), so the identity does
not depend on where the tree or the build directory lives. Flag order is part of the descriptor:
for conflicting options the later one wins on the real command line.

## Flag handling and the compile-command check

`tests/test_return_stats_identity.py` compares the descriptor with the compile command database
of a configured build (`CMAKE_EXPORT_COMPILE_COMMANDS=ON`, Makefile or Ninja generators). The
actual command is normalized first:

- the compiler, `-o` and its argument, `-c` and the source file are dropped;
- dependency-file options (`-MD`, `-MMD`, `-MP`, `-MG`, `-MF`, `-MT`, `-MQ`) are dropped;
- path-valued options are dropped with their argument: `-I`, `-isystem`, `-iquote`,
  `-idirafter`, `-iframework`, `-F`, `-include`, `-imacros`, `-isysroot` and `--sysroot`.

Header content reached through those paths is bound only by the explicit `HEADERS` list, so a
force-included file must be listed there.

Definitions (`-D...`) are compared as multisets. The remaining tokens must contain the
descriptor's flag lines as an ordered subsequence. Every other token must be one that CMake
injects from a setting the descriptor already binds: `-std=c++NN`, `-std=gnu++NN`, `-fPIC`,
`-fPIE`, `-fpic`, `-fpie`, `-pthread`, `-arch` with its architecture, `-mmacosx-version-min=`,
`--target=`, `-m32`, `-m64`, `-flto`, `-fvisibility=` and `-fvisibility-inlines-hidden`. Any
other extra token fails the check. The test also compares the compiler identity with CMake's
compiler detection and the banner with the compiler's own version output.

## Integration calls for the root build

The reducer file names are not fixed by this change; replace the placeholders. In the root
`CMakeLists.txt`, after the reducer sources joined `pineforge_hpo_core` and after the
`pineforge-hpo-native` block (a consumer must exist when the helper runs):

```cmake
include(cmake/ReturnStatsIdentity.cmake)
set(PFH_RSI_CONSUMERS "")
if(TARGET pineforge-hpo-native)
    list(APPEND PFH_RSI_CONSUMERS pineforge-hpo-native)
endif()
pfh_return_stats_identity(
    TARGET pineforge_hpo_core
    SOURCES <reducer translation units>
    HEADERS <reducer headers>
    CONTRACT "pineforge-hpo-return-stats/v1"
    SOURCE_OPTIONS -fno-fast-math -ffp-contract=off -fno-lto
    CONSUMERS ${PFH_RSI_CONSUMERS})
```

If the reducer is header-only and included by a larger translation unit, pass that translation
unit in `SOURCES`, the reducer header in `HEADERS`, and note that `SOURCE_OPTIONS` then apply to
the whole translation unit. A dedicated reducer translation unit is preferable.

Where the result-level object is written, on every sampler path:

```cpp
#include <pineforge/hpo/return_stats_identity.hpp>
// ...
const std::string identity{pineforge::hpo::return_stats_numeric_build_identity()};
// emit as "numeric_build_identity" inside the return_stats object, escaped like other strings
```

Register the test next to `pineforge_hpo_numeric_flags` in `tests/CMakeLists.txt`:

```cmake
if(dlib_SOURCE_DIR)
    add_test(NAME pineforge_hpo_return_stats_identity
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/test_return_stats_identity.py"
            "${PROJECT_SOURCE_DIR}" "${dlib_SOURCE_DIR}")
    set_tests_properties(pineforge_hpo_return_stats_identity PROPERTIES TIMEOUT 900)
endif()
```

## Proof still owed

The identity test only checks the build-system binding. These stay independent requirements and
are not replaced by it:

- the full command-line comparison with statistics off, byte for byte;
- the analytic and high-precision reference cases, degenerate and short series, unavailable
  periods, month boundaries, extreme equity values, derived overflow and the status invariant;
- reconciliation against the canonical equity curve;
- repeat runs and worker-count invariance at 1, 2, 4 and 8 workers;
- the added time per trial against identical runs without statistics, at the realized bar
  counts, with the measurement scope stated;
- after integration, a TPE run whose `numeric_build_identity` is compared before and after the
  reducer is added, and a run on every sampler path showing the same statistics identity.

## Unresolved generator cases

- Multi-configuration generators (Xcode, Visual Studio) write no compile command database, so
  the descriptor check cannot run there. The identity itself is still generated per
  configuration.
- A change to the properties of `SOURCES` or to `CMAKE_CXX_FLAGS` after the helper call is not
  seen at configure time; only the compile-command check reveals it.
- A compiler launcher, a wrapper script, response files and `CMAKE_CXX_COMPILER_ARG1` change the
  first tokens of the command; the check assumes the compiler is the first token.
- Which injected tokens CMake adds, and where, is not modelled exactly; the check accepts the
  listed tokens anywhere. Per-configuration interprocedural settings are recorded as the
  generic target property only, so keep `-fno-lto` in `SOURCE_OPTIONS`.
- Options of linked targets enter through the target's usage requirements as evaluated by
  CMake; the compile-command check is the only confirmation for targets that link libraries.
- Flags that embed the source or build directory (for example `-ffile-prefix-map=` with an
  absolute path) make the identity depend on that location. They are not rejected.
- The first banner line depends on the command used to invoke the compiler, so one compiler
  reached under two names binds two identities.
- Semicolons and square brackets in flags, definitions or paths are refused.
- The SDK or system headers behind `-isysroot` and `--sysroot` are not bound.
