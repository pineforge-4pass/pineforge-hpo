# Sobol numeric build identity

The floating-point Sobol mapper needs a numeric identity: a string that names the build whose
arithmetic produced the points, so that a continuation can be refused when the arithmetic may
differ. This note describes how the identity is produced, what it binds and what it does not,
when it is deliberately unavailable, and how to wire it into the root build. Prefix:
`portable-sobol-v1`.

Status: the helpers, the runtime unit and their tests have not been configured, built or run yet.
Every statement below describes intended behaviour and is confirmed only by the build-and-test
proof listed at the end of this note.

The identity is a distinct component identity. It does not reuse the TPE checkpoint identity,
`PFH_NUMERIC_BUILD_FLAGS_HASH` or the numeric flag recipe, and it does not change them. The
return-statistics identity is a third, separate identity: adding the Sobol sources and the Sobol
helper leaves the TPE flag files and the return-statistics descriptor and identity byte-identical
(the test builds both trees and compares the bytes).

## When it applies

A Sobol space has a numeric identity if and only if some varying column is a stepped real, a
linear real, a log real or a log integer (`SobolSampler::uses_floating_point()`). A discrete-only
space, constants and Boolean or categorical columns need none and carry null; they build and run
in every build, bound or not. The caller requests the identity only in the first case.

## What the identity string contains

`sobol_numeric_build_identity()` composes, in this order:

| Field | Source |
|---|---|
| `portable-sobol-v1` | the prefix |
| `mapper:` contract and `r` plus revision | `kSobolMapperContract`, `kSobolMapperRevision` (revision 2) |
| `table:` name, subset digest, `w` plus word size | `kSobolTableName`, `kSobolTableSubsetSha256`, `kSobolWordBits` |
| `build_sha256:` | SHA-256 of the build descriptor (below) |
| `binary64:53;eval:..;round:nearest;subnormals:gradual` | checked by `require_portable_environment()` first |
| `contract_canary:` two bit patterns | C++ unfused and fused value of one product-sum |
| `c_canary:` one bit pattern | the portable-math library's own C canary function |
| `portable_probe_sha256:` | the existing portable-math probe digest (`runtime_math_fingerprint()`) |

The environment check runs first and refuses with the TPE sampler's typed error
(`hpo_portable_math_unavailable`). An unbound build then refuses with `hpo_toolchain_unavailable`
(reason `native_runner`) whose text names the exact unbound reason, so a missing identity can
neither be published nor admit a floating-point continuation. The canary and probe fields are
sanity limits of the running process, not an attestation.

## What the build descriptor binds

The digest is taken over a descriptor text that the generator writes at build time and retains.
Nothing in it is a model of how CMake assembles flags: the unit of binding is the entry of the
JSON compilation database that the build system generated for each translation unit.

| Group | Content |
|---|---|
| C++ units | the generated compile command of `sobol_engine.cpp`, `sobol_mapper.cpp`, `sobol_sampler.cpp` and `sobol_identity.cpp` in the core target |
| C units | the generated compile command of every source of the portable-math target, whole target |
| C++ driver, C driver | each by the SHA-256 of its file after symlinks are resolved, plus CMake's id and version, the first `--version` line and `-dumpmachine`; the two drivers may differ |
| Sources | name and content digest of the eleven or more sources above |
| Headers | name and content digest of every project header the four C++ units include, the generated Joe-Kuo table `.inc`, `portable_grid.hpp`, `portable_math.hpp`, `portable.h` and the two `dint.h` |
| Forced includes | the content digest of every `-include` file in a command (the C units force `portable.h`) |
| Platform | system name and processor, the configuration name |

The stepped-grid `fma` and `nextafter` and the other inline portable operations live in
`portable.h` and `portable_math.hpp`, which every C++ unit compiles with its own flags; the
corresponding commands are in the descriptor. The portable-math target is bound whole, not as a
guessed subset: a change in any of its sources or commands moves the identity, including
`cos` and `erfc`, which Sobol does not call.

## Normalization

The same declared rule as the return-statistics identity (see
`docs/internal/return-stats-identity.md`), applied to each generated command in order, nothing
reordered: the compiler token must be the bound driver; `-c`, `-MD`, `-MMD`, `-MP`, `-MG` and
`-o`, `-MF`, `-MT`, `-MQ` with their arguments are dropped, and none of them can change
arithmetic; the source file and the path of `-I`, `-isystem`, `-iquote`, `-idirafter`,
`-iframework`, `-F`, `-isysroot`, `-B`, `--sysroot=`, `-include` and `-imacros` are rewritten by
location only (`<build>/...`, `<src>/...`, other paths verbatim), in the separate form (option,
then path) and in the joined form (path in the same token); a forced include also carries the
SHA-256 of its content; every other token is kept verbatim. The joined forms go beyond the rule of
the return-statistics identity, whose commands have none: the portable-math target is compiled
with `-include` followed directly by the absolute path of `portable.h`, which without them would
make the identity depend on the checkout path. There is no allowlist of tolerated flags and no
numeric flag remainder. A moved tree,
other object or dependency-file names, `-MMD` instead of `-MD` and a compiler reached through a
copy or a symlink with equal bytes all give the same digest; every added, removed or reordered
flag or definition gives a different one.

## When the identity is unbound

A build that cannot be bound exactly is not bound approximately. The build still succeeds, every
ordinary run and every discrete-only Sobol run is unaffected, the generated header reports
`sobol_numeric_identity_bound() == false` with a reason, and the digest is empty. Reasons:
`multi_config_generator` (Xcode, Visual Studio, Ninja Multi-Config),
`generator_without_compile_database`, `cmake_below_3_19`, `compile_database_disabled`,
`compile_database_missing`, `compile_database_unreadable`, `compile_command_missing`,
`compile_command_ambiguous`, `compile_command_unreadable`, `compiler_launcher` (a launcher or rule
launcher for the core or math target, in the directory or globally, or a banner naming a known
launcher), `compiler_arguments`, `compiler_is_script`, `compiler_mismatch` (including a C unit
compiled by the C++ driver), `compiler_unreadable`, `response_file`, `unsupported_character`,
`forced_include_unresolved`, `reducer_file_missing`, `contract_mismatch`, `ingredients_*` and
`descriptor_unrepresentable`. Like the return-statistics helper, the helper turns
`CMAKE_EXPORT_COMPILE_COMMANDS` on when the project left it undefined and respects an explicit
off.

## What the identity does not bind

- It names a build. It does not attest that two builds or two architectures compute equal
  values; equality is claimed only for build pairs that a measured-pair qualification proved.
  The canary and probe fields are checked once per call in the calling thread, not continuously.
- Operating-system and compiler-bundled headers (`stdint.h`, `errno.h`, `fenv.h`,
  `x86intrin.h`, the C++ standard library headers), the C and C++ runtime libraries and `libm`.
  The mapper's portable operations do not call `libm`, but `<cmath>` is included.
- The compiler proper behind the driver (only the driver bytes, the version and banner are
  bound), the linker, the link order, archive contents and the bytes of the final binary.
- Environment variables that the compiler reads, `-specs` files, the machine that `-march=native`
  resolves to, and the CPU that runs the process beyond the checks above.
- Other translation units that call the Sobol functions, and their flags.
- The vendored raw Joe-Kuo text file: the generated `.inc` it produced is bound by content and
  the subset digest constant is in the string; their agreement is the table-generation check.
- The correctness of the arithmetic, repeat and worker-count invariance (1, 4, 16 workers), and
  anything about the TPE or return-statistics builds.
- `numeric_build_flags.hpp`, the generated TPE header that `numeric_build.hpp` includes when it
  is on the include path: it is TPE-owned and is not an input here.

## Coupling decision to confirm

The runtime unit calls the existing `require_portable_environment()` and the existing probe, so it
includes `numeric_build.hpp` and, through it, `tpe_algorithm.hpp`; both are in the bound header
list. An edit of either moves the Sobol identity although no Sobol arithmetic changed. Removing
the coupling needs the environment check and the probe to move into a small header of their own,
which belongs to the author of those files; until then the identity errs on the conservative side.

## C++ interface

`include/pineforge/hpo/sobol_identity.hpp`, implemented in `src/core/sobol_identity.cpp`:

```cpp
namespace pineforge::hpo {
bool sobol_numeric_identity_bound();
std::string_view sobol_numeric_identity_unbound_reason();   // empty when bound
std::string sobol_numeric_build_identity();                 // throws when unbound
std::string_view sobol_numeric_identity_descriptor();       // retained for proof tooling
}
```

The generated header is included by `sobol_identity.cpp` only, through a per-source include
directory, so the descriptor literal exists in one translation unit and no other command changes.

## CMake interface

```cmake
include(cmake/SobolIdentity.cmake)
pfh_sobol_identity(
    TARGET core_target                      # compiles the four Sobol C++ sources
    PORTABLE_MATH_TARGET math_target        # C target, every source bound
    [CONSUMERS target...]                   # ordered after the identity files exist
    [SOURCE_OPTIONS option...]              # default: the strict arithmetic recipe
    [SOURCE_ROOT dir] [OUTPUT_DIR dir])
```

It creates the custom target `<core>_sobol_identity`, which runs
`cmake/GenerateSobolIdentity.cmake` before every build of the core target and rewrites its outputs
only when their bytes change. Outputs per configuration under `generated/sobol_identity/<CONFIG>/`:
`sobol_identity.descriptor.txt`, `sobol_identity.txt` (the digest, empty when unbound),
`sobol_identity_generated.hpp` and `sobol_identity.ingredients.txt`. The call fails at configure
time when one of the four C++ sources is not a source of the core target, an integration error
independent of every build setting.

The helper writes only per-source properties of the four C++ sources (`COMPILE_OPTIONS`,
`SKIP_UNITY_BUILD_INCLUSION`, `SKIP_PRECOMPILE_HEADERS`, and `INCLUDE_DIRECTORIES` of the identity
unit alone) and the `CMAKE_EXPORT_COMPILE_COMMANDS` cache default. It never writes a property of
the core or math target, `CMAKE_CXX_FLAGS` or `CMAKE_C_FLAGS`, and adds no include directory to
the core target, because that would appear in the command of every core unit and move the
return-statistics descriptor. The low-level binding functions are in the new
`cmake/CompileCommandBinding.cmake`.

## Descriptor format

```text
pineforge-hpo-sobol-identity/v1
contract=portable-sobol-v1
configuration=...
system.name=...
system.processor=...
compiler.cxx.id, .version, .banner, .target, .sha256     the C++ driver
compiler.c.id, .version, .banner, .target, .sha256       the C driver
command cxx <src>/src/core/sobol_engine.cpp              one block per unit, sorted
arg TOKEN                                                one line per normalized token
command c <src>/third_party/core_math/log/log.c
source <src>/relative/path sha256=HEX
header <src>/relative/path sha256=HEX
source.digest=HEX                                        SHA-256 of the source and header lines
```

An unbound build carries only the format line, `contract=`, `capability=unbound` and `reason=`.

## Integration calls (root and tests build files)

The sources exist now: `src/core/sobol_engine.cpp`, `sobol_mapper.cpp`, `sobol_sampler.cpp`, the
generated table `sobol_table_joe_kuo_d6_1024.inc` and the new `sobol_identity.cpp`. In the root
`CMakeLists.txt`, after the return-statistics block (so its consumers exist) and after the last
change to the compiler flags. The marker comments let the test cut the block out to build the
baseline of its invariance check:

```cmake
# PFH-SOBOL-IDENTITY-BEGIN
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/SobolIdentity.cmake")
target_sources(pineforge_hpo_core PRIVATE
    src/core/sobol_engine.cpp
    src/core/sobol_identity.cpp
    src/core/sobol_mapper.cpp
    src/core/sobol_sampler.cpp)
set(PFH_SOBOL_CONSUMER_ARGUMENTS "")
set(PFH_SOBOL_CONSUMER_TARGETS "")
foreach(PFH_SOBOL_CONSUMER IN ITEMS pineforge_hpo_engine_adapter pineforge-hpo-native)
    if(TARGET ${PFH_SOBOL_CONSUMER})
        list(APPEND PFH_SOBOL_CONSUMER_TARGETS ${PFH_SOBOL_CONSUMER})
    endif()
endforeach()
if(PFH_SOBOL_CONSUMER_TARGETS)
    list(APPEND PFH_SOBOL_CONSUMER_ARGUMENTS CONSUMERS ${PFH_SOBOL_CONSUMER_TARGETS})
endif()
pfh_sobol_identity(
    TARGET pineforge_hpo_core
    PORTABLE_MATH_TARGET pineforge_hpo_portable_math
    SOURCE_OPTIONS -fno-fast-math -ffp-contract=off -frounding-math -fno-builtin -fno-lto
    ${PFH_SOBOL_CONSUMER_ARGUMENTS})
# PFH-SOBOL-IDENTITY-END
```

If the Sobol sources are listed in the `add_library` call instead, drop the `target_sources`
lines and keep the markers around the rest. The shared flags that the identity records and does
not change: `pineforge_hpo_portable_math` is compiled with `-fno-fast-math -ffp-contract=off
-frounding-math -fno-builtin -fno-lto` and a forced include of `portable.h`, `C_STANDARD 11` and
no interprocedural optimization; `tpe_sampler.cpp` and `search_space.cpp` carry the same
recipe as per-source options; the core target has `-Wall -Wextra -Wpedantic`; the TPE flag text
is `CMAKE_CXX_FLAGS`, the per-configuration flags, the core target options and definitions and a
literal `source:-ffp-contract=off` line, and it never contained per-source options, any C flag,
a compiler identity or a Sobol unit. None of these was edited to match this identity.

In `tests/CMakeLists.txt`:

```cmake
add_executable(pineforge_hpo_test_sobol_identity test_sobol_identity.cpp)
target_link_libraries(pineforge_hpo_test_sobol_identity PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_sobol_identity PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_sobol_identity COMMAND pineforge_hpo_test_sobol_identity)
if(PINEFORGE_HPO_REQUIRE_SOBOL_IDENTITY)   # new option, default OFF, for proof builds
    set_tests_properties(pineforge_hpo_sobol_identity PROPERTIES
        ENVIRONMENT "PFH_REQUIRE_SOBOL_IDENTITY=1")
endif()
if(Python3_Interpreter_FOUND AND dlib_SOURCE_DIR)
    add_test(NAME pineforge_hpo_sobol_identity_build
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/test_sobol_identity.py"
            "${PROJECT_SOURCE_DIR}" "${dlib_SOURCE_DIR}")
    set_tests_properties(pineforge_hpo_sobol_identity_build PROPERTIES TIMEOUT 3600)
endif()
```

In the command line (`main.cpp`) and the continuation code: request the identity only when
`uses_floating_point()` is true, before any plugin, dataset, output file or parent is touched, and
pass it as the `numeric_build_identity` of `make_sobol_descriptor`. A thrown refusal is the
unbound or environment refusal; no code path may substitute an empty string or the
`flags_sha256:unavailable` marker.

## Factorization delta against the return-statistics helper

None of the five return-statistics files was edited. The low-level binding is a new file that only
the Sobol generator uses. The return-statistics generator keeps its inline copy of the rule so
that its descriptor bytes cannot move; `tests/test_sobol_identity.py` runs both generators on one
identical command and compares every normalized token, so a drift between the two copies fails the
test. Adopting the shared functions in the return-statistics generator is a separate change that
needs a golden-descriptor comparison first.

## Proof still owed

The tests check the build binding. These stay independent requirements:

- the first configure, build and run of every file in this change, on both generators;
- the descriptor against the commands that a verbose build executes, on the real core and math
  targets (the test covers one C++ and one C unit), and the include-closure check of the header
  list on the real target;
- the shipped command line refusing an unbound floating-point request before any trial, and a
  discrete-only run being byte-identical in a bound and an unbound build;
- the mapper's own proofs (reference values, branch witnesses), repeat runs and workers 1, 4 and
  16, and the measured-pair qualification of every build pair that is claimed equal.

## Remaining limits

- Path-valued options outside the lists above (for example `-iprefix`, `-iwithprefix`,
  `-fdebug-prefix-map=`) are kept verbatim: with an absolute path in them the identity depends on
  that location. None of them is in the current commands.
- The core target's usage requirements (dlib and the threads library) reach the Sobol units' commands and are
  bound as generated; a change of those requirements moves the Sobol identity although no Sobol
  source changed.
- The lookup of a unit's command uses its object path (`<target>.dir`); a CMake that names
  objects differently leaves the identity unbound, not wrong.
- The math target's sources are read at the end of the top-level directory; sources added later
  are not seen, and a source that is not C is refused through the compiler check.
- The digest cost grows with the compilation database: one pass over it per build, not measured.
- Flags that embed an absolute directory outside both trees make the identity location dependent.
