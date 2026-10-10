cmake_minimum_required(VERSION 3.17)

if(POLICY CMP0135)
    cmake_policy(SET CMP0135 NEW)
endif()

file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/VERSION"
    PINEFORGE_HPO_VERSION
    LIMIT_COUNT 1
)

project(PineForgeHPO
    VERSION ${PINEFORGE_HPO_VERSION}
    DESCRIPTION "Native hyperparameter optimization for PineForge strategies"
    LANGUAGES C CXX
)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release)
endif()

set(PINEFORGE_HPO_IS_TOP_LEVEL OFF)
if(CMAKE_SOURCE_DIR STREQUAL PROJECT_SOURCE_DIR)
    set(PINEFORGE_HPO_IS_TOP_LEVEL ON)
endif()

if(PINEFORGE_HPO_IS_TOP_LEVEL)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
    set(CMAKE_LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib")
    set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib")
endif()

option(PINEFORGE_HPO_BUILD_TESTS
    "Build the pineforge-hpo test suite" ${PINEFORGE_HPO_IS_TOP_LEVEL})
option(PINEFORGE_HPO_BUILD_EXAMPLES "Build the public C++ API examples" OFF)
option(PINEFORGE_HPO_BUILD_ENGINE_ADAPTER
    "Build the PineForge strategy-plugin execution adapter" ${PINEFORGE_HPO_IS_TOP_LEVEL})
option(PINEFORGE_HPO_BUILD_NATIVE_CLI
    "Build the native study runner" ${PINEFORGE_HPO_IS_TOP_LEVEL})
option(PINEFORGE_HPO_ENABLE_SANITIZERS "Build with AddressSanitizer and UBSan" OFF)
option(PINEFORGE_HPO_ENABLE_THREAD_SANITIZER "Build with ThreadSanitizer" OFF)
option(PINEFORGE_HPO_REQUIRE_SERIAL_GOLDEN "Fail instead of skipping unknown serial goldens" OFF)
option(PINEFORGE_HPO_REQUIRE_MPFR "Require MPFR for exact portable-math tests" OFF)
option(PINEFORGE_HPO_REQUIRE_RETURN_STATS_IDENTITY
    "Fail the identity tests when the return-statistics build identity is unbound" OFF)
option(PINEFORGE_HPO_REQUIRE_SOBOL_IDENTITY
    "Fail the Sobol tests when the Sobol numeric build identity is unbound" OFF)
if(PINEFORGE_HPO_ENABLE_SANITIZERS AND PINEFORGE_HPO_ENABLE_THREAD_SANITIZER)
    message(FATAL_ERROR "AddressSanitizer/UBSan and ThreadSanitizer need separate builds")
endif()
option(PINEFORGE_HPO_BUILD_BATCH_BENCHMARK "Build prefix/stream parity benchmark" OFF)
option(PINEFORGE_HPO_USE_SYSTEM_DLIB
    "Use an installed dlib 20.0.1 package instead of the pinned source archive" OFF)

if(PINEFORGE_HPO_BUILD_NATIVE_CLI AND NOT PINEFORGE_HPO_BUILD_ENGINE_ADAPTER)
    message(FATAL_ERROR
        "PINEFORGE_HPO_BUILD_NATIVE_CLI requires PINEFORGE_HPO_BUILD_ENGINE_ADAPTER=ON")
endif()

if(PINEFORGE_HPO_BUILD_ENGINE_ADAPTER)
    set(PINEFORGE_ENGINE_ROOT
        "${CMAKE_CURRENT_SOURCE_DIR}/external/pineforge-engine"
        CACHE PATH "Path to a pineforge-engine source/build tree")

    find_path(PINEFORGE_ENGINE_SOURCE_INCLUDE
        NAMES pineforge/pineforge.h
        HINTS "${PINEFORGE_ENGINE_ROOT}/include"
        NO_DEFAULT_PATH)
    find_path(PINEFORGE_ENGINE_BUILD_INCLUDE
        NAMES pineforge/version.h
        HINTS "${PINEFORGE_ENGINE_ROOT}/include" "${PINEFORGE_ENGINE_ROOT}/build/include"
        NO_DEFAULT_PATH)

    if(NOT PINEFORGE_ENGINE_SOURCE_INCLUDE OR NOT PINEFORGE_ENGINE_BUILD_INCLUDE)
        message(FATAL_ERROR
            "pineforge-engine public/generated headers not found. Configure with "
            "git submodule update --init external/pineforge-engine and configure the engine, "
            "or pass -DPINEFORGE_ENGINE_ROOT=/path/to/pineforge-engine, "
            "or set PINEFORGE_HPO_BUILD_ENGINE_ADAPTER=OFF and "
            "PINEFORGE_HPO_BUILD_NATIVE_CLI=OFF for a core-only build.")
    endif()
endif()

if(TARGET dlib::dlib)
    message(STATUS "Using the existing dlib::dlib target")
elseif(PINEFORGE_HPO_USE_SYSTEM_DLIB)
    find_package(dlib 20.0.1 EXACT CONFIG REQUIRED)
else()
    include(FetchContent)
    # pineforge-hpo uses only dlib's numerical/global-optimization facilities.
    # Disable unrelated optional integrations before the dependency is configured.
    set(DLIB_NO_GUI_SUPPORT ON CACHE BOOL "")
    set(DLIB_USE_CUDA OFF CACHE BOOL "")
    set(DLIB_USE_BLAS OFF CACHE BOOL "")
    set(DLIB_USE_LAPACK OFF CACHE BOOL "")
    set(DLIB_USE_FFMPEG OFF CACHE BOOL "")
    set(DLIB_JPEG_SUPPORT OFF CACHE BOOL "")
    set(DLIB_PNG_SUPPORT OFF CACHE BOOL "")
    set(DLIB_GIF_SUPPORT OFF CACHE BOOL "")
    set(DLIB_WEBP_SUPPORT OFF CACHE BOOL "")
    set(DLIB_JXL_SUPPORT OFF CACHE BOOL "")
    set(DLIB_LINK_WITH_SQLITE3 OFF CACHE BOOL "")
    set(DLIB_USE_MKL_FFT OFF CACHE BOOL "")
    FetchContent_Declare(dlib
        URL https://github.com/davisking/dlib/archive/refs/tags/v20.0.1.tar.gz
        URL_HASH SHA256=dab5b4ec4b68bd7dc128a1fb7900723f89d2da107e44cd5def7d38fc57252a9d)
    FetchContent_MakeAvailable(dlib)
endif()

add_library(pineforge_hpo_portable_math STATIC
    src/core/portable_math_canary.c
    third_party/core_math/log/log.c
    third_party/core_math/log1p/log1p.c
    third_party/core_math/exp/exp.c
    third_party/core_math/expm1/expm1.c
    third_party/core_math/cos/cos.c
    third_party/core_math/erfc/erfc.c)
target_compile_options(pineforge_hpo_portable_math PRIVATE
    -fno-fast-math -ffp-contract=off -frounding-math -fno-builtin -fno-lto
    "-include${CMAKE_CURRENT_SOURCE_DIR}/third_party/core_math/portable.h")
set_target_properties(pineforge_hpo_portable_math PROPERTIES
    C_STANDARD 11 C_STANDARD_REQUIRED ON INTERPROCEDURAL_OPTIMIZATION OFF)

add_library(pineforge_hpo_core STATIC
    src/core/dlib_sampler.cpp
    src/core/objective.cpp
    src/core/pruner.cpp
    src/core/return_stats.cpp
    src/core/sampler.cpp
    src/core/search_space.cpp
    src/core/sobol_engine.cpp
    src/core/sobol_identity.cpp
    src/core/sobol_mapper.cpp
    src/core/sobol_sampler.cpp
    src/core/tpe_sampler.cpp
    src/core/types.cpp
)
add_library(PineForgeHPO::core ALIAS pineforge_hpo_core)
target_include_directories(pineforge_hpo_core
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
        $<INSTALL_INTERFACE:include>)
target_compile_options(pineforge_hpo_core PRIVATE -Wall -Wextra -Wpedantic)
set_source_files_properties(src/core/tpe_sampler.cpp src/core/search_space.cpp PROPERTIES
    COMPILE_OPTIONS "-fno-fast-math;-ffp-contract=off;-frounding-math;-fno-builtin;-fno-lto")
set(PFH_NUMERIC_FLAGS "${CMAKE_CXX_FLAGS}\n")
set(PFH_CONFIGURATIONS ${CMAKE_BUILD_TYPE} ${CMAKE_CONFIGURATION_TYPES})
list(REMOVE_DUPLICATES PFH_CONFIGURATIONS)
foreach(PFH_CONFIG IN LISTS PFH_CONFIGURATIONS)
    string(TOUPPER "${PFH_CONFIG}" PFH_CONFIG_UPPER)
    string(APPEND PFH_NUMERIC_FLAGS
        "$<$<CONFIG:${PFH_CONFIG}>:${CMAKE_CXX_FLAGS_${PFH_CONFIG_UPPER}}>\n")
endforeach()
string(APPEND PFH_NUMERIC_FLAGS
    "$<JOIN:$<TARGET_PROPERTY:pineforge_hpo_core,COMPILE_OPTIONS>,\n>\n"
    "$<JOIN:$<TARGET_PROPERTY:pineforge_hpo_core,COMPILE_DEFINITIONS>,\n>\n"
    "source:-ffp-contract=off\n")
file(GENERATE OUTPUT
    "${CMAKE_CURRENT_BINARY_DIR}/generated/$<CONFIG>/$<COMPILE_LANGUAGE>/numeric_build_flags.txt"
    CONTENT "${PFH_NUMERIC_FLAGS}")
add_custom_target(pineforge_hpo_numeric_flags
    COMMAND "${CMAKE_COMMAND}"
        "-DPFH_FLAGS_FILE=${CMAKE_CURRENT_BINARY_DIR}/generated/$<CONFIG>/CXX/numeric_build_flags.txt"
        "-DPFH_FLAGS_HEADER=${CMAKE_CURRENT_BINARY_DIR}/generated/$<CONFIG>/CXX/numeric_build_flags.hpp"
        -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/GenerateNumericBuildFlags.cmake")
add_dependencies(pineforge_hpo_core pineforge_hpo_numeric_flags)
target_include_directories(pineforge_hpo_core PRIVATE
    "${CMAKE_CURRENT_BINARY_DIR}/generated/$<CONFIG>/CXX")
find_package(Threads REQUIRED)
target_link_libraries(pineforge_hpo_core PUBLIC
    dlib::dlib Threads::Threads pineforge_hpo_portable_math)

if(PINEFORGE_HPO_BUILD_ENGINE_ADAPTER)
    add_library(pineforge_hpo_engine_adapter STATIC
        src/engine_adapter/dataset.cpp
        src/engine_adapter/strategy_plugin.cpp
        src/engine_adapter/trial_executor.cpp
    )
    add_library(PineForgeHPO::engine_adapter ALIAS pineforge_hpo_engine_adapter)
    target_include_directories(pineforge_hpo_engine_adapter
        PUBLIC
            $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
            $<INSTALL_INTERFACE:include>
            $<BUILD_INTERFACE:${PINEFORGE_ENGINE_SOURCE_INCLUDE}>
            $<BUILD_INTERFACE:${PINEFORGE_ENGINE_BUILD_INCLUDE}>)
    target_link_libraries(pineforge_hpo_engine_adapter
        PUBLIC PineForgeHPO::core
        PRIVATE ${CMAKE_DL_LIBS})
    target_compile_options(pineforge_hpo_engine_adapter PRIVATE
        -Wall -Wextra -Wpedantic)
endif()

if(PINEFORGE_HPO_BUILD_NATIVE_CLI)
    find_package(Threads REQUIRED)
    add_executable(pineforge-hpo-native src/cli/main.cpp)
    target_compile_definitions(pineforge-hpo-native PRIVATE
        PINEFORGE_HPO_VERSION="${PROJECT_VERSION}")
    target_include_directories(pineforge-hpo-native PRIVATE
        "${PINEFORGE_ENGINE_SOURCE_INCLUDE}"
        "${PINEFORGE_ENGINE_BUILD_INCLUDE}")
    target_link_libraries(pineforge-hpo-native PRIVATE
        PineForgeHPO::engine_adapter
        Threads::Threads)
    target_compile_options(pineforge-hpo-native PRIVATE
        -Wall -Wextra -Wpedantic)
endif()

# The marker lines around this block let tests/test_return_stats_identity.py cut it out for its
# X-free baseline; they carry no other meaning.
# PFH-RETURN-STATS-IDENTITY-BEGIN
# Return-statistics reducer: its own build identity (independent of the TPE checkpoint identity
# above), and the per-source flags that keep its arithmetic uncontracted. Only per-source
# properties of the reducer, the generated directory as a private include directory of each
# consumer, and the per-target EXPORT_COMPILE_COMMANDS property of the core are written, so
# numeric_build_flags.txt above is unaffected. Keep this after the last CMAKE_CXX_FLAGS
# change and do not set properties of the reducer source later.
#
# The identity reads the compilation database, so the default build now writes
# compile_commands.json into the build directory (one extra file, accepted; the helper switches it
# on only when the project left CMAKE_EXPORT_COMPILE_COMMANDS undefined). An explicit OFF, a
# multi-configuration generator, a compiler launcher and the other situations listed in
# docs/internal/return-stats-identity.md leave the identity unbound: the build and every ordinary
# run are unaffected, and only a run that requests return statistics is refused.
#
# The consumers are the targets that include return_stats_identity.hpp: the engine adapter (the
# executor refuses statistics without a bound identity) and the command line (admission and the
# emitted identity). Their dependency on the generation target is unconditional, so every
# supported build constructs its own generated header before compiling them.
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/ReturnStatsIdentity.cmake")
# CONSUMERS is passed only with a value: the helper rejects a valueless keyword (a core-only
# build has no consumer).
set(PFH_RSI_CONSUMER_ARGUMENTS "")
set(PFH_RSI_CONSUMER_TARGETS "")
foreach(PFH_RSI_CONSUMER IN ITEMS pineforge_hpo_engine_adapter pineforge-hpo-native)
    if(TARGET ${PFH_RSI_CONSUMER})
        list(APPEND PFH_RSI_CONSUMER_TARGETS ${PFH_RSI_CONSUMER})
    endif()
endforeach()
if(PFH_RSI_CONSUMER_TARGETS)
    list(APPEND PFH_RSI_CONSUMER_ARGUMENTS CONSUMERS ${PFH_RSI_CONSUMER_TARGETS})
endif()
pfh_return_stats_identity(
    TARGET pineforge_hpo_core
    SOURCES src/core/return_stats.cpp
    HEADERS include/pineforge/hpo/return_stats.hpp
    CONTRACT "pineforge-hpo-return-stats/v1"
    SOURCE_OPTIONS -fno-fast-math -ffp-contract=off -frounding-math -fno-builtin -fno-lto
    ${PFH_RSI_CONSUMER_ARGUMENTS})
# PFH-RETURN-STATS-IDENTITY-END

# Sobol sampler numeric identity (prefix portable-sobol-v1): its own component, distinct from the
# TPE checkpoint identity and from the return-statistics identity above, and it changes neither.
# The four Sobol sources are core sources (listed above); the helper writes their per-source
# strict arithmetic recipe and binds, from the compilation database, their generated commands,
# the commands of every portable-math C source, the commands and source digests of the shipped
# core units that can supply the linker's copy of a shared inline helper (tpe_sampler.cpp and
# search_space.cpp: a deliberate, conservative coupling to those two units, whose own options and
# the TPE checkpoint identity stay as they are), the bytes of both compiler drivers and the
# project headers. See docs/internal/sobol-identity.md and docs/internal/methods-g1-wire.md.
# Keep this block after every change to the compiler flags and after the targets it names exist;
# the marker lines let tests/test_sobol_identity.py cut it out for a Sobol-free baseline.
#
# The generation target is an unconditional build dependency of the core library (which compiles
# sobol_identity.cpp, the only includer of the generated header) and of the engine adapter and the
# command line, so every supported build constructs its own header. An unbound capability (see the
# note) never stops a build or an ordinary run; it refuses only a floating-point Sobol request.
# PFH-SOBOL-IDENTITY-BEGIN
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/SobolIdentity.cmake")
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

if(PINEFORGE_HPO_ENABLE_SANITIZERS)
    foreach(target pineforge_hpo_core pineforge_hpo_engine_adapter pineforge-hpo-native
                   pineforge_hpo_portable_math)
        if(TARGET ${target})
            target_compile_options(${target} PUBLIC
                -fsanitize=address,undefined -fno-omit-frame-pointer)
            target_link_options(${target} PUBLIC -fsanitize=address,undefined)
        endif()
    endforeach()
endif()

if(PINEFORGE_HPO_ENABLE_THREAD_SANITIZER)
    foreach(target pineforge_hpo_core pineforge_hpo_engine_adapter pineforge-hpo-native
                   pineforge_hpo_portable_math)
        if(TARGET ${target})
            target_compile_options(${target} PUBLIC -fsanitize=thread -fno-omit-frame-pointer)
            target_link_options(${target} PUBLIC -fsanitize=thread)
        endif()
    endforeach()
endif()

if(PINEFORGE_HPO_BUILD_TESTS)
    include(CTest)
    enable_testing()
    add_subdirectory(tests)
endif()

if(PINEFORGE_HPO_BUILD_BATCH_BENCHMARK)
    add_executable(pineforge_hpo_compare_rungs benchmarks/batching/compare_rungs.cpp)
    target_link_libraries(pineforge_hpo_compare_rungs PRIVATE PineForgeHPO::engine_adapter)
    target_compile_options(pineforge_hpo_compare_rungs PRIVATE -Wall -Wextra -Wpedantic)
endif()

if(PINEFORGE_HPO_BUILD_EXAMPLES)
    add_subdirectory(examples/cpp)
endif()
