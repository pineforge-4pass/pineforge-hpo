# Return-statistics build identity.
#
# Binds the reducer sources, the compiler and the compile command that the build system
# generated for the reducer translation units into one opaque identity that no sampler
# checkpoint shares. Rules, fields and limits: docs/internal/return-stats-identity.md
#
#   pfh_return_stats_identity(
#       TARGET <target>                   target that compiles the reducer sources
#       SOURCES <file>...                 reducer translation units (hashed and bound)
#       [HEADERS <file>...]               headers whose bytes change the reducer
#       CONTRACT <contract>               statistics contract string
#       [SOURCE_OPTIONS <option>...]      options added to the SOURCES only
#       [SOURCE_DEFINITIONS <define>...]  definitions added to the SOURCES only
#       [CONSUMERS <target>...]           targets that include return_stats_identity.hpp
#       [SOURCE_ROOT <dir>] [OUTPUT_DIR <dir>])
#
# The identity is not computed from a model of CMake's flag assembly. At build time the generator
# script reads the entry of the JSON compilation database for each reducer source, so options of
# the target, of linked targets (usage requirements), of the source, the configuration flags and
# everything injected by CMake are bound exactly as generated, including changes made after this
# call. What cannot be bound exactly (multi-configuration generators, compiler launchers or
# wrappers, response files, an unreadable or ambiguous entry, a pre-3.19 CMake) makes the
# capability unbound: the build still succeeds, the identity is empty and the reason is recorded.
#
# Only per-source properties of SOURCES are written, plus the compile-commands export: the cache
# variable CMAKE_EXPORT_COMPILE_COMMANDS when the project left it undefined, and the per-target
# EXPORT_COMPILE_COMMANDS property of TARGET whenever the database is on. Each consumer also gets
# a dependency on the generation target and the per-configuration output directory as a private
# include directory. Target-wide options,
# target-wide definitions, CMAKE_CXX_FLAGS and the checked-in numeric flag recipe are never
# modified, so the TPE checkpoint identity cannot move.
#
# Result variables (caller scope): PFH_RETURN_STATS_IDENTITY_TARGET is the generation target and
# PFH_RETURN_STATS_IDENTITY_DIRECTORY is the per-configuration output directory (contains a
# generator expression). The directory holds return_stats_identity.descriptor.txt (the exact
# descriptor), return_stats_identity.txt (the identity) and return_stats_identity_generated.hpp.

include_guard(GLOBAL)

set(PFH_RSI_GENERATOR_SCRIPT "${CMAKE_CURRENT_LIST_DIR}/GenerateReturnStatsIdentity.cmake"
    CACHE INTERNAL "Build-time generator of the return-statistics identity")

# Writes the ingredients file. With CMake 3.19 or newer it runs at the end of the top-level
# directory, after every later change to flags, properties and launchers; the generator
# expressions in it are evaluated at generate time in any case.
function(_pfh_rsi_emit_ingredients ingredients_path target directory)
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
    string(APPEND content
        "database_enabled=${database_enabled}\n"
        "rule_launch.global=${rule_launch_global}\n"
        "rule_launch.directory=${rule_launch_directory}\n"
        "launcher.target=$<TARGET_PROPERTY:${target},CXX_COMPILER_LAUNCHER>\n"
        "rule_launch.target=$<TARGET_PROPERTY:${target},RULE_LAUNCH_COMPILE>\n")
    file(GENERATE OUTPUT "${ingredients_path}" CONTENT "${content}")
endfunction()

# Characters that split or bracket CMake list elements are refused everywhere the identity
# passes text through lists.
function(_pfh_rsi_list_hazard out text)
    string(FIND "${text}" ";" semicolon_at)
    string(FIND "${text}" "[" open_at)
    string(FIND "${text}" "]" close_at)
    if(semicolon_at EQUAL -1 AND open_at EQUAL -1 AND close_at EQUAL -1)
        set(${out} OFF PARENT_SCOPE)
    else()
        set(${out} ON PARENT_SCOPE)
    endif()
endfunction()

function(pfh_return_stats_identity)
    cmake_parse_arguments(RSI ""
        "TARGET;CONTRACT;SOURCE_ROOT;OUTPUT_DIR"
        "SOURCES;HEADERS;SOURCE_OPTIONS;SOURCE_DEFINITIONS;CONSUMERS" ${ARGN})
    if(RSI_UNPARSED_ARGUMENTS OR RSI_KEYWORDS_MISSING_VALUES)
        message(FATAL_ERROR "pfh_return_stats_identity: unrecognised or valueless arguments: "
            "${RSI_UNPARSED_ARGUMENTS} ${RSI_KEYWORDS_MISSING_VALUES}")
    endif()
    foreach(required IN ITEMS TARGET CONTRACT SOURCES)
        if(NOT RSI_${required})
            message(FATAL_ERROR "pfh_return_stats_identity: ${required} is required")
        endif()
    endforeach()
    if(NOT TARGET "${RSI_TARGET}")
        message(FATAL_ERROR "pfh_return_stats_identity: target '${RSI_TARGET}' does not exist")
    endif()
    if(NOT "${RSI_CONTRACT}" MATCHES "^[A-Za-z0-9._/:+-]+$")
        message(FATAL_ERROR
            "pfh_return_stats_identity: CONTRACT may only contain letters, digits and ._/:+-")
    endif()

    # Paths: sources are bound by project-relative name and content, never by location.
    if(NOT RSI_SOURCE_ROOT)
        set(RSI_SOURCE_ROOT "${PROJECT_SOURCE_DIR}")
    endif()
    get_filename_component(source_root "${RSI_SOURCE_ROOT}" ABSOLUTE)
    if(NOT RSI_OUTPUT_DIR)
        set(RSI_OUTPUT_DIR "${CMAKE_CURRENT_BINARY_DIR}/generated/return_stats_identity")
    endif()
    get_filename_component(output_directory "${RSI_OUTPUT_DIR}" ABSOLUTE)
    get_filename_component(build_root "${CMAKE_BINARY_DIR}" ABSOLUTE)

    set(sources "")
    set(headers "")
    foreach(kind IN ITEMS SOURCES HEADERS)
        foreach(path IN LISTS RSI_${kind})
            get_filename_component(absolute "${path}" ABSOLUTE)
            string(FIND "${absolute}" "${output_directory}/" inside_output)
            get_filename_component(file_name "${absolute}" NAME)
            if(inside_output EQUAL 0 OR file_name MATCHES "^return_stats_identity_generated\\.")
                message(FATAL_ERROR "pfh_return_stats_identity: '${file_name}' is generated by "
                    "this identity; hashing it would make the identity circular")
            endif()
            if(NOT EXISTS "${absolute}")
                message(FATAL_ERROR "pfh_return_stats_identity: '${path}' does not exist")
            endif()
            _pfh_rsi_list_hazard(hazard "${absolute}")
            if(hazard)
                message(FATAL_ERROR "pfh_return_stats_identity: brackets and semicolons in "
                    "'${file_name}' are not supported")
            endif()
            if(kind STREQUAL "SOURCES")
                list(APPEND sources "${absolute}")
            else()
                list(APPEND headers "${absolute}")
            endif()
        endforeach()
    endforeach()
    foreach(directory_path IN ITEMS "${source_root}" "${build_root}" "${output_directory}")
        _pfh_rsi_list_hazard(hazard "${directory_path}")
        if(hazard)
            message(FATAL_ERROR "pfh_return_stats_identity: brackets and semicolons in the "
                "source, build or output directory are not supported")
        endif()
    endforeach()

    # Per-source options and definitions. Flags the sources already carry, flags added later and
    # usage requirements all reach the generated command and are bound from there.
    if(RSI_SOURCE_OPTIONS)
        set_property(SOURCE ${sources} APPEND PROPERTY COMPILE_OPTIONS ${RSI_SOURCE_OPTIONS})
    endif()
    if(RSI_SOURCE_DEFINITIONS)
        set_property(SOURCE ${sources} APPEND PROPERTY COMPILE_DEFINITIONS
            ${RSI_SOURCE_DEFINITIONS})
    endif()
    # A unity file or a precompiled header would take the reducer out of its own command line.
    set_property(SOURCE ${sources} PROPERTY SKIP_UNITY_BUILD_INCLUSION ON)
    set_property(SOURCE ${sources} PROPERTY SKIP_PRECOMPILE_HEADERS ON)

    # The compilation database is the byte custody of the generated command. A project that
    # left the switch undefined gets it on; an explicit OFF is respected and leaves the
    # capability unbound. An EMPTY value counts as undefined: CMake 3.28.3 creates the cache
    # entry with an empty value before the project runs, and DEFINED is true for it. FORCE is
    # needed because a plain set(CACHE) does not overwrite an existing entry; it applies only
    # to the empty value, so an explicit OFF or ON is never overwritten.
    if(NOT DEFINED CMAKE_EXPORT_COMPILE_COMMANDS OR "${CMAKE_EXPORT_COMPILE_COMMANDS}" STREQUAL "")
        set(CMAKE_EXPORT_COMPILE_COMMANDS ON CACHE BOOL
            "Write compile_commands.json (read by the return-statistics build identity)" FORCE)
    endif()
    # Since CMake 3.20 a target's EXPORT_COMPILE_COMMANDS is initialized from the variable when
    # the target is CREATED. The reducer target exists before this call, so it kept the empty
    # value and compile_commands.json would not list it: switching the variable on is not enough.
    # Whenever the database is on (default, or an earlier helper or the project switched it on),
    # the target is listed; an explicit OFF leaves the target untouched.
    if(CMAKE_EXPORT_COMPILE_COMMANDS)
        set_property(TARGET ${RSI_TARGET} PROPERTY EXPORT_COMPILE_COMMANDS ON)
    endif()

    get_property(multi_config GLOBAL PROPERTY GENERATOR_IS_MULTI_CONFIG)
    if(multi_config)
        set(multi_config ON)
    else()
        set(multi_config OFF)
    endif()
    # The same expression names the configuration in every path and in the content.
    set(configuration "$<IF:$<BOOL:$<CONFIG>>,$<CONFIG>,none>")
    set(ingredients_path
        "${output_directory}/${configuration}/return_stats_identity.ingredients.txt")

    set(pairs
        "contract=${RSI_CONTRACT}"
        "configuration=${configuration}"
        "target=${RSI_TARGET}"
        "compiler.path=${CMAKE_CXX_COMPILER}"
        "compiler.arg1=${CMAKE_CXX_COMPILER_ARG1}"
        "compiler.id=${CMAKE_CXX_COMPILER_ID}"
        "compiler.version=${CMAKE_CXX_COMPILER_VERSION}"
        "system.name=${CMAKE_SYSTEM_NAME}"
        "system.processor=${CMAKE_SYSTEM_PROCESSOR}"
        "generator=${CMAKE_GENERATOR}"
        "multi_config=${multi_config}"
        "database=${build_root}/compile_commands.json"
        "source.root=${source_root}"
        "build.root=${build_root}")
    foreach(path IN LISTS sources)
        list(APPEND pairs "source=${path}")
    endforeach()
    foreach(path IN LISTS headers)
        list(APPEND pairs "header=${path}")
    endforeach()
    if(CMAKE_VERSION VERSION_LESS 3.19)
        _pfh_rsi_emit_ingredients("${ingredients_path}" "${RSI_TARGET}"
            "${CMAKE_CURRENT_SOURCE_DIR}" ${pairs})
    else()
        # The arguments of a deferred call are evaluated when it runs, at the end of the top-level
        # directory, where the variables of this function no longer exist: written as
        # "${RSI_TARGET}" they reach the writer empty. EVAL expands them here instead, and the
        # bracket arguments keep the deferred call from expanding them a second time. The
        # writer receives the same strings as in the direct call above.
        set(deferred_call "cmake_language(DEFER DIRECTORY [==[${CMAKE_SOURCE_DIR}]==] CALL")
        string(APPEND deferred_call " _pfh_rsi_emit_ingredients [==[${ingredients_path}]==]")
        string(APPEND deferred_call " [==[${RSI_TARGET}]==] [==[${CMAKE_CURRENT_SOURCE_DIR}]==]")
        foreach(pair IN LISTS pairs)
            string(APPEND deferred_call " [==[${pair}]==]")
        endforeach()
        cmake_language(EVAL CODE "${deferred_call})")
    endif()

    set(identity_target "${RSI_TARGET}_return_stats_identity")
    add_custom_target(${identity_target}
        COMMAND "${CMAKE_COMMAND}"
            "-DPFH_RSI_INGREDIENTS=${ingredients_path}"
            "-DPFH_RSI_OUTPUT_DIR=${output_directory}/${configuration}"
            "-DPFH_RSI_CONTRACT=${RSI_CONTRACT}"
            -P "${PFH_RSI_GENERATOR_SCRIPT}"
        COMMENT "Binding the return-statistics build identity"
        VERBATIM)
    foreach(consumer IN LISTS RSI_CONSUMERS)
        add_dependencies(${consumer} ${identity_target})
        target_include_directories(${consumer} PRIVATE "${output_directory}/${configuration}")
    endforeach()

    set(PFH_RETURN_STATS_IDENTITY_TARGET "${identity_target}" PARENT_SCOPE)
    set(PFH_RETURN_STATS_IDENTITY_DIRECTORY "${output_directory}/${configuration}" PARENT_SCOPE)
endfunction()
