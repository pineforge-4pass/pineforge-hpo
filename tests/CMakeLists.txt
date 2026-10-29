add_executable(pineforge_hpo_test_core test_core.cpp)
target_link_libraries(pineforge_hpo_test_core PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_core PRIVATE -Wall -Wextra -Wpedantic)

add_test(NAME pineforge_hpo_core COMMAND pineforge_hpo_test_core)

add_executable(pineforge_hpo_test_trial_spool test_trial_spool.cpp)
target_include_directories(pineforge_hpo_test_trial_spool PRIVATE
    "${PROJECT_SOURCE_DIR}/src/cli")
target_link_libraries(pineforge_hpo_test_trial_spool PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_trial_spool PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_trial_spool COMMAND pineforge_hpo_test_trial_spool
    "${CMAKE_CURRENT_BINARY_DIR}/trial-spool")

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

# The block reducer lives in the same strict translation unit as the statistics reducer, and this
# test freezes exact dyadic literals, so it needs the same two flags.
add_executable(pineforge_hpo_test_return_blocks test_return_blocks.cpp)
target_link_libraries(pineforge_hpo_test_return_blocks PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_return_blocks PRIVATE
    -fno-fast-math -ffp-contract=off -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_return_blocks COMMAND pineforge_hpo_test_return_blocks)

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
if(Python3_Interpreter_FOUND)
    add_test(NAME pineforge_hpo_release_version
        COMMAND "${Python3_EXECUTABLE}" -I
            "${CMAKE_CURRENT_SOURCE_DIR}/test_release_version.py" -v)
    add_test(NAME pineforge_hpo_no_time_limits
        COMMAND "${Python3_EXECUTABLE}" -I
            "${CMAKE_CURRENT_SOURCE_DIR}/test_no_time_limits.py" -v)
endif()
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

# The runtime half of the Sobol numeric identity, read back from the build it is linked into. The
# generated header reaches only sobol_identity.cpp inside the core library; the core library
# depends on the generation target, so linking this test builds the header first. With
# PINEFORGE_HPO_REQUIRE_SOBOL_IDENTITY an unbound build is a failure, not a pass.
add_executable(pineforge_hpo_test_sobol_identity test_sobol_identity.cpp)
target_link_libraries(pineforge_hpo_test_sobol_identity PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_sobol_identity PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_sobol_identity COMMAND pineforge_hpo_test_sobol_identity)
if(PINEFORGE_HPO_REQUIRE_SOBOL_IDENTITY)
    set_property(TEST pineforge_hpo_sobol_identity APPEND PROPERTY
        ENVIRONMENT "PFH_REQUIRE_SOBOL_IDENTITY=1")
endif()

if(Python3_Interpreter_FOUND AND UNIX)
    add_test(NAME pineforge_hpo_pbo_curve_checker
        COMMAND "${Python3_EXECUTABLE}" -I
            "${CMAKE_CURRENT_SOURCE_DIR}/test_pbo_curve_checker.py" -v)
    # Independent 64-bit oracle (big integers, imports nothing from src/), then the engine test.
    add_test(NAME pineforge_hpo_sobol_oracle64_generate
        COMMAND "${Python3_EXECUTABLE}" -I
            "${CMAKE_CURRENT_SOURCE_DIR}/fixtures/sobol/engine/oracle64.py"
            --table "${PFH_SOBOL_TABLE_DIR}/new-joe-kuo-6.21201.first1024"
            --fixtures "${CMAKE_CURRENT_SOURCE_DIR}/fixtures/sobol/engine"
            --out "${PFH_SOBOL_ENGINE_BUILD_DIR}/oracle64.tsv")
    set_tests_properties(pineforge_hpo_sobol_oracle64_generate PROPERTIES
        FIXTURES_SETUP sobol_oracle64)
    add_test(NAME pineforge_hpo_sobol_engine COMMAND pineforge_hpo_test_sobol_engine
        --oracle "${PFH_SOBOL_ENGINE_BUILD_DIR}/oracle64.tsv")
    set_tests_properties(pineforge_hpo_sobol_engine PROPERTIES
        FIXTURES_REQUIRED sobol_oracle64)
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
        FIXTURES_SETUP sobol_mapper_reference)
    add_test(NAME pineforge_hpo_sobol_mapper COMMAND pineforge_hpo_test_sobol_mapper)
    set_tests_properties(pineforge_hpo_sobol_mapper PROPERTIES
        FIXTURES_REQUIRED sobol_mapper_reference)
    # Licence artefacts: verbatim bytes and hashes, NOTICE text, wheel-metadata entry. With
    # PFH_SOBOL_SDIST and PFH_SOBOL_WHEEL set in the environment it also inspects built packages.
    # The trailing -v goes to unittest (the script takes the repository root first): every skipped
    # test then prints its own "... skipped '<reason>'" line beside the "(skipped=N)" summary.
    add_test(NAME pineforge_hpo_sobol_licence
        COMMAND "${Python3_EXECUTABLE}" -I "${CMAKE_CURRENT_SOURCE_DIR}/test_sobol_licence.py"
            "${PROJECT_SOURCE_DIR}" -v)
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

add_executable(pineforge_hpo_test_tpe_stress test_tpe_stress.cpp)
target_link_libraries(pineforge_hpo_test_tpe_stress PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_tpe_stress PRIVATE -Wall -Wextra -Wpedantic)

add_test(NAME pineforge_hpo_tpe_stress COMMAND pineforge_hpo_test_tpe_stress)
# The hard suite intentionally runs 20 optimizer/seed combinations with no
# wall-time quality contract.

add_executable(pineforge_hpo_test_tpe_concurrency test_tpe_concurrency.cpp)
target_link_libraries(pineforge_hpo_test_tpe_concurrency PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_tpe_concurrency PRIVATE -Wall -Wextra -Wpedantic)

add_test(NAME pineforge_hpo_tpe_concurrency COMMAND pineforge_hpo_test_tpe_concurrency)

add_executable(pineforge_hpo_test_tpe_scale test_tpe_scale.cpp)
target_link_libraries(pineforge_hpo_test_tpe_scale PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_tpe_scale PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_tpe_scale COMMAND pineforge_hpo_test_tpe_scale)
add_executable(pineforge_hpo_test_ordinal_set test_ordinal_set.cpp)
target_include_directories(pineforge_hpo_test_ordinal_set PRIVATE
    "${PROJECT_SOURCE_DIR}/src/core" "${PROJECT_SOURCE_DIR}/include")
target_compile_options(pineforge_hpo_test_ordinal_set PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_ordinal_set COMMAND pineforge_hpo_test_ordinal_set)

if(UNIX AND TARGET PineForgeHPO::engine_adapter)
    # Selected support is opt-in on separately named fixtures. The default fake must never
    # acquire the six selected/observer exports, even through accidental engine linkage.
    find_package(Python3 COMPONENTS Interpreter REQUIRED)
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
    add_custom_command(TARGET pineforge_hpo_fake_strategy POST_BUILD
        COMMAND "${Python3_EXECUTABLE}" -I
            "${CMAKE_CURRENT_SOURCE_DIR}/test_fake_strategy_exports.py"
            --source "${CMAKE_CURRENT_SOURCE_DIR}/test_engine_adapter.cpp"
            --source "${CMAKE_CURRENT_SOURCE_DIR}/test_selected_evaluation.cpp"
            --plugin "$<TARGET_FILE:pineforge_hpo_fake_strategy>"
        VERBATIM)
    add_test(NAME pineforge_hpo_fake_strategy_exports
        COMMAND "${Python3_EXECUTABLE}" -I
            "${CMAKE_CURRENT_SOURCE_DIR}/test_fake_strategy_exports.py"
            --source "${CMAKE_CURRENT_SOURCE_DIR}/test_engine_adapter.cpp"
            --source "${CMAKE_CURRENT_SOURCE_DIR}/test_selected_evaluation.cpp"
            --plugin "$<TARGET_FILE:pineforge_hpo_fake_strategy>")

    set(PFH_SELECTED_FAKE_TARGETS pineforge_hpo_fake_strategy_selected)
    foreach(omission RANGE 1 6)
        list(APPEND PFH_SELECTED_FAKE_TARGETS
            pineforge_hpo_fake_strategy_selected_omit_${omission})
    endforeach()
    list(APPEND PFH_SELECTED_FAKE_TARGETS
        pineforge_hpo_fake_strategy_selected_no_version
        pineforge_hpo_fake_strategy_selected_no_settings)
    foreach(plugin IN LISTS PFH_SELECTED_FAKE_TARGETS)
        add_library(${plugin} SHARED test_engine_adapter.cpp)
        target_compile_definitions(${plugin} PRIVATE PINEFORGE_HPO_FAKE_SELECTED=1)
        target_include_directories(${plugin} PRIVATE
            "${PINEFORGE_ENGINE_SOURCE_INCLUDE}" "${PINEFORGE_ENGINE_BUILD_INCLUDE}")
        target_compile_options(${plugin} PRIVATE -Wall -Wextra -Wpedantic)
        set_target_properties(${plugin} PROPERTIES PREFIX "" OUTPUT_NAME "${plugin}")
        if(PINEFORGE_HPO_ENABLE_SANITIZERS)
            target_compile_options(${plugin} PRIVATE
                -fsanitize=address,undefined -fno-omit-frame-pointer)
            target_link_options(${plugin} PRIVATE -fsanitize=address,undefined)
        elseif(PINEFORGE_HPO_ENABLE_THREAD_SANITIZER)
            target_compile_options(${plugin} PRIVATE -fsanitize=thread -fno-omit-frame-pointer)
            target_link_options(${plugin} PRIVATE -fsanitize=thread)
        endif()
    endforeach()
    foreach(omission RANGE 1 6)
        target_compile_definitions(pineforge_hpo_fake_strategy_selected_omit_${omission} PRIVATE
            PINEFORGE_HPO_FAKE_SELECTED_OMIT=${omission})
    endforeach()
    target_compile_definitions(pineforge_hpo_fake_strategy_selected_no_version PRIVATE
        PINEFORGE_HPO_FAKE_SELECTED_NO_VERSION=1)
    target_compile_definitions(pineforge_hpo_fake_strategy_selected_no_settings PRIVATE
        PINEFORGE_HPO_FAKE_SELECTED_NO_SETTINGS=1)
    add_executable(pineforge_hpo_test_selected_evaluation test_selected_evaluation.cpp)
    target_link_libraries(pineforge_hpo_test_selected_evaluation PRIVATE
        PineForgeHPO::engine_adapter ${CMAKE_DL_LIBS})
    target_compile_options(pineforge_hpo_test_selected_evaluation PRIVATE -Wall -Wextra -Wpedantic)
    add_dependencies(pineforge_hpo_test_selected_evaluation
        pineforge_hpo_fake_strategy ${PFH_SELECTED_FAKE_TARGETS})
    add_test(NAME pineforge_hpo_selected_evaluation
        COMMAND pineforge_hpo_test_selected_evaluation
            $<TARGET_FILE:pineforge_hpo_fake_strategy>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_selected>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_selected_omit_1>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_selected_omit_2>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_selected_omit_3>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_selected_omit_4>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_selected_omit_5>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_selected_omit_6>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_selected_no_version>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_selected_no_settings>)
    if(PINEFORGE_HPO_REQUIRE_RETURN_STATS_IDENTITY)
        set_tests_properties(pineforge_hpo_selected_evaluation PROPERTIES
            ENVIRONMENT "PFH_REQUIRE_RETURN_STATS_IDENTITY=1")
    endif()

    # The planner loader refuses a run directory with a group- or world-writable
    # ancestor that is not sticky. A host whose work directory is group-writable
    # (a setgid CI work directory, mode 0770) sets PFH_TEST_RUN_ROOT, at configure
    # time, to a fresh private directory whose ancestors pass; the check itself
    # stays as it is. Without it the run roots stay under the build directory.
    if(DEFINED ENV{PFH_TEST_RUN_ROOT} AND NOT "$ENV{PFH_TEST_RUN_ROOT}" STREQUAL "")
        set(PFH_TEST_RUN_ROOT "$ENV{PFH_TEST_RUN_ROOT}")
    else()
        set(PFH_TEST_RUN_ROOT "${CMAKE_CURRENT_BINARY_DIR}")
    endif()

    add_executable(pineforge_hpo_test_window_plan test_window_plan.cpp)
    target_link_libraries(pineforge_hpo_test_window_plan PRIVATE
        PineForgeHPO::engine_adapter ${CMAKE_DL_LIBS})
    target_compile_options(pineforge_hpo_test_window_plan PRIVATE -Wall -Wextra -Wpedantic)
    add_dependencies(pineforge_hpo_test_window_plan pineforge_hpo_fake_strategy)
    add_test(NAME pineforge_hpo_window_plan
        COMMAND pineforge_hpo_test_window_plan
            "$<TARGET_FILE:pineforge_hpo_fake_strategy>"
            "${PFH_TEST_RUN_ROOT}/window-plan-runs")

    add_executable(pineforge_hpo_test_selected_planning test_selected_planning.cpp)
    target_link_libraries(pineforge_hpo_test_selected_planning PRIVATE
        PineForgeHPO::engine_adapter ${CMAKE_DL_LIBS})
    target_compile_options(pineforge_hpo_test_selected_planning PRIVATE -Wall -Wextra -Wpedantic)
    add_test(NAME pineforge_hpo_selected_planning
        COMMAND pineforge_hpo_test_selected_planning
            "${PFH_TEST_RUN_ROOT}/selected-planning-runs")

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

    # Keep the production runner and the ordinary fake plugin untouched. This variant adds
    # protocol bytes at worker exit and after the runner's real SIGTERM handler returns.
    file(GENERATE OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/no_improvement_strategy.cpp" CONTENT [=[
#define run_backtest_full fixture_run_backtest_full
#include "test_engine_adapter.cpp"
#undef run_backtest_full
#include <csignal>

namespace {
struct WorkerExitTrace {
    int descriptor = -1;
    ~WorkerExitTrace() {
        if (descriptor < 0)
            return;
        ssize_t count;
        do {
            count = ::write(descriptor, "J", 1);
        } while (count < 0 && errno == EINTR);
        if (count != 1)
            ::_exit(90);
    }
};
thread_local WorkerExitTrace worker_exit_trace;
struct sigaction runner_sigterm {};
volatile std::sig_atomic_t signal_trace_fd = -1;

void trace_sigterm(int number) {
    const int saved_errno = errno;
    runner_sigterm.sa_handler(number);
    ssize_t count;
    do {
        count = ::write(static_cast<int>(signal_trace_fd), "T", 1);
    } while (count < 0 && errno == EINTR);
    if (count != 1)
        ::_exit(91);
    errno = saved_errno;
}
}  // namespace

extern "C" {
PF_API void run_backtest_full(pf_strategy_t strategy, pf_bar_t* bars, int bar_count,
                             const char* input_timeframe, const char* script_timeframe,
                             int bar_magnifier, int magnifier_samples,
                             pf_magnifier_distribution_t magnifier_distribution,
                             pf_report_t* report) {
    auto* state = fake(strategy);
    const auto exited = state->inputs.find("WorkerExitFd");
    if (exited != state->inputs.end())
        worker_exit_trace.descriptor = std::stoi(exited->second);
    const auto delivered = state->inputs.find("SignalTraceFd");
    if (delivered != state->inputs.end() && signal_trace_fd < 0) {
        if (::sigaction(SIGTERM, nullptr, &runner_sigterm) != 0 ||
            (runner_sigterm.sa_flags & SA_SIGINFO) != 0 ||
            runner_sigterm.sa_handler == SIG_DFL || runner_sigterm.sa_handler == SIG_IGN) {
            state->error = "cannot observe runner SIGTERM handler";
            return;
        }
        signal_trace_fd = std::stoi(delivered->second);
        auto action = runner_sigterm;
        action.sa_handler = trace_sigterm;
        if (::sigaction(SIGTERM, &action, nullptr) != 0) {
            state->error = "cannot install SIGTERM delivery trace";
            return;
        }
    }
    fixture_run_backtest_full(strategy, bars, bar_count, input_timeframe, script_timeframe,
                             bar_magnifier, magnifier_samples, magnifier_distribution, report);
}
}  // extern "C"
]=])
    add_library(pineforge_hpo_no_improvement_strategy SHARED
        "${CMAKE_CURRENT_BINARY_DIR}/no_improvement_strategy.cpp")
    target_compile_definitions(pineforge_hpo_no_improvement_strategy PRIVATE
        PINEFORGE_HPO_FAKE_PLUGIN=1)
    target_include_directories(pineforge_hpo_no_improvement_strategy PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}"
        "${PINEFORGE_ENGINE_SOURCE_INCLUDE}"
        "${PINEFORGE_ENGINE_BUILD_INCLUDE}")
    target_compile_options(pineforge_hpo_no_improvement_strategy PRIVATE
        -Wall -Wextra -Wpedantic)
    set_target_properties(pineforge_hpo_no_improvement_strategy PROPERTIES
        PREFIX ""
        OUTPUT_NAME "no_improvement_strategy")

    foreach(plugin pineforge_hpo_fake_strategy pineforge_hpo_fake_strategy_no_timezone
                   pineforge_hpo_fake_strategy_no_metadata pineforge_hpo_no_improvement_strategy)
        if(PINEFORGE_HPO_ENABLE_SANITIZERS)
            target_compile_options(${plugin} PRIVATE
                -fsanitize=address,undefined -fno-omit-frame-pointer)
            target_link_options(${plugin} PRIVATE -fsanitize=address,undefined)
        elseif(PINEFORGE_HPO_ENABLE_THREAD_SANITIZER)
            target_compile_options(${plugin} PRIVATE -fsanitize=thread -fno-omit-frame-pointer)
            target_link_options(${plugin} PRIVATE -fsanitize=thread)
        endif()
    endforeach()

    foreach(extension CODED STATUS CHECKED BAD_ABI SCRATCH SCRATCH_NO_TABLES SCRATCH_NO_ARENA)
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
    foreach(plugin pineforge_hpo_fake_strategy_scratch
                   pineforge_hpo_fake_strategy_scratch_no_tables
                   pineforge_hpo_fake_strategy_scratch_no_arena)
        target_compile_definitions(${plugin} PRIVATE
            PINEFORGE_HPO_FAKE_PLUGIN_SCRATCH=1 PINEFORGE_HPO_FAKE_PLUGIN_CODED=1)
    endforeach()
    target_compile_definitions(pineforge_hpo_fake_strategy_scratch_no_tables PRIVATE
        PINEFORGE_HPO_FAKE_PLUGIN_NO_TABLES=1)
    target_compile_definitions(pineforge_hpo_fake_strategy_scratch_no_arena PRIVATE
        PINEFORGE_HPO_FAKE_PLUGIN_NO_ARENA=1)

    add_executable(pineforge_hpo_test_engine_adapter test_engine_adapter.cpp)
    target_link_libraries(pineforge_hpo_test_engine_adapter PRIVATE
        PineForgeHPO::engine_adapter
        ${CMAKE_DL_LIBS})
    target_compile_options(pineforge_hpo_test_engine_adapter PRIVATE
        -Wall -Wextra -Wpedantic)
    add_library(pineforge_hpo_test_signal_macro OBJECT EXCLUDE_FROM_ALL test_signal_macro.cpp)
    target_link_libraries(pineforge_hpo_test_signal_macro PRIVATE PineForgeHPO::engine_adapter)
    # test_signal_macro.cpp includes src/cli/main.cpp, hence return_stats_identity.hpp and the
    # generated header behind it: the generation dependency and the per-configuration include
    # directory are given the way pfh_return_stats_identity gives them to its CONSUMERS.
    add_dependencies(pineforge_hpo_test_signal_macro ${PFH_RETURN_STATS_IDENTITY_TARGET})
    target_include_directories(pineforge_hpo_test_signal_macro PRIVATE
        "${PFH_RETURN_STATS_IDENTITY_DIRECTORY}")
    add_test(NAME pineforge_hpo_signal_macro
        COMMAND "${CMAKE_COMMAND}" --build "${CMAKE_BINARY_DIR}"
            --target pineforge_hpo_test_signal_macro)

    # A separate translation of the CLI, built only for proof tests. The production target
    # receives no proof definition, dependency or installation change.
    find_package(Threads REQUIRED)
    add_executable(pineforge-hpo-native-proof EXCLUDE_FROM_ALL
        "${PROJECT_SOURCE_DIR}/src/cli/main.cpp"
        "${PROJECT_SOURCE_DIR}/src/cli/runner_seccomp.cpp")
    target_compile_definitions(pineforge-hpo-native-proof PRIVATE
        PINEFORGE_HPO_PROOF_CURVES=1 PINEFORGE_HPO_VERSION="${PINEFORGE_HPO_VERSION}")
    target_link_libraries(pineforge-hpo-native-proof PRIVATE
        PineForgeHPO::engine_adapter Threads::Threads)
    add_dependencies(pineforge-hpo-native-proof ${PFH_RETURN_STATS_IDENTITY_TARGET})
    target_include_directories(pineforge-hpo-native-proof PRIVATE
        "${PINEFORGE_ENGINE_SOURCE_INCLUDE}" "${PINEFORGE_ENGINE_BUILD_INCLUDE}"
        "${PFH_RETURN_STATS_IDENTITY_DIRECTORY}")
    target_compile_options(pineforge-hpo-native-proof PRIVATE -Wall -Wextra -Wpedantic)
    # Like the proof CLI, this includes the real coordinator in a separate test translation.
    # Its hook definition and controlled clock never reach pineforge-hpo-native.
    add_executable(pineforge_hpo_test_evaluation_completion test_evaluation_completion.cpp
        "${PROJECT_SOURCE_DIR}/src/cli/runner_seccomp.cpp")
    target_compile_definitions(pineforge_hpo_test_evaluation_completion PRIVATE
        PINEFORGE_HPO_VERSION="${PINEFORGE_HPO_VERSION}")
    target_link_libraries(pineforge_hpo_test_evaluation_completion PRIVATE
        PineForgeHPO::engine_adapter Threads::Threads)
    add_dependencies(pineforge_hpo_test_evaluation_completion ${PFH_RETURN_STATS_IDENTITY_TARGET})
    target_include_directories(pineforge_hpo_test_evaluation_completion PRIVATE
        "${PFH_RETURN_STATS_IDENTITY_DIRECTORY}")
    target_compile_options(pineforge_hpo_test_evaluation_completion PRIVATE
        -Wall -Wextra -Wpedantic)
    if(PINEFORGE_HPO_ENABLE_SANITIZERS)
        target_compile_options(pineforge_hpo_test_evaluation_completion PRIVATE
            -fsanitize=address,undefined -fno-omit-frame-pointer)
        target_link_options(pineforge_hpo_test_evaluation_completion PRIVATE
            -fsanitize=address,undefined)
    elseif(PINEFORGE_HPO_ENABLE_THREAD_SANITIZER)
        target_compile_options(pineforge_hpo_test_evaluation_completion PRIVATE
            -fsanitize=thread -fno-omit-frame-pointer)
        target_link_options(pineforge_hpo_test_evaluation_completion PRIVATE -fsanitize=thread)
    endif()
    add_executable(pineforge_hpo_pbo_curve_reference EXCLUDE_FROM_ALL real_strategy_curve.cpp)
    target_compile_definitions(pineforge_hpo_pbo_curve_reference PRIVATE PINEFORGE_HPO_PBO_PROBE=1)
    target_link_libraries(pineforge_hpo_pbo_curve_reference PRIVATE PineForgeHPO::engine_adapter)
    target_compile_options(pineforge_hpo_pbo_curve_reference PRIVATE -Wall -Wextra -Wpedantic)
    add_library(pineforge_hpo_pbo_proof_strategy SHARED EXCLUDE_FROM_ALL pbo_proof_strategy.cpp)
    target_include_directories(pineforge_hpo_pbo_proof_strategy PRIVATE
        "${PINEFORGE_ENGINE_SOURCE_INCLUDE}" "${PINEFORGE_ENGINE_BUILD_INCLUDE}")
    target_compile_options(pineforge_hpo_pbo_proof_strategy PRIVATE -Wall -Wextra -Wpedantic)
    set_target_properties(pineforge_hpo_pbo_proof_strategy PROPERTIES PREFIX "")
    if(PINEFORGE_HPO_ENABLE_SANITIZERS)
        target_compile_options(pineforge_hpo_pbo_proof_strategy PRIVATE
            -fsanitize=address,undefined -fno-omit-frame-pointer)
        target_link_options(pineforge_hpo_pbo_proof_strategy PRIVATE -fsanitize=address,undefined)
        target_compile_options(pineforge-hpo-native-proof PRIVATE
            -fsanitize=address,undefined -fno-omit-frame-pointer)
        target_link_options(pineforge-hpo-native-proof PRIVATE -fsanitize=address,undefined)
        target_compile_options(pineforge_hpo_pbo_curve_reference PRIVATE
            -fsanitize=address,undefined -fno-omit-frame-pointer)
        target_link_options(pineforge_hpo_pbo_curve_reference PRIVATE -fsanitize=address,undefined)
    elseif(PINEFORGE_HPO_ENABLE_THREAD_SANITIZER)
        target_compile_options(pineforge_hpo_pbo_proof_strategy PRIVATE
            -fsanitize=thread -fno-omit-frame-pointer)
        target_link_options(pineforge_hpo_pbo_proof_strategy PRIVATE -fsanitize=thread)
        target_compile_options(pineforge-hpo-native-proof PRIVATE
            -fsanitize=thread -fno-omit-frame-pointer)
        target_link_options(pineforge-hpo-native-proof PRIVATE -fsanitize=thread)
        target_compile_options(pineforge_hpo_pbo_curve_reference PRIVATE
            -fsanitize=thread -fno-omit-frame-pointer)
        target_link_options(pineforge_hpo_pbo_curve_reference PRIVATE -fsanitize=thread)
    endif()
    add_dependencies(pineforge_hpo_test_engine_adapter
        pineforge_hpo_fake_strategy
        pineforge_hpo_fake_strategy_no_timezone
        pineforge_hpo_fake_strategy_no_metadata
        pineforge_hpo_fake_strategy_coded
        pineforge_hpo_fake_strategy_status
        pineforge_hpo_fake_strategy_checked
        pineforge_hpo_fake_strategy_bad_abi
        pineforge_hpo_fake_strategy_scratch
        pineforge_hpo_fake_strategy_scratch_no_tables
        pineforge_hpo_fake_strategy_scratch_no_arena)

    add_test(NAME pineforge_hpo_engine_adapter
        COMMAND pineforge_hpo_test_engine_adapter
            $<TARGET_FILE:pineforge_hpo_fake_strategy>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_no_timezone>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_no_metadata>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_coded>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_status>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_checked>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_bad_abi>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_scratch>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_scratch_no_tables>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_scratch_no_arena>)
    if(PINEFORGE_HPO_REQUIRE_RETURN_STATS_IDENTITY)
        set_property(TEST pineforge_hpo_engine_adapter APPEND PROPERTY
            ENVIRONMENT "PFH_REQUIRE_RETURN_STATS_IDENTITY=1")
    endif()

    add_executable(pineforge_hpo_test_dataset_view test_dataset_view.cpp)
    target_link_libraries(pineforge_hpo_test_dataset_view PRIVATE
        PineForgeHPO::engine_adapter ${CMAKE_DL_LIBS})
    add_dependencies(pineforge_hpo_test_dataset_view pineforge_hpo_fake_strategy)
    target_compile_options(pineforge_hpo_test_dataset_view PRIVATE -Wall -Wextra -Wpedantic)
    add_test(NAME pineforge_hpo_dataset_view COMMAND pineforge_hpo_test_dataset_view
        $<TARGET_FILE:pineforge_hpo_fake_strategy>)
    if(PINEFORGE_HPO_REQUIRE_RETURN_STATS_IDENTITY)
        set_property(TEST pineforge_hpo_dataset_view APPEND PROPERTY
            ENVIRONMENT "PFH_REQUIRE_RETURN_STATS_IDENTITY=1")
    endif()

    # The private native hook includes the production entry point, like the existing
    # signal-macro fixture. No public CLI flag or installation target is added.
    add_executable(pineforge_hpo_test_study_split test_study_split.cpp
        "${PROJECT_SOURCE_DIR}/src/cli/runner_seccomp.cpp")
    target_compile_definitions(pineforge_hpo_test_study_split PRIVATE
        PINEFORGE_HPO_PROOF_CURVES=1 PINEFORGE_HPO_VERSION="${PINEFORGE_HPO_VERSION}")
    target_link_libraries(pineforge_hpo_test_study_split PRIVATE PineForgeHPO::engine_adapter)
    add_dependencies(pineforge_hpo_test_study_split
        ${PFH_RETURN_STATS_IDENTITY_TARGET} pineforge_hpo_fake_strategy)
    target_include_directories(pineforge_hpo_test_study_split PRIVATE
        "${PFH_RETURN_STATS_IDENTITY_DIRECTORY}")
    target_compile_options(pineforge_hpo_test_study_split PRIVATE -Wall -Wextra -Wpedantic)
    if(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND NOT PINEFORGE_HPO_ENABLE_SANITIZERS
            AND NOT PINEFORGE_HPO_ENABLE_THREAD_SANITIZER)
        target_compile_definitions(pineforge_hpo_test_study_split PRIVATE
            PINEFORGE_HPO_STUDY_RSS_GATE=1)
    endif()
    # The ordinary pin runs the real pineforge-hpo-native only when that target exists.
    # Otherwise it uses the same production entry point compiled into the private test target
    # with PINEFORGE_HPO_PROOF_CURVES=1 and main renamed.
    if(TARGET pineforge-hpo-native)
        set(PFH_STUDY_PIN_NATIVE $<TARGET_FILE:pineforge-hpo-native>)
        add_dependencies(pineforge_hpo_test_study_split pineforge-hpo-native)
    else()
        set(PFH_STUDY_PIN_NATIVE $<TARGET_FILE:pineforge_hpo_test_study_split>)
    endif()
    add_test(NAME pineforge_hpo_study_split COMMAND pineforge_hpo_test_study_split
        ${PFH_STUDY_PIN_NATIVE} $<TARGET_FILE:pineforge_hpo_fake_strategy>
        "${CMAKE_CURRENT_BINARY_DIR}/study-split")
    if(PINEFORGE_HPO_REQUIRE_RETURN_STATS_IDENTITY)
        set_property(TEST pineforge_hpo_study_split APPEND PROPERTY
            ENVIRONMENT "PFH_REQUIRE_RETURN_STATS_IDENTITY=1")
    endif()

    add_executable(pineforge_hpo_test_dataset_bin test_dataset_bin.cpp)
    target_link_libraries(pineforge_hpo_test_dataset_bin PRIVATE PineForgeHPO::engine_adapter)
    target_compile_options(pineforge_hpo_test_dataset_bin PRIVATE -Wall -Wextra -Wpedantic)

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
    target_link_libraries(pineforge_hpo_symbol_feed_report PRIVATE
        PineForgeHPO::engine_adapter ${CMAKE_DL_LIBS})
    if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
        add_test(NAME pineforge_hpo_jailed_profile_e2e
            COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/test_jailed_profile_e2e.py")
        set_tests_properties(pineforge_hpo_jailed_profile_e2e PROPERTIES
            SKIP_REGULAR_EXPRESSION "SKIP: jailed profile requires Linux sealed memfds")
    endif()

    # Rolling-schedule replay: the test runs the real native binary on the rolling schedule at
    # several worker counts, reads each result file and replays every proposal from it.
    if(TARGET pineforge-hpo-native)
        add_executable(pineforge_hpo_test_rolling_replay test_rolling_replay.cpp)
        target_link_libraries(pineforge_hpo_test_rolling_replay PRIVATE PineForgeHPO::core)
        target_compile_options(pineforge_hpo_test_rolling_replay PRIVATE -Wall -Wextra -Wpedantic)
        add_dependencies(pineforge_hpo_test_rolling_replay
            pineforge-hpo-native pineforge_hpo_fake_strategy)
        add_test(NAME pineforge_hpo_rolling_replay
            COMMAND pineforge_hpo_test_rolling_replay
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
    endif()

    find_package(Python3 COMPONENTS Interpreter)
    if(Python3_Interpreter_FOUND)
        set(PFH_DATASET_BIN_FIXTURES "${CMAKE_CURRENT_BINARY_DIR}/dataset-bin-fixtures")
        add_test(NAME pineforge_hpo_dataset_bin_fixtures
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/generate_dataset_bin_fixtures.py"
                "${PFH_DATASET_BIN_FIXTURES}")
        set_tests_properties(pineforge_hpo_dataset_bin_fixtures PROPERTIES
            FIXTURES_SETUP dataset_bin_inputs)
        add_test(NAME pineforge_hpo_dataset_bin COMMAND pineforge_hpo_test_dataset_bin
            "${PFH_DATASET_BIN_FIXTURES}")
        # R4 uses the same C++ executable and real mapping, with unconditional seam references.
        add_test(NAME pineforge_hpo_dataset_bin_identity COMMAND pineforge_hpo_test_dataset_bin
            "${PFH_DATASET_BIN_FIXTURES}" --mapped-identity)
        set_tests_properties(pineforge_hpo_dataset_bin pineforge_hpo_dataset_bin_identity PROPERTIES
            FIXTURES_REQUIRED dataset_bin_inputs)
    else()
        message(WARNING "SKIP: Python not found; binary-loader tests will not run")
        add_test(NAME pineforge_hpo_dataset_bin_requires_python
            COMMAND "${CMAKE_COMMAND}" -E false)
    endif()
    if(Python3_Interpreter_FOUND AND TARGET pineforge-hpo-native)
        add_test(NAME pineforge_hpo_dataset_bin_sidecars
            COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/test_dataset_bin_sidecars.py"
                $<TARGET_FILE:pineforge-hpo-native> $<TARGET_FILE:pineforge_hpo_fake_strategy>
                "${PFH_DATASET_BIN_FIXTURES}" --modes)
        set_tests_properties(pineforge_hpo_dataset_bin_sidecars PROPERTIES
            FIXTURES_REQUIRED dataset_bin_inputs)
        add_test(NAME pineforge_hpo_pbo_proof_build
            COMMAND "${CMAKE_COMMAND}" --build "${CMAKE_BINARY_DIR}" --config $<CONFIG>
                --target pineforge-hpo-native-proof pineforge_hpo_pbo_proof_strategy
                    pineforge_hpo_pbo_curve_reference)
        set_tests_properties(pineforge_hpo_pbo_proof_build PROPERTIES
            FIXTURES_SETUP pbo_proof_executables RUN_SERIAL TRUE)
        add_test(NAME pineforge_hpo_pbo_proof_cli
            COMMAND "${Python3_EXECUTABLE}" -I
                "${CMAKE_CURRENT_SOURCE_DIR}/test_pbo_proof_cli.py"
                $<TARGET_FILE:pineforge-hpo-native> $<TARGET_FILE:pineforge-hpo-native-proof>
                $<TARGET_FILE:pineforge_hpo_pbo_proof_strategy> -v)
        set_tests_properties(pineforge_hpo_pbo_proof_cli PROPERTIES
            FIXTURES_REQUIRED pbo_proof_executables)
        set(PINEFORGE_HPO_PBO_PROOF_PLUGIN "" CACHE FILEPATH
            "Compiled threshold strategy plugin for the synthetic PBO proof")
        # The end-to-end proof needs a plugin compiled from the ordinary artifact route, which an
        # ordinary build does not have. It is registered only when that plugin is configured, and
        # the driver then fails (never skips) if the plugin, its manifest or any input is missing.
        if(PINEFORGE_HPO_PBO_PROOF_PLUGIN)
            add_test(NAME pineforge_hpo_pbo_native_proof
                COMMAND "${Python3_EXECUTABLE}" -I
                    "${CMAKE_CURRENT_SOURCE_DIR}/g2_oracles/pbo_native_proof.py"
                    --native $<TARGET_FILE:pineforge-hpo-native-proof>
                    --plugin "${PINEFORGE_HPO_PBO_PROOF_PLUGIN}"
                    --harness $<TARGET_FILE:pineforge_hpo_pbo_curve_reference>
                    --engine-root "${PINEFORGE_ENGINE_ROOT}"
                    --out "${CMAKE_CURRENT_BINARY_DIR}/pbo-proof" --S 16)
            set_tests_properties(pineforge_hpo_pbo_native_proof PROPERTIES
                FIXTURES_REQUIRED pbo_proof_executables)
        endif()
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
        endif()
        if(dlib_SOURCE_DIR)
            add_test(NAME pineforge_hpo_release_version_integration
                COMMAND "${Python3_EXECUTABLE}" -I
                    "${CMAKE_CURRENT_SOURCE_DIR}/test_release_version_integration.py"
                    "${PINEFORGE_ENGINE_ROOT}" "${dlib_SOURCE_DIR}"
                    $<TARGET_FILE:pineforge_hpo_fake_strategy> -v)
            set_tests_properties(pineforge_hpo_release_version_integration PROPERTIES
                RUN_SERIAL TRUE)
            add_test(NAME pineforge_hpo_numeric_flags
                COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/test_numeric_flags.py"
                    "${PROJECT_SOURCE_DIR}" "${dlib_SOURCE_DIR}")
            add_test(NAME pineforge_hpo_return_stats_identity
                COMMAND "${Python3_EXECUTABLE}"
                    "${CMAKE_CURRENT_SOURCE_DIR}/test_return_stats_identity.py"
                    "${PROJECT_SOURCE_DIR}" "${dlib_SOURCE_DIR}")
            # The Sobol identity's build binding on synthetic databases, fixture projects and
            # copies of the real project (both generators): serial, long, many configures.
            add_test(NAME pineforge_hpo_sobol_identity_build
                COMMAND "${Python3_EXECUTABLE}"
                    "${CMAKE_CURRENT_SOURCE_DIR}/test_sobol_identity.py"
                    "${PROJECT_SOURCE_DIR}" "${dlib_SOURCE_DIR}")
            set_tests_properties(pineforge_hpo_sobol_identity_build PROPERTIES
                RUN_SERIAL TRUE)
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
                RUN_SERIAL TRUE)
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
        set(PFH_SECCOMP_BUILD_PROBES OFF)
        set(PFH_SECCOMP_UNAVAILABLE_REASON platform)
        if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
            # Check the compiler ABI, including -m32/-mx32 and endian flags, rather than
            # infer it from a host processor name. Unsupported builds still test refusal.
            include(CheckCXXSourceCompiles)
            # No backslash line continuations in the source below. With them the kept compile
            # source had "#if !defined(__LP64__) ||" at the end of a line with nothing after it
            # (seen in the try_compile source on x86_64 GCC 13.3), a preprocessor error that made
            # the check fail on every platform.
            check_cxx_source_compiles([=[
                #if !defined(__LP64__)
                #error Unsupported seccomp ABI
                #endif
                #if !defined(__x86_64__) && !defined(__aarch64__)
                #error Unsupported seccomp ABI
                #endif
                #if !defined(__BYTE_ORDER__) || !defined(__ORDER_LITTLE_ENDIAN__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
                #error Unsupported seccomp ABI
                #endif
                int main() { return 0; }
            ]=] PFH_SECCOMP_TEST_ARCH_SUPPORTED)
            set(PFH_SECCOMP_UNAVAILABLE_REASON architecture)
            if(PFH_SECCOMP_TEST_ARCH_SUPPORTED)
                # Match production prerequisites: incomplete Linux headers exercise a
                # typed refusal without breaking the test-fixture build.
                check_cxx_source_compiles([=[
                    #include <linux/audit.h>
                    #include <linux/filter.h>
                    #include <linux/sched.h>
                    #include <linux/seccomp.h>
                    #include <sys/prctl.h>
                    #include <sys/syscall.h>
                    #include <sys/utsname.h>
                    int main() {
                        const unsigned required[] = {
                            PR_SET_NO_NEW_PRIVS, PR_SET_SECCOMP, SECCOMP_MODE_FILTER,
                            SECCOMP_RET_KILL_PROCESS, CLONE_THREAD,
                            __NR_execve, __NR_execveat, __NR_clone, __NR_clone3,
                            __NR_ptrace, __NR_process_vm_readv, __NR_process_vm_writev,
                    #if defined(__x86_64__)
                            AUDIT_ARCH_X86_64, __X32_SYSCALL_BIT, __NR_fork, __NR_vfork,
                    #else
                            AUDIT_ARCH_AARCH64,
                    #endif
                        };
                        (void)required;
                        return 0;
                    }
                ]=] PFH_SECCOMP_TEST_HEADERS_SUPPORTED)
                set(PFH_SECCOMP_UNAVAILABLE_REASON headers)
                if(PFH_SECCOMP_TEST_HEADERS_SUPPORTED)
                    set(PFH_SECCOMP_BUILD_PROBES ON)
                endif()
            endif()
        endif()
        # On Linux x86_64 and aarch64 the seccomp filter is supported, so a build that cannot
        # build the real probes must fail here instead of registering a test that skips: a
        # skipped or missing seccomp end-to-end test reads green in CI and proves nothing.
        # A local -m32 or -mx32 build on an x86_64 host is not LP64, so it also fails here.
        if(CMAKE_SYSTEM_NAME STREQUAL "Linux"
                AND CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86_64|AMD64|aarch64|arm64)$"
                AND NOT PFH_SECCOMP_BUILD_PROBES)
            message(FATAL_ERROR
                "The seccomp end-to-end probes cannot be built on this ${CMAKE_SYSTEM_PROCESSOR} "
                "Linux build with ${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION} "
                "(${CMAKE_CXX_COMPILER}) (reason: ${PFH_SECCOMP_UNAVAILABLE_REASON}). Check "
                "PFH_SECCOMP_TEST_ARCH_SUPPORTED and PFH_SECCOMP_TEST_HEADERS_SUPPORTED in "
                "CMakeFiles/CMakeConfigureLog.yaml; this configuration must not skip the test.")
        endif()
        set(PFH_SECCOMP_TEST_NAME pineforge_hpo_seccomp_unavailable)
        set(PFH_SECCOMP_TEST_ARGUMENTS --plugin $<TARGET_FILE:pineforge_hpo_fake_strategy>
            --unavailable-reason ${PFH_SECCOMP_UNAVAILABLE_REASON})
        if(PFH_SECCOMP_BUILD_PROBES)
            add_library(pineforge_hpo_seccomp_probe_strategy SHARED seccomp_probe_strategy.cpp)
            target_include_directories(pineforge_hpo_seccomp_probe_strategy PRIVATE
                "${PINEFORGE_ENGINE_SOURCE_INCLUDE}" "${PINEFORGE_ENGINE_BUILD_INCLUDE}")
            target_link_libraries(pineforge_hpo_seccomp_probe_strategy PRIVATE Threads::Threads)
            target_compile_options(pineforge_hpo_seccomp_probe_strategy PRIVATE
                -Wall -Wextra -Wpedantic)
            set_target_properties(pineforge_hpo_seccomp_probe_strategy PROPERTIES PREFIX "")
            add_executable(pineforge_hpo_seccomp_exec_helper seccomp_exec_helper.cpp)
            target_compile_options(pineforge_hpo_seccomp_exec_helper PRIVATE
                -Wall -Wextra -Wpedantic)
            if(PINEFORGE_HPO_ENABLE_SANITIZERS)
                target_compile_options(pineforge_hpo_seccomp_probe_strategy PRIVATE
                    -fsanitize=address,undefined -fno-omit-frame-pointer)
                target_link_options(pineforge_hpo_seccomp_probe_strategy PRIVATE
                    -fsanitize=address,undefined)
            elseif(PINEFORGE_HPO_ENABLE_THREAD_SANITIZER)
                target_compile_options(pineforge_hpo_seccomp_probe_strategy PRIVATE
                    -fsanitize=thread -fno-omit-frame-pointer)
                target_link_options(pineforge_hpo_seccomp_probe_strategy PRIVATE -fsanitize=thread)
            endif()
            set(PFH_SECCOMP_TEST_NAME pineforge_hpo_seccomp_e2e)
            set(PFH_SECCOMP_TEST_ARGUMENTS
                --plugin $<TARGET_FILE:pineforge_hpo_seccomp_probe_strategy>
                --exec-helper $<TARGET_FILE:pineforge_hpo_seccomp_exec_helper>)
        else()
            add_test(NAME pineforge_hpo_seccomp_e2e
                COMMAND "${CMAKE_COMMAND}" -E echo
                    "SKIP: seccomp syscall probes need Linux ABI/headers:"
                    "${PFH_SECCOMP_UNAVAILABLE_REASON}")
            set_tests_properties(pineforge_hpo_seccomp_e2e PROPERTIES
                SKIP_REGULAR_EXPRESSION "SKIP: seccomp syscall probes")
        endif()
        add_test(NAME ${PFH_SECCOMP_TEST_NAME}
            COMMAND "${Python3_EXECUTABLE}" -I "${CMAKE_CURRENT_SOURCE_DIR}/test_seccomp_e2e.py"
                --native $<TARGET_FILE:pineforge-hpo-native>
                ${PFH_SECCOMP_TEST_ARGUMENTS}
                --output "${CMAKE_CURRENT_BINARY_DIR}/seccomp")
        add_test(NAME pineforge_hpo_batching
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_batching.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        add_test(NAME pineforge_hpo_rolling_schedule
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_rolling_schedule.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        # The default schedule must give the bytes of v0.12.0: a native binary built from the
        # v0.12.0 tag is compared with this build on the same studies. That needs a second build,
        # so configure with -DPINEFORGE_HPO_V012_NATIVE=/path/to/pineforge-hpo-native. The test is
        # always registered; without that path it reports itself skipped (exit status 77) instead
        # of being absent from the test list.
        set(pfh_v012_native "${PINEFORGE_HPO_V012_NATIVE}")
        if(NOT pfh_v012_native)
            set(pfh_v012_native "not-provided")
        endif()
        add_test(NAME pineforge_hpo_rolling_default_v012
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_rolling_default_v012.py"
                $<TARGET_FILE:pineforge-hpo-native>
                "${pfh_v012_native}"
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        set_tests_properties(pineforge_hpo_rolling_default_v012 PROPERTIES
            SKIP_RETURN_CODE 77)
        add_test(NAME pineforge_hpo_no_improvement
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_no_improvement.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_no_improvement_strategy>)
        if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
            set_property(TEST pineforge_hpo_no_improvement APPEND PROPERTY
                ENVIRONMENT "PFH_REQUIRE_FIFO_HANDSHAKE=1")
            add_test(NAME pineforge_hpo_evaluation_completion
                COMMAND "${Python3_EXECUTABLE}" -I
                    "${CMAKE_CURRENT_SOURCE_DIR}/test_evaluation_completion.py"
                    $<TARGET_FILE:pineforge-hpo-native>
                    $<TARGET_FILE:pineforge_hpo_test_evaluation_completion>
                    $<TARGET_FILE:pineforge_hpo_fake_strategy>
                    "${CMAKE_CURRENT_BINARY_DIR}/evaluation-completion")
        endif()
        add_test(NAME pineforge_hpo_sobol_cli
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_sobol_cli.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        if(PINEFORGE_HPO_REQUIRE_SOBOL_IDENTITY)
            set_property(TEST pineforge_hpo_sobol_cli APPEND PROPERTY
                ENVIRONMENT "PFH_REQUIRE_SOBOL_IDENTITY=1")
        endif()
        add_test(NAME pineforge_hpo_candidate_list_cli
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_candidate_list_cli.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        add_test(NAME pineforge_hpo_return_stats_cli
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_return_stats_cli.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        if(PINEFORGE_HPO_REQUIRE_RETURN_STATS_IDENTITY)
            set_property(TEST pineforge_hpo_return_stats_cli APPEND PROPERTY
                ENVIRONMENT "PFH_REQUIRE_RETURN_STATS_IDENTITY=1")
        endif()
        add_test(NAME pineforge_hpo_pbo_blocks_cli
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_pbo_blocks_cli.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        if(PINEFORGE_HPO_REQUIRE_RETURN_STATS_IDENTITY)
            set_property(TEST pineforge_hpo_pbo_blocks_cli APPEND PROPERTY
                ENVIRONMENT "PFH_REQUIRE_RETURN_STATS_IDENTITY=1")
        endif()
        add_test(NAME pineforge_hpo_trial_outputs
            COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/test_trial_outputs.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        add_test(NAME pineforge_hpo_row_space
            COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/test_row_space.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        add_test(NAME pineforge_hpo_streamed_trial_retention
            COMMAND "${Python3_EXECUTABLE}" -I
                "${CMAKE_CURRENT_SOURCE_DIR}/test_streamed_trial_retention.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>
                "${CMAKE_CURRENT_BINARY_DIR}/streamed-trial-retention")
        # Keep other CTest cases from contending with this small RSS-delta gate.
        # It is always run on Linux; no skip code or sanitizer-specific threshold is supplied.
        set_tests_properties(pineforge_hpo_streamed_trial_retention PROPERTIES RUN_SERIAL TRUE)
        add_test(NAME pineforge_hpo_warm_start_contract
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_warm_start_contract.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        add_test(NAME pineforge_hpo_warm_binary_contract
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_warm_binary_contract.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
        foreach(contract syminfo progress final_write_signal progress_nonblocking progress_error
                         cancel deadline metrics magnifier timeout wall_timeout
                         timeout_progress_error)
            add_test(NAME pineforge_hpo_runner_${contract}
                COMMAND "${Python3_EXECUTABLE}"
                    "${CMAKE_CURRENT_SOURCE_DIR}/test_runner_contract.py"
                    $<TARGET_FILE:pineforge-hpo-native>
                    $<TARGET_FILE:pineforge_hpo_fake_strategy> ${contract})
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
