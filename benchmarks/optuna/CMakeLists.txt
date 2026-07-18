cmake_minimum_required(VERSION 3.17)

project(PineForgeHPOOptunaBenchmark LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

get_filename_component(PINEFORGE_HPO_ROOT "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)
set(PINEFORGE_HPO_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(PINEFORGE_HPO_BUILD_ENGINE_ADAPTER OFF CACHE BOOL "" FORCE)
set(PINEFORGE_HPO_BUILD_NATIVE_CLI OFF CACHE BOOL "" FORCE)
add_subdirectory(
    "${PINEFORGE_HPO_ROOT}"
    "${CMAKE_CURRENT_BINARY_DIR}/pineforge-hpo"
    EXCLUDE_FROM_ALL
)

add_executable(pineforge_hpo_optuna_native native_benchmark.cpp)
target_link_libraries(pineforge_hpo_optuna_native PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_optuna_native PRIVATE -Wall -Wextra -Wpedantic)
