#pragma once

#include <pineforge/hpo/error.hpp>

#include "portable_math.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <stdexcept>

namespace pineforge::hpo::detail {

struct DoublePair {
    double high;
    double low;
};

inline DoublePair exact_sum(double first, double second) {
    const double high = first + second;
    const double second_part = high - first;
    return {high, (first - (high - second_part)) + (second - second_part)};
}

inline DoublePair grid_coordinate(double value, double low, double step) {
    DoublePair difference = exact_sum(value, -low);
    if (!std::isfinite(difference.high)) {
        difference = exact_sum(value * 0.5, -low * 0.5);
        step *= 0.5;
    }
    const double quotient = difference.high / step;
    if (!std::isfinite(quotient))
        return {quotient, 0.0};
    const double remainder = math::fma(-quotient, step, difference.high) + difference.low;
    return exact_sum(quotient, remainder / step);
}

inline double pair_floor(DoublePair value) {
    const double integral = math::floor(value.high);
    return integral + math::floor((value.high - integral) + value.low);
}

inline double pair_round(DoublePair value) {
    const double lower = pair_floor(value);
    const double fraction = (value.high - lower) + value.low;
    return fraction >= 0.5 ? lower + 1.0 : lower;
}

inline DoublePair integer_pair(std::uint64_t value) {
    return exact_sum(static_cast<double>(value >> 32) * 0x1p32,
                     static_cast<double>(value & 0xffffffffU));
}

inline std::uint64_t pair_floor_unsigned(DoublePair value) {
    if (!std::isfinite(value.high) || value.high < 0.0 || value.high > 0x1p64 ||
        (value.high == 0x1p64 && value.low >= 0.0))
        throw TypedHpoError<std::overflow_error>("hpo_study_spec_invalid",
                                                 {{"reason", "search_space"}},
                                                 "grid coordinate exceeds uint64_t");
    const double integral = math::floor(value.high);
    const double residual = math::floor((value.high - integral) + value.low);
    const auto base = integral == 0x1p64 ? UINT64_MAX :
                      static_cast<std::uint64_t>(integral);
    const double adjustment = residual + (integral == 0x1p64 ? 1.0 : 0.0);
    if (adjustment < 0.0) {
        const auto amount = static_cast<std::uint64_t>(-adjustment);
        if (amount > base)
            throw TypedHpoError<std::overflow_error>(
                "hpo_study_spec_invalid", {{"reason", "search_space"}}, "negative grid coordinate");
        return base - amount;
    }
    const auto amount = static_cast<std::uint64_t>(adjustment);
    if (amount > UINT64_MAX - base)
        throw TypedHpoError<std::overflow_error>("hpo_study_spec_invalid",
                                                 {{"reason", "search_space"}},
                                                 "grid coordinate exceeds uint64_t");
    return base + amount;
}

inline std::uint64_t portable_grid_count(double low, double high, double step) {
    auto last = pair_floor_unsigned(grid_coordinate(high, low, step));
    if (last == UINT64_MAX)
        throw TypedHpoError<std::overflow_error>("hpo_study_spec_invalid",
                                                 {{"reason", "search_space"}},
                                                 "real dimension cardinality exceeds uint64_t");
    const auto next = last + 1;
    const double decoded = math::fma(static_cast<double>(next), step, low);
    if (std::isfinite(decoded) && decoded <= math::nextafter(high,
            std::numeric_limits<double>::infinity()))
        last = next;
    if (last == std::numeric_limits<std::uint64_t>::max())
        throw TypedHpoError<std::overflow_error>("hpo_study_spec_invalid",
                                                 {{"reason", "search_space"}},
                                                 "real dimension cardinality exceeds uint64_t");
    return last + 1;
}

inline double center_coordinate(std::uint64_t ordinal, std::uint64_t count) {
    const auto integer = integer_pair(ordinal);
    const auto center = exact_sum(integer.high, integer.low + 0.5);
    const auto denominator = integer_pair(count);
    const double quotient = center.high / denominator.high;
    const double remainder = math::fma(-quotient, denominator.high, center.high) +
                             center.low - quotient * denominator.low;
    return quotient + remainder / denominator.high;
}

inline std::uint64_t coordinate_ordinal(double normalized, std::uint64_t count) {
    const double unit = std::clamp(normalized, 0.0, 1.0);
    const auto denominator = integer_pair(count);
    const double product = unit * denominator.high;
    const auto coordinate = exact_sum(product, math::fma(unit, denominator.high, -product) +
                                              unit * denominator.low);
    return std::min(pair_floor_unsigned(coordinate), count - 1);
}

}
