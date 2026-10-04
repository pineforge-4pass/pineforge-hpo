#pragma once

#include <cfenv>
#include <cfloat>
#include <limits>
#include <locale>
#include <sstream>
#include <string>
#if __has_include("numeric_build_flags.hpp")
#include "numeric_build_flags.hpp"
#endif

namespace pineforge::hpo::detail {

inline std::string numeric_build_identity() {
    std::ostringstream output;
    output.imbue(std::locale::classic());
#ifdef __clang__
    output << "clang:" << __clang_version__;
#else
    output << "gcc:" << __VERSION__;
#endif
#ifdef _LIBCPP_VERSION
    output << ";libc++:" << _LIBCPP_VERSION;
#elif defined(__GLIBCXX__)
    output << ";libstdc++:" << __GLIBCXX__;
#endif
#ifdef __GLIBC__
    output << ";glibc:" << __GLIBC__ << '.' << __GLIBC_MINOR__;
#endif
#ifdef __ENVIRONMENT_MAC_OS_X_VERSION_MIN_REQUIRED__
    output << ";macos:" << __ENVIRONMENT_MAC_OS_X_VERSION_MIN_REQUIRED__;
#endif
#ifdef __aarch64__
    output << ";aarch64";
#elif defined(__x86_64__)
    output << ";x86_64";
#endif
#ifdef __FMA__
    output << ";fma";
#endif
#ifdef __AVX2__
    output << ";avx2";
#endif
#ifdef __FAST_MATH__
    output << ";fast_math";
#endif
#ifdef __FINITE_MATH_ONLY__
    output << ";finite_math:" << __FINITE_MATH_ONLY__;
#endif
#ifdef PFH_FP_CONTRACT_OFF
    output << ";fp_contract:off";
#else
    output << ";fp_contract:unspecified";
#endif
#ifdef PFH_NUMERIC_BUILD_FLAGS
    output << ";flags:" << PFH_NUMERIC_BUILD_FLAGS;
#endif
    output << ";eval:" << FLT_EVAL_METHOD << ";double:" << sizeof(double) << ':'
           << std::numeric_limits<double>::digits << ':'
           << std::numeric_limits<double>::is_iec559 << ";round:" << std::fegetround();
    return output.str();
}

}
