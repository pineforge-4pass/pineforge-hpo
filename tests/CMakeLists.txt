add_executable(pineforge_hpo_test_core test_core.cpp)
target_link_libraries(pineforge_hpo_test_core PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_core PRIVATE -Wall -Wextra -Wpedantic)

add_test(NAME pineforge_hpo_core COMMAND pineforge_hpo_test_core)

add_executable(pineforge_hpo_test_portable_math_canary test_portable_math_canary.cpp)
target_link_libraries(pineforge_hpo_test_portable_math_canary PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_portable_math_canary PRIVATE
    -fno-fast-math -ffp-contract=off)
add_test(NAME pineforge_hpo_portable_math_contraction
    COMMAND pineforge_hpo_test_portable_math_canary)

add_executable(pineforge_hpo_test_portable_math test_portable_math.cpp)
target_link_libraries(pineforge_hpo_test_portable_math PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_portable_math PRIVATE -ffp-contract=off)
find_path(PFH_MPFR_INCLUDE mpfr.h)
find_library(PFH_MPFR_LIBRARY mpfr)
if(PFH_MPFR_INCLUDE AND PFH_MPFR_LIBRARY)
    target_include_directories(pineforge_hpo_test_portable_math PRIVATE "${PFH_MPFR_INCLUDE}")
    target_link_libraries(pineforge_hpo_test_portable_math PRIVATE "${PFH_MPFR_LIBRARY}")
    target_compile_definitions(pineforge_hpo_test_portable_math PRIVATE PFH_HAVE_MPFR=1)
elseif(PINEFORGE_HPO_REQUIRE_MPFR)
    message(FATAL_ERROR "MPFR is required for exact portable-math tests (mpfr.h and library)")
else()
    message(WARNING "SKIP: MPFR not found; exact portable-math tests will be skipped")
endif()
add_test(NAME pineforge_hpo_portable_math_accuracy COMMAND pineforge_hpo_test_portable_math)
set_tests_properties(pineforge_hpo_portable_math_accuracy PROPERTIES SKIP_RETURN_CODE 77)

add_executable(pineforge_hpo_portable_math_proof portable_math_proof.cpp)
target_link_libraries(pineforge_hpo_portable_math_proof PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_portable_math_proof PRIVATE -ffp-contract=off)
add_test(NAME pineforge_hpo_portable_math_serial COMMAND pineforge_hpo_portable_math_proof
    "${CMAKE_CURRENT_BINARY_DIR}/portable-proof/serial" 1)
add_test(NAME pineforge_hpo_portable_math_threaded COMMAND pineforge_hpo_portable_math_proof
    "${CMAKE_CURRENT_BINARY_DIR}/portable-proof/threaded" 8
    "${CMAKE_CURRENT_BINARY_DIR}/portable-proof/serial")
add_test(NAME pineforge_hpo_portable_math_intel_restore COMMAND pineforge_hpo_portable_math_proof
    "${CMAKE_CURRENT_BINARY_DIR}/portable-proof/intel-restored" 8
    "${CMAKE_CURRENT_SOURCE_DIR}/fixtures/portable-math-intel")
set_tests_properties(pineforge_hpo_portable_math_serial PROPERTIES
    FIXTURES_SETUP portable_checkpoint)
set_tests_properties(pineforge_hpo_portable_math_threaded PROPERTIES
    FIXTURES_REQUIRED portable_checkpoint)

add_executable(pineforge_hpo_test_numeric_unavailable test_numeric_unavailable.cpp
    "${PROJECT_SOURCE_DIR}/src/core/tpe_sampler.cpp")
target_link_libraries(pineforge_hpo_test_numeric_unavailable PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_numeric_unavailable PRIVATE
    -fno-fast-math -ffp-contract=off -frounding-math -fno-builtin -fno-lto)
add_test(NAME pineforge_hpo_numeric_unavailable COMMAND pineforge_hpo_test_numeric_unavailable)

add_executable(pineforge_hpo_test_warm_start test_warm_start.cpp)
target_link_libraries(pineforge_hpo_test_warm_start PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_warm_start PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_warm_start COMMAND pineforge_hpo_test_warm_start)
add_test(NAME pineforge_hpo_serial_golden COMMAND pineforge_hpo_test_warm_start --serial-golden)
if(PINEFORGE_HPO_REQUIRE_SERIAL_GOLDEN)
    set_tests_properties(pineforge_hpo_serial_golden PROPERTIES
        ENVIRONMENT "PFH_REQUIRE_SERIAL_GOLDEN=1")
endif()
target_compile_definitions(pineforge_hpo_test_warm_start PRIVATE
    PFH_TPE_SERIAL_GOLDENS="${CMAKE_CURRENT_SOURCE_DIR}/fixtures/tpe-serial-goldens.txt")

add_executable(pineforge_hpo_checkpoint_exchange checkpoint_exchange.cpp)
target_link_libraries(pineforge_hpo_checkpoint_exchange PRIVATE PineForgeHPO::core)

add_executable(pineforge_hpo_test_mt_state test_mt_state.cpp)
target_link_libraries(pineforge_hpo_test_mt_state PRIVATE PineForgeHPO::core)
target_compile_definitions(pineforge_hpo_test_mt_state PRIVATE
    PFH_MT_FIXTURE="${CMAKE_CURRENT_SOURCE_DIR}/fixtures/tpe-mt64-golden.txt"
    PFH_MT_GOLDEN_HASH="3132b2ec79fbb1b9741d1c771356ba94ffce7e41c5e172e442ddc5350fb6db70")
add_test(NAME pineforge_hpo_mt_state COMMAND pineforge_hpo_test_mt_state)

add_executable(pineforge_hpo_test_warm_binary test_warm_binary.cpp)
target_include_directories(pineforge_hpo_test_warm_binary PRIVATE "${PROJECT_SOURCE_DIR}/src/cli")
target_compile_definitions(pineforge_hpo_test_warm_binary PRIVATE
    PFH_WARM_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}/fixtures")
target_link_libraries(pineforge_hpo_test_warm_binary PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_warm_binary PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_warm_binary COMMAND pineforge_hpo_test_warm_binary)

add_executable(pineforge_hpo_test_return_stats test_return_stats.cpp)
target_link_libraries(pineforge_hpo_test_return_stats PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_return_stats PRIVATE
    -fno-fast-math -ffp-contract=off -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_return_stats COMMAND pineforge_hpo_test_return_stats)

# Both public headers in one translation unit, and the real build's generated identity read back
# against its own descriptor. The generation target is an unconditional build dependency.
add_executable(pineforge_hpo_test_return_stats_headers test_return_stats_headers.cpp)
add_dependencies(pineforge_hpo_test_return_stats_headers ${PFH_RETURN_STATS_IDENTITY_TARGET})
target_include_directories(pineforge_hpo_test_return_stats_headers PRIVATE
    "${PFH_RETURN_STATS_IDENTITY_DIRECTORY}")
target_link_libraries(pineforge_hpo_test_return_stats_headers PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_return_stats_headers PRIVATE
    -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_return_stats_headers
    COMMAND pineforge_hpo_test_return_stats_headers)
if(PINEFORGE_HPO_REQUIRE_RETURN_STATS_IDENTITY)
    set_tests_properties(pineforge_hpo_return_stats_headers PROPERTIES
        ENVIRONMENT "PFH_REQUIRE_RETURN_STATS_IDENTITY=1")
endif()

add_executable(pineforge_hpo_test_candidate_list test_candidate_list.cpp)
target_include_directories(pineforge_hpo_test_candidate_list PRIVATE
    "${PROJECT_SOURCE_DIR}/src/cli")
target_link_libraries(pineforge_hpo_test_candidate_list PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_candidate_list PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_candidate_list COMMAND pineforge_hpo_test_candidate_list)

# ---- Sobol sampler: engine, mapper and sampler, descriptor and parent admission -------------------
# Fixtures that must be generated are generated into THIS build tree (never into the source tree):
# the independent 64-bit oracle for the engine, and the mapper's float reference. The mapper test
# reads its fixtures from the build-tree directory below, into which the hand-written source
# fixture is copied at configure time and the generated reference is written by a setup test.
find_package(Python3 COMPONENTS Interpreter QUIET)
set(PFH_SOBOL_TABLE_DIR "${PROJECT_SOURCE_DIR}/third_party/sobol_joe_kuo")
set(PFH_SOBOL_ENGINE_BUILD_DIR "${CMAKE_CURRENT_BINARY_DIR}/sobol-engine")
set(PFH_SOBOL_MAPPER_BUILD_DIR "${CMAKE_CURRENT_BINARY_DIR}/sobol-mapper-fixtures")
file(MAKE_DIRECTORY "${PFH_SOBOL_ENGINE_BUILD_DIR}" "${PFH_SOBOL_MAPPER_BUILD_DIR}")
configure_file("${CMAKE_CURRENT_SOURCE_DIR}/fixtures/sobol/mapper/high64-boundaries.tsv"
    "${PFH_SOBOL_MAPPER_BUILD_DIR}/high64-boundaries.tsv" COPYONLY)

add_executable(pineforge_hpo_test_sobol_engine test_sobol_engine.cpp)
target_include_directories(pineforge_hpo_test_sobol_engine PRIVATE
    "${PROJECT_SOURCE_DIR}/src/core")
target_link_libraries(pineforge_hpo_test_sobol_engine PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_sobol_engine PRIVATE -Wall -Wextra -Wpedantic)
target_compile_definitions(pineforge_hpo_test_sobol_engine PRIVATE
    PFH_SOBOL_TABLE_FILE="${PFH_SOBOL_TABLE_DIR}/new-joe-kuo-6.21201.first1024"
    PFH_SOBOL_LICENCE_FILE="${PFH_SOBOL_TABLE_DIR}/LICENSE"
    PFH_SOBOL_INC_FILE="${PROJECT_SOURCE_DIR}/src/core/sobol_table_joe_kuo_d6_1024.inc"
    PFH_SOBOL_FIXTURE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/fixtures/sobol/engine")

add_executable(pineforge_hpo_test_sobol_mapper test_sobol_mapper.cpp)
target_include_directories(pineforge_hpo_test_sobol_mapper PRIVATE
    "${PROJECT_SOURCE_DIR}/src/core")
target_link_libraries(pineforge_hpo_test_sobol_mapper PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_sobol_mapper PRIVATE
    -Wall -Wextra -Wpedantic -fno-fast-math -ffp-contract=off)
target_compile_definitions(pineforge_hpo_test_sobol_mapper PRIVATE
    PFH_SOBOL_MAPPER_FIXTURES="${PFH_SOBOL_MAPPER_BUILD_DIR}")

add_executable(pineforge_hpo_test_sobol_continuation test_sobol_continuation.cpp)
target_include_directories(pineforge_hpo_test_sobol_continuation PRIVATE
    "${PROJECT_SOURCE_DIR}/src/cli" "${PROJECT_SOURCE_DIR}/src/core")
target_link_libraries(pineforge_hpo_test_sobol_continuation PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_sobol_continuation PRIVATE
    -Wall -Wextra -Wpedantic -fno-fast-math -ffp-contract=off)
target_compile_definitions(pineforge_hpo_test_sobol_continuation PRIVATE
    PFH_SOBOL_CONTINUATION_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}/fixtures/sobol/continuation")
add_test(NAME pineforge_hpo_sobol_continuation COMMAND pineforge_hpo_test_sobol_continuation)
set_tests_properties(pineforge_hpo_sobol_continuation PROPERTIES TIMEOUT 900)

# The runtime half of the Sobol numeric identity, read back from the build it is linked into. The
# generated header reaches only sobol_identity.cpp inside the core library; the core library
# depends on the generation target, so linking this test builds the header first. With
# PINEFORGE_HPO_REQUIRE_SOBOL_IDENTITY an unbound build is a failure, not a pass.
add_executable(pineforge_hpo_test_sobol_identity test_sobol_identity.cpp)
target_link_libraries(pineforge_hpo_test_sobol_identity PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_sobol_identity PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_sobol_identity COMMAND pineforge_hpo_test_sobol_identity)
set_tests_properties(pineforge_hpo_sobol_identity PROPERTIES TIMEOUT 600)
if(PINEFORGE_HPO_REQUIRE_SOBOL_IDENTITY)
    set_property(TEST pineforge_hpo_sobol_identity APPEND PROPERTY
        ENVIRONMENT "PFH_REQUIRE_SOBOL_IDENTITY=1")
endif()

if(Python3_Interpreter_FOUND AND UNIX)
    # Independent 64-bit oracle (big integers, imports nothing from src/), then the engine test.
    add_test(NAME pineforge_hpo_sobol_oracle64_generate
        COMMAND "${Python3_EXECUTABLE}" -I
            "${CMAKE_CURRENT_SOURCE_DIR}/fixtures/sobol/engine/oracle64.py"
            --table "${PFH_SOBOL_TABLE_DIR}/new-joe-kuo-6.21201.first1024"
            --fixtures "${CMAKE_CURRENT_SOURCE_DIR}/fixtures/sobol/engine"
            --out "${PFH_SOBOL_ENGINE_BUILD_DIR}/oracle64.tsv")
    set_tests_properties(pineforge_hpo_sobol_oracle64_generate PROPERTIES
        FIXTURES_SETUP sobol_oracle64 TIMEOUT 900)
    add_test(NAME pineforge_hpo_sobol_engine COMMAND pineforge_hpo_test_sobol_engine
        --oracle "${PFH_SOBOL_ENGINE_BUILD_DIR}/oracle64.tsv")
    set_tests_properties(pineforge_hpo_sobol_engine PROPERTIES
        FIXTURES_REQUIRED sobol_oracle64 TIMEOUT 900)
    add_test(NAME pineforge_hpo_sobol_table_generation
        COMMAND "${Python3_EXECUTABLE}" -I "${PROJECT_SOURCE_DIR}/scripts/gen_sobol_table.py"
            --check)
    # The mapper's float reference is produced on the test host by the independent generator
    # (stdout redirected into the build tree); the checked-in sources stay untouched.
    add_test(NAME pineforge_hpo_sobol_mapper_reference_generate
        COMMAND sh -c "\"$0\" -I \"$1\" > \"$2\""
            "${Python3_EXECUTABLE}"
            "${CMAKE_CURRENT_SOURCE_DIR}/fixtures/sobol/mapper/mapper_reference.py"
            "${PFH_SOBOL_MAPPER_BUILD_DIR}/float-reference.tsv")
    set_tests_properties(pineforge_hpo_sobol_mapper_reference_generate PROPERTIES
        FIXTURES_SETUP sobol_mapper_reference TIMEOUT 1800)
    add_test(NAME pineforge_hpo_sobol_mapper COMMAND pineforge_hpo_test_sobol_mapper)
    set_tests_properties(pineforge_hpo_sobol_mapper PROPERTIES
        FIXTURES_REQUIRED sobol_mapper_reference TIMEOUT 1800)
    # Licence artefacts: verbatim bytes and hashes, NOTICE text, wheel-metadata entry. With
    # PFH_SOBOL_SDIST and PFH_SOBOL_WHEEL set in the environment it also inspects built packages.
    add_test(NAME pineforge_hpo_sobol_licence
        COMMAND "${Python3_EXECUTABLE}" -I "${CMAKE_CURRENT_SOURCE_DIR}/test_sobol_licence.py"
            "${PROJECT_SOURCE_DIR}")
else()
    message(STATUS "Skipping the Sobol oracle, table-generation, mapper and licence tests: "
        "a UNIX host with Python 3 is required")
endif()

add_executable(pineforge_hpo_test_finite_search_space test_finite_search_space.cpp)
target_link_libraries(pineforge_hpo_test_finite_search_space PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_finite_search_space PRIVATE
    -Wall -Wextra -Wpedantic)

add_test(NAME pineforge_hpo_finite_search_space COMMAND pineforge_hpo_test_finite_search_space)

add_executable(pineforge_hpo_test_log_search_space test_log_search_space.cpp)
target_link_libraries(pineforge_hpo_test_log_search_space PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_log_search_space PRIVATE -Wall -Wextra -Wpedantic)

add_test(NAME pineforge_hpo_log_search_space COMMAND pineforge_hpo_test_log_search_space)

add_executable(pineforge_hpo_test_tpe_quality test_tpe_quality.cpp)
target_link_libraries(pineforge_hpo_test_tpe_quality PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_tpe_quality PRIVATE -Wall -Wextra -Wpedantic)

add_test(NAME pineforge_hpo_tpe_quality COMMAND pineforge_hpo_test_tpe_quality)
set_tests_properties(pineforge_hpo_tpe_quality PROPERTIES TIMEOUT 300)

add_executable(pineforge_hpo_test_tpe_stress test_tpe_stress.cpp)
target_link_libraries(pineforge_hpo_test_tpe_stress PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_tpe_stress PRIVATE -Wall -Wextra -Wpedantic)

add_test(NAME pineforge_hpo_tpe_stress COMMAND pineforge_hpo_test_tpe_stress)
# The hard suite intentionally runs 20 optimizer/seed combinations.  Hosted
# runners can take more than 60 seconds under load even though the suite has no
# wall-time quality contract, so leave enough headroom without reducing its
# statistical coverage.
set_tests_properties(pineforge_hpo_tpe_stress PROPERTIES TIMEOUT 180)

if(PINEFORGE_HPO_ENABLE_SANITIZERS OR PINEFORGE_HPO_ENABLE_THREAD_SANITIZER)
    set_tests_properties(pineforge_hpo_tpe_quality PROPERTIES TIMEOUT 300)
    set_tests_properties(pineforge_hpo_tpe_stress PROPERTIES TIMEOUT 900)
endif()

add_executable(pineforge_hpo_test_tpe_concurrency test_tpe_concurrency.cpp)
target_link_libraries(pineforge_hpo_test_tpe_concurrency PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_tpe_concurrency PRIVATE -Wall -Wextra -Wpedantic)

add_test(NAME pineforge_hpo_tpe_concurrency COMMAND pineforge_hpo_test_tpe_concurrency)

add_executable(pineforge_hpo_test_tpe_scale test_tpe_scale.cpp)
target_link_libraries(pineforge_hpo_test_tpe_scale PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_tpe_scale PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_tpe_scale COMMAND pineforge_hpo_test_tpe_scale)
set_tests_properties(pineforge_hpo_tpe_scale PROPERTIES TIMEOUT 240)
add_executable(pineforge_hpo_test_ordinal_set test_ordinal_set.cpp)
target_include_directories(pineforge_hpo_test_ordinal_set PRIVATE
    "${PROJECT_SOURCE_DIR}/src/core" "${PROJECT_SOURCE_DIR}/include")
target_compile_options(pineforge_hpo_test_ordinal_set PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_ordinal_set COMMAND pineforge_hpo_test_ordinal_set)
set_tests_properties(pineforge_hpo_tpe_concurrency PROPERTIES TIMEOUT 30)

if(UNIX AND TARGET PineForgeHPO::engine_adapter)
    # The engine-adapter fixture is compiled four ways from one source:
    #   1. a test executable;
    #   2. a complete strategy plugin;
    #   3. a legacy plugin without the optional chart-timezone export;
    #   4. a legacy plugin without the optional syminfo-metadata export.
    # $<TARGET_FILE:...> keeps the test independent of .dylib/.so naming.
    add_library(pineforge_hpo_fake_strategy SHARED test_engine_adapter.cpp)
    file(READ "${CMAKE_CURRENT_SOURCE_DIR}/fixtures/fake-input-manifest.json" fake_input_manifest)
    file(GENERATE OUTPUT "$<TARGET_FILE_DIR:pineforge_hpo_fake_strategy>/manifest.json"
        CONTENT "${fake_input_manifest}")
    target_compile_definitions(pineforge_hpo_fake_strategy PRIVATE
        PINEFORGE_HPO_FAKE_PLUGIN=1)
    target_include_directories(pineforge_hpo_fake_strategy PRIVATE
        "${PINEFORGE_ENGINE_SOURCE_INCLUDE}"
        "${PINEFORGE_ENGINE_BUILD_INCLUDE}")
    target_compile_options(pineforge_hpo_fake_strategy PRIVATE
        -Wall -Wextra -Wpedantic)
    set_target_properties(pineforge_hpo_fake_strategy PROPERTIES
        PREFIX ""
        OUTPUT_NAME "fake_strategy")

    add_library(pineforge_hpo_fake_strategy_no_timezone SHARED
        test_engine_adapter.cpp)
    target_compile_definitions(pineforge_hpo_fake_strategy_no_timezone PRIVATE
        PINEFORGE_HPO_FAKE_PLUGIN=1
        PINEFORGE_HPO_FAKE_PLUGIN_NO_TIMEZONE=1)
    target_include_directories(pineforge_hpo_fake_strategy_no_timezone PRIVATE
        "${PINEFORGE_ENGINE_SOURCE_INCLUDE}"
        "${PINEFORGE_ENGINE_BUILD_INCLUDE}")
    target_compile_options(pineforge_hpo_fake_strategy_no_timezone PRIVATE
        -Wall -Wextra -Wpedantic)
    set_target_properties(pineforge_hpo_fake_strategy_no_timezone PROPERTIES
        PREFIX ""
        OUTPUT_NAME "fake_strategy_no_timezone")

    add_library(pineforge_hpo_fake_strategy_no_metadata SHARED
        test_engine_adapter.cpp)
    target_compile_definitions(pineforge_hpo_fake_strategy_no_metadata PRIVATE
        PINEFORGE_HPO_FAKE_PLUGIN=1
        PINEFORGE_HPO_FAKE_PLUGIN_NO_METADATA=1)
    target_include_directories(pineforge_hpo_fake_strategy_no_metadata PRIVATE
        "${PINEFORGE_ENGINE_SOURCE_INCLUDE}"
        "${PINEFORGE_ENGINE_BUILD_INCLUDE}")
    target_compile_options(pineforge_hpo_fake_strategy_no_metadata PRIVATE
        -Wall -Wextra -Wpedantic)
    set_target_properties(pineforge_hpo_fake_strategy_no_metadata PROPERTIES
        PREFIX ""
        OUTPUT_NAME "fake_strategy_no_metadata")

    foreach(plugin pineforge_hpo_fake_strategy pineforge_hpo_fake_strategy_no_timezone
                   pineforge_hpo_fake_strategy_no_metadata)
        if(PINEFORGE_HPO_ENABLE_SANITIZERS)
            target_compile_options(${plugin} PRIVATE
                -fsanitize=address,undefined -fno-omit-frame-pointer)
            target_link_options(${plugin} PRIVATE -fsanitize=address,undefined)
        elseif(PINEFORGE_HPO_ENABLE_THREAD_SANITIZER)
            target_compile_options(${plugin} PRIVATE -fsanitize=thread -fno-omit-frame-pointer)
            target_link_options(${plugin} PRIVATE -fsanitize=thread)
        endif()
    endforeach()

    foreach(extension CODED STATUS CHECKED BAD_ABI)
        string(TOLOWER "${extension}" extension_name)
        add_library(pineforge_hpo_fake_strategy_${extension_name} SHARED test_engine_adapter.cpp)
        target_compile_definitions(pineforge_hpo_fake_strategy_${extension_name} PRIVATE
            PINEFORGE_HPO_FAKE_PLUGIN=1 PINEFORGE_HPO_FAKE_PLUGIN_${extension}=1)
        target_include_directories(pineforge_hpo_fake_strategy_${extension_name} PRIVATE
            "${PINEFORGE_ENGINE_SOURCE_INCLUDE}" "${PINEFORGE_ENGINE_BUILD_INCLUDE}")
        set_target_properties(pineforge_hpo_fake_strategy_${extension_name} PROPERTIES
            PREFIX "" OUTPUT_NAME "fake_strategy_${extension_name}")
        target_compile_options(pineforge_hpo_fake_strategy_${extension_name} PRIVATE
            -Wall -Wextra -Wpedantic)
        if(PINEFORGE_HPO_ENABLE_SANITIZERS)
            target_compile_options(pineforge_hpo_fake_strategy_${extension_name} PRIVATE
                -fsanitize=address,undefined -fno-omit-frame-pointer)
            target_link_options(pineforge_hpo_fake_strategy_${extension_name} PRIVATE
                -fsanitize=address,undefined)
        elseif(PINEFORGE_HPO_ENABLE_THREAD_SANITIZER)
            target_compile_options(pineforge_hpo_fake_strategy_${extension_name} PRIVATE
                -fsanitize=thread -fno-omit-frame-pointer)
            target_link_options(pineforge_hpo_fake_strategy_${extension_name} PRIVATE
                -fsanitize=thread)
        endif()
    endforeach()

    add_executable(pineforge_hpo_test_engine_adapter test_engine_adapter.cpp)
    target_link_libraries(pineforge_hpo_test_engine_adapter PRIVATE
        PineForgeHPO::engine_adapter
        ${CMAKE_DL_LIBS})
    target_compile_options(pineforge_hpo_test_engine_adapter PRIVATE
        -Wall -Wextra -Wpedantic)
    add_library(pineforge_hpo_test_signal_macro OBJECT EXCLUDE_FROM_ALL test_signal_macro.cpp)
    target_link_libraries(pineforge_hpo_test_signal_macro PRIVATE PineForgeHPO::engine_adapter)
    add_test(NAME pineforge_hpo_signal_macro
        COMMAND "${CMAKE_COMMAND}" --build "${CMAKE_BINARY_DIR}"
            --target pineforge_hpo_test_signal_macro)
    add_dependencies(pineforge_hpo_test_engine_adapter
        pineforge_hpo_fake_strategy
        pineforge_hpo_fake_strategy_no_timezone
        pineforge_hpo_fake_strategy_no_metadata
        pineforge_hpo_fake_strategy_coded
        pineforge_hpo_fake_strategy_status
        pineforge_hpo_fake_strategy_checked
        pineforge_hpo_fake_strategy_bad_abi)

    add_test(NAME pineforge_hpo_engine_adapter
        COMMAND pineforge_hpo_test_engine_adapter
            $<TARGET_FILE:pineforge_hpo_fake_strategy>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_no_timezone>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_no_metadata>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_coded>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_status>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_checked>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_bad_abi>)

    add_executable(pineforge_hpo_test_symbol_feeds test_symbol_feeds.cpp)
    target_include_directories(pineforge_hpo_test_symbol_feeds PRIVATE "${PROJECT_SOURCE_DIR}/src/cli")
    target_link_libraries(pineforge_hpo_test_symbol_feeds PRIVATE PineForgeHPO::engine_adapter)
    add_dependencies(pineforge_hpo_test_symbol_feeds pineforge_hpo_fake_strategy
        pineforge_hpo_fake_strategy_no_metadata)
    add_test(NAME pineforge_hpo_symbol_feeds COMMAND pineforge_hpo_test_symbol_feeds
        $<TARGET_FILE:pineforge_hpo_fake_strategy>
        $<TARGET_FILE:pineforge_hpo_fake_strategy_no_metadata>)

    add_executable(pineforge_hpo_symbol_feed_report symbol_feed_report.cpp)
    target_include_directories(pineforge_hpo_symbol_feed_report PRIVATE "${PROJECT_SOURCE_DIR}/src/cli")
    target_link_libraries(pineforge_hpo_symbol_feed_report PRIVATE PineForgeHPO::engine_adapter)

    find_package(Python3 COMPONENTS Interpreter)
    if(Python3_Interpreter_FOUND AND TARGET pineforge-hpo-native)
        add_test(NAME pineforge_hpo_failure_codes
            COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/test_failure_codes.py"
                $<TARGET_FILE:pineforge-hpo-native> $<TARGET_FILE:pineforge_hpo_fake_strategy>
                $<TARGET_FILE:pineforge_hpo_fake_strategy_coded>
                $<TARGET_FILE:pineforge_hpo_fake_strategy_status>
                $<TARGET_FILE:pineforge_hpo_fake_strategy_checked>
                $<TARGET_FILE:pineforge_hpo_fake_strategy_bad_abi>)
        if(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND NOT PINEFORGE_HPO_ENABLE_SANITIZERS
                AND NOT PINEFORGE_HPO_ENABLE_THREAD_SANITIZER)
            add_library(pineforge_hpo_libm_identity_shim SHARED libm_identity_shim.cpp)
            target_link_libraries(pineforge_hpo_libm_identity_shim PRIVATE ${CMAKE_DL_LIBS} m)
            target_compile_options(pineforge_hpo_libm_identity_shim PRIVATE
                -Wall -Wextra -Wpedantic -fno-builtin)
            add_test(NAME pineforge_hpo_libm_identity
                COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/test_libm_identity.py"
                    $<TARGET_FILE:pineforge-hpo-native> $<TARGET_FILE:pineforge_hpo_fake_strategy>
                    $<TARGET_FILE:pineforge_hpo_libm_identity_shim>)
            set_tests_properties(pineforge_hpo_libm_identity PROPERTIES TIMEOUT 600)
        endif()
        if(dlib_SOURCE_DIR)
            add_test(NAME pineforge_hpo_numeric_flags
                COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/test_numeric_flags.py"
                    "${PROJECT_SOURCE_DIR}" "${dlib_SOURCE_DIR}")
            add_test(NAME pineforge_hpo_return_stats_identity
                COMMAND "${Python3_EXECUTABLE}"
                    "${CMAKE_CURRENT_SOURCE_DIR}/test_return_stats_identity.py"
                    "${PROJECT_SOURCE_DIR}" "${dlib_SOURCE_DIR}")
            set_tests_properties(pineforge_hpo_return_stats_identity PROPERTIES TIMEOUT 1800)
            # The Sobol identity's build binding on synthetic databases, fixture projects and
            # copies of the real project (both generators): serial, long, many configures.
            add_test(NAME pineforge_hpo_sobol_identity_build
                COMMAND "${Python3_EXECUTABLE}"
                    "${CMAKE_CURRENT_SOURCE_DIR}/test_sobol_identity.py"
                    "${PROJECT_SOURCE_DIR}" "${dlib_SOURCE_DIR}")
            set_tests_properties(pineforge_hpo_sobol_identity_build PROPERTIES
                TIMEOUT 3600 RUN_SERIAL TRUE)
            # Fresh configurations of the whole project (explicit-OFF and multi-configuration
            # trees) plus a verbose rebuild of the real reducer object: serial, long.
            add_test(NAME pineforge_hpo_return_stats_integration
                COMMAND "${Python3_EXECUTABLE}"
                    "${CMAKE_CURRENT_SOURCE_DIR}/test_return_stats_integration.py"
                    $<TARGET_FILE:pineforge-hpo-native>
                    $<TARGET_FILE:pineforge_hpo_fake_strategy>
                    "${CMAKE_BINARY_DIR}" "${PROJECT_SOURCE_DIR}" "${PINEFORGE_ENGINE_ROOT}"
                    "${dlib_SOURCE_DIR}")
            set_tests_properties(pineforge_hpo_return_stats_integration PROPERTIES
                TIMEOUT 3600 RUN_SERIAL TRUE)
            if(PINEFORGE_HPO_REQUIRE_RETURN_STATS_IDENTITY)
                set_property(TEST pineforge_hpo_return_stats_integration APPEND PROPERTY
                    ENVIRONMENT "PFH_REQUIRE_RETURN_STATS_IDENTITY=1")
            endif()
        endif()
        add_test(NAME pineforge_hpo_symbol_feeds_contract
            COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/test_symbol_feeds_contract.py"
                $<TARGET_FILE:pineforge-hpo-native> $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        add_test(NAME pineforge_hpo_native_cli
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_native_cli.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        add_test(NAME pineforge_hpo_batching
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_batching.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        add_test(NAME pineforge_hpo_no_improvement
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_no_improvement.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        set_tests_properties(pineforge_hpo_no_improvement PROPERTIES TIMEOUT 600)
        add_test(NAME pineforge_hpo_sobol_cli
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_sobol_cli.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        set_tests_properties(pineforge_hpo_sobol_cli PROPERTIES TIMEOUT 3600)
        if(PINEFORGE_HPO_REQUIRE_SOBOL_IDENTITY)
            set_property(TEST pineforge_hpo_sobol_cli APPEND PROPERTY
                ENVIRONMENT "PFH_REQUIRE_SOBOL_IDENTITY=1")
        endif()
        add_test(NAME pineforge_hpo_candidate_list_cli
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_candidate_list_cli.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        set_tests_properties(pineforge_hpo_candidate_list_cli PROPERTIES TIMEOUT 900)
        add_test(NAME pineforge_hpo_return_stats_cli
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_return_stats_cli.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        set_tests_properties(pineforge_hpo_return_stats_cli PROPERTIES TIMEOUT 900)
        if(PINEFORGE_HPO_REQUIRE_RETURN_STATS_IDENTITY)
            set_property(TEST pineforge_hpo_return_stats_cli APPEND PROPERTY
                ENVIRONMENT "PFH_REQUIRE_RETURN_STATS_IDENTITY=1")
        endif()
        add_test(NAME pineforge_hpo_trial_outputs
            COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/test_trial_outputs.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        set_tests_properties(pineforge_hpo_trial_outputs PROPERTIES TIMEOUT 180)
        add_test(NAME pineforge_hpo_warm_start_contract
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_warm_start_contract.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        set_tests_properties(pineforge_hpo_warm_start_contract PROPERTIES TIMEOUT 600)
        add_test(NAME pineforge_hpo_warm_binary_contract
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_warm_binary_contract.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        set_tests_properties(pineforge_hpo_warm_binary_contract PROPERTIES TIMEOUT 600)
        foreach(contract syminfo progress final_write_signal progress_nonblocking progress_error
                         cancel deadline metrics magnifier timeout timeout_progress_error)
            add_test(NAME pineforge_hpo_runner_${contract}
                COMMAND "${Python3_EXECUTABLE}"
                    "${CMAKE_CURRENT_SOURCE_DIR}/test_runner_contract.py"
                    $<TARGET_FILE:pineforge-hpo-native>
                    $<TARGET_FILE:pineforge_hpo_fake_strategy> ${contract})
            set_tests_properties(pineforge_hpo_runner_${contract} PROPERTIES TIMEOUT 60)
        endforeach()
        if(PINEFORGE_HPO_TEST_ENGINE_BUILD)
            add_test(NAME pineforge_hpo_installed_prefix
                COMMAND "${CMAKE_COMMAND}"
                    -DENGINE_BUILD=${PINEFORGE_HPO_TEST_ENGINE_BUILD}
                    -DHPO_SOURCE=${PROJECT_SOURCE_DIR}
                    -DTEST_ROOT=${CMAKE_CURRENT_BINARY_DIR}/installed-prefix
                    -DDLIB_SOURCE=${dlib_SOURCE_DIR}
                    -P "${CMAKE_CURRENT_SOURCE_DIR}/test_installed_prefix.cmake")
        endif()
    else()
        message(STATUS "Skipping native CLI regression test: Python 3 not found")
    endif()
else()
    message(STATUS
        "Skipping engine-adapter dynamic-plugin tests: adapter/CLI disabled or dlopen unavailable")
endif()
add_executable(pineforge_hpo_test_pruner test_pruner.cpp)
target_link_libraries(pineforge_hpo_test_pruner PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_pruner PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_pruner COMMAND pineforge_hpo_test_pruner)

find_package(Threads REQUIRED)
add_executable(pineforge_hpo_test_batch_executor test_batch_executor.cpp)
target_include_directories(pineforge_hpo_test_batch_executor PRIVATE "${PROJECT_SOURCE_DIR}/src/cli")
target_link_libraries(pineforge_hpo_test_batch_executor PRIVATE PineForgeHPO::core Threads::Threads)
target_compile_options(pineforge_hpo_test_batch_executor PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_batch_executor COMMAND pineforge_hpo_test_batch_executor)
set_tests_properties(pineforge_hpo_batch_executor PROPERTIES TIMEOUT 15)
