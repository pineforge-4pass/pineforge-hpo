# Return-statistics build identity

The return-statistics reducer reports one result-level object when return statistics are
requested. That object carries `numeric_build_identity`, an opaque identifier of the build that
computed the statistics. This note describes how the identifier is produced, what it binds, the
normalization rule that makes it location independent, when it is deliberately unavailable, and
how to wire it into the root build. Contract string: `pineforge-hpo-return-stats/v1`.

Status: the helper, the generator and their tests have not been configured, built or run yet.
Every statement below describes intended behaviour; it is confirmed only by the build-and-test
proof listed at the end of this note.

The identity is independent of every sampler. Grid, random, TPE and candidate-list runs all
report the same value for the same build. It is never derived from, combined with, or compared
to a TPE checkpoint identity (`numeric_build_identity()` in the sampler core), and it shares no
file, macro or generated flag hash with it.

## How the identity is derived

There is no model of how CMake assembles compiler flags. At build time a script reads the entry
of the JSON compilation database (`compile_commands.json`) that the build system generated for
each reducer translation unit, normalizes it by the rule below, and hashes the result together
with the other fields. Whatever CMake puts on the real command line is therefore bound as
generated: `CMAKE_CXX_FLAGS`, the active configuration's flags, options and definitions of the
target, usage requirements inherited from linked targets, per-source options, and flags CMake
injects itself (standard, position-independent code, interprocedural optimization, platform
flags). Changes made after the helper was called, such as a later per-source property, a later
target option or a later flag assignment, are bound for the same reason.

| Field | Where it comes from |
|---|---|
| Contract string | the `CONTRACT` argument of the CMake helper |
| Reducer source digest | SHA-256 of every listed source and header, hashed at build time |
| Compile command | the compilation database entry of each reducer source, normalized |
| Compiler driver | SHA-256 of the driver file after symlinks are resolved |
| Compiler identity | `CMAKE_CXX_COMPILER_ID` and version, first `--version` line, `-dumpmachine` |
| Platform | `CMAKE_SYSTEM_NAME` and `CMAKE_SYSTEM_PROCESSOR` |
| Build configuration | the active configuration name |

The identity is the SHA-256 digest of the descriptor text, prefixed
`pineforge-hpo-return-stats-build/v2:sha256:`. The descriptor is retained next to it.

## Normalization rule

Applied to the generated command, in order of appearance, nothing reordered:

1. The first token must be the compiler whose driver bytes are bound (same file after symlinks
   are resolved). Its path is replaced by that custody, so a compiler reached through another
   path or alias binds the same when the bytes are equal.
2. Dropped: `-c`, `-MD`, `-MMD`, `-MP`, `-MG`, and `-o`, `-MF`, `-MT`, `-MQ` together with their
   argument. None of these can change arithmetic.
3. Rewritten, location only: the source file, and the path of `-I`, `-isystem`, `-iquote`,
   `-idirafter`, `-iframework`, `-F`, `-isysroot`, `-B`, `--sysroot=` and `-include` / `-imacros`.
   A path inside the build tree becomes `<build>/...`, one inside the source tree `<src>/...`,
   any other path stays verbatim. A forced include also carries the SHA-256 of its content.
4. Every other token, including every `-D`, `-U`, `-O`, `-f`, `-m`, `-std` and warning option,
   is kept verbatim in command-line order. There is no list of options that are ignored.

A command that holds a response file token (`@file`), a character that would split a CMake list
(semicolon, square bracket), a forced include that cannot be read, or a launcher in front of the
compiler is not normalized: the identity is unbound instead.

## When the identity is unavailable

The identity is bound only when the generated command can be read exactly. Otherwise the build
still succeeds, ordinary runs without return statistics are unchanged, and the generated header
reports `return_stats_identity_bound() == false` with a reason, an empty identity and an empty
source digest. A caller must refuse to publish return statistics for such a build. Reasons:

| Reason | Meaning |
|---|---|
| `multi_config_generator` | Xcode, Visual Studio or Ninja Multi-Config: no single generated command per source |
| `generator_without_compile_database` | any generator other than Unix, MSYS or MinGW Makefiles, or Ninja |
| `cmake_below_3_19` | the build-time reader needs JSON support |
| `compile_database_disabled` | the project explicitly set `CMAKE_EXPORT_COMPILE_COMMANDS` off |
| `compile_database_missing`, `compile_database_unreadable` | no usable `compile_commands.json` |
| `compile_command_missing`, `compile_command_ambiguous`, `compile_command_unreadable` | not exactly one command for a reducer source in the target |
| `compiler_launcher` | a compiler launcher or rule launcher is set for the target, the directory or globally, or the compiler's version banner names a known launcher |
| `compiler_arguments`, `compiler_is_script`, `compiler_mismatch`, `compiler_unreadable` | the compiler is a wrapper, a script, differs from the one in the command, or cannot be read |
| `response_file` | the command uses a response file |
| `unsupported_character`, `forced_include_unresolved`, `reducer_file_missing` | text or files the identity cannot bind |
| `contract_mismatch`, `ingredients_*`, `descriptor_unrepresentable` | inconsistent inputs |

When the project leaves `CMAKE_EXPORT_COMPILE_COMMANDS` undefined, the helper turns it on, so the
default build of the project binds. This writes one extra file into the build tree and changes
nothing else.

## What the identity does not claim

- It names a build. It does not attest that two builds produce equal arithmetic, and it says
  nothing about other architectures. Cross-architecture equality is claimed only for the pairs a
  proof actually tested.
- It contains no canary result. A compiled arithmetic canary, where one exists, is a
  fail-closed sanity check of its own; one canary does not attest all arithmetic.
- It does not replace the repeat proofs, the worker-count invariance proofs (1, 2, 4 and 8
  workers), the measured-pair qualification, or the independent analytic and high-precision
  reference cases.
- It binds the generated compile command, the listed reducer files, forced includes and the
  compiler driver. Operating-system and compiler-bundled headers, libraries behind include
  paths, the compiler proper behind the driver, link-time inputs, the runtime floating-point
  environment, the operating system's math library and environment variables read by the
  compiler are not bound. A header the reducer includes from the repository must be listed in
  `HEADERS`.

## C++ interface

`include/pineforge/hpo/return_stats_identity.hpp` is header-only and includes the generated
header `return_stats_identity_generated.hpp` with angle brackets. A translation unit built
without the helper's include directory fails with a missing-file error.

```cpp
#include <pineforge/hpo/return_stats_identity.hpp>

namespace pineforge::hpo {
constexpr bool return_stats_identity_bound() noexcept;
constexpr std::string_view return_stats_identity_unbound_reason() noexcept;  // empty if bound
constexpr std::string_view return_stats_numeric_build_identity() noexcept;   // empty if unbound
constexpr std::string_view return_stats_identity_descriptor() noexcept;
constexpr std::string_view return_stats_source_digest() noexcept;            // empty if unbound
constexpr std::string_view return_stats_contract() noexcept;
}
```

The header defines no `kReturnStatsContract`; that name belongs to the reducer header. The header
includes no sampler, checkpoint or TPE header.

## CMake interface

```cmake
include(cmake/ReturnStatsIdentity.cmake)
pfh_return_stats_identity(
    TARGET target                   # target that compiles the reducer sources
    SOURCES file...                 # reducer translation units
    [HEADERS file...]               # headers whose bytes change the reducer
    CONTRACT contract               # statistics contract string
    [SOURCE_OPTIONS option...]      # options added to the SOURCES only
    [SOURCE_DEFINITIONS define...]  # definitions added to the SOURCES only
    [CONSUMERS target...]           # targets that include return_stats_identity.hpp
    [SOURCE_ROOT dir] [OUTPUT_DIR dir])
```

The helper creates the custom target `<target>_return_stats_identity`. Each consumer gets a
dependency on it and the per-configuration output directory as a private include directory. The
target runs `cmake/GenerateReturnStatsIdentity.cmake` on every build. It rewrites its three
outputs only when their bytes change, so an unchanged build does not recompile its consumers.
Outputs in the per-configuration directory (`PFH_RETURN_STATS_IDENTITY_DIRECTORY`):

| File | Content |
|---|---|
| `return_stats_identity.descriptor.txt` | the exact descriptor; its SHA-256 is the identity |
| `return_stats_identity.txt` | the identity, one line, empty when unbound |
| `return_stats_identity_generated.hpp` | constants read by the public header |
| `return_stats_identity.ingredients.txt` | the generator's inputs (retained for proof) |

The listed files are the only hashed inputs. The helper refuses any listed file inside the output
directory or named like the generated header, so the identity cannot hash itself.

The helper writes only per-source properties of `SOURCES` (`COMPILE_OPTIONS`,
`COMPILE_DEFINITIONS`, `SKIP_UNITY_BUILD_INCLUSION`, `SKIP_PRECOMPILE_HEADERS`), the
`CMAKE_EXPORT_COMPILE_COMMANDS` cache entry when it is undefined and, when that switch is on, the
per-target `EXPORT_COMPILE_COMMANDS` property of `TARGET`. It never calls
`target_compile_options`, `target_compile_definitions` or `add_compile_options`, and it never
assigns `CMAKE_CXX_FLAGS`, so the target-wide flags that the numeric flag recipe hashes for the
TPE checkpoint identity stay byte-identical. With CMake 3.19 or newer the ingredients are written
at the end of the top-level directory, after any later change to launchers; their generator
expressions are evaluated at generate time in any case.

## Descriptor format

UTF-8, LF line endings, no absolute path for files inside the source or build tree. Order:

```text
pineforge-hpo-return-stats-identity/v2
contract=...
configuration=...
compiler.id=...
compiler.version=...
compiler.banner=...
compiler.target=...
compiler.sha256=...                  SHA-256 of the compiler driver
system.name=...
system.processor=...
command <src>/relative/source.cpp    one block per reducer source, sorted
arg TOKEN                            one line per normalized token, in command-line order
source <src>/relative/path sha256=HEX
header <src>/relative/path sha256=HEX
source.digest=HEX                    SHA-256 of the source and header lines above
```

An unbound build carries only the header line, `contract=`, `capability=unbound` and `reason=`.

## Integration calls for the root build

The reducer files exist in the repository: `src/core/return_stats.cpp` and
`include/pineforge/hpo/return_stats.hpp`; the source includes nothing but that header and
standard headers. In the root `CMakeLists.txt`, after `pineforge_hpo_core` is defined and after
the last assignment to `CMAKE_CXX_FLAGS` and its per-configuration variants:

```cmake
include(cmake/ReturnStatsIdentity.cmake)
target_sources(pineforge_hpo_core PRIVATE src/core/return_stats.cpp)
set(PFH_RSI_CONSUMERS "")
foreach(consumer IN ITEMS pineforge_hpo_engine_adapter pineforge-hpo-native)
    if(TARGET ${consumer})
        list(APPEND PFH_RSI_CONSUMERS ${consumer})
    endif()
endforeach()
pfh_return_stats_identity(
    TARGET pineforge_hpo_core
    SOURCES src/core/return_stats.cpp
    HEADERS include/pineforge/hpo/return_stats.hpp
    CONTRACT "pineforge-hpo-return-stats/v1"
    SOURCE_OPTIONS -fno-fast-math -ffp-contract=off -frounding-math -fno-builtin -fno-lto
    CONSUMERS ${PFH_RSI_CONSUMERS})
```

The call is placed after the consumer targets exist. Where the result-level `return_stats`
object is written, on every sampler path:

```cpp
#include <pineforge/hpo/return_stats.hpp>
#include <pineforge/hpo/return_stats_identity.hpp>

static_assert(pineforge::hpo::return_stats_contract() == pineforge::hpo::kReturnStatsContract);

// when return statistics are requested:
if (!pineforge::hpo::return_stats_identity_bound()) {
    // refuse with a typed error naming return_stats_identity_unbound_reason()
}
const std::string identity{pineforge::hpo::return_stats_numeric_build_identity()};
// emit as "numeric_build_identity" inside the return_stats object, escaped like other strings
```

Runs that request no return statistics never reach this code. Register the test next to
`pineforge_hpo_numeric_flags` in `tests/CMakeLists.txt`:

```cmake
if(dlib_SOURCE_DIR)
    add_test(NAME pineforge_hpo_return_stats_identity
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/test_return_stats_identity.py"
            "${PROJECT_SOURCE_DIR}" "${dlib_SOURCE_DIR}")
    set_tests_properties(pineforge_hpo_return_stats_identity PROPERTIES TIMEOUT 1800)
endif()
```

## Proof still owed

The identity test checks the build binding and the normalization rule. These stay independent
requirements and are not replaced by it:

- the full command-line comparison with statistics off, byte for byte;
- the analytic and high-precision reference cases, degenerate and short series, unavailable
  periods, month boundaries, extreme equity values, derived overflow and the status invariant;
- reconciliation against the canonical equity curve;
- repeat runs and worker-count invariance at 1, 2, 4 and 8 workers;
- the added time per trial against identical runs without statistics, at the realized bar
  counts, with the measurement scope stated;
- after integration, a TPE run whose `numeric_build_identity` is compared before and after the
  reducer is added, a run on every sampler path showing the same statistics identity, and a
  check on the real core target that the descriptor equals the compile command that the build
  executes;
- the build time added by the identity target on a project-sized compilation database.

## Remaining limits

- Multi-configuration generators and the cases in the table above are unbound by design, not
  approximated. Making one of them bind needs a generator-specific reader of its own.
- The compilation database does not show a compiler launcher; the helper checks every place
  CMake reads one (target property, rule launchers at target, directory and global scope) and
  refuses a version banner that names `ccache`, `sccache`, `distcc` or `icecc`, but a launcher
  injected outside CMake that announces nothing is covered only by the driver bytes.
- Command-line variable overrides of the build tool and environment variables read by the
  compiler are not visible in the compilation database.
- The object path names the owning target (`<target>.dir`); a future CMake that names objects
  differently makes the lookup fail, which leaves the identity unbound.
- Flags that embed an absolute directory outside both trees (for example
  `-ffile-prefix-map=` with such a path) make the identity depend on that location.
- Options that only change diagnostics, such as colored output, are kept in the command and
  therefore bound: two builds that differ only in them have different identities.
- The build-time script parses the whole compilation database once per source and per build, so
  its cost grows with the database; it has not been measured.
- CMake older than 3.19 leaves the identity unbound. This and the forced
  `CMAKE_EXPORT_COMPILE_COMMANDS` default are the two restrictions beyond the situations listed
  above, and both are open for review.
