#include "pineforge/hpo/sampler.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <type_traits>
#include <utility>

namespace pineforge::hpo {

WarmStartObservation WarmStartSource::observation(const SearchSpace& space,
                                                 std::uint64_t row) const {
    Candidate candidate;
    candidate.id = id(row);
    for (std::size_t index = 0; index < space.dimensions().size(); ++index)
        candidate.values.emplace(std::string(dimension_name(space.dimensions()[index])),
                                 parameter(row, index));
    return {std::move(candidate), objective(row)};
}
namespace {

std::uint64_t integer_count(const IntegerDimension& dimension) {
    const std::uint64_t span =
        static_cast<std::uint64_t>(dimension.high()) - static_cast<std::uint64_t>(dimension.low());
    const std::uint64_t quotient = span / static_cast<std::uint64_t>(dimension.step());
    if (quotient == std::numeric_limits<std::uint64_t>::max()) {
        throw std::overflow_error("integer dimension cardinality exceeds uint64_t");
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
    if (!dimension.step().has_value()) {
        throw std::invalid_argument("grid sampling a real dimension requires a step: " +
                                    dimension.name());
    }
    const long double low = static_cast<long double>(dimension.low());
    const long double high = static_cast<long double>(dimension.high());
    const long double step = *dimension.step();
    const long double span = high - low;
    const long double scaled = std::isfinite(span) ? span / step : high / step - low / step;
    const long double floored = std::floor(scaled);
    if (floored >= static_cast<long double>(std::numeric_limits<std::uint64_t>::max())) {
        throw std::overflow_error("real dimension cardinality exceeds uint64_t");
    }
    std::uint64_t last_index = static_cast<std::uint64_t>(floored);
    const std::uint64_t next_index = last_index + 1;
    const double decoded_next =
        std::fma(static_cast<double>(next_index), *dimension.step(), dimension.low());
    if (std::isfinite(decoded_next) &&
        decoded_next <= std::nextafter(dimension.high(), std::numeric_limits<double>::infinity())) {
        last_index = next_index;
    }
    if (last_index == std::numeric_limits<std::uint64_t>::max()) {
        throw std::overflow_error("real dimension cardinality exceeds uint64_t");
    }
    return last_index + 1;
}

double real_at(const RealDimension& dimension, std::uint64_t index) {
    const std::uint64_t count = real_grid_count(dimension);
    double decoded = std::fma(static_cast<double>(index), *dimension.step(), dimension.low());
    if (index + 1 == count && decoded > dimension.high() &&
        decoded <= std::nextafter(dimension.high(), std::numeric_limits<double>::infinity())) {
        decoded = dimension.high();
    }
    return decoded;
}

std::uint64_t bounded_random(std::mt19937_64& engine, std::uint64_t bound) {
    if (bound == 0) {
        throw std::invalid_argument("random bound must be positive");
    }
    // Rejection sampling avoids modulo bias while depending only on the
    // standardized mt19937_64 output, rather than implementation-specific
    // std::uniform_*_distribution behavior.
    const std::uint64_t threshold = (0U - bound) % bound;
    while (true) {
        const std::uint64_t value = engine();
        if (value >= threshold) {
            return value % bound;
        }
    }
}

double unit_random(std::mt19937_64& engine) {
    constexpr double kInverse53 = 1.0 / 9007199254740992.0;
    return static_cast<double>(engine() >> 11U) * kInverse53;
}

std::int64_t sample_log_integer(const IntegerDimension& dimension, double unit) {
    if (dimension.low() == dimension.high()) {
        return dimension.low();
    }

    const std::uint64_t span = static_cast<std::uint64_t>(dimension.high()) -
                               static_cast<std::uint64_t>(dimension.low()) + 1;
    const long double lower_edge = static_cast<long double>(dimension.low()) - 0.5L;
    const long double log_ratio = std::log1p(static_cast<long double>(span) / lower_edge);
    // Algebraically this samples z uniformly from
    // [ln(low - 0.5), ln(high + 0.5)] and rounds exp(z). Computing the
    // displacement with expm1 preserves adjacent int64 values whose absolute
    // logarithms collapse to the same floating-point number.
    const long double displacement = lower_edge * std::expm1(log_ratio * unit);
    if (displacement <= 0.0L) {
        return dimension.low();
    }
    if (displacement >= static_cast<long double>(span)) {
        return dimension.high();
    }
    const auto ordinal = static_cast<std::uint64_t>(std::floor(displacement));
    return static_cast<std::int64_t>(static_cast<std::uint64_t>(dimension.low()) + ordinal);
}

double sample_log_real(const RealDimension& dimension, double unit) {
    if (dimension.low() == dimension.high()) {
        return dimension.low();
    }

    const long double low = dimension.low();
    const long double high = dimension.high();
    const long double relative_span = (high - low) / low;
    const long double log_ratio =
        std::isfinite(relative_span) ? std::log1p(relative_span) : std::log(high) - std::log(low);
    const double value = static_cast<double>(low * std::exp(log_ratio * unit));
    return std::clamp(value, dimension.low(), dimension.high());
}

}  // namespace

const char* candidate_policy_name(CandidatePolicy policy) noexcept {
    switch (policy) {
    case CandidatePolicy::SamplerDefault:
        return "sampler_default";
    case CandidatePolicy::WithoutReplacement:
        return "without_replacement";
    case CandidatePolicy::Exhaustive:
        return "exhaustive";
    }
    return "unknown";
}

std::uint64_t continuation_seed(std::uint64_t seed, std::uint64_t warm_trials) noexcept {
    if (warm_trials == 0)
        return seed;
    auto mixed = seed ^ (warm_trials + 0x9e3779b97f4a7c15ULL);
    mixed = (mixed ^ (mixed >> 30)) * 0xbf58476d1ce4e5b9ULL;
    mixed = (mixed ^ (mixed >> 27)) * 0x94d049bb133111ebULL;
    return mixed ^ (mixed >> 31);
}

GridSampler::GridSampler(SearchSpace space) : space_(std::move(space)) {
    const auto cardinality = space_.finite_cardinality();
    if (!cardinality.has_value()) {
        throw std::invalid_argument(
            "grid sampling requires a step on every varying real dimension");
    }
    total_candidates_ = *cardinality;
}

std::optional<Candidate> GridSampler::next() {
    if (generated_ >= total_candidates_) {
        return std::nullopt;
    }

    Candidate candidate = space_.candidate_at(generated_, generated_);
    ++generated_;
    return candidate;
}

void GridSampler::reset() {
    generated_ = 0;
}

RandomSampler::RandomSampler(SearchSpace space, std::uint64_t seed, std::uint64_t max_candidates)
    : space_(std::move(space)), seed_(seed), max_candidates_(max_candidates), engine_(seed) {}

std::optional<Candidate> RandomSampler::next() {
    if (max_candidates_ != 0 && generated_ >= max_candidates_) {
        return std::nullopt;
    }

    Candidate candidate;
    candidate.id = generated_;
    for (const auto& dimension : space_.dimensions()) {
        ParameterValue value = std::visit(
            [&](const auto& item) -> ParameterValue {
                using T = std::decay_t<decltype(item)>;
                if constexpr (std::is_same_v<T, IntegerDimension>) {
                    if (item.log()) {
                        return sample_log_integer(item, unit_random(engine_));
                    }
                    return integer_at(item, bounded_random(engine_, integer_count(item)));
                } else if constexpr (std::is_same_v<T, RealDimension>) {
                    if (item.log()) {
                        return sample_log_real(item, unit_random(engine_));
                    }
                    if (item.step().has_value()) {
                        return real_at(item, bounded_random(engine_, real_grid_count(item)));
                    }
                    if (item.low() == item.high()) {
                        return item.low();
                    }
                    const double unit = unit_random(engine_);
                    double sampled = item.low() + (item.high() - item.low()) * unit;
                    if (!std::isfinite(sampled)) {
                        sampled = item.low() * (1.0 - unit) + item.high() * unit;
                    }
                    if (!std::isfinite(sampled)) {
                        sampled = item.low();
                    }
                    return sampled;
                } else if constexpr (std::is_same_v<T, BooleanDimension>) {
                    return bounded_random(engine_, 2) != 0;
                } else {
                    return item.choices()[static_cast<std::size_t>(
                        bounded_random(engine_, item.choices().size()))];
                }
            },
            dimension);
        candidate.values.emplace(std::string(dimension_name(dimension)), std::move(value));
    }
    ++generated_;
    return candidate;
}

void RandomSampler::reset() {
    engine_.seed(seed_);
    generated_ = 0;
}

}  // namespace pineforge::hpo
