# Build-time half of pfh_return_stats_identity(); run with cmake -P before every build of a
# target that consumes the identity.
#
#   PFH_RSI_INGREDIENTS  file written by file(GENERATE) for the active configuration
#   PFH_RSI_OUTPUT_DIR   directory receiving the descriptor, the identity and the header
#   PFH_RSI_CONTRACT     contract string (also present in the ingredients)
#
# The identity binds the compile command that the build system generated for each reducer
# translation unit: the entry of the JSON compilation database, read here, not a model of how
# CMake would assemble it. The reducer files, the forced-include files and the compiler driver
# are hashed here, at build time, so an edit moves the identity without a reconfigure.
#
# A build that cannot be bound exactly is not bound approximately: the outputs then carry an
# unbound capability with a reason, an empty identity and the exact reason in the descriptor.
# This script never fails the build for a data-dependent condition. Outputs are rewritten only
# when their bytes change, and only the listed reducer files are inputs, so the generated header
# is never an input of its own identity.

cmake_minimum_required(VERSION 3.17)

foreach(variable IN ITEMS PFH_RSI_INGREDIENTS PFH_RSI_OUTPUT_DIR PFH_RSI_CONTRACT)
    if(NOT DEFINED ${variable})
        message(FATAL_ERROR "return-statistics identity: ${variable} is not set")
    endif()
endforeach()
if(NOT "${PFH_RSI_CONTRACT}" MATCHES "^[A-Za-z0-9._/:+-]+$")
    message(FATAL_ERROR "return-statistics identity: unsupported characters in the contract")
endif()

set(PFH_RSI_FORMAT "pineforge-hpo-return-stats-identity/v2")
set(PFH_RSI_IDENTITY_PREFIX "pineforge-hpo-return-stats-build/v2:sha256:")

# Leaves the calling function with the reason recorded in the script scope.
macro(pfh_rsi_refuse reason_text)
    set(PFH_RSI_REASON "${reason_text}" PARENT_SCOPE)
    return()
endmacro()

function(pfh_rsi_write_if_changed path content)
    if(EXISTS "${path}")
        file(READ "${path}" existing)
        if("${existing}" STREQUAL "${content}")
            return()
        endif()
    endif()
    file(WRITE "${path}" "${content}")
endfunction()

# Declared normalization of a path-valued option: relative paths are resolved against the
# command's directory, then a path inside the build tree or the source tree is rewritten
# relative to that root, so a moved tree keeps its identity. Any other path stays verbatim.
function(pfh_rsi_rewrite_path out value base_directory source_root build_root)
    if(NOT IS_ABSOLUTE "${value}")
        set(value "${base_directory}/${value}")
    endif()
    get_filename_component(value "${value}" ABSOLUTE)
    string(LENGTH "${value}" value_length)
    foreach(label IN ITEMS build src)
        if(label STREQUAL "build")
            set(root "${build_root}")
        else()
            set(root "${source_root}")
        endif()
        string(LENGTH "${root}" root_length)
        string(FIND "${value}/" "${root}/" at)
        if(at EQUAL 0)
            if(value_length LESS_EQUAL root_length)
                set(${out} "<${label}>" PARENT_SCOPE)
            else()
                math(EXPR start "${root_length} + 1")
                string(SUBSTRING "${value}" ${start} -1 rest)
                set(${out} "<${label}>/${rest}" PARENT_SCOPE)
            endif()
            return()
        endif()
    endforeach()
    set(${out} "${value}" PARENT_SCOPE)
endfunction()

function(pfh_rsi_bind)
    if(CMAKE_VERSION VERSION_LESS 3.19)
        pfh_rsi_refuse(cmake_below_3_19)
    endif()
    if(NOT EXISTS "${PFH_RSI_INGREDIENTS}")
        pfh_rsi_refuse(ingredients_missing)
    endif()
    file(READ "${PFH_RSI_INGREDIENTS}" text)
    # Lists are used below, so characters that would split or bracket list elements are refused.
    string(FIND "${text}" ";" at)
    if(NOT at EQUAL -1)
        pfh_rsi_refuse(unsupported_character)
    endif()
    string(FIND "${text}" "[" at)
    if(NOT at EQUAL -1)
        pfh_rsi_refuse(unsupported_character)
    endif()
    string(FIND "${text}" "]" at)
    if(NOT at EQUAL -1)
        pfh_rsi_refuse(unsupported_character)
    endif()

    set(sources "")
    set(headers "")
    string(REPLACE "\n" ";" lines "${text}")
    foreach(line IN LISTS lines)
        if("${line}" STREQUAL "")
            continue()
        endif()
        if("${line}" MATCHES "^([a-z][a-z0-9_.]*)=(.*)$")
            set(key "${CMAKE_MATCH_1}")
            set(value "${CMAKE_MATCH_2}")
            if(key STREQUAL "source")
                list(APPEND sources "${value}")
            elseif(key STREQUAL "header")
                list(APPEND headers "${value}")
            else()
                set(in_${key} "${value}")
            endif()
        else()
            pfh_rsi_refuse(ingredients_malformed)
        endif()
    endforeach()
    foreach(key IN ITEMS contract configuration target compiler.path compiler.id
            compiler.version system.name system.processor generator database source.root
            build.root)
        if("${in_${key}}" STREQUAL "")
            pfh_rsi_refuse(ingredients_incomplete)
        endif()
    endforeach()
    if(NOT "${in_contract}" STREQUAL "${PFH_RSI_CONTRACT}")
        pfh_rsi_refuse(contract_mismatch)
    endif()
    if(NOT sources)
        pfh_rsi_refuse(ingredients_incomplete)
    endif()

    # Capabilities that cannot be bound exactly.
    if(in_multi_config)
        pfh_rsi_refuse(multi_config_generator)
    endif()
    if(NOT "${in_generator}" MATCHES "^((Unix|MSYS|MinGW) Makefiles|Ninja)$")
        pfh_rsi_refuse(generator_without_compile_database)
    endif()
    if(NOT in_database_enabled)
        pfh_rsi_refuse(compile_database_disabled)
    endif()
    # The compilation database does not show launchers; every place CMake reads one is checked.
    foreach(launcher IN ITEMS launcher.target rule_launch.target rule_launch.global
            rule_launch.directory)
        if(NOT "${in_${launcher}}" STREQUAL "")
            pfh_rsi_refuse(compiler_launcher)
        endif()
    endforeach()
    if(NOT "${in_compiler.arg1}" STREQUAL "")
        pfh_rsi_refuse(compiler_arguments)
    endif()

    # Compiler custody: the bytes of the driver, whatever path or alias reaches it.
    if(NOT EXISTS "${in_compiler.path}")
        pfh_rsi_refuse(compiler_unreadable)
    endif()
    get_filename_component(compiler_real "${in_compiler.path}" REALPATH)
    file(READ "${compiler_real}" compiler_magic LIMIT 2 HEX)
    if("${compiler_magic}" STREQUAL "2321")
        pfh_rsi_refuse(compiler_is_script)
    endif()
    file(SHA256 "${compiler_real}" compiler_digest)
    set(compiler_banner "unavailable")
    execute_process(COMMAND "${in_compiler.path}" --version
        RESULT_VARIABLE banner_status OUTPUT_VARIABLE banner_text ERROR_QUIET
        TIMEOUT 30 OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(banner_status EQUAL 0 AND NOT "${banner_text}" STREQUAL "")
        string(FIND "${banner_text}" "\n" newline_at)
        if(newline_at EQUAL -1)
            set(compiler_banner "${banner_text}")
        else()
            string(SUBSTRING "${banner_text}" 0 ${newline_at} compiler_banner)
        endif()
        string(REPLACE "\r" "" compiler_banner "${compiler_banner}")
        string(REPLACE ";" "," compiler_banner "${compiler_banner}")
        # A launcher installed under the compiler's name announces itself in its banner.
        string(TOLOWER "${compiler_banner}" banner_lower)
        if("${banner_lower}" MATCHES "ccache|sccache|distcc|icecc")
            pfh_rsi_refuse(compiler_launcher)
        endif()
    endif()
    set(compiler_target "unavailable")
    execute_process(COMMAND "${in_compiler.path}" -dumpmachine
        RESULT_VARIABLE target_status OUTPUT_VARIABLE target_text ERROR_QUIET
        TIMEOUT 30 OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(target_status EQUAL 0 AND NOT "${target_text}" STREQUAL "")
        string(REPLACE "\n" "" compiler_target "${target_text}")
        string(REPLACE ";" "," compiler_target "${compiler_target}")
    endif()

    # Reducer files: project-relative name and content digest.
    list(REMOVE_DUPLICATES sources)
    list(REMOVE_DUPLICATES headers)
    set(file_text "")
    foreach(kind IN ITEMS source header)
        set(entries "")
        foreach(path IN LISTS ${kind}s)
            if(NOT EXISTS "${path}")
                pfh_rsi_refuse(reducer_file_missing)
            endif()
            file(SHA256 "${path}" file_digest)
            pfh_rsi_rewrite_path(display "${path}" "${in_build.root}" "${in_source.root}"
                "${in_build.root}")
            list(APPEND entries "${kind} ${display} sha256=${file_digest}")
        endforeach()
        list(SORT entries)
        foreach(entry IN LISTS entries)
            string(APPEND file_text "${entry}\n")
        endforeach()
    endforeach()
    string(SHA256 source_digest "${file_text}")

    # The generated compile command of every reducer translation unit.
    if(NOT EXISTS "${in_database}")
        pfh_rsi_refuse(compile_database_missing)
    endif()
    file(READ "${in_database}" database)
    string(JSON database_count ERROR_VARIABLE database_error LENGTH "${database}")
    if(database_error OR NOT database_count GREATER 0)
        pfh_rsi_refuse(compile_database_unreadable)
    endif()
    math(EXPR database_last "${database_count} - 1")
    set(blocks "")
    foreach(source_path IN LISTS sources)
        set(matches "")
        foreach(index RANGE ${database_last})
            string(JSON entry_file ERROR_VARIABLE entry_error GET "${database}" ${index} file)
            if(entry_error)
                continue()
            endif()
            get_filename_component(entry_file "${entry_file}" ABSOLUTE)
            if(NOT "${entry_file}" STREQUAL "${source_path}")
                continue()
            endif()
            string(JSON entry_command ERROR_VARIABLE entry_error GET "${database}" ${index}
                command)
            if(entry_error)
                pfh_rsi_refuse(compile_command_unreadable)
            endif()
            # The object path names the target that owns the translation unit.
            string(FIND "${entry_command}" "/${in_target}.dir/" owner_at)
            if(NOT owner_at EQUAL -1)
                list(APPEND matches ${index})
            endif()
        endforeach()
        list(LENGTH matches match_count)
        if(match_count EQUAL 0)
            pfh_rsi_refuse(compile_command_missing)
        endif()
        if(match_count GREATER 1)
            pfh_rsi_refuse(compile_command_ambiguous)
        endif()
        string(JSON entry_command GET "${database}" ${matches} command)
        string(JSON entry_directory GET "${database}" ${matches} directory)
        foreach(character IN ITEMS "\n" "\r")
            string(FIND "${entry_command}" "${character}" at)
            if(NOT at EQUAL -1)
                pfh_rsi_refuse(unsupported_character)
            endif()
        endforeach()
        string(FIND "${entry_command}" ";" at)
        if(NOT at EQUAL -1)
            pfh_rsi_refuse(unsupported_character)
        endif()
        string(FIND "${entry_command}" "[" at)
        if(NOT at EQUAL -1)
            pfh_rsi_refuse(unsupported_character)
        endif()
        string(FIND "${entry_command}" "]" at)
        if(NOT at EQUAL -1)
            pfh_rsi_refuse(unsupported_character)
        endif()
        separate_arguments(tokens UNIX_COMMAND "${entry_command}")
        list(LENGTH tokens token_count)
        if(token_count LESS 2)
            pfh_rsi_refuse(compile_command_unreadable)
        endif()
        # The first token must be the very compiler whose bytes are bound: this refuses any
        # launcher or wrapper that is visible in the command.
        list(GET tokens 0 compiler_token)
        get_filename_component(compiler_token_real "${compiler_token}" REALPATH
            BASE_DIR "${entry_directory}")
        if(NOT "${compiler_token_real}" STREQUAL "${compiler_real}")
            pfh_rsi_refuse(compiler_mismatch)
        endif()
        list(REMOVE_AT tokens 0)

        # Declared normalization. Dropped: the compile-only switch, dependency-file options and
        # the object path (none can change arithmetic). Rewritten: the source file and the
        # path of path-valued options (location only). Forced includes also carry the digest
        # of their content. Everything else is kept verbatim, in command-line order.
        pfh_rsi_rewrite_path(source_display "${source_path}" "${entry_directory}"
            "${in_source.root}" "${in_build.root}")
        set(block "command ${source_display}\n")
        set(pending "")
        foreach(token IN LISTS tokens)
            if("${pending}" STREQUAL "drop")
                set(pending "")
                continue()
            endif()
            if("${pending}" STREQUAL "path")
                pfh_rsi_rewrite_path(rewritten "${token}" "${entry_directory}"
                    "${in_source.root}" "${in_build.root}")
                string(APPEND block "arg ${rewritten}\n")
                set(pending "")
                continue()
            endif()
            if("${pending}" STREQUAL "include")
                if(NOT IS_ABSOLUTE "${token}")
                    set(token "${entry_directory}/${token}")
                endif()
                if(NOT EXISTS "${token}")
                    pfh_rsi_refuse(forced_include_unresolved)
                endif()
                file(SHA256 "${token}" include_digest)
                pfh_rsi_rewrite_path(rewritten "${token}" "${entry_directory}"
                    "${in_source.root}" "${in_build.root}")
                string(APPEND block "arg ${rewritten}#sha256=${include_digest}\n")
                set(pending "")
                continue()
            endif()
            if("${token}" MATCHES "^@")
                pfh_rsi_refuse(response_file)
            endif()
            if("${token}" STREQUAL "-c" OR "${token}" STREQUAL "-MD"
                    OR "${token}" STREQUAL "-MMD" OR "${token}" STREQUAL "-MP"
                    OR "${token}" STREQUAL "-MG")
                continue()
            endif()
            if("${token}" STREQUAL "-o" OR "${token}" STREQUAL "-MF"
                    OR "${token}" STREQUAL "-MT" OR "${token}" STREQUAL "-MQ")
                set(pending "drop")
                continue()
            endif()
            if("${token}" STREQUAL "-include" OR "${token}" STREQUAL "-imacros")
                string(APPEND block "arg ${token}\n")
                set(pending "include")
                continue()
            endif()
            if("${token}" STREQUAL "-I" OR "${token}" STREQUAL "-isystem"
                    OR "${token}" STREQUAL "-iquote" OR "${token}" STREQUAL "-idirafter"
                    OR "${token}" STREQUAL "-iframework" OR "${token}" STREQUAL "-F"
                    OR "${token}" STREQUAL "-isysroot" OR "${token}" STREQUAL "-B")
                string(APPEND block "arg ${token}\n")
                set(pending "path")
                continue()
            endif()
            if("${token}" MATCHES "^(-I|-F|-B|--sysroot=)(.+)$")
                set(joined_option "${CMAKE_MATCH_1}")
                pfh_rsi_rewrite_path(rewritten "${CMAKE_MATCH_2}" "${entry_directory}"
                    "${in_source.root}" "${in_build.root}")
                string(APPEND block "arg ${joined_option}${rewritten}\n")
                continue()
            endif()
            if("${token}" STREQUAL "${source_path}")
                string(APPEND block "arg ${source_display}\n")
                continue()
            endif()
            string(APPEND block "arg ${token}\n")
        endforeach()
        if(NOT "${pending}" STREQUAL "")
            pfh_rsi_refuse(compile_command_unreadable)
        endif()
        list(APPEND blocks "${block}")
    endforeach()
    list(SORT blocks)
    set(command_text "")
    foreach(block IN LISTS blocks)
        string(APPEND command_text "${block}")
    endforeach()

    set(descriptor "${PFH_RSI_FORMAT}\n")
    string(APPEND descriptor
        "contract=${in_contract}\n"
        "configuration=${in_configuration}\n"
        "compiler.id=${in_compiler.id}\n"
        "compiler.version=${in_compiler.version}\n"
        "compiler.banner=${compiler_banner}\n"
        "compiler.target=${compiler_target}\n"
        "compiler.sha256=${compiler_digest}\n"
        "system.name=${in_system.name}\n"
        "system.processor=${in_system.processor}\n"
        "${command_text}${file_text}"
        "source.digest=${source_digest}\n")
    set(PFH_RSI_DESCRIPTOR "${descriptor}" PARENT_SCOPE)
    set(PFH_RSI_SOURCE_DIGEST "${source_digest}" PARENT_SCOPE)
endfunction()

set(PFH_RSI_REASON "")
set(PFH_RSI_DESCRIPTOR "")
set(PFH_RSI_SOURCE_DIGEST "")
pfh_rsi_bind()

# The raw-string delimiter must not occur in the text it quotes.
if("${PFH_RSI_REASON}" STREQUAL "")
    string(FIND "${PFH_RSI_DESCRIPTOR}" ")pfhrsi\"" delimiter_at)
    if(NOT delimiter_at EQUAL -1)
        set(PFH_RSI_REASON "descriptor_unrepresentable")
    endif()
endif()

if("${PFH_RSI_REASON}" STREQUAL "")
    set(bound true)
    set(descriptor "${PFH_RSI_DESCRIPTOR}")
    string(SHA256 descriptor_digest "${descriptor}")
    set(identity "${PFH_RSI_IDENTITY_PREFIX}${descriptor_digest}")
    set(source_digest "${PFH_RSI_SOURCE_DIGEST}")
else()
    set(bound false)
    string(CONCAT descriptor
        "${PFH_RSI_FORMAT}\n"
        "contract=${PFH_RSI_CONTRACT}\n"
        "capability=unbound\n"
        "reason=${PFH_RSI_REASON}\n")
    set(identity "")
    set(source_digest "")
endif()

string(CONCAT header
    "// Generated by GenerateReturnStatsIdentity.cmake. Do not edit.\n"
    "#pragma once\n"
    "\n"
    "namespace pineforge::hpo::detail::return_stats_identity_generated {\n"
    "\n"
    "inline constexpr bool kBound = ${bound};\n"
    "inline constexpr char kContract[] = \"${PFH_RSI_CONTRACT}\";\n"
    "inline constexpr char kUnboundReason[] = \"${PFH_RSI_REASON}\";\n"
    "inline constexpr char kIdentity[] = \"${identity}\";\n"
    "inline constexpr char kSourceDigest[] = \"${source_digest}\";\n"
    "inline constexpr char kDescriptor[] = R\"pfhrsi(${descriptor})pfhrsi\";\n"
    "\n"
    "}  // namespace pineforge::hpo::detail::return_stats_identity_generated\n")

file(MAKE_DIRECTORY "${PFH_RSI_OUTPUT_DIR}")
pfh_rsi_write_if_changed("${PFH_RSI_OUTPUT_DIR}/return_stats_identity.descriptor.txt"
    "${descriptor}")
pfh_rsi_write_if_changed("${PFH_RSI_OUTPUT_DIR}/return_stats_identity.txt" "${identity}\n")
pfh_rsi_write_if_changed("${PFH_RSI_OUTPUT_DIR}/return_stats_identity_generated.hpp" "${header}")
if(bound)
    message(STATUS "return-statistics identity: ${identity}")
else()
    message(STATUS "return-statistics identity unbound: ${PFH_RSI_REASON}")
endif()
