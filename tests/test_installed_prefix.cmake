file(REMOVE_RECURSE "${TEST_ROOT}")
execute_process(COMMAND "${CMAKE_COMMAND}" --install "${ENGINE_BUILD}"
    --prefix "${TEST_ROOT}/engine" RESULT_VARIABLE installed)
if(NOT installed EQUAL 0)
    message(FATAL_ERROR "Pinned engine installation failed")
endif()
execute_process(COMMAND "${CMAKE_COMMAND}" -S "${HPO_SOURCE}" -B "${TEST_ROOT}/hpo"
    -DPINEFORGE_ENGINE_ROOT=${TEST_ROOT}/engine
    -DFETCHCONTENT_SOURCE_DIR_DLIB=${DLIB_SOURCE}
    -DPINEFORGE_HPO_BUILD_TESTS=OFF RESULT_VARIABLE configured)
if(NOT configured EQUAL 0)
    message(FATAL_ERROR "HPO could not configure against the installed engine prefix")
endif()
execute_process(COMMAND "${CMAKE_COMMAND}" --build "${TEST_ROOT}/hpo" -j2
    RESULT_VARIABLE built)
if(NOT built EQUAL 0)
    message(FATAL_ERROR "HPO could not build against the installed engine prefix")
endif()
