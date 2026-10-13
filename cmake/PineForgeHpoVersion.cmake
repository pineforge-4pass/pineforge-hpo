# Split a final or release-candidate version without changing the full input.
function(pineforge_hpo_parse_version full_version core_output suffix_output)
    if(NOT "${full_version}" MATCHES
            "^((0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*))(rc[1-9][0-9]*)?$")
        message(FATAL_ERROR
            "Invalid VERSION '${full_version}': expected N.N.N or N.N.NrcM "
            "with no leading zeros and M >= 1.")
    endif()
    set("${core_output}" "${CMAKE_MATCH_1}" PARENT_SCOPE)
    set("${suffix_output}" "${CMAKE_MATCH_5}" PARENT_SCOPE)
endfunction()
