# Build-time half of pfh_return_stats_identity(); run with cmake -P before every build of a
# target that consumes the identity.
#
#   PFH_RSI_INGREDIENTS  file written by file(GENERATE) for the active configuration
#   PFH_RSI_OUTPUT_DIR   directory receiving the descriptor, the identity and the header
#
# The reducer sources and headers are hashed here, at build time, so an edit changes the identity
# without a reconfigure. The output files are rewritten only when their bytes change, which keeps
# an unchanged build from recompiling its consumers. Only the listed reducer files are hashed:
# the generated header and the descriptor are never inputs, so the identity is not circular.

cmake_minimum_required(VERSION 3.17)

function(pfh_rsi_fail reason)
    message(FATAL_ERROR "return-statistics identity: ${reason}")
endfunction()

function(pfh_rsi_write_if_changed path content)
    if(EXISTS "${path}")
        file(READ "${path}" existing)
        if("${existing}" STREQUAL "${content}")
            return()
        endif()
    endif()
    file(WRITE "${path}" "${content}")
endfunction()

foreach(variable IN ITEMS PFH_RSI_INGREDIENTS PFH_RSI_OUTPUT_DIR)
    if(NOT DEFINED ${variable})
        pfh_rsi_fail("${variable} is not set")
    endif()
endforeach()
if(NOT EXISTS "${PFH_RSI_INGREDIENTS}")
    pfh_rsi_fail("the ingredients file is missing; run the CMake generate step again")
endif()

file(READ "${PFH_RSI_INGREDIENTS}" ingredient_text)
# Lists are used below, so characters that would split or bracket list elements are refused.
string(FIND "${ingredient_text}" ";" unsupported_at)
if(NOT unsupported_at EQUAL -1)
    pfh_rsi_fail("a semicolon inside a flag, definition or path is not supported")
endif()
string(FIND "${ingredient_text}" "[" unsupported_at)
if(NOT unsupported_at EQUAL -1)
    pfh_rsi_fail("a bracket inside a flag, definition or path is not supported")
endif()
string(FIND "${ingredient_text}" "]" unsupported_at)
if(NOT unsupported_at EQUAL -1)
    pfh_rsi_fail("a bracket inside a flag, definition or path is not supported")
endif()

string(REPLACE "\n" ";" ingredient_lines "${ingredient_text}")
set(cxx_flag_text "")
set(config_flag_text "")
set(target_flag_tokens "")
set(source_flag_text "")
set(target_definitions "")
set(source_definitions "")
set(source_paths "")
set(header_paths "")
set(source_root "")
foreach(line IN LISTS ingredient_lines)
    if("${line}" STREQUAL "")
        continue()
    endif()
    if(NOT "${line}" MATCHES "^[a-z][a-z0-9_.]*=")
        pfh_rsi_fail("malformed ingredient line: ${line}")
    endif()
    string(REGEX REPLACE "=.*$" "" key "${line}")
    string(REGEX REPLACE "^[^=]*=" "" value "${line}")
    if(key STREQUAL "flags.cxx" OR key STREQUAL "flags.config")
        # CMAKE_CXX_FLAGS and the configuration flags are shell-style strings.
        separate_arguments(tokens UNIX_COMMAND "${value}")
        foreach(token IN LISTS tokens)
            if(key STREQUAL "flags.cxx")
                string(APPEND cxx_flag_text "flag cxx ${token}\n")
            else()
                string(APPEND config_flag_text "flag config ${token}\n")
            endif()
        endforeach()
    elseif(key STREQUAL "flags.target" OR key STREQUAL "flags.source")
        # One compile option per line; SHELL: groups expand to several tokens, as in CMake.
        if("${value}" STREQUAL "")
            continue()
        endif()
        if("${value}" MATCHES "^SHELL:")
            string(REGEX REPLACE "^SHELL:" "" shell_value "${value}")
            separate_arguments(tokens UNIX_COMMAND "${shell_value}")
        else()
            set(tokens "${value}")
        endif()
        if(key STREQUAL "flags.target")
            list(APPEND target_flag_tokens ${tokens})
        else()
            foreach(token IN LISTS tokens)
                string(APPEND source_flag_text "flag source ${token}\n")
            endforeach()
        endif()
    elseif(key STREQUAL "define.target" OR key STREQUAL "define.source")
        if("${value}" STREQUAL "")
            continue()
        endif()
        string(REGEX REPLACE "^-D" "" definition "${value}")
        if(key STREQUAL "define.target")
            list(APPEND target_definitions "${definition}")
        else()
            list(APPEND source_definitions "${definition}")
        endif()
    elseif(key STREQUAL "source")
        list(APPEND source_paths "${value}")
    elseif(key STREQUAL "header")
        list(APPEND header_paths "${value}")
    elseif(key STREQUAL "source.root")
        set(source_root "${value}")
    else()
        set(scalar_${key} "${value}")
    endif()
endforeach()

if("${source_root}" STREQUAL "")
    pfh_rsi_fail("the ingredients do not name a source root")
endif()
if(NOT source_paths)
    pfh_rsi_fail("the ingredients list no reducer source")
endif()
foreach(key IN ITEMS contract configuration compiler.id compiler.version)
    if("${scalar_${key}}" STREQUAL "")
        pfh_rsi_fail("the ingredient '${key}' is missing or empty")
    endif()
endforeach()
if(NOT "${scalar_contract}" MATCHES "^[A-Za-z0-9._/:+-]+$")
    pfh_rsi_fail("the contract string contains unsupported characters")
endif()

# CMake removes repeated target-level options and definitions, keeping the first occurrence;
# the descriptor records the same set so it matches the real command line.
list(REMOVE_DUPLICATES target_flag_tokens)
list(REMOVE_DUPLICATES target_definitions)
set(target_flag_text "")
foreach(token IN LISTS target_flag_tokens)
    string(APPEND target_flag_text "flag target ${token}\n")
endforeach()
set(definition_text "")
foreach(definition IN LISTS target_definitions)
    string(APPEND definition_text "define target ${definition}\n")
endforeach()
foreach(definition IN LISTS source_definitions)
    string(APPEND definition_text "define source ${definition}\n")
endforeach()

# Project-relative name plus content hash for every listed file, sorted by name.
list(REMOVE_DUPLICATES source_paths)
list(REMOVE_DUPLICATES header_paths)
set(file_text "")
foreach(kind IN ITEMS source header)
    set(entries "")
    foreach(path IN LISTS ${kind}_paths)
        if(NOT EXISTS "${path}")
            pfh_rsi_fail("the ${kind} '${path}' does not exist")
        endif()
        file(RELATIVE_PATH relative_path "${source_root}" "${path}")
        file(SHA256 "${path}" file_digest)
        list(APPEND entries "${kind} ${relative_path} sha256=${file_digest}")
    endforeach()
    list(SORT entries)
    foreach(entry IN LISTS entries)
        string(APPEND file_text "${entry}\n")
    endforeach()
endforeach()
string(SHA256 source_digest "${file_text}")

set(descriptor "pineforge-hpo-return-stats-identity/v1\n")
foreach(key IN ITEMS contract configuration compiler.id compiler.version compiler.banner
        compiler.target system.name system.processor osx.architectures osx.deployment_target
        language.standard language.extensions position.independent interprocedural)
    if(NOT DEFINED scalar_${key})
        pfh_rsi_fail("the ingredient '${key}' is missing")
    endif()
    string(APPEND descriptor "${key}=${scalar_${key}}\n")
endforeach()
string(APPEND descriptor
    "${cxx_flag_text}${config_flag_text}${target_flag_text}${source_flag_text}"
    "${definition_text}${file_text}"
    "source.digest=${source_digest}\n")

# The raw-string delimiter must not occur in the text it quotes.
string(FIND "${descriptor}" ")pfhrsi\"" delimiter_at)
if(NOT delimiter_at EQUAL -1)
    pfh_rsi_fail("the descriptor contains the raw-string delimiter")
endif()

string(SHA256 descriptor_digest "${descriptor}")
set(identity "pineforge-hpo-return-stats-build/v1:sha256:${descriptor_digest}")

string(CONCAT header
    "// Generated by GenerateReturnStatsIdentity.cmake. Do not edit.\n"
    "#pragma once\n"
    "\n"
    "namespace pineforge::hpo::detail::return_stats_identity_generated {\n"
    "\n"
    "inline constexpr char kContract[] = \"${scalar_contract}\";\n"
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
message(STATUS "return-statistics identity: ${identity}")
