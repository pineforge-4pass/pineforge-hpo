# Sobol numeric build identity.
#
# Binds the Sobol sampler sources, the generated Joe-Kuo table, the portable-math inline headers
# and the portable-math C target, together with the compile command that the build system
# generated for every one of those translation units and the bytes of both compiler drivers, into
# one component identity. It is distinct from the TPE checkpoint identity and from the
# return-statistics identity, and it changes neither. Rules, fields and limits:
# docs/internal/sobol-identity.md
#
#   pfh_sobol_identity(
#       TARGET <core>                    target that compiles the Sobol sources
#       PORTABLE_MATH_TARGET <math>      C target whose every source is bound
#       [CONSUMERS <target>...]          targets ordered after the identity files exist
#       [SOURCE_OPTIONS <option>...]     per-source options of the Sobol C++ sources
#       [SOURCE_ROOT <dir>] [OUTPUT_DIR <dir>])
#
# The bound C++ translation units are src/core/sobol_engine.cpp, sobol_mapper.cpp, sobol_sampler.cpp
# and sobol_identity.cpp; the call fails at configure time when one of them is not a source of
# TARGET (an integration error, independent of any build setting). The C translation units are the
# sources of PORTABLE_MATH_TARGET at the end of the top-level directory.
#
# Conservative provider coupling. The runtime helpers the Sobol mapper and identity unit call
# (the detail::math wrappers of portable_math.hpp, the stepped-grid helpers of portable_grid.hpp,
# require_portable_environment() and runtime_math_fingerprint() of numeric_build.hpp) are plain
# inline functions with external linkage. Every translation unit that includes those headers may
# emit a copy, and the linker keeps one of them, so the copy that executes is not necessarily the
# one compiled by a Sobol unit. The descriptor therefore also binds, as providers, the source
# digest and the generated compile command of every shipped core translation unit that includes
# one of those headers: src/core/search_space.cpp and src/core/tpe_sampler.cpp. Their options,
# the TPE flag text and the TPE checkpoint identity are neither read nor changed; the Sobol
# identity simply moves when a provider's command or source moves. SOURCE_OPTIONS defaults to
# the strict arithmetic recipe -fno-fast-math -ffp-contract=off -frounding-math -fno-builtin
# -fno-lto and is written as a per-source property of the four C++ sources only. The options and
# definitions of TARGET and of PORTABLE_MATH_TARGET, CMAKE_CXX_FLAGS, CMAKE_C_FLAGS, the numeric
# flag recipe of the TPE identity and the return-statistics helper are never touched. No include
# directory is added to TARGET: the generated header reaches sobol_identity.cpp through a
# per-source include directory, so no command of any other translation unit changes. The one
# target property it sets is EXPORT_COMPILE_COMMANDS, on TARGET and PORTABLE_MATH_TARGET while the
# compile-commands export is on.
#
# Like the return-statistics helper, the identity is read from the compilation database at build
# time, so usage requirements and every change made before the end of the top-level directory are
# bound as generated. A situation that cannot be bound exactly (multi-configuration generators,
# launchers or wrappers, response files, an unreadable or ambiguous entry, a pre-3.19 CMake) makes
# the capability unbound: the build still succeeds, the digest is empty and the reason is recorded.
#
# Result variables (caller scope): PFH_SOBOL_IDENTITY_TARGET is the generation target and
# PFH_SOBOL_IDENTITY_DIRECTORY the per-configuration output directory (contains a generator
# expression). The directory holds sobol_identity.descriptor.txt (the exact descriptor),
# sobol_identity.txt (the build digest) and sobol_identity_generated.hpp.

include_guard(GLOBAL)

set(PFH_SOBOL_GENERATOR_SCRIPT "${CMAKE_CURRENT_LIST_DIR}/GenerateSobolIdentity.cmake"
    CACHE INTERNAL "Build-time generator of the Sobol numeric identity")
set(PFH_SOBOL_CONTRACT_STRING "portable-sobol-v1"
    CACHE INTERNAL "Identity prefix of the Sobol numeric identity")

# Project-relative inventory. The header list is every project header that the four C++
# translation units include, directly or not; tests/test_sobol_identity.py recomputes the include
# closure with the compiler and fails when this list is incomplete.
set(_PFH_SOBOL_CXX_SOURCES
    src/core/sobol_engine.cpp
    src/core/sobol_identity.cpp
    src/core/sobol_mapper.cpp
    src/core/sobol_sampler.cpp)
# Shipped core translation units outside the four above that include numeric_build.hpp,
# portable_math.hpp or portable_grid.hpp, directly or through another project header, and so can
# supply the linker's copy of a shared inline helper. tests/test_sobol_identity.py recomputes the
# list from the include closure of every translation unit of the shipped targets and fails when a
# provider is missing here. Bound by source digest and generated compile command, whatever flags
# they carry.
set(_PFH_SOBOL_PROVIDER_SOURCES
    src/core/search_space.cpp
    src/core/tpe_sampler.cpp)
set(_PFH_SOBOL_HEADERS
    include/pineforge/hpo/error.hpp
    include/pineforge/hpo/sampler.hpp
    include/pineforge/hpo/search_space.hpp
    include/pineforge/hpo/sobol_identity.hpp
    include/pineforge/hpo/types.hpp
    src/core/numeric_build.hpp
    src/core/portable_grid.hpp
    src/core/portable_math.hpp
    src/core/sha256.hpp
    src/core/sobol_engine.hpp
    src/core/sobol_mapper.hpp
    src/core/sobol_sampler.hpp
    src/core/sobol_table_joe_kuo_d6_1024.inc
    src/core/tpe_algorithm.hpp
    third_party/core_math/log/dint.h
    third_party/core_math/log1p/dint.h
    third_party/core_math/portable.h)

# Writes the ingredients file. With CMake 3.19 or newer it runs at the end of the top-level
# directory, after every later change to flags, properties, sources and launchers.
function(_pfh_sobol_emit_ingredients ingredients_path core math directory)
    get_property(rule_launch_global GLOBAL PROPERTY RULE_LAUNCH_COMPILE)
    get_property(rule_launch_directory DIRECTORY "${directory}" PROPERTY RULE_LAUNCH_COMPILE)
    set(database_enabled OFF)
    if(CMAKE_EXPORT_COMPILE_COMMANDS)
        set(database_enabled ON)
    endif()
    set(content "")
    foreach(pair IN LISTS ARGN)
        string(APPEND content "${pair}\n")
    endforeach()
    # Every source of the math target as it stands now, resolved against its own directory.
    get_target_property(math_sources ${math} SOURCES)
    get_target_property(math_directory ${math} SOURCE_DIR)
    foreach(math_source IN LISTS math_sources)
        get_filename_component(math_source "${math_source}" ABSOLUTE BASE_DIR "${math_directory}")
        string(APPEND content "source.c=${math_source}\n")
    endforeach()
    string(APPEND content
        "database_enabled=${database_enabled}\n"
        "rule_launch.global=${rule_launch_global}\n"
        "rule_launch.directory=${rule_launch_directory}\n"
        "launcher.core=$<TARGET_PROPERTY:${core},CXX_COMPILER_LAUNCHER>\n"
        "launcher.math=$<TARGET_PROPERTY:${math},C_COMPILER_LAUNCHER>\n"
        "rule_launch.core=$<TARGET_PROPERTY:${core},RULE_LAUNCH_COMPILE>\n"
        "rule_launch.math=$<TARGET_PROPERTY:${math},RULE_LAUNCH_COMPILE>\n")
    file(GENERATE OUTPUT "${ingredients_path}" CONTENT "${content}")
endfunction()

function(_pfh_sobol_list_hazard out text)
    string(FIND "${text}" ";" semicolon_at)
    string(FIND "${text}" "[" open_at)
    string(FIND "${text}" "]" close_at)
    if(semicolon_at EQUAL -1 AND open_at EQUAL -1 AND close_at EQUAL -1)
        set(${out} OFF PARENT_SCOPE)
    else()
        set(${out} ON PARENT_SCOPE)
    endif()
endfunction()

function(pfh_sobol_identity)
    cmake_parse_arguments(SI "" "TARGET;PORTABLE_MATH_TARGET;SOURCE_ROOT;OUTPUT_DIR"
        "CONSUMERS;SOURCE_OPTIONS" ${ARGN})
    if(SI_UNPARSED_ARGUMENTS OR SI_KEYWORDS_MISSING_VALUES)
        message(FATAL_ERROR "pfh_sobol_identity: unrecognised or valueless arguments: "
            "${SI_UNPARSED_ARGUMENTS} ${SI_KEYWORDS_MISSING_VALUES}")
    endif()
    foreach(required IN ITEMS TARGET PORTABLE_MATH_TARGET)
        if(NOT SI_${required})
            message(FATAL_ERROR "pfh_sobol_identity: ${required} is required")
        endif()
        if(NOT TARGET "${SI_${required}}")
            message(FATAL_ERROR "pfh_sobol_identity: target '${SI_${required}}' does not exist")
        endif()
    endforeach()
    if(NOT SI_SOURCE_OPTIONS)
        set(SI_SOURCE_OPTIONS -fno-fast-math -ffp-contract=off -frounding-math -fno-builtin
            -fno-lto)
    endif()

    if(NOT SI_SOURCE_ROOT)
        set(SI_SOURCE_ROOT "${PROJECT_SOURCE_DIR}")
    endif()
    get_filename_component(source_root "${SI_SOURCE_ROOT}" ABSOLUTE)
    if(NOT SI_OUTPUT_DIR)
        set(SI_OUTPUT_DIR "${CMAKE_CURRENT_BINARY_DIR}/generated/sobol_identity")
    endif()
    get_filename_component(output_directory "${SI_OUTPUT_DIR}" ABSOLUTE)
    get_filename_component(build_root "${CMAKE_BINARY_DIR}" ABSOLUTE)
    foreach(directory_path IN ITEMS "${source_root}" "${build_root}" "${output_directory}")
        _pfh_sobol_list_hazard(hazard "${directory_path}")
        if(hazard)
            message(FATAL_ERROR "pfh_sobol_identity: brackets and semicolons in the source, "
                "build or output directory are not supported")
        endif()
    endforeach()

    # Inventory: absolute paths, all present, none generated by this identity.
    set(cxx_sources "")
    set(provider_sources "")
    set(headers "")
    foreach(kind IN ITEMS CXX_SOURCES PROVIDER_SOURCES HEADERS)
        foreach(relative IN LISTS _PFH_SOBOL_${kind})
            get_filename_component(absolute "${source_root}/${relative}" ABSOLUTE)
            get_filename_component(file_name "${absolute}" NAME)
            string(FIND "${absolute}" "${output_directory}/" inside_output)
            if(inside_output EQUAL 0 OR file_name MATCHES "^sobol_identity_generated\\.")
                message(FATAL_ERROR "pfh_sobol_identity: '${file_name}' is generated by this "
                    "identity; hashing it would make the identity circular")
            endif()
            if(NOT EXISTS "${absolute}")
                message(FATAL_ERROR "pfh_sobol_identity: '${relative}' does not exist")
            endif()
            if(kind STREQUAL "CXX_SOURCES")
                list(APPEND cxx_sources "${absolute}")
            elseif(kind STREQUAL "PROVIDER_SOURCES")
                list(APPEND provider_sources "${absolute}")
            else()
                list(APPEND headers "${absolute}")
            endif()
        endforeach()
    endforeach()

    # Every bound C++ source must be compiled by TARGET, or its command cannot be found.
    get_target_property(target_sources ${SI_TARGET} SOURCES)
    get_target_property(target_directory ${SI_TARGET} SOURCE_DIR)
    set(target_absolute "")
    foreach(target_source IN LISTS target_sources)
        get_filename_component(target_source "${target_source}" ABSOLUTE
            BASE_DIR "${target_directory}")
        list(APPEND target_absolute "${target_source}")
    endforeach()
    foreach(source IN LISTS cxx_sources)
        list(FIND target_absolute "${source}" found)
        if(found EQUAL -1)
            message(FATAL_ERROR "pfh_sobol_identity: '${source}' is not a source of target "
                "${SI_TARGET}; add every Sobol source to it before this call")
        endif()
    endforeach()
    # A provider that the target does not compile has no command to bind. It is a project fact
    # (the inline helpers are emitted by that unit), not a build setting, so it is an error.
    foreach(source IN LISTS provider_sources)
        list(FIND target_absolute "${source}" found)
        if(found EQUAL -1)
            message(FATAL_ERROR "pfh_sobol_identity: the shared-helper provider '${source}' is "
                "not a source of target ${SI_TARGET}")
        endif()
    endforeach()

    # Per-source properties only. The generated header reaches sobol_identity.cpp alone.
    set(configuration "$<IF:$<BOOL:$<CONFIG>>,$<CONFIG>,none>")
    foreach(source IN LISTS cxx_sources)
        set_property(SOURCE "${source}" APPEND PROPERTY COMPILE_OPTIONS ${SI_SOURCE_OPTIONS})
        set_property(SOURCE "${source}" PROPERTY SKIP_UNITY_BUILD_INCLUSION ON)
        set_property(SOURCE "${source}" PROPERTY SKIP_PRECOMPILE_HEADERS ON)
    endforeach()
    set_property(SOURCE "${source_root}/src/core/sobol_identity.cpp" APPEND PROPERTY
        INCLUDE_DIRECTORIES "${output_directory}/${configuration}")

    # The compilation database is the byte custody of the generated commands. An empty value
    # counts as undefined (CMake 3.28.3 creates the cache entry empty and DEFINED is true for
    # it); FORCE only in that branch, so an explicit OFF or ON is never overwritten.
    if(NOT DEFINED CMAKE_EXPORT_COMPILE_COMMANDS OR "${CMAKE_EXPORT_COMPILE_COMMANDS}" STREQUAL "")
        set(CMAKE_EXPORT_COMPILE_COMMANDS ON CACHE BOOL
            "Write compile_commands.json (read by the build identities)" FORCE)
    endif()
    # Since CMake 3.20 a target's EXPORT_COMPILE_COMMANDS is initialized from the variable when
    # the target is CREATED. The core and math targets exist before this call, so they kept the
    # empty value and compile_commands.json would not list their sources: switching the variable
    # on is not enough. The database must list the sources of exactly these two targets. The
    # property is set whenever the database is on, not only in the branch above: in the integrated
    # root the return-statistics helper runs first and has already switched the variable on, so
    # this helper never enters that branch while its math target still has the empty value.
    # An explicit OFF leaves both targets untouched.
    if(CMAKE_EXPORT_COMPILE_COMMANDS)
        set_property(TARGET ${SI_TARGET} ${SI_PORTABLE_MATH_TARGET} PROPERTY EXPORT_COMPILE_COMMANDS ON)
    endif()
    get_property(multi_config GLOBAL PROPERTY GENERATOR_IS_MULTI_CONFIG)
    if(multi_config)
        set(multi_config ON)
    else()
        set(multi_config OFF)
    endif()

    set(ingredients_path
        "${output_directory}/${configuration}/sobol_identity.ingredients.txt")
    set(pairs
        "contract=${PFH_SOBOL_CONTRACT_STRING}"
        "configuration=${configuration}"
        "core.target=${SI_TARGET}"
        "math.target=${SI_PORTABLE_MATH_TARGET}"
        "cxx.path=${CMAKE_CXX_COMPILER}"
        "cxx.arg1=${CMAKE_CXX_COMPILER_ARG1}"
        "cxx.id=${CMAKE_CXX_COMPILER_ID}"
        "cxx.version=${CMAKE_CXX_COMPILER_VERSION}"
        "c.path=${CMAKE_C_COMPILER}"
        "c.arg1=${CMAKE_C_COMPILER_ARG1}"
        "c.id=${CMAKE_C_COMPILER_ID}"
        "c.version=${CMAKE_C_COMPILER_VERSION}"
        "system.name=${CMAKE_SYSTEM_NAME}"
        "system.processor=${CMAKE_SYSTEM_PROCESSOR}"
        "generator=${CMAKE_GENERATOR}"
        "multi_config=${multi_config}"
        "database=${build_root}/compile_commands.json"
        "source.root=${source_root}"
        "build.root=${build_root}")
    foreach(path IN LISTS cxx_sources)
        list(APPEND pairs "source.cxx=${path}")
    endforeach()
    foreach(path IN LISTS provider_sources)
        list(APPEND pairs "source.provider=${path}")
    endforeach()
    foreach(path IN LISTS headers)
        list(APPEND pairs "header=${path}")
    endforeach()
    if(CMAKE_VERSION VERSION_LESS 3.19)
        _pfh_sobol_emit_ingredients("${ingredients_path}" "${SI_TARGET}"
            "${SI_PORTABLE_MATH_TARGET}" "${CMAKE_CURRENT_SOURCE_DIR}" ${pairs})
    else()
        # The arguments of a deferred call are evaluated when it runs, at the end of the top-level
        # directory, where the variables of this function no longer exist: written as
        # "${SI_TARGET}" they reach the writer empty. EVAL expands them here instead, and the
        # bracket arguments keep the deferred call from expanding them a second time. The
        # writer receives the same strings as in the direct call above.
        set(deferred_call "cmake_language(DEFER DIRECTORY [==[${CMAKE_SOURCE_DIR}]==] CALL")
        string(APPEND deferred_call " _pfh_sobol_emit_ingredients [==[${ingredients_path}]==]")
        string(APPEND deferred_call " [==[${SI_TARGET}]==] [==[${SI_PORTABLE_MATH_TARGET}]==]")
        string(APPEND deferred_call " [==[${CMAKE_CURRENT_SOURCE_DIR}]==]")
        foreach(pair IN LISTS pairs)
            string(APPEND deferred_call " [==[${pair}]==]")
        endforeach()
        cmake_language(EVAL CODE "${deferred_call})")
    endif()

    set(identity_target "${SI_TARGET}_sobol_identity")
    add_custom_target(${identity_target}
        COMMAND "${CMAKE_COMMAND}"
            "-DPFH_SOBOL_INGREDIENTS=${ingredients_path}"
            "-DPFH_SOBOL_OUTPUT_DIR=${output_directory}/${configuration}"
            "-DPFH_SOBOL_CONTRACT=${PFH_SOBOL_CONTRACT_STRING}"
            -P "${PFH_SOBOL_GENERATOR_SCRIPT}"
        COMMENT "Binding the Sobol numeric build identity"
        VERBATIM)
    # TARGET compiles sobol_identity.cpp, which includes the generated header.
    add_dependencies(${SI_TARGET} ${identity_target})
    foreach(consumer IN LISTS SI_CONSUMERS)
        add_dependencies(${consumer} ${identity_target})
    endforeach()

    set(PFH_SOBOL_IDENTITY_TARGET "${identity_target}" PARENT_SCOPE)
    set(PFH_SOBOL_IDENTITY_DIRECTORY "${output_directory}/${configuration}" PARENT_SCOPE)
endfunction()
