// UNEXECUTED source preparation (methods-sobol-mapper leaf, base d2f83326); see sobol_mapper.hpp.
//
// The per-dimension count and lattice decoders below replicate, line for line, the anonymous-namespace
// authority in search_space.cpp (integer_count, integer_at, real_grid_count, real_at). They cannot be
// called from there, and calling SearchSpace::finite_cardinality()/candidate_at() would couple one
// dimension to the overflow of the whole product. tests/test_sobol_mapper.cpp checks the equality
// against the authority for every ordinal of several finite spaces.
#include "sobol_mapper.hpp"

#include <pineforge/hpo/error.hpp>

#include "portable_grid.hpp"
#include "portable_math.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <utility>

namespace pineforge::hpo::detail {
namespace {

std::uint64_t integer_count(const IntegerDimension& dimension) {
    const std::uint64_t span =
        static_cast<std::uint64_t>(dimension.high()) - static_cast<std::uint64_t>(dimension.low());
    const std::uint64_t quotient = span / static_cast<std::uint64_t>(dimension.step());
    if (quotient == std::numeric_limits<std::uint64_t>::max()) {
        throw TypedHpoError<std::overflow_error>(
            "hpo_study_spec_invalid", {{"reason", "sampler"}},
            "sobol integer dimension cardinality exceeds uint64_t: " + dimension.name());
    }
    return quotient + 1;
}

std::int64_t integer_at(const IntegerDimension& dimension, std::uint64_t index) {
    const std::uint64_t offset = index * static_cast<std::uint64_t>(dimension.step());
    const std::int64_t low = dimension.low();
    if (low >= 0) {
        return low + static_cast<std::int64_t>(offset);
    }

    const std::uint64_t magnitude_to_zero = 0U - static_cast<std::uint64_t>(low);
    if (offset < magnitude_to_zero) {
        return low + static_cast<std::int64_t>(offset);
    }
    if (offset == magnitude_to_zero) {
        return 0;
    }
    return static_cast<std::int64_t>(offset - magnitude_to_zero);
}

std::uint64_t real_grid_count(const RealDimension& dimension) {
    try {
        return portable_grid_count(dimension.low(), dimension.high(), *dimension.step());
    } catch (const std::overflow_error&) {
        throw TypedHpoError<std::overflow_error>(
            "hpo_study_spec_invalid", {{"reason", "sampler"}},
            "sobol stepped real dimension cardinality exceeds uint64_t: " + dimension.name());
    }
}

double real_at(const RealDimension& dimension, std::uint64_t index, std::uint64_t count) {
    double decoded = math::fma(static_cast<double>(index), *dimension.step(), dimension.low());
    if (index + 1 == count && decoded > dimension.high() &&
        decoded <= math::nextafter(dimension.high(), std::numeric_limits<double>::infinity())) {
        decoded = dimension.high();
    }
    if (!std::isfinite(decoded) || decoded < dimension.low() || decoded > dimension.high()) {
        throw TypedHpoError<std::logic_error>(
            "hpo_invariant", {},
            "sobol real grid decoder escaped dimension bounds: " + dimension.name());
    }
    return decoded;
}

// low + (high - low) * u with the finite fallbacks of sampler.cpp:229-237, then clamped (N6).
// Each product is stored through a volatile object so no compiler can fuse it with the following
// addition, whatever -ffp-contract says; the result then equals separately rounded IEEE operations.
double linear_real(double low, double high, double unit) noexcept {
    volatile double scaled = (high - low) * unit;
    double sampled = low + scaled;
    if (!std::isfinite(sampled)) {
        volatile double from_low = low * (1.0 - unit);
        volatile double from_high = high * unit;
        sampled = from_low + from_high;
    }
    if (!std::isfinite(sampled)) {
        sampled = low;
    }
    return std::clamp(sampled, low, high);
}

// low * exp(z), z = L * u with L fixed per dimension (N6), mapper revision 2 (AR amendment of
// 2026-10-09 14:10).
//
// The literal product is used whenever exp(z) is finite, so those results are byte-identical to
// revision 1. exp(z) overflows binary64 only when high / low is not finite (L = log(high) - log(low),
// at most about 1454.2 for [smallest subnormal, DBL_MAX]); the clamp would then turn +inf into `high`,
// fabricating an endpoint for an interior point. In exactly that case q = exp(z * 0.25) is computed
// once (z * 0.25 is exact, and L_max / 4 is far inside exp's finite domain, so q is finite) and the
// result is built by four ordered binary64 multiplications starting from the positive endpoint `low`:
//   v1 = low * q, v2 = v1 * q, v3 = v2 * q, v4 = v3 * q,
// followed by the bounds clamp. The half-exponent form of revision 1 is NOT used: half of
// L * (63/64) already exceeds the largest finite exp argument (about 709.78) for the smallest
// subnormal and DBL_MAX, so it could itself overflow.
//
// Order is part of the contract: no reassociation, no extended intermediate precision, and no
// formula chosen by host. Each product goes through a volatile object so no compiler can regroup the
// chain (a pure multiplication chain cannot be contracted, but regrouping would change rounding); the
// -fno-fast-math recipe and its identity binding remain required and are not replaced by this barrier.
double log_real(double low, double high, double log_ratio, double unit) noexcept {
    const double exponent = log_ratio * unit;
    const double growth = math::exp(exponent);
    double value;
    if (std::isinf(growth)) {
        const double quarter = math::exp(exponent * 0.25);
        volatile double factor1 = low * quarter;
        volatile double factor2 = factor1 * quarter;
        volatile double factor3 = factor2 * quarter;
        volatile double factor4 = factor3 * quarter;
        value = factor4;
    } else {
        value = low * growth;
    }
    return std::clamp(value, low, high);
}

// The binary64 form of sampler.cpp:115-137 (sample_log_integer): displacement = (low - 0.5) *
// expm1(L * u) with L = log1p(span / (low - 0.5)). lower_edge and span are exact because the
// constructor enforces 1 <= low <= 2^52 and span <= 2^53.
std::int64_t log_integer(const IntegerDimension& dimension,
                         double lower_edge,
                         double span,
                         double log_ratio,
                         double unit) noexcept {
    const double displacement = lower_edge * math::expm1(log_ratio * unit);
    if (displacement <= 0.0) {
        return dimension.low();
    }
    if (displacement >= span) {
        return dimension.high();
    }
    const auto ordinal = static_cast<std::uint64_t>(math::floor(displacement));
    return static_cast<std::int64_t>(static_cast<std::uint64_t>(dimension.low()) + ordinal);
}

}  // namespace

std::uint64_t sobol_mul_high64(std::uint64_t coordinate, std::uint64_t count) noexcept {
    constexpr std::uint64_t kLow32 = 0xFFFFFFFFULL;
    const std::uint64_t coordinate_low = coordinate & kLow32;
    const std::uint64_t coordinate_high = coordinate >> 32U;
    const std::uint64_t count_low = count & kLow32;
    const std::uint64_t count_high = count >> 32U;

    const std::uint64_t low_low = coordinate_low * count_low;
    const std::uint64_t high_low = coordinate_high * count_low;
    const std::uint64_t low_high = coordinate_low * count_high;
    const std::uint64_t high_high = coordinate_high * count_high;

    // The middle column cannot overflow: (2^32-1) + (2^32-1) + (2^32-1)^2 < 2^64.
    const std::uint64_t middle = (low_low >> 32U) + (high_low & kLow32) + low_high;
    return high_high + (high_low >> 32U) + (middle >> 32U);
}

double sobol_unit53(std::uint64_t coordinate) noexcept {
    return static_cast<double>(coordinate >> 11U) * 0x1p-53;
}

SobolDimensionMap::SobolDimensionMap(const Dimension& dimension)
    : dimension_(dimension), name_(dimension_name(dimension)) {
    if (const auto* integer = std::get_if<IntegerDimension>(&dimension_)) {
        if (integer->low() == integer->high()) {
            kind_ = SobolMapKind::Constant;
            constant_ = integer->low();
        } else if (integer->log()) {
            kind_ = SobolMapKind::LogInteger;
            const std::int64_t low = integer->low();
            const std::uint64_t span = static_cast<std::uint64_t>(integer->high()) -
                                       static_cast<std::uint64_t>(low) + 1;
            if (low < 1 || low > kSobolLogIntegerMaxLow || span > kSobolLogIntegerMaxSpan) {
                throw TypedHpoError<std::invalid_argument>(
                    "hpo_study_spec_invalid", {{"reason", "sampler"}},
                    "sobol log integer dimension needs 1 <= low <= 2^52 and high - low + 1 <= 2^53: " +
                        name_);
            }
            lower_edge_ = static_cast<double>(low) - 0.5;
            span_ = static_cast<double>(span);
            log_ratio_ = math::log1p(span_ / lower_edge_);
        } else {
            kind_ = SobolMapKind::Integer;
            count_ = integer_count(*integer);
        }
    } else if (const auto* real = std::get_if<RealDimension>(&dimension_)) {
        if (real->low() == real->high()) {
            kind_ = SobolMapKind::Constant;
            constant_ = real->low();
        } else if (real->log()) {
            kind_ = SobolMapKind::LogReal;
            const double relative_span = (real->high() - real->low()) / real->low();
            log_ratio_ = std::isfinite(relative_span)
                             ? math::log1p(relative_span)
                             : math::log(real->high()) - math::log(real->low());
        } else if (real->step().has_value()) {
            kind_ = SobolMapKind::SteppedReal;
            count_ = real_grid_count(*real);
        } else {
            kind_ = SobolMapKind::LinearReal;
        }
    } else if (std::holds_alternative<BooleanDimension>(dimension_)) {
        kind_ = SobolMapKind::Boolean;
        count_ = 2;
    } else {
        const auto& choices = std::get<CategoricalDimension>(dimension_).choices();
        if (choices.size() == 1) {
            kind_ = SobolMapKind::Constant;
            constant_ = choices.front();
        } else {
            kind_ = SobolMapKind::Categorical;
            count_ = static_cast<std::uint64_t>(choices.size());
        }
    }
}

ParameterValue SobolDimensionMap::value_at(std::uint64_t coordinate) const {
    switch (kind_) {
    case SobolMapKind::Constant:
        return constant_;
    case SobolMapKind::Integer:
        return integer_at(std::get<IntegerDimension>(dimension_),
                          sobol_mul_high64(coordinate, count_));
    case SobolMapKind::SteppedReal:
        return real_at(std::get<RealDimension>(dimension_), sobol_mul_high64(coordinate, count_),
                       count_);
    case SobolMapKind::Boolean:
        return sobol_mul_high64(coordinate, 2) != 0;
    case SobolMapKind::Categorical:
        return std::get<CategoricalDimension>(dimension_)
            .choices()[static_cast<std::size_t>(sobol_mul_high64(coordinate, count_))];
    case SobolMapKind::LinearReal: {
        const auto& real = std::get<RealDimension>(dimension_);
        return linear_real(real.low(), real.high(), sobol_unit53(coordinate));
    }
    case SobolMapKind::LogReal: {
        const auto& real = std::get<RealDimension>(dimension_);
        return log_real(real.low(), real.high(), log_ratio_, sobol_unit53(coordinate));
    }
    case SobolMapKind::LogInteger:
        return log_integer(std::get<IntegerDimension>(dimension_), lower_edge_, span_, log_ratio_,
                           sobol_unit53(coordinate));
    }
    throw TypedHpoError<std::logic_error>("hpo_invariant", {}, "unknown sobol mapping kind");
}

}  // namespace pineforge::hpo::detail
