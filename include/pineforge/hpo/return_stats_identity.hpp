#pragma once

#include "return_stats_identity_generated.hpp"

#include <string_view>

namespace pineforge::hpo {

/// Statistics contract that the return-statistics build identity is bound to.
inline constexpr std::string_view kReturnStatsContract = "pineforge-hpo-return-stats/v1";

namespace detail {

static_assert(std::string_view{return_stats_identity_generated::kContract} == kReturnStatsContract,
              "this build was configured with a different return-statistics contract");

}  // namespace detail

/// Contract string that was bound into this build's identity at configure time.
constexpr std::string_view return_stats_contract() noexcept {
    return {detail::return_stats_identity_generated::kContract,
            sizeof(detail::return_stats_identity_generated::kContract) - 1};
}

/// Opaque identity of the build that computes return statistics.
///
/// It binds the reducer source digest, the compiler identity and version, the actual
/// translation-unit compile flags and the contract string. It is the same on every sampler path
/// (grid, random, TPE and candidate lists) and is never derived from, combined with or compared
/// to a sampler checkpoint identity. Equal identities name equal builds; they do not attest that
/// two builds produce equal arithmetic, and they say nothing about other architectures.
constexpr std::string_view return_stats_numeric_build_identity() noexcept {
    return {detail::return_stats_identity_generated::kIdentity,
            sizeof(detail::return_stats_identity_generated::kIdentity) - 1};
}

/// Exact descriptor text that the identity is the SHA-256 digest of.
///
/// The text names every hashed source and header with its digest, the compiler fields and one
/// line per compile flag or definition. Proof tooling compares it with the compile command
/// database of the build; it is not part of any result.
constexpr std::string_view return_stats_identity_descriptor() noexcept {
    return {detail::return_stats_identity_generated::kDescriptor,
            sizeof(detail::return_stats_identity_generated::kDescriptor) - 1};
}

/// SHA-256 digest, in lower-case hexadecimal, of the sorted reducer source and header entries.
constexpr std::string_view return_stats_source_digest() noexcept {
    return {detail::return_stats_identity_generated::kSourceDigest,
            sizeof(detail::return_stats_identity_generated::kSourceDigest) - 1};
}

}  // namespace pineforge::hpo
