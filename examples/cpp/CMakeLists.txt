add_executable(pineforge_hpo_example_ask_tell ask_tell.cpp)
target_link_libraries(pineforge_hpo_example_ask_tell PRIVATE PineForgeHPO::core)
target_compile_options(pineforge_hpo_example_ask_tell PRIVATE
    -Wall -Wextra -Wpedantic)
