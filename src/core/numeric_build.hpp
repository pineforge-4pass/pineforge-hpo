#pragma once

#include <cfenv>
#include <cfloat>
#include <cmath>
#include <cstring>
#include <iomanip>
#include <limits>
#include <locale>
#include <sstream>
#include <string>
#include "sha256.hpp"
#if __has_include("numeric_build_flags.hpp")
#include "numeric_build_flags.hpp"
#endif

namespace pineforge::hpo::detail {

inline std::string runtime_math_fingerprint() {
    std::ostringstream values;
    values.imbue(std::locale::classic());
    values << std::hexfloat << std::setprecision(std::numeric_limits<long double>::max_digits10);
    using DoubleFunction = double (*)(double);
    using LongFunction = long double (*)(long double);
    DoubleFunction volatile doubles[]{std::log, std::log1p, std::exp, std::expm1,
        std::sqrt, std::cos, std::erfc, std::floor, std::ceil, std::round};
    LongFunction volatile longs[]{std::log, std::log1p, std::exp, std::expm1,
        std::sqrt, std::cos, std::erfc, std::floor, std::ceil, std::round};
    double (*volatile double_fma)(double, double, double) = std::fma;
    long double (*volatile long_fma)(long double, long double, long double) = std::fma;
    for (const double input : {0x1p-40, 0.125, 0.5, 0.9375, 1.0, 1.5, 5.0, 32.0}) {
        volatile double argument = input;
        volatile long double wide_argument = input;
        for (const auto function : doubles) {
            const double result = function(argument);
            std::uint64_t bits;
            std::memcpy(&bits, &result, sizeof(bits));
            values << bits << ' ';
        }
        for (const auto function : longs)
            values << function(wide_argument) << ' ';
        values << double_fma(argument, 0x1.0000000000001p0, -0.5) << ' '
               << long_fma(wide_argument, 0x1.0000000000001p0L, -0.5L) << ' ';
    }
    return sha256(values.str());
}

inline std::string numeric_build_identity(double contraction, double fused) {
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
#ifdef PFH_NUMERIC_BUILD_FLAGS_HASH
    output << ";flags_sha256:" << PFH_NUMERIC_BUILD_FLAGS_HASH;
#else
    output << ";flags_sha256:unavailable";
#endif
    std::uint64_t contraction_bits, fused_bits;
    std::memcpy(&contraction_bits, &contraction, sizeof(contraction_bits));
    std::memcpy(&fused_bits, &fused, sizeof(fused_bits));
    output << ";contract_canary:" << contraction_bits << ':' << fused_bits
           << ";libm_probe_sha256:" << runtime_math_fingerprint();
    output << ";eval:" << FLT_EVAL_METHOD << ";double:" << sizeof(double) << ':'
           << std::numeric_limits<double>::digits << ':'
           << std::numeric_limits<double>::is_iec559 << ";long_double:"
           << std::numeric_limits<long double>::digits << ':'
           << std::numeric_limits<long double>::max_exponent
           << ";round:" << std::fegetround();
    return output.str();
}

}
