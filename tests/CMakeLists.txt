add_executable(pineforge_hpo_test_core test_core.cpp)
target_link_libraries(pineforge_hpo_test_core PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_core PRIVATE -Wall -Wextra -Wpedantic)

add_test(NAME pineforge_hpo_core COMMAND pineforge_hpo_test_core)

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
set_tests_properties(pineforge_hpo_tpe_quality PROPERTIES TIMEOUT 60)

add_executable(pineforge_hpo_test_tpe_stress test_tpe_stress.cpp)
target_link_libraries(pineforge_hpo_test_tpe_stress PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_tpe_stress PRIVATE -Wall -Wextra -Wpedantic)

add_test(NAME pineforge_hpo_tpe_stress COMMAND pineforge_hpo_test_tpe_stress)
set_tests_properties(pineforge_hpo_tpe_stress PROPERTIES TIMEOUT 60)

add_executable(pineforge_hpo_test_tpe_concurrency test_tpe_concurrency.cpp)
target_link_libraries(pineforge_hpo_test_tpe_concurrency PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_test_tpe_concurrency PRIVATE -Wall -Wextra -Wpedantic)

add_test(NAME pineforge_hpo_tpe_concurrency COMMAND pineforge_hpo_test_tpe_concurrency)
set_tests_properties(pineforge_hpo_tpe_concurrency PROPERTIES TIMEOUT 30)

if(UNIX AND TARGET PineForgeHPO::engine_adapter)
    # The engine-adapter fixture is compiled three ways from one source:
    #   1. a test executable;
    #   2. a complete strategy plugin;
    #   3. a legacy plugin without the optional chart-timezone export.
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

    add_executable(pineforge_hpo_test_engine_adapter test_engine_adapter.cpp)
    target_link_libraries(pineforge_hpo_test_engine_adapter PRIVATE
        PineForgeHPO::engine_adapter
        ${CMAKE_DL_LIBS})
    target_compile_options(pineforge_hpo_test_engine_adapter PRIVATE
        -Wall -Wextra -Wpedantic)
    add_dependencies(pineforge_hpo_test_engine_adapter
        pineforge_hpo_fake_strategy
        pineforge_hpo_fake_strategy_no_timezone)

    add_test(NAME pineforge_hpo_engine_adapter
        COMMAND pineforge_hpo_test_engine_adapter
            $<TARGET_FILE:pineforge_hpo_fake_strategy>
            $<TARGET_FILE:pineforge_hpo_fake_strategy_no_timezone>)

    find_package(Python3 COMPONENTS Interpreter)
    if(Python3_Interpreter_FOUND AND TARGET pineforge-hpo-native)
        add_test(NAME pineforge_hpo_native_cli
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/test_native_cli.py"
                $<TARGET_FILE:pineforge-hpo-native>
                $<TARGET_FILE:pineforge_hpo_fake_strategy>)
    else()
        message(STATUS "Skipping native CLI regression test: Python 3 not found")
    endif()
else()
    message(STATUS
        "Skipping engine-adapter dynamic-plugin tests: adapter/CLI disabled or dlopen unavailable")
endif()
