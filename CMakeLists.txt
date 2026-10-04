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
    LANGUAGES CXX
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

add_library(pineforge_hpo_core STATIC
    src/core/dlib_sampler.cpp
    src/core/objective.cpp
    src/core/pruner.cpp
    src/core/sampler.cpp
    src/core/search_space.cpp
    src/core/tpe_sampler.cpp
    src/core/types.cpp
)
add_library(PineForgeHPO::core ALIAS pineforge_hpo_core)
target_include_directories(pineforge_hpo_core
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
        $<INSTALL_INTERFACE:include>)
target_compile_options(pineforge_hpo_core PRIVATE
    -Wall -Wextra -Wpedantic)
find_package(Threads REQUIRED)
target_link_libraries(pineforge_hpo_core PUBLIC dlib::dlib Threads::Threads)

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

if(PINEFORGE_HPO_ENABLE_SANITIZERS)
    foreach(target pineforge_hpo_core pineforge_hpo_engine_adapter pineforge-hpo-native)
        if(TARGET ${target})
            target_compile_options(${target} PUBLIC
                -fsanitize=address,undefined -fno-omit-frame-pointer)
            target_link_options(${target} PUBLIC -fsanitize=address,undefined)
        endif()
    endforeach()
endif()

if(PINEFORGE_HPO_ENABLE_THREAD_SANITIZER)
    foreach(target pineforge_hpo_core pineforge_hpo_engine_adapter pineforge-hpo-native)
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
