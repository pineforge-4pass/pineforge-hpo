# Build-time half of pfh_sobol_identity(); run with cmake -P before every build of the core target.
#
#   PFH_SOBOL_INGREDIENTS  file written by file(GENERATE) for the active configuration
#   PFH_SOBOL_OUTPUT_DIR   directory receiving the descriptor, the digest and the generated header
#   PFH_SOBOL_CONTRACT     identity prefix (also present in the ingredients)
#
# The descriptor binds, for the Sobol numeric identity:
#   - the compile command that the build system generated for each Sobol C++ translation unit and
#     for each C translation unit of the portable-math target (compilation database entries,
#     normalized by cmake/CompileCommandBinding.cmake);
#   - the bytes of the C++ compiler driver and of the C compiler driver, separately;
#   - the digest of every listed source, header, the generated Joe-Kuo table and the forced
#     includes of the commands.
# A build that cannot be bound exactly is not bound approximately: the outputs then carry an
# unbound capability with a reason, an empty digest and the reason in the descriptor. This script
# never fails the build for a data-dependent condition. Outputs are rewritten only when their bytes
# change, and the generated header is never an input of its own digest.

cmake_minimum_required(VERSION 3.17)

foreach(variable IN ITEMS PFH_SOBOL_INGREDIENTS PFH_SOBOL_OUTPUT_DIR PFH_SOBOL_CONTRACT)
    if(NOT DEFINED ${variable})
        message(FATAL_ERROR "sobol identity: ${variable} is not set")
    endif()
endforeach()
if(NOT "${PFH_SOBOL_CONTRACT}" MATCHES "^[A-Za-z0-9._/:+-]+$")
    message(FATAL_ERROR "sobol identity: unsupported characters in the contract")
endif()

include("${CMAKE_CURRENT_LIST_DIR}/CompileCommandBinding.cmake")

set(PFH_SOBOL_FORMAT "pineforge-hpo-sobol-identity/v1")

macro(pfh_sobol_refuse reason_text)
    set(PFH_SOBOL_REASON "${reason_text}" PARENT_SCOPE)
    return()
endmacro()

# Leaves pfh_sobol_bind() when the shared helper reported a refusal.
macro(pfh_sobol_forward)
    if(NOT "${PFH_CCB_REASON}" STREQUAL "")
        set(PFH_SOBOL_REASON "${PFH_CCB_REASON}" PARENT_SCOPE)
        return()
    endif()
endmacro()

function(pfh_sobol_write_if_changed path content)
    if(EXISTS "${path}")
        file(READ "${path}" existing)
        if("${existing}" STREQUAL "${content}")
            return()
        endif()
    endif()
    file(WRITE "${path}" "${content}")
endfunction()

function(pfh_sobol_bind)
    if(CMAKE_VERSION VERSION_LESS 3.19)
        pfh_sobol_refuse(cmake_below_3_19)
    endif()
    if(NOT EXISTS "${PFH_SOBOL_INGREDIENTS}")
        pfh_sobol_refuse(ingredients_missing)
    endif()
    file(READ "${PFH_SOBOL_INGREDIENTS}" text)
    _pfh_ccb_has_list_hazard(hazard "${text}")
    if(hazard)
        # Only newlines separate lines here, so a hazard means a semicolon or a bracket.
        string(REPLACE "\n" "" without_newlines "${text}")
        _pfh_ccb_has_list_hazard(hazard "${without_newlines}")
        if(hazard)
            pfh_sobol_refuse(unsupported_character)
        endif()
    endif()

    set(cxx_sources "")
    set(c_sources "")
    set(headers "")
    string(REPLACE "\n" ";" lines "${text}")
    foreach(line IN LISTS lines)
        if("${line}" STREQUAL "")
            continue()
        endif()
        if("${line}" MATCHES "^([a-z][a-z0-9_.]*)=(.*)$")
            set(key "${CMAKE_MATCH_1}")
            set(value "${CMAKE_MATCH_2}")
            if(key STREQUAL "source.cxx")
                list(APPEND cxx_sources "${value}")
            elseif(key STREQUAL "source.c")
                list(APPEND c_sources "${value}")
            elseif(key STREQUAL "header")
                list(APPEND headers "${value}")
            else()
                set(in_${key} "${value}")
            endif()
        else()
            pfh_sobol_refuse(ingredients_malformed)
        endif()
    endforeach()
    foreach(key IN ITEMS contract configuration core.target math.target cxx.path cxx.id
            cxx.version c.path c.id c.version system.name system.processor generator database
            source.root build.root)
        if("${in_${key}}" STREQUAL "")
            pfh_sobol_refuse(ingredients_incomplete)
        endif()
    endforeach()
    if(NOT "${in_contract}" STREQUAL "${PFH_SOBOL_CONTRACT}")
        pfh_sobol_refuse(contract_mismatch)
    endif()
    if(NOT cxx_sources OR NOT c_sources)
        pfh_sobol_refuse(ingredients_incomplete)
    endif()

    # Capabilities that cannot be bound exactly.
    if(in_multi_config)
        pfh_sobol_refuse(multi_config_generator)
    endif()
    if(NOT "${in_generator}" MATCHES "^((Unix|MSYS|MinGW) Makefiles|Ninja)$")
        pfh_sobol_refuse(generator_without_compile_database)
    endif()
    if(NOT in_database_enabled)
        pfh_sobol_refuse(compile_database_disabled)
    endif()
    # The compilation database does not show launchers; every place CMake reads one is checked.
    foreach(launcher IN ITEMS launcher.core launcher.math rule_launch.global
            rule_launch.directory rule_launch.core rule_launch.math)
        if(NOT "${in_${launcher}}" STREQUAL "")
            pfh_sobol_refuse(compiler_launcher)
        endif()
    endforeach()
    if(NOT "${in_cxx.arg1}" STREQUAL "" OR NOT "${in_c.arg1}" STREQUAL "")
        pfh_sobol_refuse(compiler_arguments)
    endif()

    # Compiler custody: the C++ driver and the C driver, each by its own bytes.
    pfh_ccb_compiler_custody(cxx "${in_cxx.path}")
    pfh_sobol_forward()
    pfh_ccb_compiler_custody(cc "${in_c.path}")
    pfh_sobol_forward()

    # Sources and headers: project-relative name and content digest.
    list(REMOVE_DUPLICATES cxx_sources)
    list(REMOVE_DUPLICATES c_sources)
    list(REMOVE_DUPLICATES headers)
    set(file_text "")
    set(source_entries "")
    foreach(path IN LISTS cxx_sources c_sources)
        if(NOT EXISTS "${path}")
            pfh_sobol_refuse(reducer_file_missing)
        endif()
        file(SHA256 "${path}" file_digest)
        pfh_ccb_rewrite_path(display "${path}" "${in_build.root}" "${in_source.root}"
            "${in_build.root}")
        list(APPEND source_entries "source ${display} sha256=${file_digest}")
    endforeach()
    list(SORT source_entries)
    set(header_entries "")
    foreach(path IN LISTS headers)
        if(NOT EXISTS "${path}")
            pfh_sobol_refuse(reducer_file_missing)
        endif()
        file(SHA256 "${path}" file_digest)
        pfh_ccb_rewrite_path(display "${path}" "${in_build.root}" "${in_source.root}"
            "${in_build.root}")
        list(APPEND header_entries "header ${display} sha256=${file_digest}")
    endforeach()
    list(SORT header_entries)
    foreach(entry IN LISTS source_entries header_entries)
        string(APPEND file_text "${entry}\n")
    endforeach()
    string(SHA256 source_digest "${file_text}")

    # The generated compile command of every translation unit, one pass over the database.
    pfh_ccb_load_database(database_text database_files "${in_database}")
    pfh_sobol_forward()
    pfh_ccb_bind_sources(cxx_blocks cxx "${database_text}" "${database_files}"
        "${in_core.target}" "${cxx_real}" "${in_source.root}" "${in_build.root}" ${cxx_sources})
    pfh_sobol_forward()
    pfh_ccb_bind_sources(c_blocks c "${database_text}" "${database_files}"
        "${in_math.target}" "${cc_real}" "${in_source.root}" "${in_build.root}" ${c_sources})
    pfh_sobol_forward()
    set(blocks ${cxx_blocks} ${c_blocks})
    list(SORT blocks)
    set(command_text "")
    foreach(block IN LISTS blocks)
        string(APPEND command_text "${block}")
    endforeach()

    set(descriptor "${PFH_SOBOL_FORMAT}\n")
    string(APPEND descriptor
        "contract=${in_contract}\n"
        "configuration=${in_configuration}\n"
        "system.name=${in_system.name}\n"
        "system.processor=${in_system.processor}\n"
        "compiler.cxx.id=${in_cxx.id}\n"
        "compiler.cxx.version=${in_cxx.version}\n"
        "compiler.cxx.banner=${cxx_banner}\n"
        "compiler.cxx.target=${cxx_target}\n"
        "compiler.cxx.sha256=${cxx_sha256}\n"
        "compiler.c.id=${in_c.id}\n"
        "compiler.c.version=${in_c.version}\n"
        "compiler.c.banner=${cc_banner}\n"
        "compiler.c.target=${cc_target}\n"
        "compiler.c.sha256=${cc_sha256}\n"
        "${command_text}${file_text}"
        "source.digest=${source_digest}\n")
    set(PFH_SOBOL_DESCRIPTOR "${descriptor}" PARENT_SCOPE)
    set(PFH_SOBOL_SOURCE_DIGEST "${source_digest}" PARENT_SCOPE)
endfunction()

set(PFH_SOBOL_REASON "")
set(PFH_SOBOL_DESCRIPTOR "")
set(PFH_SOBOL_SOURCE_DIGEST "")
pfh_sobol_bind()

# The raw-string delimiter must not occur in the text it quotes.
if("${PFH_SOBOL_REASON}" STREQUAL "")
    string(FIND "${PFH_SOBOL_DESCRIPTOR}" ")pfhsobol\"" delimiter_at)
    if(NOT delimiter_at EQUAL -1)
        set(PFH_SOBOL_REASON "descriptor_unrepresentable")
    endif()
endif()

if("${PFH_SOBOL_REASON}" STREQUAL "")
    set(bound true)
    set(descriptor "${PFH_SOBOL_DESCRIPTOR}")
    string(SHA256 build_digest "${descriptor}")
    set(source_digest "${PFH_SOBOL_SOURCE_DIGEST}")
else()
    set(bound false)
    string(CONCAT descriptor
        "${PFH_SOBOL_FORMAT}\n"
        "contract=${PFH_SOBOL_CONTRACT}\n"
        "capability=unbound\n"
        "reason=${PFH_SOBOL_REASON}\n")
    set(build_digest "")
    set(source_digest "")
endif()

string(CONCAT header
    "// Generated by GenerateSobolIdentity.cmake. Do not edit.\n"
    "#pragma once\n"
    "\n"
    "namespace pineforge::hpo::detail::sobol_identity_generated {\n"
    "\n"
    "inline constexpr bool kBound = ${bound};\n"
    "inline constexpr char kContract[] = \"${PFH_SOBOL_CONTRACT}\";\n"
    "inline constexpr char kUnboundReason[] = \"${PFH_SOBOL_REASON}\";\n"
    "inline constexpr char kBuildDigest[] = \"${build_digest}\";\n"
    "inline constexpr char kSourceDigest[] = \"${source_digest}\";\n"
    "inline constexpr char kDescriptor[] = R\"pfhsobol(${descriptor})pfhsobol\";\n"
    "\n"
    "}  // namespace pineforge::hpo::detail::sobol_identity_generated\n")

file(MAKE_DIRECTORY "${PFH_SOBOL_OUTPUT_DIR}")
pfh_sobol_write_if_changed("${PFH_SOBOL_OUTPUT_DIR}/sobol_identity.descriptor.txt" "${descriptor}")
pfh_sobol_write_if_changed("${PFH_SOBOL_OUTPUT_DIR}/sobol_identity.txt" "${build_digest}\n")
pfh_sobol_write_if_changed("${PFH_SOBOL_OUTPUT_DIR}/sobol_identity_generated.hpp" "${header}")
if(bound)
    message(STATUS "sobol numeric identity build digest: ${build_digest}")
else()
    message(STATUS "sobol numeric identity unbound: ${PFH_SOBOL_REASON}")
endif()
