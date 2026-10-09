# Return-statistics build identity.
#
# Binds the reducer sources, the compiler and the actual translation-unit flags into one opaque
# identity that no sampler checkpoint shares. Fields and limits:
# docs/internal/return-stats-identity.md
#
#   pfh_return_stats_identity(
#       TARGET <target>                   target that compiles the reducer sources
#       SOURCES <file>...                 reducer translation units (hashed and flag-bound)
#       [HEADERS <file>...]               headers whose bytes change the reducer
#       CONTRACT <contract>               statistics contract string
#       [SOURCE_OPTIONS <option>...]      per-source options: applied to SOURCES and bound
#       [SOURCE_DEFINITIONS <define>...]  per-source definitions: applied to SOURCES and bound
#       [CONSUMERS <target>...]           targets that include return_stats_identity.hpp
#       [COMPILER_ID <id>] [COMPILER_VERSION <version>] [COMPILER_BANNER <text>]
#       [SOURCE_ROOT <dir>] [OUTPUT_DIR <dir>])
#
# Call it after the reducer sources joined TARGET and after the last change to CMAKE_CXX_FLAGS
# and its per-configuration variants, and leave the properties of SOURCES alone afterwards:
# those are read now, and later changes are invisible to it (the compile-command check in
# tests/test_return_stats_identity.py catches them). Options and definitions of TARGET are read
# when the build system is generated, so they may still change after the call. Only per-source
# properties of SOURCES are written. Target-wide options, target-wide definitions,
# CMAKE_CXX_FLAGS and the checked-in numeric flag recipe are never modified, so the TPE
# checkpoint identity cannot move.
#
# Result variables (caller scope): PFH_RETURN_STATS_IDENTITY_TARGET is the generation target and
# PFH_RETURN_STATS_IDENTITY_DIRECTORY is the per-configuration output directory (contains a
# generator expression). The directory holds return_stats_identity.descriptor.txt (the exact
# descriptor), return_stats_identity.txt (the identity) and return_stats_identity_generated.hpp.

include_guard(GLOBAL)

set(PFH_RSI_GENERATOR_SCRIPT "${CMAKE_CURRENT_LIST_DIR}/GenerateReturnStatsIdentity.cmake"
    CACHE INTERNAL "Build-time generator of the return-statistics identity")

function(pfh_return_stats_identity)
    cmake_parse_arguments(RSI ""
        "TARGET;CONTRACT;COMPILER_ID;COMPILER_VERSION;COMPILER_BANNER;SOURCE_ROOT;OUTPUT_DIR"
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
    if(NOT CMAKE_CONFIGURATION_TYPES AND NOT CMAKE_BUILD_TYPE)
        message(FATAL_ERROR "pfh_return_stats_identity: the identity binds the build "
            "configuration, so CMAKE_BUILD_TYPE (single-config) or CMAKE_CONFIGURATION_TYPES "
            "must be set")
    endif()

    # Compiler identity: explicit arguments win over what CMake detected.
    if(NOT RSI_COMPILER_ID)
        set(RSI_COMPILER_ID "${CMAKE_CXX_COMPILER_ID}")
    endif()
    if(NOT RSI_COMPILER_VERSION)
        set(RSI_COMPILER_VERSION "${CMAKE_CXX_COMPILER_VERSION}")
    endif()
    if(NOT RSI_COMPILER_ID OR NOT RSI_COMPILER_VERSION)
        message(FATAL_ERROR "pfh_return_stats_identity: the C++ compiler identity or version is "
            "unknown; pass COMPILER_ID and COMPILER_VERSION")
    endif()
    # The first banner line separates distribution builds that share a version number. It is
    # bound verbatim: the same compiler reached through another command name binds differently.
    if(NOT RSI_COMPILER_BANNER)
        set(RSI_COMPILER_BANNER "unavailable")
        execute_process(COMMAND "${CMAKE_CXX_COMPILER}" --version
            RESULT_VARIABLE banner_status OUTPUT_VARIABLE banner_text ERROR_QUIET
            OUTPUT_STRIP_TRAILING_WHITESPACE)
        if(banner_status EQUAL 0 AND banner_text)
            string(FIND "${banner_text}" "\n" newline_at)
            if(newline_at EQUAL -1)
                set(RSI_COMPILER_BANNER "${banner_text}")
            else()
                string(SUBSTRING "${banner_text}" 0 ${newline_at} RSI_COMPILER_BANNER)
            endif()
        endif()
    endif()
    string(REPLACE "\r" "" RSI_COMPILER_BANNER "${RSI_COMPILER_BANNER}")
    string(REPLACE ";" "," RSI_COMPILER_BANNER "${RSI_COMPILER_BANNER}")
    # The compiler's target triple separates architectures that share a compiler version.
    set(compiler_target "unavailable")
    execute_process(COMMAND "${CMAKE_CXX_COMPILER}" -dumpmachine
        RESULT_VARIABLE target_status OUTPUT_VARIABLE target_text ERROR_QUIET
        OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(target_status EQUAL 0 AND target_text)
        string(REPLACE "\n" "" compiler_target "${target_text}")
        string(REPLACE ";" "," compiler_target "${compiler_target}")
    endif()
    string(REPLACE ";" "," osx_architectures "${CMAKE_OSX_ARCHITECTURES}")

    # Paths: sources are bound by project-relative name and content, never by location.
    if(NOT RSI_SOURCE_ROOT)
        set(RSI_SOURCE_ROOT "${PROJECT_SOURCE_DIR}")
    endif()
    get_filename_component(source_root "${RSI_SOURCE_ROOT}" ABSOLUTE)
    if(NOT RSI_OUTPUT_DIR)
        set(RSI_OUTPUT_DIR "${CMAKE_CURRENT_BINARY_DIR}/generated/return_stats_identity")
    endif()
    get_filename_component(output_directory "${RSI_OUTPUT_DIR}" ABSOLUTE)

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
            string(FIND "${absolute}" "[" bracket_open)
            string(FIND "${absolute}" "]" bracket_close)
            if(NOT bracket_open EQUAL -1 OR NOT bracket_close EQUAL -1)
                message(FATAL_ERROR
                    "pfh_return_stats_identity: brackets in '${file_name}' are not supported")
            endif()
            if(kind STREQUAL "SOURCES")
                list(APPEND sources "${absolute}")
            else()
                list(APPEND headers "${absolute}")
            endif()
        endforeach()
    endforeach()

    # Every flag of a reducer translation unit must pass through this call, otherwise the
    # identity could not know about it.
    foreach(path IN LISTS sources)
        foreach(property IN ITEMS COMPILE_FLAGS COMPILE_OPTIONS COMPILE_DEFINITIONS)
            get_source_file_property(existing "${path}" ${property})
            if(existing)
                get_filename_component(file_name "${path}" NAME)
                message(FATAL_ERROR "pfh_return_stats_identity: '${file_name}' already has "
                    "${property}; pass per-source flags through SOURCE_OPTIONS and "
                    "SOURCE_DEFINITIONS so they are bound")
            endif()
        endforeach()
    endforeach()
    if(RSI_SOURCE_OPTIONS)
        set_property(SOURCE ${sources} APPEND PROPERTY COMPILE_OPTIONS ${RSI_SOURCE_OPTIONS})
    endif()
    if(RSI_SOURCE_DEFINITIONS)
        set_property(SOURCE ${sources} APPEND PROPERTY COMPILE_DEFINITIONS
            ${RSI_SOURCE_DEFINITIONS})
    endif()
    # A unity or precompiled-header build would change the command line of these units.
    set_property(SOURCE ${sources} PROPERTY SKIP_UNITY_BUILD_INCLUSION ON)
    set_property(SOURCE ${sources} PROPERTY SKIP_PRECOMPILE_HEADERS ON)

    # Configuration flags use the same shape as the checked-in numeric flag recipe.
    set(configurations ${CMAKE_BUILD_TYPE} ${CMAKE_CONFIGURATION_TYPES})
    list(REMOVE_DUPLICATES configurations)
    set(configuration_flags "")
    foreach(configuration IN LISTS configurations)
        string(TOUPPER "${configuration}" configuration_upper)
        string(APPEND configuration_flags
            "$<$<CONFIG:${configuration}>:${CMAKE_CXX_FLAGS_${configuration_upper}}>")
    endforeach()

    set(target_property_prefix "$<TARGET_PROPERTY:${RSI_TARGET},")
    set(ingredients "")
    string(APPEND ingredients
        "contract=${RSI_CONTRACT}\n"
        "configuration=$<CONFIG>\n"
        "compiler.id=${RSI_COMPILER_ID}\n"
        "compiler.version=${RSI_COMPILER_VERSION}\n"
        "compiler.banner=${RSI_COMPILER_BANNER}\n"
        "compiler.target=${compiler_target}\n"
        "system.name=${CMAKE_SYSTEM_NAME}\n"
        "system.processor=${CMAKE_SYSTEM_PROCESSOR}\n"
        "osx.architectures=${osx_architectures}\n"
        "osx.deployment_target=${CMAKE_OSX_DEPLOYMENT_TARGET}\n"
        "language.standard=${target_property_prefix}CXX_STANDARD>\n"
        "language.extensions=${target_property_prefix}CXX_EXTENSIONS>\n"
        "position.independent=${target_property_prefix}POSITION_INDEPENDENT_CODE>\n"
        "interprocedural=${target_property_prefix}INTERPROCEDURAL_OPTIMIZATION>\n"
        "flags.cxx=${CMAKE_CXX_FLAGS}\n"
        "flags.config=${configuration_flags}\n"
        "flags.target=$<JOIN:${target_property_prefix}COMPILE_OPTIONS>,\nflags.target=>\n"
        "define.target=$<JOIN:${target_property_prefix}COMPILE_DEFINITIONS>,\ndefine.target=>\n")
    foreach(option IN LISTS RSI_SOURCE_OPTIONS)
        string(APPEND ingredients "flags.source=${option}\n")
    endforeach()
    foreach(definition IN LISTS RSI_SOURCE_DEFINITIONS)
        string(APPEND ingredients "define.source=${definition}\n")
    endforeach()
    foreach(path IN LISTS sources)
        string(APPEND ingredients "source=${path}\n")
    endforeach()
    foreach(path IN LISTS headers)
        string(APPEND ingredients "header=${path}\n")
    endforeach()
    string(APPEND ingredients "source.root=${source_root}\n")

    # The language directory mirrors the numeric flag recipe: options written with
    # COMPILE_LANGUAGE conditions evaluate for the language of the generated file, and only
    # the CXX file is consumed.
    file(GENERATE
        OUTPUT "${output_directory}/$<CONFIG>/$<COMPILE_LANGUAGE>/return_stats_identity.ingredients.txt"
        CONTENT "${ingredients}")

    set(identity_target "${RSI_TARGET}_return_stats_identity")
    add_custom_target(${identity_target}
        COMMAND "${CMAKE_COMMAND}"
            "-DPFH_RSI_INGREDIENTS=${output_directory}/$<CONFIG>/CXX/return_stats_identity.ingredients.txt"
            "-DPFH_RSI_OUTPUT_DIR=${output_directory}/$<CONFIG>"
            -P "${PFH_RSI_GENERATOR_SCRIPT}"
        COMMENT "Binding the return-statistics build identity"
        VERBATIM)
    foreach(consumer IN LISTS RSI_CONSUMERS)
        add_dependencies(${consumer} ${identity_target})
        target_include_directories(${consumer} PRIVATE "${output_directory}/$<CONFIG>")
    endforeach()

    set(PFH_RETURN_STATS_IDENTITY_TARGET "${identity_target}" PARENT_SCOPE)
    set(PFH_RETURN_STATS_IDENTITY_DIRECTORY "${output_directory}/$<CONFIG>" PARENT_SCOPE)
endfunction()
