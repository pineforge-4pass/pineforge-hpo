#pragma once

// Maps one 64-bit Sobol coordinate X' onto one search-space dimension (contract N6).
//
// STATUS: UNEXECUTED source preparation (methods-sobol-mapper leaf, base d2f83326). Nothing here has
// been compiled or run; the first compile and every test belong to the spot proof.
//
// Rules, from methods-sobol-contract.pin.md and methods-sobol-contract.revised.md N5/N6:
//   - discrete dimensions take idx = floor(X' * count / 2^64), the high word of the exact 128-bit
//     product, and decode idx on the same per-dimension lattice as SearchSpace::candidate_at
//     (integer_count/integer_at and portable_grid_count/real_at of search_space.cpp);
//   - continuous dimensions take the 53-bit unit u = (X' >> 11) * 2^-53, exactly;
//   - no rejection, reseeding, retry or fallback to another random source;
//   - the count of one dimension is never combined with another: a space whose product overflows
//     uint64 (or that has a continuous real) is still indexable dimension by dimension.
//
// Build requirement for the integrator: sobol_mapper.cpp MUST be compiled with
// -fno-fast-math -ffp-contract=off -frounding-math -fno-builtin -fno-lto, the same recipe as
// search_space.cpp/tpe_sampler.cpp, and those actual per-source flags MUST be bound into the Sobol
// numeric identity (see docs in reports/ar/methods-sobol-mapper-prep.md, section "Identity needs").
// The linear-real expression is additionally written so a fused multiply-add cannot form across
// statements (volatile barrier), but that is a second line of defence, not a substitute.

#include <pineforge/hpo/search_space.hpp>
#include <pineforge/hpo/types.hpp>

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>

namespace pineforge::hpo::detail {

/// Name of the mapping contract; bound by the Sobol mapper identity.
inline constexpr std::string_view kSobolMapperContract = "pineforge_sobol_mapper_v1";

/// Revision of the mapping arithmetic. Bump on ANY change of a mapped value, even by one ulp.
/// The Sobol identity (descriptor hash, src/cli/sobol_continuation.hpp) binds this value.
///   1: first draft, never released. Log-real exp overflow used two half-exponent factors, which can
///      itself overflow (smallest subnormal .. DBL_MAX near unit 63/64).
///   2: log-real exp overflow uses q = exp(z * 0.25) and four ordered multiplications from `low`
///      (AR amendment 2026-10-09 14:10). Results where exp(z) is finite are byte-identical to 1.
inline constexpr std::uint32_t kSobolMapperRevision = 2;

// The column ceiling (columns 0..1023, 1024 varying dimensions) is the engine's detail::kSobolMaxColumns
// in sobol_engine.hpp; it is deliberately not redefined here (a second inline constexpr of the same
// name in this namespace would be a redefinition error wherever both headers are included).

/// A log-integer dimension needs low - 0.5 exact in binary64, which holds only for low <= 2^52 (N6).
inline constexpr std::int64_t kSobolLogIntegerMaxLow = std::int64_t{1} << 52;

/// A log-integer dimension needs high - low + 1 exact in binary64, i.e. at most 2^53 values (N6).
inline constexpr std::uint64_t kSobolLogIntegerMaxSpan = std::uint64_t{1} << 53;

/// High 64 bits of the exact 128-bit product coordinate * count, i.e. floor(coordinate * count / 2^64).
/// Pure 64-bit limb arithmetic; no compiler extension. The result is below count when count >= 1.
std::uint64_t sobol_mul_high64(std::uint64_t coordinate, std::uint64_t count) noexcept;

/// The 53-bit continuous unit (coordinate >> 11) * 2^-53 in [0, 1 - 2^-53], exact in binary64.
double sobol_unit53(std::uint64_t coordinate) noexcept;

/// How a dimension consumes its coordinate.
enum class SobolMapKind : std::uint8_t {
    Constant,     ///< low == high (or one categorical choice): no column, no coordinate.
    Integer,      ///< Linear integer lattice, count from integer_count.
    SteppedReal,  ///< Stepped-real lattice, count from portable_grid_count.
    Boolean,      ///< Two-point lattice.
    Categorical,  ///< Lattice over two or more choices.
    LinearReal,   ///< Continuous: low + (high - low) * u.
    LogReal,      ///< Continuous: low * exp(L * u).
    LogInteger,   ///< Continuous latent log-uniform integer (binary64 form of sampler.cpp:115-137).
};

/// True for the kinds whose value passes through binary64 arithmetic, hence through the build recipe
/// and the portable-math identity (N11): stepped real, linear real, log real and log integer.
inline constexpr bool sobol_map_uses_floating_point(SobolMapKind kind) noexcept {
    return kind == SobolMapKind::SteppedReal || kind == SobolMapKind::LinearReal ||
           kind == SobolMapKind::LogReal || kind == SobolMapKind::LogInteger;
}

/// Immutable per-dimension mapper. Construction validates and precomputes; value_at() is a pure
/// function of the coordinate and may be called concurrently.
class SobolDimensionMap {
public:
    /// Builds the mapper for @p dimension.
    /// @throws TypedHpoError<std::overflow_error> (hpo_study_spec_invalid, reason sampler) when a
    ///         discrete count does not fit uint64 (an integer range with 2^64 values).
    /// @throws TypedHpoError<std::invalid_argument> (hpo_study_spec_invalid, reason sampler) for a
    ///         log-integer range outside 1 <= low <= 2^52 and high - low + 1 <= 2^53.
    explicit SobolDimensionMap(const Dimension& dimension);

    /// Mapping kind; Constant dimensions take no Sobol column.
    SobolMapKind kind() const noexcept { return kind_; }
    /// True when the dimension consumes a Sobol column (kind != Constant).
    bool varying() const noexcept { return kind_ != SobolMapKind::Constant; }
    /// Dimension name.
    const std::string& name() const noexcept { return name_; }
    /// Lattice size of the four discrete kinds; zero for Constant and the continuous kinds.
    std::uint64_t count() const noexcept { return count_; }
    /// The value for @p coordinate, deterministic and independent of any other call.
    /// @throws TypedHpoError<std::logic_error> (hpo_invariant) if a stepped-real decode escapes its bounds.
    ParameterValue value_at(std::uint64_t coordinate) const;

private:
    Dimension dimension_;
    std::string name_;
    SobolMapKind kind_ = SobolMapKind::Constant;
    std::uint64_t count_ = 0;
    ParameterValue constant_ = std::int64_t{0};
    // Log kinds: lower_edge_ = low - 0.5 and span_ = high - low + 1 (log integer); log_ratio_ is
    // log1p(span / lower_edge) (log integer) or L of N6 (log real).
    double lower_edge_ = 0.0;
    double span_ = 0.0;
    double log_ratio_ = 0.0;
};

}  // namespace pineforge::hpo::detail
