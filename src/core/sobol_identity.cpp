// Runtime composition of the Sobol numeric identity.
//
// The generated header carries the digest of the build descriptor, bound at build time to the
// compile commands of the Sobol and portable-math translation units (cmake/SobolIdentity.cmake).
// This translation unit adds what only the running process can say: the mapper and table
// constants, and the current rounding, contraction-canary and portable-probe evidence. It must be
// compiled with the same strict arithmetic options as the other Sobol sources; the helper applies
// them, and the tests check the bound command.
#include <pineforge/hpo/sobol_identity.hpp>

#include <sobol_identity_generated.hpp>

#include <pineforge/hpo/error.hpp>

#include "numeric_build.hpp"
#include "sobol_engine.hpp"
#include "sobol_mapper.hpp"

#include <cfloat>
#include <locale>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>

namespace pineforge::hpo {
namespace {

namespace generated = detail::sobol_identity_generated;

}  // namespace

bool sobol_numeric_identity_bound() { return generated::kBound; }

std::string_view sobol_numeric_identity_unbound_reason() {
    return {generated::kUnboundReason, sizeof(generated::kUnboundReason) - 1};
}

std::string_view sobol_numeric_identity_descriptor() {
    return {generated::kDescriptor, sizeof(generated::kDescriptor) - 1};
}

std::string sobol_numeric_build_identity() {
    // The portable environment first: the same typed refusal as the TPE sampler.
    detail::require_portable_environment();
    if (!generated::kBound) {
        const auto reason = sobol_numeric_identity_unbound_reason();
        throw TypedHpoError<std::runtime_error>(
            "hpo_toolchain_unavailable", {{"reason", "native_runner"}},
            "Sobol floating-point sampling is unavailable in this build: its numeric identity is "
            "unbound (" +
                std::string(reason.empty() ? std::string_view("identity_missing") : reason) +
                ")");
    }
    // Contraction canaries. A contracted C++ translation unit makes the first value equal to the
    // fused one; the C value comes from the portable-math library's own canary function.
    volatile double first = 0x1.0000000000001p0;
    volatile double second = 0x1.ffffffffffffep-1;
    volatile double third = -1.0;
    const double arithmetic = first * second + third;
    const double fused = detail::math::fma(first, second, third);
    const double c_arithmetic = pfh_math_contraction_canary(first, second, third);

    std::ostringstream output;
    output.imbue(std::locale::classic());
    output << "portable-sobol-v1"
           << ";mapper:" << detail::kSobolMapperContract << ":r" << detail::kSobolMapperRevision
           << ";table:" << detail::kSobolTableName << ':' << detail::kSobolTableSubsetSha256
           << ":w" << detail::kSobolWordBits
           << ";build_sha256:" << generated::kBuildDigest
           << ";binary64:53;eval:" << FLT_EVAL_METHOD << ";round:nearest;subnormals:gradual"
           << ";contract_canary:" << pfh_math_bits(arithmetic) << ':' << pfh_math_bits(fused)
           << ";c_canary:" << pfh_math_bits(c_arithmetic)
           << ";portable_probe_sha256:" << detail::runtime_math_fingerprint();
    return output.str();
}

}  // namespace pineforge::hpo
