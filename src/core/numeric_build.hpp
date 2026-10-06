#pragma once

#include "sha256.hpp"
#include "portable_math.hpp"
#include "tpe_algorithm.hpp"

#include <array>
#include <cerrno>
#include <cfenv>
#include <cfloat>
#include <cstdint>
#include <iomanip>
#include <limits>
#include <locale>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>

#if __has_include("numeric_build_flags.hpp")
#include "numeric_build_flags.hpp"
#endif

namespace pineforge::hpo::detail {

inline void require_portable_environment() {
    static_assert(sizeof(double) == 8 && std::numeric_limits<double>::digits == 53 &&
                  std::numeric_limits<double>::is_iec559, "TPE requires IEEE-754 binary64");
    if (FLT_EVAL_METHOD != 0 || std::fegetround() != FE_TONEAREST)
        throw std::invalid_argument("portable TPE requires binary64 round-to-nearest evaluation");
#if defined(__FAST_MATH__) && __FAST_MATH__
    throw std::invalid_argument("portable TPE refuses fast-math");
#endif
#if defined(__x86_64__)
    std::uint32_t control;
    __asm__("stmxcsr %0" : "=m"(control));
    if (control & ((1U << 15) | (1U << 6)))
        throw std::invalid_argument("portable TPE requires gradual underflow (FTZ/DAZ off)");
    static const bool fma_available = __builtin_cpu_supports("fma");
    if (!fma_available)
        throw std::invalid_argument("portable TPE requires x86-64 FMA3");
#elif defined(__aarch64__)
    std::uint64_t control;
    __asm__("mrs %0, fpcr" : "=r"(control));
    if (control & ((std::uint64_t{1} << 24) | (std::uint64_t{1} << 19)))
        throw std::invalid_argument("portable TPE requires gradual underflow (FZ off)");
#endif
}

inline const std::string& runtime_math_fingerprint() {
    static const std::string fingerprint = [] {
        const int saved_errno = errno;
        std::fenv_t environment;
        std::fegetenv(&environment);
        std::ostringstream values;
        values.imbue(std::locale::classic());
        const std::array<double, 12> arguments = {
            0x1p-1074, 0x1p-1022, 0x1p-52, 0.125, 0.5, 0.75,
            1.0, 1.5, 2.0, 8.0, 32.0, 700.0};
        for (const double argument : arguments) {
            for (const double result : {math::log(argument), math::log1p(argument),
                    math::exp(argument), math::expm1(argument), math::cos(argument),
                    math::erfc(argument), math::sqrt(argument)})
                values << pfh_math_bits(result) << ':';
        }
        for (const double argument : {-0.0, -0x1p-52, -0.5, -0.75, -32.0, -700.0}) {
            for (const double result : {math::exp(argument), math::expm1(argument),
                    math::cos(argument), math::erfc(argument)})
                values << pfh_math_bits(result) << ':';
            if (argument > -1.0)
                values << pfh_math_bits(math::log1p(argument)) << ':';
        }
        const auto result = sha256(values.str());
        std::fesetenv(&environment);
        errno = saved_errno;
        return result;
    }();
    return fingerprint;
}

inline std::string numeric_build_identity(double contraction, double fused,
    std::uint32_t = 0, const std::optional<std::string>& = {}) {
    std::ostringstream output;
    output.imbue(std::locale::classic());
    output << "portable-tpe-v" << kTpeAlgorithmRevision
           << ";core_math:aa66f20b0118453890acb29b98b51c9c8dd92118"
           << ";binary64:53;math_shim:1;semantic_flags:strict"
           << ";contract_canary:" << pfh_math_bits(contraction) << ':' << pfh_math_bits(fused)
           << ";libm_functions:0;portable_probe_sha256:" << runtime_math_fingerprint()
           << ";eval:" << FLT_EVAL_METHOD << ";round:nearest;subnormals:gradual";
#ifndef PFH_NUMERIC_BUILD_FLAGS_HASH
    output << ";flags_sha256:unavailable";
#endif
    return output.str();
}

}
