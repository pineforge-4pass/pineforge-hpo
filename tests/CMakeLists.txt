add_executable(pineforge_hpo_test_core test_core.cpp)
target_link_libraries(pineforge_hpo_test_core PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_core PRIVATE -Wall -Wextra -Wpedantic)

add_test(NAME pineforge_hpo_core COMMAND pineforge_hpo_test_core)

add_executable(pineforge_hpo_test_warm_start test_warm_start.cpp)
target_link_libraries(pineforge_hpo_test_warm_start PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_warm_start PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_warm_start COMMAND pineforge_hpo_test_warm_start)

add_executable(pineforge_hpo_test_warm_binary test_warm_binary.cpp)
target_include_directories(pineforge_hpo_test_warm_binary PRIVATE "${PROJECT_SOURCE_DIR}/src/cli")
target_compile_definitions(pineforge_hpo_test_warm_binary PRIVATE
    PFH_WARM_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}/fixtures")
target_link_libraries(pineforge_hpo_test_warm_binary PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_warm_binary PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME pineforge_hpo_warm_binary COMMAND pineforge_hpo_test_warm_binary)

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
target_include_directories(pineforge_hpo_test_ordinal_set PRIVATE "${PROJECT_SOURCE_DIR}/src/core")
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
        pineforge_hpo_fake_strategy_no_metadata)

    add_test(NAME pineforge_hpo_engine_adapter
        COMMAND pineforge_hpo_test_engine_adapter
            $<TARGET_FILE:pineforge_hpo_fake_strategy>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_no_timezone>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_no_metadata>)

    find_package(Python3 COMPONENTS Interpreter)
    if(Python3_Interpreter_FOUND AND TARGET pineforge-hpo-native)
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
