#pragma once

#include <array>
#include <cfenv>
#include <cerrno>
#include <cfloat>
#include <cmath>
#include <cstring>
#include <iomanip>
#include <limits>
#include <locale>
#include <optional>
#include <sstream>
#include <string>
#include "sha256.hpp"
#if __has_include("numeric_build_flags.hpp")
#include "numeric_build_flags.hpp"
#endif

namespace pineforge::hpo::detail {

inline std::array<std::string, 22> compute_runtime_math_fingerprints() {
    std::fenv_t environment;
    std::fegetenv(&environment);
    const auto saved_errno = errno;
    std::array<std::ostringstream, 22> values;
    for (auto& stream : values) {
        stream.imbue(std::locale::classic());
        stream << std::hexfloat <<
            std::setprecision(std::numeric_limits<long double>::max_digits10);
    }
    using DoubleFunction = double (*)(double);
    using LongFunction = long double (*)(long double);
    DoubleFunction volatile doubles[]{std::log, std::log1p, std::exp, std::expm1,
        std::sqrt, std::cos, std::erfc, std::floor, std::ceil, std::round};
    LongFunction volatile longs[]{std::log, std::log1p, std::exp, std::expm1,
        std::sqrt, std::cos, std::erfc, std::floor, std::ceil, std::round};
    double (*volatile double_fma)(double, double, double) = std::fma;
    long double (*volatile long_fma)(long double, long double, long double) = std::fma;
    for (std::uint32_t index = 0; index < 4096; ++index) {
        const double ratio = static_cast<double>(index) / 4095.0;
        const double positive = std::ldexp(1.0 + ratio, static_cast<int>(index % 1800) - 900);
        const double near_zero = std::ldexp(1.0 + ratio, -static_cast<int>(index % 900) - 1);
        const double log1p_input = index % 2 ? -ratio * (1.0 - 0x1p-52) : positive;
        const double expm1_input = index % 2 ? -near_zero : -745.0 + ratio * 1454.0;
        const std::array<double, 10> inputs{positive, log1p_input, -745.0 + ratio * 1454.0,
            expm1_input, positive, ratio * 6.283185307179586,
            -32.0 + ratio * 64.0, (ratio - 0.5) * 0x1p40,
            (ratio - 0.5) * 0x1p40, (ratio - 0.5) * 0x1p40};
        for (std::size_t function_index = 0; function_index < inputs.size(); ++function_index) {
            volatile double argument = inputs[function_index];
            const auto function = doubles[function_index];
            const double result = function(argument);
            std::uint64_t bits;
            std::memcpy(&bits, &result, sizeof(bits));
            values[function_index] << bits << ' ';
            volatile long double wide_argument = inputs[function_index];
            values[10 + function_index] << longs[function_index](wide_argument) << ' ';
        }
        volatile double argument = positive;
        volatile long double wide_argument = positive;
        values[20] << double_fma(argument, 0x1.0000000000001p0, -0.5) << ' ';
        values[21] << long_fma(wide_argument, 0x1.0000000000001p0L, -0.5L) << ' ';
    }
    std::array<std::string, 22> result;
    for (std::size_t index = 0; index < result.size(); ++index)
        result[index] = sha256(values[index].str());
    std::fesetenv(&environment);
    errno = saved_errno;
    return result;
}

inline const std::array<std::string, 22>& runtime_math_fingerprints() {
    static const auto fingerprint = compute_runtime_math_fingerprints();
    return fingerprint;
}

inline std::string runtime_math_fingerprint(
    std::uint32_t functions, const std::optional<std::string>& long_log1p_override = {}) {
    const auto& fingerprints = runtime_math_fingerprints();
    std::string values = "scoped-libm-v1:" + std::to_string(functions) + ':';
    for (std::size_t index = 0; index < fingerprints.size(); ++index) {
        if (functions & (std::uint32_t{1} << index))
            values += index == 11 && long_log1p_override ? *long_log1p_override :
                                                         fingerprints[index];
    }
    return sha256(values);
}

inline const std::string& runtime_math_fingerprint() {
    static const auto fingerprint = runtime_math_fingerprint((std::uint32_t{1} << 22) - 1);
    return fingerprint;
}

inline std::string long_log1p_probe(long double (*function)(long double)) {
    std::fenv_t environment;
    std::fegetenv(&environment);
    const auto saved_errno = errno;
    std::ostringstream values;
    values.imbue(std::locale::classic());
    values << std::hexfloat << std::setprecision(std::numeric_limits<long double>::max_digits10);
    for (std::uint32_t index = 0; index < 4096; ++index) {
        const double ratio = static_cast<double>(index) / 4095.0;
        const double positive = std::ldexp(1.0 + ratio, static_cast<int>(index % 1800) - 900);
        volatile long double argument = index % 2 ? -ratio * (1.0 - 0x1p-52) : positive;
        values << function(argument) << ' ';
    }
    const auto result = sha256(values.str());
    std::fesetenv(&environment);
    errno = saved_errno;
    return result;
}

inline std::string numeric_build_identity(double contraction, double fused,
    std::uint32_t functions = (std::uint32_t{1} << 22) - 1,
    const std::optional<std::string>& long_log1p_override = {}) {
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
           << ";libm_functions:" << functions
           << ";libm_probe_sha256:" << runtime_math_fingerprint(functions, long_log1p_override);
    output << ";eval:" << FLT_EVAL_METHOD << ";double:" << sizeof(double) << ':'
           << std::numeric_limits<double>::digits << ':'
           << std::numeric_limits<double>::is_iec559 << ";long_double:"
           << std::numeric_limits<long double>::digits << ':'
           << std::numeric_limits<long double>::max_exponent
           << ";round:" << std::fegetround();
    return output.str();
}

}
