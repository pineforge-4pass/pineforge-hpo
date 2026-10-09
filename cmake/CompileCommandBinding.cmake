# Low-level binding of the compile commands that the build system generated.
#
# Used by cmake/GenerateSobolIdentity.cmake. It implements the declared normalization rule of
# cmake/GenerateReturnStatsIdentity.cmake (documented in docs/internal/return-stats-identity.md)
# as functions, extended to several translation-unit groups and compilers. The return-statistics
# generator keeps its own inline copy for now, so its descriptor bytes cannot move; the cross-check
# in tests/test_sobol_identity.py runs both on the same compilation-database entry.
#
# Nothing here models how CMake assembles flags: the unit of binding is the entry of the JSON
# compilation database. Every function reports a refusal by setting PFH_CCB_REASON in its caller's
# scope and returning; an empty PFH_CCB_REASON means success. Nothing here fails the build.
#
# Normalization of one generated command, in order of appearance and without reordering:
#   - the first token must be the compiler whose driver bytes are bound (same file after symlinks);
#   - dropped: -c, -MD, -MMD, -MP, -MG, and -o, -MF, -MT, -MQ with their argument;
#   - rewritten, location only: the source file and the path of -I, -isystem, -iquote, -idirafter,
#     -iframework, -F, -isysroot, -B, --sysroot= and -include/-imacros; a path inside the build
#     tree becomes <build>/..., one inside the source tree <src>/..., any other stays verbatim;
#     a forced include also carries the SHA-256 of its content;
#   - every other token (-D, -U, -O, -f, -m, -std, -W, ...) is kept verbatim.
# A response file, a list-splitting character (semicolon, bracket), a forced include that cannot
# be read or a launcher in front of the compiler is not normalized: the caller reports it unbound.

include_guard(GLOBAL)

macro(_pfh_ccb_refuse reason_text)
    set(PFH_CCB_REASON "${reason_text}" PARENT_SCOPE)
    return()
endmacro()

# Characters that split or bracket CMake list elements.
function(_pfh_ccb_has_list_hazard out text)
    string(FIND "${text}" ";" semicolon_at)
    string(FIND "${text}" "[" open_at)
    string(FIND "${text}" "]" close_at)
    string(FIND "${text}" "\n" newline_at)
    string(FIND "${text}" "\r" return_at)
    if(semicolon_at EQUAL -1 AND open_at EQUAL -1 AND close_at EQUAL -1
            AND newline_at EQUAL -1 AND return_at EQUAL -1)
        set(${out} OFF PARENT_SCOPE)
    else()
        set(${out} ON PARENT_SCOPE)
    endif()
endfunction()

# Location-only rewrite of one path: relative values are resolved against base_directory.
function(pfh_ccb_rewrite_path out value base_directory source_root build_root)
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

# Byte custody of one compiler driver, whatever path or alias reaches it. Sets <prefix>_real,
# <prefix>_sha256, <prefix>_banner and <prefix>_target in the caller's scope.
function(pfh_ccb_compiler_custody prefix compiler)
    set(PFH_CCB_REASON "" PARENT_SCOPE)
    if(NOT EXISTS "${compiler}")
        _pfh_ccb_refuse(compiler_unreadable)
    endif()
    get_filename_component(real "${compiler}" REALPATH)
    file(READ "${real}" magic LIMIT 2 HEX)
    if("${magic}" STREQUAL "2321")
        _pfh_ccb_refuse(compiler_is_script)
    endif()
    file(SHA256 "${real}" digest)
    set(banner "unavailable")
    execute_process(COMMAND "${compiler}" --version
        RESULT_VARIABLE banner_status OUTPUT_VARIABLE banner_text ERROR_QUIET
        TIMEOUT 30 OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(banner_status EQUAL 0 AND NOT "${banner_text}" STREQUAL "")
        string(FIND "${banner_text}" "\n" newline_at)
        if(newline_at EQUAL -1)
            set(banner "${banner_text}")
        else()
            string(SUBSTRING "${banner_text}" 0 ${newline_at} banner)
        endif()
        string(REPLACE "\r" "" banner "${banner}")
        string(REPLACE ";" "," banner "${banner}")
        # A launcher installed under the compiler's name announces itself in its banner.
        string(TOLOWER "${banner}" banner_lower)
        if("${banner_lower}" MATCHES "ccache|sccache|distcc|icecc")
            _pfh_ccb_refuse(compiler_launcher)
        endif()
    endif()
    set(target "unavailable")
    execute_process(COMMAND "${compiler}" -dumpmachine
        RESULT_VARIABLE target_status OUTPUT_VARIABLE target_text ERROR_QUIET
        TIMEOUT 30 OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(target_status EQUAL 0 AND NOT "${target_text}" STREQUAL "")
        string(REPLACE "\n" "" target "${target_text}")
        string(REPLACE ";" "," target "${target}")
    endif()
    set(${prefix}_real "${real}" PARENT_SCOPE)
    set(${prefix}_sha256 "${digest}" PARENT_SCOPE)
    set(${prefix}_banner "${banner}" PARENT_SCOPE)
    set(${prefix}_target "${target}" PARENT_SCOPE)
endfunction()

# Reads the compilation database once. out_text receives the JSON text; out_files receives one
# absolute source path per entry (positional, "<none>" where an entry has none or a path that
# cannot be listed safely).
function(pfh_ccb_load_database out_text out_files database_path)
    set(PFH_CCB_REASON "" PARENT_SCOPE)
    if(NOT EXISTS "${database_path}")
        _pfh_ccb_refuse(compile_database_missing)
    endif()
    file(READ "${database_path}" text)
    string(JSON count ERROR_VARIABLE count_error LENGTH "${text}")
    if(count_error OR NOT count GREATER 0)
        _pfh_ccb_refuse(compile_database_unreadable)
    endif()
    math(EXPR last "${count} - 1")
    set(files "")
    foreach(index RANGE ${last})
        string(JSON entry_file ERROR_VARIABLE entry_error GET "${text}" ${index} file)
        if(entry_error)
            set(entry_file "<none>")
        else()
            _pfh_ccb_has_list_hazard(hazard "${entry_file}")
            if(hazard)
                set(entry_file "<none>")
            else()
                get_filename_component(entry_file "${entry_file}" ABSOLUTE)
            endif()
        endif()
        list(APPEND files "${entry_file}")
    endforeach()
    set(${out_text} "${text}" PARENT_SCOPE)
    set(${out_files} "${files}" PARENT_SCOPE)
endfunction()

# Normalizes the generated command of every source in ARGN (absolute paths) that the target owns.
# out_blocks receives a list with one block per source: "command <label> <source>" followed by one
# "arg <token>" line per normalized token.
function(pfh_ccb_bind_sources out_blocks label database files target compiler_real source_root
        build_root)
    set(PFH_CCB_REASON "" PARENT_SCOPE)
    set(blocks "")
    foreach(source_path IN LISTS ARGN)
        # Entries for this file whose object path names this target.
        set(matches "")
        set(index 0)
        foreach(entry_file IN LISTS files)
            if("${entry_file}" STREQUAL "${source_path}")
                string(JSON entry_command ERROR_VARIABLE entry_error GET "${database}" ${index}
                    command)
                if(entry_error)
                    _pfh_ccb_refuse(compile_command_unreadable)
                endif()
                string(FIND "${entry_command}" "/${target}.dir/" owner_at)
                if(NOT owner_at EQUAL -1)
                    list(APPEND matches ${index})
                endif()
            endif()
            math(EXPR index "${index} + 1")
        endforeach()
        list(LENGTH matches match_count)
        if(match_count EQUAL 0)
            _pfh_ccb_refuse(compile_command_missing)
        endif()
        if(match_count GREATER 1)
            _pfh_ccb_refuse(compile_command_ambiguous)
        endif()
        string(JSON entry_command GET "${database}" ${matches} command)
        string(JSON entry_directory GET "${database}" ${matches} directory)
        _pfh_ccb_has_list_hazard(hazard "${entry_command}")
        if(hazard)
            _pfh_ccb_refuse(unsupported_character)
        endif()
        separate_arguments(tokens UNIX_COMMAND "${entry_command}")
        list(LENGTH tokens token_count)
        if(token_count LESS 2)
            _pfh_ccb_refuse(compile_command_unreadable)
        endif()
        # The first token must be the very compiler whose bytes are bound: this refuses any
        # launcher or wrapper that is visible in the command.
        list(GET tokens 0 compiler_token)
        get_filename_component(compiler_token_real "${compiler_token}" REALPATH
            BASE_DIR "${entry_directory}")
        if(NOT "${compiler_token_real}" STREQUAL "${compiler_real}")
            _pfh_ccb_refuse(compiler_mismatch)
        endif()
        list(REMOVE_AT tokens 0)

        pfh_ccb_rewrite_path(source_display "${source_path}" "${entry_directory}"
            "${source_root}" "${build_root}")
        set(block "command ${label} ${source_display}\n")
        set(pending "")
        foreach(token IN LISTS tokens)
            if("${pending}" STREQUAL "drop")
                set(pending "")
                continue()
            endif()
            if("${pending}" STREQUAL "path")
                pfh_ccb_rewrite_path(rewritten "${token}" "${entry_directory}"
                    "${source_root}" "${build_root}")
                string(APPEND block "arg ${rewritten}\n")
                set(pending "")
                continue()
            endif()
            if("${pending}" STREQUAL "include")
                if(NOT IS_ABSOLUTE "${token}")
                    set(token "${entry_directory}/${token}")
                endif()
                if(NOT EXISTS "${token}")
                    _pfh_ccb_refuse(forced_include_unresolved)
                endif()
                file(SHA256 "${token}" include_digest)
                pfh_ccb_rewrite_path(rewritten "${token}" "${entry_directory}"
                    "${source_root}" "${build_root}")
                string(APPEND block "arg ${rewritten}#sha256=${include_digest}\n")
                set(pending "")
                continue()
            endif()
            if("${token}" MATCHES "^@")
                _pfh_ccb_refuse(response_file)
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
                pfh_ccb_rewrite_path(rewritten "${CMAKE_MATCH_2}" "${entry_directory}"
                    "${source_root}" "${build_root}")
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
            _pfh_ccb_refuse(compile_command_unreadable)
        endif()
        list(APPEND blocks "${block}")
    endforeach()
    set(${out_blocks} "${blocks}" PARENT_SCOPE)
endfunction()
