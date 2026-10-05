#include "pineforge/hpo/sampler.hpp"
#include "ordinal_set.hpp"
#include "sha256.hpp"
#include "mt19937_64.hpp"
#include "numeric_build.hpp"
#include "dimension_workers.hpp"
#include "sampler_checkpoint.hpp"
#include "tpe_test_hooks.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <iomanip>
#include <limits>
#include <map>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <type_traits>
#include <unordered_set>
#include <utility>
#include <vector>

namespace pineforge::hpo {
namespace detail {

thread_local TpeLogRatioObserver tpe_log_ratio_observer = nullptr;
thread_local std::optional<double> tpe_contraction_override;
thread_local std::optional<std::string> tpe_long_log1p_override;

void set_tpe_log_ratio_observer(TpeLogRatioObserver observer) noexcept {
    tpe_log_ratio_observer = observer;
}

void set_tpe_contraction_override(std::optional<double> value) noexcept {
    tpe_contraction_override = value;
}

void set_tpe_long_log1p_probe(long double (*function)(long double)) {
    tpe_long_log1p_override = function ? std::optional<std::string>(long_log1p_probe(function)) :
                                       std::nullopt;
}

std::string tpe_numeric_identity(const SearchSpace& space) {
    std::uint32_t functions = (std::uint32_t{1} << 7) | (std::uint32_t{1} << 8) |
                              (std::uint32_t{1} << 20);
    for (const auto& dimension : space.dimensions()) {
        std::visit([&](const auto& item) {
            using Item = std::decay_t<decltype(item)>;
            if constexpr (std::is_same_v<Item, IntegerDimension> ||
                          std::is_same_v<Item, RealDimension>) {
                if (item.low() != item.high()) {
                    functions |= (std::uint32_t{1} << 10) - 1;
                    if constexpr (std::is_same_v<Item, IntegerDimension>)
                        functions |= std::uint32_t{1} << 17;
                }
                if constexpr (std::is_same_v<Item, RealDimension>)
                    if (item.step())
                        functions |= (std::uint32_t{1} << 17) | (std::uint32_t{1} << 19);
                if (item.log()) {
                    functions |= (std::uint32_t{1} << 10) | (std::uint32_t{1} << 11) |
                                 (std::uint32_t{1} << 13);
                    if constexpr (std::is_same_v<Item, RealDimension>)
                        functions |= std::uint32_t{1} << 12;
                    else
                        functions |= std::uint32_t{1} << 17;
                }
            } else if constexpr (std::is_same_v<Item, CategoricalDimension>) {
                if (item.choices().size() > 1)
                    functions |= std::uint32_t{1};
            } else {
                functions |= std::uint32_t{1};
            }
        }, dimension);
    }
    volatile double first = 0x1.0000000000001p0;
    volatile double second = 0x1.ffffffffffffep-1;
    volatile double third = -1.0;
    const double arithmetic = first * second + third;
    const double fused = std::fma(first, second, third);
    return numeric_build_identity(tpe_contraction_override.value_or(arithmetic), fused,
                                  functions, tpe_long_log1p_override);
}

std::string tpe_numeric_identity() {
    return tpe_numeric_identity(SearchSpace({RealDimension("probe", 0.0, 1.0)}));
}

}
namespace {

constexpr std::uint32_t kTpeAlgorithmRevision = 1;
constexpr double kInverse53 = 1.0 / 9007199254740992.0;
constexpr double kLogSqrtTwoPi = 0.91893853320467274178;
constexpr double kSqrtTwo = 1.41421356237309504880;
constexpr double kTwoPi = 6.28318530717958647693;
constexpr std::uint64_t kMaxExactlyRepresentableBins = std::uint64_t{1} << 53U;
constexpr std::uint64_t kMaxEiCandidates = 1'000'000;

std::uint64_t double_bits(double value) {
    std::uint64_t bits;
    std::memcpy(&bits, &value, sizeof(bits));
    return bits;
}

void state_value(std::ostream& output, const ParameterValue& value) {
    output << value.index() << ':';
    std::visit([&](const auto& item) {
        using Value = std::decay_t<decltype(item)>;
        if constexpr (std::is_same_v<Value, double>)
            output << double_bits(item);
        else if constexpr (std::is_same_v<Value, std::string>)
            output << std::quoted(item);
        else
            output << item;
    }, value);
    output << ' ';
}

std::string state_signature(const SearchSpace& space, std::uint64_t seed,
                            ObjectiveDirection direction, CandidatePolicy policy,
                            const TpeSamplerConfig& config, const std::string& numeric_build) {
    std::ostringstream output;
    output.imbue(std::locale::classic());
    output << "tpe_revision:" << kTpeAlgorithmRevision << ' ' << seed << ' '
           << static_cast<int>(direction) << ' '
           << static_cast<int>(policy) << ' ' << config.startup_trials << ' '
           << config.ei_candidates << ' ' << double_bits(config.gamma_fraction) << ' '
           << config.gamma_cap << ' ' << double_bits(config.prior_weight) << ' '
           << config.constant_liar << ' ' << config.history_switch.value_or(0) << ' '
           << config.scale_ei_candidates << ' ' << config.bad_reservoir_size << ' '
           << std::quoted(numeric_build) << ' ';
    for (const auto& dimension : space.dimensions()) {
        output << dimension.index() << ' ' << std::quoted(std::string(dimension_name(dimension)))
               << ' ';
        std::visit([&](const auto& item) {
            using Value = std::decay_t<decltype(item)>;
            if constexpr (std::is_same_v<Value, IntegerDimension>) {
                output << item.low() << ' ' << item.high() << ' ' << item.step() << ' '
                       << item.log() << ' ';
            } else if constexpr (std::is_same_v<Value, RealDimension>) {
                output << double_bits(item.low()) << ' ' << double_bits(item.high()) << ' '
                       << item.step().has_value() << ' '
                       << double_bits(item.step().value_or(0)) << ' ' << item.log() << ' ';
            } else if constexpr (std::is_same_v<Value, CategoricalDimension>) {
                output << item.choices().size() << ' ';
                for (const auto& value : item.choices())
                    state_value(output, value);
            }
        }, dimension);
    }
    return detail::sha256(output.str());
}

using StateFingerprint = std::array<std::uint64_t, 4>;

void fingerprint_record(StateFingerprint& fingerprint, const std::string& record) {
    const auto digest = detail::sha256(record);
    for (std::size_t index = 0; index < fingerprint.size(); ++index)
        fingerprint[index] ^= std::stoull(digest.substr(index * 16, 16), nullptr, 16);
}

template <typename GetValue>
void fingerprint_candidate(StateFingerprint& fingerprint, std::uint64_t identifier,
                           std::size_t dimensions, GetValue&& get_value) {
    std::ostringstream record;
    record.imbue(std::locale::classic());
    record << "candidate " << identifier << ' ';
    for (std::size_t column = 0; column < dimensions; ++column)
        state_value(record, get_value(column));
    fingerprint_record(fingerprint, record.str());
}

void fingerprint_score(StateFingerprint& fingerprint, std::uint64_t identifier, double score) {
    fingerprint_record(fingerprint, "objective " + std::to_string(identifier) + ' ' +
                        std::to_string(double_bits(score)));
}

std::uint64_t bounded_random(detail::Mt19937_64& engine, std::uint64_t bound) {
    if (bound == 0) {
        throw std::invalid_argument("TPE random bound must be positive");
    }
    const std::uint64_t threshold = (0U - bound) % bound;
    while (true) {
        const std::uint64_t value = engine();
        if (value >= threshold) {
            return value % bound;
        }
    }
}

double unit_random(detail::Mt19937_64& engine) {
    return static_cast<double>(engine() >> 11U) * kInverse53;
}

double open_unit_random(detail::Mt19937_64& engine) {
    return (static_cast<double>(engine() >> 11U) + 0.5) * kInverse53;
}

double standard_normal(detail::Mt19937_64& engine) {
    // Do not cache the second Box-Muller variate: every call consumes exactly
    // two engine values, which makes reset/replay behavior straightforward.
    const double radius = std::sqrt(-2.0 * std::log(open_unit_random(engine)));
    return radius * std::cos(kTwoPi * open_unit_random(engine));
}

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

std::uint64_t integer_ordinal(const IntegerDimension& dimension, std::int64_t value) {
    const std::uint64_t offset =
        static_cast<std::uint64_t>(value) - static_cast<std::uint64_t>(dimension.low());
    return offset / static_cast<std::uint64_t>(dimension.step());
}

std::uint64_t real_grid_count(const RealDimension& dimension) {
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

std::uint64_t real_ordinal(const RealDimension& dimension, double value, std::uint64_t count) {
    const long double scaled =
        (static_cast<long double>(value) - dimension.low()) / *dimension.step();
    const long double rounded = std::round(scaled);
    if (rounded <= 0.0L) {
        return 0;
    }
    if (rounded >= static_cast<long double>(count - 1)) {
        return count - 1;
    }
    return static_cast<std::uint64_t>(rounded);
}

double normalized_ordinal(std::uint64_t ordinal, std::uint64_t count) {
    if (count <= 1) {
        return 0.5;
    }
    return static_cast<double>((static_cast<long double>(ordinal) + 0.5L) /
                               static_cast<long double>(count));
}

std::uint64_t decoded_ordinal(double normalized, std::uint64_t count) {
    if (count <= 1) {
        return 0;
    }
    const long double bounded = std::clamp(static_cast<long double>(normalized), 0.0L, 1.0L);
    const long double rounded = std::floor(bounded * count);
    if (rounded >= static_cast<long double>(count - 1)) {
        return count - 1;
    }
    return static_cast<std::uint64_t>(rounded);
}

std::pair<double, double> normalized_bin(std::uint64_t ordinal, std::uint64_t count) {
    const long double denominator = static_cast<long double>(count);
    const double lower = static_cast<double>(static_cast<long double>(ordinal) / denominator);
    const double upper = static_cast<double>(static_cast<long double>(ordinal + 1) / denominator);
    return {std::clamp(lower, 0.0, 1.0), std::clamp(upper, 0.0, 1.0)};
}

double log_normal_cdf(double value) {
    if (value < -10.0) {
        // Mills-ratio expansion.  In this range the alternating terms shrink
        // rapidly, and this avoids erfc underflow in the far left tail.
        const double inverse_square = 1.0 / (value * value);
        const double correction =
            1.0 + inverse_square *
                      (-1.0 +
                       inverse_square *
                           (3.0 + inverse_square *
                                      (-15.0 + inverse_square * (105.0 - 945.0 * inverse_square))));
        return -0.5 * value * value - std::log(-value) - kLogSqrtTwoPi + std::log(correction);
    }
    if (value >= 0.0) {
        const double upper_tail = 0.5 * std::erfc(value / kSqrtTwo);
        return std::log1p(-upper_tail);
    }
    return std::log(0.5 * std::erfc(-value / kSqrtTwo));
}

double log_difference(double larger_log, double smaller_log) {
    if (smaller_log == -std::numeric_limits<double>::infinity()) {
        return larger_log;
    }
    const double delta = smaller_log - larger_log;
    if (delta >= 0.0) {
        return -std::numeric_limits<double>::infinity();
    }
    return larger_log + std::log(-std::expm1(delta));
}

double log_normal_interval(double lower, double upper) {
    if (!(lower < upper)) {
        return -std::numeric_limits<double>::infinity();
    }

    const double width = upper - lower;
    const double midpoint = lower + 0.5 * width;
    if (std::isfinite(width) && width * std::max(1.0, std::abs(midpoint)) < 1e-5) {
        // The midpoint rule is tail-stable for narrow intervals.  The second
        // order correction keeps its relative error O(width^4).
        const double correction = (midpoint * midpoint - 1.0) * width * width / 24.0;
        return -0.5 * midpoint * midpoint - kLogSqrtTwoPi + std::log(width) +
               std::log1p(correction);
    }

    if (lower >= 0.0) {
        // In the right tail, subtract survival probabilities rather than two
        // CDF values that have both rounded close to one.
        return log_difference(log_normal_cdf(-lower), log_normal_cdf(-upper));
    }
    return log_difference(log_normal_cdf(upper), log_normal_cdf(lower));
}

std::vector<double> observation_weights(std::size_t count) {
    std::vector<double> weights(count, 1.0);
    if (count < 25) {
        return weights;
    }

    const std::size_t ramp_count = count - 25;
    const double first = 1.0 / static_cast<double>(count);
    if (ramp_count == 1) {
        weights[0] = first;
        return weights;
    }
    for (std::size_t i = 0; i < ramp_count; ++i) {
        const double position = static_cast<double>(i) / static_cast<double>(ramp_count - 1);
        weights[i] = first + (1.0 - first) * position;
    }
    return weights;
}

struct WeightedNumericValue {
    double value = 0.0;
    double weight = 0.0;
};

class NumericModel {
public:
    NumericModel(std::vector<double> values,
                 const std::vector<double>& weights,
                 double prior_weight, bool fast_density = false)
        : fast_density_(fast_density) {
        if (values.size() != weights.size()) {
            throw std::logic_error("TPE numeric values and weights have different sizes");
        }

        std::vector<WeightedNumericValue> sorted;
        sorted.reserve(values.size());
        for (std::size_t i = 0; i < values.size(); ++i) {
            sorted.push_back({std::clamp(values[i], 0.0, 1.0), weights[i]});
        }
        std::stable_sort(sorted.begin(), sorted.end(), [](const auto& left, const auto& right) {
            return left.value < right.value;
        });

        const double minimum_sigma =
            1.0 / static_cast<double>(std::min<std::size_t>(100, values.size() + 2));
        components_.reserve(sorted.size() + 1);
        for (std::size_t i = 0; i < sorted.size(); ++i) {
            double left_distance = i == 0 ? sorted[i].value : sorted[i].value - sorted[i - 1].value;
            double right_distance = i + 1 == sorted.size() ? 1.0 - sorted[i].value
                                                           : sorted[i + 1].value - sorted[i].value;
            if (sorted.size() >= 2 && i == 0) {
                left_distance = right_distance;
            }
            if (sorted.size() >= 2 && i + 1 == sorted.size()) {
                right_distance = left_distance;
            }
            const double sigma =
                std::clamp(std::max(left_distance, right_distance), minimum_sigma, 1.0);
            add_component(sorted[i].value, sigma, sorted[i].weight);
        }
        add_component(0.5, 1.0, prior_weight);
        for (auto& component : components_) {
            component.log_weight = std::log(component.weight / total_weight_);
            component.log_normalizer = truncated_log_normalizer(component);
            component.log_sigma = std::log(component.sigma);
            component.amplitude = std::exp(component.log_weight - kLogSqrtTwoPi -
                                           component.log_sigma - component.log_normalizer);
        }
        if (fast_density_) {
            for (const auto& component : components_) {
                const double step = 1.0 / (density_.size() - 1) / component.sigma;
                const auto first = static_cast<std::size_t>(std::max(0.0,
                    std::floor((component.mean - 8.0 * component.sigma) *
                               (density_.size() - 1))));
                const auto last = static_cast<std::size_t>(std::min(
                    static_cast<double>(density_.size() - 1),
                    std::ceil((component.mean + 8.0 * component.sigma) *
                              (density_.size() - 1))));
                const double standardized =
                    (static_cast<double>(first) / (density_.size() - 1) - component.mean) /
                    component.sigma;
                double term = component.amplitude * std::exp(-0.5 * standardized * standardized);
                double ratio = std::exp(-standardized * step - 0.5 * step * step);
                const double ratio_step = std::exp(-step * step);
                for (auto index = first; index <= last; ++index) {
                    density_[index] += term;
                    term *= ratio;
                    ratio *= ratio_step;
                }
            }
        }
    }

    double sample(detail::Mt19937_64& engine) const {
        const double target = unit_random(engine) * total_weight_;
        double cumulative = 0.0;
        const Component* selected = &components_.back();
        for (const auto& component : components_) {
            cumulative += component.weight;
            if (target < cumulative) {
                selected = &component;
                break;
            }
        }

        for (std::size_t attempt = 0; attempt < 128; ++attempt) {
            const double value = selected->mean + selected->sigma * standard_normal(engine);
            if (value >= 0.0 && value <= 1.0) {
                return value;
            }
        }
        // This path is extraordinarily unlikely, but a bounded fallback keeps
        // ask() total and consumes a deterministic number of additional bits.
        return unit_random(engine);
    }

    double log_density(double value) const {
        const double bounded = std::clamp(value, 0.0, 1.0);
        if (fast_density_) {
            const double position = bounded * (density_.size() - 1);
            const auto index = std::min(static_cast<std::size_t>(position), density_.size() - 2);
            const double fraction = position - index;
            return std::log(density_[index] + fraction * (density_[index + 1] - density_[index]));
        }
        return mixture_log_density([&](const Component& component) {
            const double standardized = (bounded - component.mean) / component.sigma;
            return -kLogSqrtTwoPi - component.log_sigma - 0.5 * standardized * standardized -
                   component.log_normalizer;
        });
    }

    double log_bin_mass(double lower, double upper) const {
        const double bounded_lower = std::clamp(lower, 0.0, 1.0);
        const double bounded_upper = std::clamp(upper, bounded_lower, 1.0);
        if (fast_density_ && bounded_upper - bounded_lower <= 1e-4 &&
            bounded_upper > bounded_lower) {
            return log_density((bounded_lower + bounded_upper) * 0.5) +
                   std::log(bounded_upper - bounded_lower);
        }
        return mixture_log_density([&](const Component& component) {
            const double standardized_lower = (bounded_lower - component.mean) / component.sigma;
            const double standardized_upper = (bounded_upper - component.mean) / component.sigma;
            return log_normal_interval(standardized_lower, standardized_upper) -
                   component.log_normalizer;
        });
    }

    struct PendingComponent {
        double mean;
        double sigma;
        double amplitude;
    };

    std::vector<PendingComponent> pending_components(const std::vector<double>& values) const {
        std::vector<PendingComponent> result;
        result.reserve(values.size());
        const double sigma = 1.0 /
            std::min<std::size_t>(100, components_.size() + values.size() + 1);
        for (const double mean : values) {
            const double normalizer = log_normal_interval(-mean / sigma, (1.0 - mean) / sigma);
            result.push_back({mean, sigma,
                              std::exp(-kLogSqrtTwoPi - std::log(sigma) - normalizer)});
        }
        return result;
    }

    double pending_log_density(double value, const std::vector<PendingComponent>& pending) const {
        if (pending.empty())
            return log_density(value);
        double density = std::exp(log_density(value)) * total_weight_;
        for (const auto& component : pending) {
            const double standardized = (value - component.mean) / component.sigma;
            density += component.amplitude * std::exp(-0.5 * standardized * standardized);
        }
        return std::log(density / (total_weight_ + pending.size()));
    }

    double pending_log_bin_mass(double lower, double upper,
                               const std::vector<PendingComponent>& pending) const {
        if (pending.empty())
            return log_bin_mass(lower, upper);
        if (fast_density_ && upper - lower <= 1e-4 && upper > lower)
            return pending_log_density((lower + upper) * 0.5, pending) + std::log(upper - lower);
        double mass = std::exp(log_bin_mass(lower, upper)) * total_weight_;
        for (const auto& component : pending) {
            const double normalizer = std::exp(log_normal_interval(
                -component.mean / component.sigma, (1.0 - component.mean) / component.sigma));
            mass += std::exp(log_normal_interval((lower - component.mean) / component.sigma,
                (upper - component.mean) / component.sigma)) / normalizer;
        }
        return std::log(mass / (total_weight_ + pending.size()));
    }

private:
    struct Component {
        double mean;
        double sigma;
        double weight;
        double log_weight = 0.0;
        double log_normalizer = 0.0;
        double log_sigma = 0.0;
        double amplitude = 0.0;
    };

    static double truncated_log_normalizer(const Component& component) {
        return log_normal_interval(-component.mean / component.sigma,
                                   (1.0 - component.mean) / component.sigma);
    }

    template <typename ComponentLogProbability>
    double mixture_log_density(ComponentLogProbability&& component_log_probability) const {
        double largest = -std::numeric_limits<double>::infinity();
        terms_.clear();
        for (const auto& component : components_) {
            const double log_probability =
                component.log_weight + component_log_probability(component);
            terms_.push_back(log_probability);
            largest = std::max(largest, log_probability);
        }
        if (largest == -std::numeric_limits<double>::infinity()) {
            return largest;
        }

        double scaled_sum = 0.0;
        for (const double term : terms_) {
            scaled_sum += std::exp(term - largest);
        }
        return largest + std::log(scaled_sum);
    }

    void add_component(double mean, double sigma, double weight) {
        if (!std::isfinite(mean) || !std::isfinite(sigma) || sigma <= 0.0 ||
            !std::isfinite(weight) || weight <= 0.0) {
            throw std::logic_error("invalid TPE numeric mixture component");
        }
        components_.push_back({mean, sigma, weight});
        total_weight_ += weight;
    }

    std::vector<Component> components_;
    std::array<double, 513> density_{};
    // Shared model scratch is accessed only while the owning sampler holds Impl::mutex_.
    mutable std::vector<double> terms_;
    bool fast_density_;
    double total_weight_ = 0.0;
};

class CategoricalModel {
public:
    CategoricalModel(std::size_t choice_count,
                     const std::vector<std::size_t>& values,
                     const std::vector<double>& weights,
                     double prior_weight)
        : masses_(choice_count, 0.0) {
        if (values.size() != weights.size()) {
            throw std::logic_error("TPE categorical values and weights have different sizes");
        }
        double mixture_weight = prior_weight;
        for (const double weight : weights) {
            mixture_weight += weight;
        }
        mixture_weight_ = mixture_weight;

        // Each observed categorical kernel is a smoothed point mass.  The
        // final prior kernel is uniform.  A mixture of categorical kernels can
        // be collapsed into this single probability vector without changing
        // either sampling or likelihood evaluation.
        const double alpha = prior_weight / static_cast<double>(values.size() + 1);
        const double observed_normalizer = 1.0 + alpha * static_cast<double>(choice_count);
        double baseline_sum = prior_weight / mixture_weight / static_cast<double>(choice_count);
        for (std::size_t i = 0; i < values.size(); ++i) {
            if (values[i] >= masses_.size()) {
                throw std::logic_error("TPE categorical observation is out of range");
            }
            const double component_weight = weights[i] / mixture_weight;
            baseline_sum += component_weight * alpha / observed_normalizer;
            masses_[values[i]] += component_weight / observed_normalizer;
        }
        for (double& mass : masses_) {
            mass += baseline_sum;
        }
        for (const double mass : masses_) {
            total_mass_ += mass;
        }
    }

    std::size_t sample(detail::Mt19937_64& engine) const {
        const double target = unit_random(engine) * total_mass_;
        double cumulative = 0.0;
        for (std::size_t i = 0; i < masses_.size(); ++i) {
            cumulative += masses_[i];
            if (target < cumulative) {
                return i;
            }
        }
        return masses_.size() - 1;
    }

    double log_density(std::size_t value) const {
        return std::log(masses_.at(value) / total_mass_);
    }

    double pending_log_density(std::size_t value, const std::vector<double>& pending) const {
        if (pending.empty())
            return log_density(value);
        double mass = masses_.at(value) / total_mass_ * mixture_weight_;
        const double alpha = 1.0 / (mixture_weight_ + pending.size());
        for (const double observed : pending)
            mass += (alpha + (observed == value ? 1.0 : 0.0)) / (1.0 + alpha * masses_.size());
        return std::log(mass / (mixture_weight_ + pending.size()));
    }

private:
    std::vector<double> masses_;
    double total_mass_ = 0.0;
    double mixture_weight_ = 0.0;
};

void validate_config(const TpeSamplerConfig& config) {
    if (config.max_threads > 1024)
        throw std::invalid_argument("TPE max_threads must be in [0, 1024]");
    if (config.history_switch && *config.history_switch == 0)
        throw std::invalid_argument("TPE history_switch must be positive");
    if (config.startup_trials == 0) {
        throw std::invalid_argument("TPE startup_trials must be positive");
    }
    if (config.ei_candidates == 0 || config.ei_candidates > kMaxEiCandidates) {
        throw std::invalid_argument("TPE ei_candidates must be in [1, 1000000]");
    }
    if (config.scale_ei_candidates == 0 || config.scale_ei_candidates > kMaxEiCandidates)
        throw std::invalid_argument("TPE scale_ei_candidates must be in [1, 1000000]");
    if (config.bad_reservoir_size > 65536)
        throw std::invalid_argument("TPE bad_reservoir_size must be in [0, 65536]");
    if (!std::isfinite(config.gamma_fraction) || config.gamma_fraction <= 0.0 ||
        config.gamma_fraction > 1.0) {
        throw std::invalid_argument("TPE gamma_fraction must be finite and in (0, 1]");
    }
    if (config.gamma_cap == 0) {
        throw std::invalid_argument("TPE gamma_cap must be positive");
    }
    if (!std::isfinite(config.prior_weight) || config.prior_weight <= 0.0) {
        throw std::invalid_argument("TPE prior_weight must be finite and positive");
    }
}

}  // namespace

class TpeSampler::Impl {
public:
    class FiniteReservationSet {
    public:
        explicit FiniteReservationSet(std::uint64_t cardinality)
            : cardinality_(cardinality), dense_(cardinality <= kDenseCardinalityLimit) {
            if (dense_) {
                const std::uint64_t words = cardinality / 64U + (cardinality % 64U != 0 ? 1U : 0U);
                dense_words_.assign(static_cast<std::size_t>(words), 0);
            }
        }

        bool contains(std::uint64_t ordinal) const {
            if (ordinal >= cardinality_) {
                throw std::out_of_range("TPE finite candidate ordinal is out of range");
            }
            if (dense_) {
                const std::size_t word = static_cast<std::size_t>(ordinal / 64U);
                const std::uint64_t mask = std::uint64_t{1} << (ordinal % 64U);
                return (dense_words_[word] & mask) != 0;
            }
            return sparse_ordinals_.contains(ordinal);
        }

        bool insert(std::uint64_t ordinal) {
            if (ordinal >= cardinality_) {
                throw std::out_of_range("TPE finite candidate ordinal is out of range");
            }
            if (dense_) {
                const std::size_t word = static_cast<std::size_t>(ordinal / 64U);
                const std::uint64_t mask = std::uint64_t{1} << (ordinal % 64U);
                if ((dense_words_[word] & mask) != 0) {
                    return false;
                }
                dense_words_[word] |= mask;
                ++size_;
                return true;
            }
            const bool inserted = sparse_ordinals_.insert(ordinal);
            if (inserted) {
                ++size_;
            }
            return inserted;
        }

        void clear() {
            std::fill(dense_words_.begin(), dense_words_.end(), 0);
            sparse_ordinals_.clear();
            size_ = 0;
        }

        std::uint64_t size() const noexcept { return size_; }

    private:
        static constexpr std::uint64_t kDenseCardinalityLimit = 100'000'000;

        std::uint64_t cardinality_;
        bool dense_;
        std::vector<std::uint64_t> dense_words_;
        detail::OrdinalSet sparse_ordinals_;
        std::uint64_t size_ = 0;
    };

    enum class EncodingKind {
        FixedInteger,
        Integer,
        LogInteger,
        FixedReal,
        ContinuousReal,
        LogContinuousReal,
        SteppedReal,
        Boolean,
        FixedCategorical,
        Categorical,
    };

    struct Encoding {
        EncodingKind kind;
        std::uint64_t count = 0;
    };

    struct RelativeLogDomain {
        // Logarithmic coordinates are represented relative to a positive
        // reference instead of as two large absolute logarithms.  This avoids
        // losing a narrow multiplicative interval to cancellation.
        long double reference;
        long double span;
    };

    struct CompletedObservation {
        Candidate candidate;
        double score;
    };

    struct ObservationView {
        const Candidate* candidate;
        std::uint64_t row;
    };

    static constexpr std::uint64_t owned_marker = std::uint64_t{1} << 63;

    ObservationView view(std::uint64_t reference) const {
        return reference & owned_marker
            ? ObservationView{&owned_history_.at(reference).candidate, 0}
            : ObservationView{nullptr, reference};
    }

    std::uint64_t observation_id(const ObservationView& observation) const {
        return observation.candidate ? observation.candidate->id : source_->id(observation.row);
    }

    double observation_score(std::uint64_t reference) const {
        return reference & owned_marker ? owned_history_.at(reference).score :
            *source_->objective(reference) * source_direction_;
    }

    ParameterValue observation_value(const ObservationView& observation, std::size_t column,
                                     const std::string& name) const {
        return observation.candidate ? *observation.candidate->find(name) :
            source_->parameter(observation.row, column);
    }

    void discard_observation(std::uint64_t reference) {
        if (reference & owned_marker)
            owned_history_.erase(reference);
    }

    struct DimensionModel {
        enum class Kind {
            Fixed,
            Numeric,
            Categorical,
        };

        Kind kind = Kind::Fixed;
        std::shared_ptr<NumericModel> good_numeric;
        std::shared_ptr<NumericModel> bad_numeric;
        std::shared_ptr<CategoricalModel> good_categorical;
        std::shared_ptr<CategoricalModel> bad_categorical;
    };

    const std::string numeric_build_;

    Impl(const SearchSpace& space,
         std::uint64_t seed,
         std::optional<std::uint64_t> finite_cardinality)
        : numeric_build_(detail::tpe_numeric_identity(space)),
          engine_(seed), reservoir_engine_(seed ^ 0xd1b54a32d192ed03ULL),
          finite_cardinality_(finite_cardinality) {
        if (finite_cardinality_.has_value()) {
            reservations_.emplace(*finite_cardinality_);
        }
        encodings_.reserve(space.dimensions().size());
        for (const auto& dimension : space.dimensions()) {
            const Encoding encoding = make_encoding(dimension);
            has_varying_dimension_ =
                has_varying_dimension_ || (encoding.kind != EncodingKind::FixedInteger &&
                                           encoding.kind != EncodingKind::FixedReal &&
                                           encoding.kind != EncodingKind::FixedCategorical);
            encodings_.push_back(encoding);
        }
    }

    void reset(std::uint64_t seed) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!pending_.empty()) {
            throw std::logic_error("TPE cannot reset while candidates are outstanding");
        }
        engine_.seed(seed);
        reservoir_engine_.seed(seed ^ 0xd1b54a32d192ed03ULL);
        history_.clear();
        older_bad_.clear();
        owned_history_.clear();
        source_.reset();
        next_owned_ = 0;
        older_bad_seen_ = 0;
        compact_history_ = false;
        cached_compact_ = false;
        cached_epoch_ = 0;
        cached_models_.clear();
        good_ids_.clear();
        bad_ids_.clear();
        bad_model_ids_.clear();
        fingerprint_ = {};
        attempted_ = 0;
        pending_.clear();
        pending_encodings_.clear();
        if (reservations_.has_value()) {
            reservations_->clear();
        }
        fallback_cursor_ = 0;
        next_id_ = 0;
        generated_.store(0, std::memory_order_relaxed);
        completed_.store(0, std::memory_order_relaxed);
        outstanding_.store(0, std::memory_order_relaxed);
        duplicate_proposals_skipped_.store(0, std::memory_order_relaxed);
    }

    Candidate random_candidate(const SearchSpace& space, std::uint64_t id) {
        Candidate candidate;
        candidate.id = id;
        for (std::size_t i = 0; i < space.dimensions().size(); ++i) {
            const auto& dimension = space.dimensions()[i];
            const auto& encoding = encodings_[i];
            ParameterValue value = std::visit(
                [&](const auto& item) -> ParameterValue {
                    using T = std::decay_t<decltype(item)>;
                    if constexpr (std::is_same_v<T, IntegerDimension>) {
                        if (encoding.kind == EncodingKind::FixedInteger) {
                            return item.low();
                        }
                        if (encoding.kind == EncodingKind::LogInteger) {
                            return decode_numeric(dimension, encoding, unit_random(engine_));
                        }
                        return integer_at(item, bounded_random(engine_, encoding.count));
                    } else if constexpr (std::is_same_v<T, RealDimension>) {
                        if (encoding.kind == EncodingKind::FixedReal) {
                            return item.low();
                        }
                        if (encoding.kind == EncodingKind::LogContinuousReal) {
                            return decode_numeric(dimension, encoding, unit_random(engine_));
                        }
                        if (encoding.kind == EncodingKind::SteppedReal) {
                            return real_at(item, bounded_random(engine_, encoding.count));
                        }
                        return decode_continuous(item, unit_random(engine_));
                    } else if constexpr (std::is_same_v<T, BooleanDimension>) {
                        return bounded_random(engine_, 2) != 0;
                    } else {
                        if (encoding.kind == EncodingKind::FixedCategorical) {
                            return item.choices().front();
                        }
                        return item.choices()[static_cast<std::size_t>(
                            bounded_random(engine_, encoding.count))];
                    }
                },
                dimension);
            candidate.values.emplace(std::string(dimension_name(dimension)), std::move(value));
        }
        return candidate;
    }

    void refresh_models(const SearchSpace& space, const TpeSamplerConfig& config) {
        std::vector<std::uint64_t> ranked;
        ranked.reserve(history_.size() + older_bad_.size());
        for (const auto& observation : history_) {
            ranked.push_back(observation);
        }
        for (const auto& observation : older_bad_)
            ranked.push_back(observation);
        std::sort(ranked.begin(), ranked.end(), [&](auto left, auto right) {
            if (observation_score(left) != observation_score(right)) {
                return observation_score(left) > observation_score(right);
            }
            return observation_id(view(left)) < observation_id(view(right));
        });

        const std::size_t good_count = compact_history_
            ? std::min(history_.size() - 1,
                       gamma_count(completed_.load(std::memory_order_relaxed), config))
            : gamma_count(history_.size(), config);
        std::vector<ObservationView> good;
        std::vector<ObservationView> bad;
        good.reserve(good_count);
        bad.reserve(ranked.size() - good_count + pending_.size());
        for (std::size_t i = 0; i < ranked.size(); ++i) {
            (i < good_count ? good : bad).push_back(view(ranked[i]));
        }

        auto chronological = [&](const auto& left, const auto& right) {
            return observation_id(left) < observation_id(right);
        };
        std::sort(good.begin(), good.end(), chronological);
        std::sort(bad.begin(), bad.end(), chronological);
        if (config.constant_liar && !compact_history_) {
            for (const auto& [pending_id, candidate] : pending_) {
                (void)pending_id;
                bad.push_back({&candidate, 0});
            }
            std::sort(bad.begin(), bad.end(), chronological);
        }

        const auto good_weights = observation_weights(good.size());
        const auto bad_weights = observation_weights(bad.size());
        std::vector<std::uint64_t> good_ids;
        std::vector<std::uint64_t> bad_ids;
        for (const auto& observation : good)
            good_ids.push_back(observation_id(observation));
        for (const auto& observation : bad)
            bad_ids.push_back(observation_id(observation));
        const auto model_epoch = completed_.load(std::memory_order_relaxed) / 32;
        const bool reuse_good = good_ids == good_ids_ &&
                                !cached_models_.empty() &&
                                compact_history_ == cached_compact_;
        const bool reuse_bad = (compact_history_ ? model_epoch == cached_epoch_
                                                : bad_ids == bad_ids_) &&
                               !cached_models_.empty() &&
                               compact_history_ == cached_compact_;
        if (!reuse_good || !reuse_bad) {
            std::vector<DimensionModel> updated(space.dimensions().size());
            try {
                dimension_work(updated.size(), !compact_history_ && bad.size() >= 4096, config,
                               [&](std::size_t index) {
                    auto* cached = cached_models_.empty() ? nullptr : &cached_models_[index];
                    if (cached && !reuse_bad) {
                        cached->bad_numeric.reset();
                        cached->bad_categorical.reset();
                    }
                    updated[index] = build_model(index, space.dimensions()[index],
                        encodings_[index], good, bad, good_weights, bad_weights,
                        config.prior_weight, compact_history_, cached, reuse_good, reuse_bad);
                });
            } catch (...) {
                cached_models_.clear();
                good_ids_.clear();
                bad_ids_.clear();
                bad_model_ids_.clear();
                throw;
            }
            cached_models_ = std::move(updated);
            if (!reuse_bad)
                bad_model_ids_ = bad_ids;
            good_ids_ = std::move(good_ids);
            bad_ids_ = std::move(bad_ids);
            cached_compact_ = compact_history_;
            cached_epoch_ = model_epoch;
        }
    }

    std::optional<Candidate> tpe_candidate(const SearchSpace& space,
                                         std::uint64_t id,
                                         const TpeSamplerConfig& config) {
        const auto model_epoch = completed_.load(std::memory_order_relaxed) / 32;
        bool good_changed = false;
        if (compact_history_ && cached_compact_ && !cached_models_.empty()) {
            const auto good_count = std::min(history_.size() - 1,
                gamma_count(completed_.load(std::memory_order_relaxed), config));
            std::vector<std::uint64_t> current_ids;
            current_ids.reserve(good_count);
            for (std::size_t index = 0; index < good_count; ++index)
                current_ids.push_back(observation_id(view(history_[index])));
            std::sort(current_ids.begin(), current_ids.end());
            good_changed = current_ids != good_ids_;
        }
        if (!compact_history_ || !cached_compact_ || cached_models_.empty() ||
            model_epoch != cached_epoch_ || good_changed)
            refresh_models(space, config);
        const auto& models = cached_models_;

        std::vector<std::vector<double>> pending_values(models.size());
        std::vector<std::vector<NumericModel::PendingComponent>> pending_numeric(models.size());
        if (compact_history_ && config.constant_liar) {
            for (std::size_t index = 0; index < models.size(); ++index) {
                for (const auto& entry : pending_encodings_)
                    pending_values[index].push_back(entry.second[index]);
                if (models[index].kind == DimensionModel::Kind::Numeric)
                    pending_numeric[index] =
                        models[index].bad_numeric->pending_components(pending_values[index]);
            }
        }

        std::optional<Candidate> best;
        std::vector<ParameterValue> values;
        std::vector<ParameterValue> best_values;
        values.reserve(models.size());
        std::unordered_set<std::uint64_t> acquisition_ordinals;
        double best_log_ratio = -std::numeric_limits<double>::infinity();
        const auto attempts = compact_history_
            ? std::min(config.ei_candidates, config.scale_ei_candidates) : config.ei_candidates;
        for (std::uint64_t attempt = 0; attempt < attempts; ++attempt) {
            Candidate candidate;
            candidate.id = id;
            values.clear();
            values.resize(models.size());
            std::vector<double> samples(models.size());
            std::vector<double> contributions(models.size());
            for (std::size_t column = 0; column < models.size(); ++column) {
                const auto& dimension = space.dimensions()[column];
                const auto& model = models[column];
                if (model.kind == DimensionModel::Kind::Numeric) {
                    samples[column] = model.good_numeric->sample(engine_);
                    values[column] = decode_numeric(dimension, encodings_[column], samples[column]);
                } else if (model.kind == DimensionModel::Kind::Categorical) {
                    samples[column] = model.good_categorical->sample(engine_);
                    values[column] = decode_categorical(dimension,
                        static_cast<std::size_t>(samples[column]));
                } else {
                    values[column] = fixed_value(dimension);
                }
            }
            dimension_work(models.size(), !compact_history_ && history_.size() >= 4096, config,
                           [&](std::size_t column) {
                const auto& dimension = space.dimensions()[column];
                const auto& encoding = encodings_[column];
                const auto& model = models[column];
                if (model.kind == DimensionModel::Kind::Numeric) {
                    const bool continuous = encoding.kind == EncodingKind::ContinuousReal ||
                                            encoding.kind == EncodingKind::LogContinuousReal;
                    if (continuous) {
                        const double legal = compact_history_ ? samples[column] :
                            encode_numeric(dimension, encoding, values[column]);
                        contributions[column] = model.good_numeric->log_density(legal) -
                            model.bad_numeric->pending_log_density(legal, pending_numeric[column]);
                    } else {
                        const auto bounds = numeric_bin(dimension, encoding, values[column]);
                        contributions[column] =
                            model.good_numeric->log_bin_mass(bounds.first, bounds.second) -
                            model.bad_numeric->pending_log_bin_mass(bounds.first, bounds.second,
                                                                   pending_numeric[column]);
                    }
                } else if (model.kind == DimensionModel::Kind::Categorical) {
                    const auto sampled = static_cast<std::size_t>(samples[column]);
                    contributions[column] = model.good_categorical->log_density(sampled) -
                        model.bad_categorical->pending_log_density(sampled, pending_values[column]);
                }
            });
            double log_ratio = 0.0;
            for (std::size_t column = 0; column < models.size(); ++column) {
                if (!std::isfinite(contributions[column])) {
                    throw std::logic_error(
                        "TPE produced a non-finite acquisition contribution for dimension: " +
                        std::string(dimension_name(space.dimensions()[column])));
                }
                log_ratio += contributions[column];
                if (!std::isfinite(log_ratio))
                    throw std::logic_error("TPE acquisition log ratio is non-finite");
            }
            if (detail::tpe_log_ratio_observer)
                detail::tpe_log_ratio_observer(log_ratio);
            if (reservations_.has_value()) {
                for (std::size_t index = 0; index < values.size(); ++index)
                    candidate.values.emplace(std::string(dimension_name(space.dimensions()[index])),
                                             values[index]);
                const std::uint64_t ordinal = space.candidate_ordinal(candidate);
                if (reservations_->contains(ordinal) ||
                    !acquisition_ordinals.insert(ordinal).second) {
                    duplicate_proposals_skipped_.fetch_add(1, std::memory_order_relaxed);
                    continue;
                }
            }
            if (!best.has_value() || log_ratio > best_log_ratio) {
                best_log_ratio = log_ratio;
                best = std::move(candidate);
                best_values = values;
            }
        }
        if (best && !reservations_) {
            for (std::size_t index = 0; index < best_values.size(); ++index)
                best->values.emplace(std::string(dimension_name(space.dimensions()[index])),
                                     std::move(best_values[index]));
        }
        return best;
    }

    bool reserve_candidate(const SearchSpace& space, const Candidate& candidate) {
        if (!reservations_.has_value()) {
            return true;
        }
        return reservations_->insert(space.candidate_ordinal(candidate));
    }

    std::optional<Candidate> fallback_candidate(const SearchSpace& space, std::uint64_t id) {
        if (!reservations_.has_value() || !finite_cardinality_.has_value()) {
            return std::nullopt;
        }
        while (fallback_cursor_ < *finite_cardinality_ &&
               reservations_->contains(fallback_cursor_)) {
            ++fallback_cursor_;
        }
        if (fallback_cursor_ >= *finite_cardinality_) {
            return std::nullopt;
        }
        Candidate candidate = space.candidate_at(fallback_cursor_, id);
        if (!reservations_->insert(fallback_cursor_)) {
            throw std::logic_error("TPE finite fallback failed to reserve an unseen candidate");
        }
        ++fallback_cursor_;
        return candidate;
    }

    void retain_observation(Candidate candidate, double score) {
        const auto reference = owned_marker | next_owned_++;
        owned_history_.emplace(reference, CompletedObservation{std::move(candidate), score});
        history_.push_back(reference);
    }

    void compact_observations(const TpeSamplerConfig& config) {
        if (!config.history_switch)
            return;
        const std::size_t elite_count = static_cast<std::size_t>(config.gamma_cap);
        const std::size_t recent_count = 64;
        if (!compact_history_ &&
            completed_.load(std::memory_order_relaxed) < *config.history_switch)
            return;
        compact_history_ = true;
        if (history_.size() <= elite_count + recent_count)
            return;
        std::sort(history_.begin(), history_.end(), [&](auto left, auto right) {
            return observation_score(left) != observation_score(right)
                ? observation_score(left) > observation_score(right)
                : observation_id(view(left)) < observation_id(view(right));
        });
        std::sort(history_.begin() + elite_count, history_.end(),
                  [&](auto left, auto right) {
                      return observation_id(view(left)) > observation_id(view(right));
                  });
        const auto retained = elite_count + recent_count;
        for (auto index = history_.size(); index > retained; --index) {
            ++older_bad_seen_;
            if (older_bad_.size() < config.bad_reservoir_size) {
                older_bad_.push_back(history_[index - 1]);
            } else if (config.bad_reservoir_size != 0) {
                const auto selected = bounded_random(reservoir_engine_, older_bad_seen_);
                if (selected < older_bad_.size()) {
                    discard_observation(older_bad_[selected]);
                    older_bad_[selected] = history_[index - 1];
                } else {
                    discard_observation(history_[index - 1]);
                }
            } else {
                discard_observation(history_[index - 1]);
            }
        }
        history_.resize(retained);
    }

    bool register_pending(const SearchSpace& space, const Candidate& candidate) {
        std::vector<double> values;
        values.reserve(encodings_.size());
        for (std::size_t index = 0; index < encodings_.size(); ++index) {
            const auto& dimension = space.dimensions()[index];
            const auto& encoding = encodings_[index];
            const auto& value = *candidate.find(std::string(dimension_name(dimension)));
            if (encoding.kind == EncodingKind::FixedInteger ||
                encoding.kind == EncodingKind::FixedReal)
                values.push_back(0.5);
            else if (encoding.kind == EncodingKind::Boolean ||
                encoding.kind == EncodingKind::Categorical ||
                encoding.kind == EncodingKind::FixedCategorical)
                values.push_back(categorical_index(dimension, value));
            else
                values.push_back(encode_numeric(dimension, encoding, value));
        }
        const auto inserted = pending_.emplace(candidate.id, candidate).second;
        if (inserted)
            pending_encodings_.emplace(candidate.id, std::move(values));
        return inserted;
    }

    bool finite_space_exhausted() const noexcept {
        return reservations_.has_value() && finite_cardinality_.has_value() &&
               reservations_->size() == *finite_cardinality_;
    }

    std::mutex mutex_;
    detail::Mt19937_64 engine_;
    detail::Mt19937_64 reservoir_engine_;
    std::vector<Encoding> encodings_;
    std::vector<std::uint64_t> history_;
    std::vector<std::uint64_t> older_bad_;
    std::map<std::uint64_t, CompletedObservation> owned_history_;
    std::shared_ptr<const WarmStartSource> source_;
    double source_direction_ = 1.0;
    std::uint64_t next_owned_ = 0;
    std::uint64_t older_bad_seen_ = 0;
    bool compact_history_ = false;
    bool cached_compact_ = false;
    std::uint64_t cached_epoch_ = 0;
    std::vector<DimensionModel> cached_models_;
    std::vector<std::uint64_t> good_ids_;
    std::vector<std::uint64_t> bad_ids_;
    std::vector<std::uint64_t> bad_model_ids_;
    StateFingerprint fingerprint_{};
    std::uint64_t attempted_ = 0;
    std::map<std::uint64_t, Candidate> pending_;
    std::map<std::uint64_t, std::vector<double>> pending_encodings_;
    std::optional<std::uint64_t> finite_cardinality_;
    std::optional<FiniteReservationSet> reservations_;
    std::uint64_t fallback_cursor_ = 0;
    std::uint64_t next_id_ = 0;
    bool has_varying_dimension_ = false;
    std::atomic<std::uint64_t> generated_{0};
    std::atomic<std::uint64_t> completed_{0};
    std::atomic<std::uint64_t> outstanding_{0};
    std::atomic<std::uint64_t> duplicate_proposals_skipped_{0};

    std::string write_state(const std::string& signature) const {
        std::ostringstream output;
        output.imbue(std::locale::classic());
        output << std::quoted(signature) << '\n'
               << std::quoted(numeric_build_) << '\n'
               << attempted_ << ' ' << next_id_ << ' '
               << completed_.load(std::memory_order_relaxed) << ' ' << fallback_cursor_ << ' '
               << compact_history_ << ' ' << cached_compact_ << ' ' << cached_epoch_ << ' '
               << older_bad_seen_ << '\n';
        for (const auto word : fingerprint_)
            output << word << ' ';
        output << '\n';
        engine_.write(output);
        reservoir_engine_.write(output);
        const auto references = [&](const auto& values) {
            output << (compact_history_ ? values.size() : 0) << ' ';
            if (compact_history_)
                for (const auto reference : values)
                    output << observation_id(view(reference)) << ' ';
            output << '\n';
        };
        references(history_);
        references(older_bad_);
        const auto identifiers = [&](const auto& values) {
            output << (cached_compact_ ? values.size() : 0) << ' ';
            if (cached_compact_)
                for (const auto identifier : values)
                    output << identifier << ' ';
            output << '\n';
        };
        identifiers(good_ids_);
        identifiers(bad_model_ids_);
        const auto payload = output.str();
        return "PFHTPE2\n" + detail::sha256(payload) + '\n' + payload;
    }

    bool read_state(const std::string& state, const std::string& signature,
                    const SearchSpace& space, const TpeSamplerConfig& config) {
        if (state.empty())
            return false;
        if (!detail::current_sampler_checkpoint(state))
            return false;
        std::istringstream input(state.substr(73));
        input.imbue(std::locale::classic());
        std::string stored_signature;
        if (!(input >> std::quoted(stored_signature)))
            throw std::invalid_argument("invalid TPE sampler-state signature");
        if (stored_signature != signature)
            return false;
        std::string stored_build;
        if (!(input >> std::quoted(stored_build)))
            throw std::invalid_argument("invalid TPE sampler-state build identity");
        if (stored_build != numeric_build_ ||
            stored_build.find(";flags_sha256:unavailable") != std::string::npos)
            return false;
        std::uint64_t attempts, next_id, completed, fallback, epoch, older_seen;
        unsigned compact, cached_compact;
        StateFingerprint fingerprint{};
        detail::Mt19937_64 engine;
        detail::Mt19937_64 reservoir;
        if (!(input >> attempts >> next_id >> completed >> fallback >> compact >> cached_compact
                    >> epoch >> older_seen) || compact > 1 || cached_compact > 1)
            throw std::invalid_argument("invalid TPE sampler-state counters");
        for (auto& word : fingerprint)
            input >> word;
        if (!input)
            throw std::invalid_argument("invalid TPE sampler-state fingerprint");
        if (attempts != source_->size() || next_id != next_id_ ||
            completed != completed_.load(std::memory_order_relaxed) ||
            fingerprint != fingerprint_)
            return false;
        engine.read(input);
        reservoir.read(input);
        const auto identifiers = [&] {
            std::uint64_t count;
            if (!(input >> count) || count > source_->size() || count > state.size())
                throw std::invalid_argument("invalid TPE sampler-state observation count");
            std::vector<std::uint64_t> values(static_cast<std::size_t>(count));
            std::unordered_set<std::uint64_t> seen;
            for (auto& identifier : values)
                if (!(input >> identifier) || !seen.insert(identifier).second)
                    throw std::invalid_argument("invalid TPE sampler-state observation IDs");
            return values;
        };
        const auto history = identifiers();
        const auto older = identifiers();
        auto good = identifiers();
        auto bad = identifiers();
        input >> std::ws;
        if (!input.eof() || (!compact && (!history.empty() || !older.empty() ||
                                         !good.empty() || !bad.empty())) ||
            (compact && !config.history_switch) ||
            (finite_cardinality_ && fallback > *finite_cardinality_))
            throw std::invalid_argument("invalid TPE sampler-state structure");
        const auto row_for_id = [&](std::uint64_t identifier) {
            std::uint64_t begin = 0;
            std::uint64_t end = source_->size();
            while (begin < end) {
                const auto middle = begin + (end - begin) / 2;
                if (source_->id(middle) < identifier)
                    begin = middle + 1;
                else
                    end = middle;
            }
            if (begin == source_->size() || source_->id(begin) != identifier)
                throw std::invalid_argument("TPE sampler-state references missing trial");
            return begin;
        };
        const auto references = [&](const auto& values) {
            std::vector<std::uint64_t> rows;
            for (const auto identifier : values) {
                const auto row = row_for_id(identifier);
                if (!source_->objective(row))
                    throw std::invalid_argument("TPE sampler-state retains abandoned trial");
                rows.push_back(row);
            }
            return rows;
        };
        if (compact) {
            history_ = references(history);
            older_bad_ = references(older);
            std::vector<ObservationView> good_views;
            std::vector<ObservationView> bad_views;
            for (const auto identifier : good)
                good_views.push_back(view(row_for_id(identifier)));
            for (const auto identifier : bad)
                bad_views.push_back(view(row_for_id(identifier)));
            if (!good.empty() || !bad.empty()) {
                const auto good_weights = observation_weights(good.size());
                const auto bad_weights = observation_weights(bad.size());
                for (std::size_t column = 0; column < encodings_.size(); ++column)
                    cached_models_.push_back(build_model(column, space.dimensions()[column],
                        encodings_[column], good_views, bad_views, good_weights, bad_weights,
                        config.prior_weight, cached_compact != 0, nullptr, false, false));
            }
            good_ids_ = std::move(good);
            bad_ids_ = bad;
            bad_model_ids_ = std::move(bad);
        }
        engine_ = engine;
        reservoir_engine_ = reservoir;
        fallback_cursor_ = fallback;
        compact_history_ = compact != 0;
        cached_compact_ = cached_compact != 0;
        cached_epoch_ = epoch;
        older_bad_seen_ = older_seen;
        return true;
    }

private:
    template <typename Function>
    void dimension_work(std::size_t dimensions, bool parallel, const TpeSamplerConfig& config,
                        Function&& function) {
        if (!parallel) {
            for (std::size_t index = 0; index < dimensions; ++index)
                function(index);
            return;
        }
        const auto requested = config.max_threads ? config.max_threads : 8;
        workers_.run(dimensions, std::min(requested, available_cpus_), function);
    }

    detail::DimensionWorkers workers_;
    unsigned available_cpus_ = detail::available_cpus();

    static Encoding make_encoding(const Dimension& dimension) {
        return std::visit(
            [](const auto& item) -> Encoding {
                using T = std::decay_t<decltype(item)>;
                if constexpr (std::is_same_v<T, IntegerDimension>) {
                    const std::uint64_t count = integer_count(item);
                    if (count > kMaxExactlyRepresentableBins) {
                        throw std::invalid_argument(
                            "TPE cannot exactly encode an integer dimension with more than "
                            "2^53 values");
                    }
                    if (count == 1) {
                        return {EncodingKind::FixedInteger, count};
                    }
                    if (item.log()) {
                        validate_log_integer_encoding(item, count);
                        return {EncodingKind::LogInteger, count};
                    }
                    return {EncodingKind::Integer, count};
                } else if constexpr (std::is_same_v<T, RealDimension>) {
                    if (item.low() == item.high()) {
                        return {EncodingKind::FixedReal, 1};
                    }
                    if (item.step().has_value()) {
                        const std::uint64_t count = real_grid_count(item);
                        if (count > kMaxExactlyRepresentableBins) {
                            throw std::invalid_argument(
                                "TPE cannot exactly encode a stepped real dimension with more "
                                "than 2^53 values");
                        }
                        return {count == 1 ? EncodingKind::FixedReal : EncodingKind::SteppedReal,
                                count};
                    }
                    if (item.log()) {
                        validate_log_real_encoding(item);
                        return {EncodingKind::LogContinuousReal, 0};
                    }
                    return {EncodingKind::ContinuousReal, 0};
                } else if constexpr (std::is_same_v<T, BooleanDimension>) {
                    return {EncodingKind::Boolean, 2};
                } else {
                    const auto count = static_cast<std::uint64_t>(item.choices().size());
                    return {count == 1 ? EncodingKind::FixedCategorical : EncodingKind::Categorical,
                            count};
                }
            },
            dimension);
    }

    static std::size_t gamma_count(std::size_t completed, const TpeSamplerConfig& config) {
        if (completed == 0) {
            return 0;
        }
        // gamma_fraction is a double-valued public setting, so evaluate the
        // product in that same domain.  Promoting its already-rounded value to
        // a wider long double first makes ceil(0.10 * 10) platform-dependent:
        // it is 1 on targets where long double == double and can become 2 on
        // targets with extended precision.
        const double requested =
            std::ceil(config.gamma_fraction * static_cast<double>(completed));
        const std::uint64_t requested_u64 =
            requested >= static_cast<double>(std::numeric_limits<std::uint64_t>::max())
                ? std::numeric_limits<std::uint64_t>::max()
                : static_cast<std::uint64_t>(requested);
        std::uint64_t count = std::max<std::uint64_t>(1, requested_u64);
        count = std::min(count, config.gamma_cap);
        if (completed > 1) {
            count = std::min<std::uint64_t>(count, completed - 1);
        }
        return static_cast<std::size_t>(count);
    }

    static double decode_continuous(const RealDimension& dimension, double normalized) {
        const double unit = std::clamp(normalized, 0.0, 1.0);
        if (unit == 0.0) {
            return dimension.low();
        }
        if (unit == 1.0) {
            return dimension.high();
        }

        const double span = dimension.high() - dimension.low();
        if (std::isfinite(span)) {
            const double decoded = dimension.low() + span * unit;
            if (std::isfinite(decoded)) {
                return std::clamp(decoded, dimension.low(), dimension.high());
            }
        }

        // Scale before interpolation so this remains finite even on platforms
        // where long double has the same exponent range as double and
        // high-low would overflow.
        const double scale = std::max(std::abs(dimension.low()), std::abs(dimension.high()));
        const double scaled_low = dimension.low() / scale;
        const double scaled_high = dimension.high() / scale;
        const double scaled =
            std::clamp(scaled_low * (1.0 - unit) + scaled_high * unit, scaled_low, scaled_high);
        const double decoded = scaled * scale;
        if (!std::isfinite(decoded)) {
            throw std::logic_error("TPE produced a non-finite continuous value");
        }
        return std::clamp(decoded, dimension.low(), dimension.high());
    }

    static double encode_continuous(const RealDimension& dimension, double value) {
        if (value <= dimension.low()) {
            return 0.0;
        }
        if (value >= dimension.high()) {
            return 1.0;
        }
        const double span = dimension.high() - dimension.low();
        if (std::isfinite(span)) {
            return std::clamp((value - dimension.low()) / span, 0.0, 1.0);
        }
        const double scale = std::max(std::abs(dimension.low()), std::abs(dimension.high()));
        const double scaled_low = dimension.low() / scale;
        const double scaled_high = dimension.high() / scale;
        const double scaled_value = value / scale;
        return std::clamp((scaled_value - scaled_low) / (scaled_high - scaled_low), 0.0, 1.0);
    }

    static long double stable_relative_log(long double value, long double reference) {
        const long double relative = (value - reference) / reference;
        if (std::isfinite(relative)) {
            return std::log1p(relative);
        }
        // A ratio can overflow when long double has the same range as double.
        // In that case the operands are far apart, so subtracting their logs
        // does not suffer the narrow-interval cancellation avoided above.
        return std::log(value) - std::log(reference);
    }

    static RelativeLogDomain log_integer_domain(const IntegerDimension& dimension) {
        const long double reference = static_cast<long double>(dimension.low()) - 0.5L;
        const long double count = static_cast<long double>(integer_count(dimension));
        return {reference, std::log1p(count / reference)};
    }

    static RelativeLogDomain log_real_domain(const RealDimension& dimension) {
        const long double reference = static_cast<long double>(dimension.low());
        return {reference,
                stable_relative_log(static_cast<long double>(dimension.high()), reference)};
    }

    static void validate_log_domain(const RelativeLogDomain& domain, const std::string& name) {
        if (!std::isfinite(domain.reference) || domain.reference <= 0.0L ||
            !std::isfinite(domain.span) || domain.span <= 0.0L) {
            throw std::invalid_argument("TPE log transform is not representable for dimension: " +
                                        name);
        }
    }

    static double normalize_log_offset(long double offset, const RelativeLogDomain& domain) {
        const long double normalized = std::log1p(offset / domain.reference) / domain.span;
        if (!std::isfinite(normalized)) {
            throw std::logic_error("TPE produced a non-finite relative log coordinate");
        }
        return std::clamp(static_cast<double>(normalized), 0.0, 1.0);
    }

    static double normalize_log_value(long double value, const RelativeLogDomain& domain) {
        const long double normalized = stable_relative_log(value, domain.reference) / domain.span;
        if (!std::isfinite(normalized)) {
            throw std::logic_error("TPE produced a non-finite log coordinate");
        }
        return std::clamp(static_cast<double>(normalized), 0.0, 1.0);
    }

    static void validate_log_integer_encoding(const IntegerDimension& dimension,
                                              std::uint64_t count) {
        const RelativeLogDomain domain = log_integer_domain(dimension);
        validate_log_domain(domain, dimension.name());
        if (count <= 1) {
            return;
        }

        auto valid_bin = [&](long double lower_offset, long double center_offset,
                             long double upper_offset) {
            const double lower = normalize_log_offset(lower_offset, domain);
            const double center = normalize_log_offset(center_offset, domain);
            const double upper = normalize_log_offset(upper_offset, domain);
            return lower < center && center < upper;
        };

        const long double count_ld = static_cast<long double>(count);
        const std::uint64_t middle = count / 2;
        const long double middle_ld = static_cast<long double>(middle);
        if (!valid_bin(0.0L, 0.5L, 1.0L) ||
            !valid_bin(middle_ld, middle_ld + 0.5L, middle_ld + 1.0L) ||
            !valid_bin(count_ld - 1.0L, count_ld - 0.5L, count_ld)) {
            throw std::invalid_argument(
                "TPE log integer transform cannot represent every discrete bin: " +
                dimension.name());
        }
    }

    static void validate_log_real_encoding(const RealDimension& dimension) {
        const RelativeLogDomain domain = log_real_domain(dimension);
        validate_log_domain(domain, dimension.name());
    }

    static std::int64_t decode_log_integer(const IntegerDimension& dimension, double normalized) {
        const long double unit = std::clamp(static_cast<long double>(normalized), 0.0L, 1.0L);
        if (unit <= 0.0L) {
            return dimension.low();
        }
        if (unit >= 1.0L) {
            return dimension.high();
        }

        const RelativeLogDomain domain = log_integer_domain(dimension);
        const long double offset = domain.reference * std::expm1(domain.span * unit);
        if (!std::isfinite(offset)) {
            throw std::logic_error("TPE produced a non-finite log-integer offset");
        }
        const std::uint64_t count = integer_count(dimension);
        if (offset <= 0.0L) {
            return dimension.low();
        }
        if (offset >= static_cast<long double>(count)) {
            return dimension.high();
        }
        const auto ordinal = static_cast<std::uint64_t>(std::floor(offset));
        return integer_at(dimension, std::min(ordinal, count - 1));
    }

    static double decode_log_real(const RealDimension& dimension, double normalized) {
        const long double unit = std::clamp(static_cast<long double>(normalized), 0.0L, 1.0L);
        if (unit <= 0.0L) {
            return dimension.low();
        }
        if (unit >= 1.0L) {
            return dimension.high();
        }

        const RelativeLogDomain domain = log_real_domain(dimension);
        const long double delta = domain.span * unit;
        long double value;
        if (delta <= 0.5L) {
            value = domain.reference + domain.reference * std::expm1(delta);
        } else {
            value = std::exp(std::log(domain.reference) + delta);
        }
        const double decoded = static_cast<double>(value);
        if (!std::isfinite(decoded)) {
            throw std::logic_error("TPE produced a non-finite log-real value");
        }
        return std::clamp(decoded, dimension.low(), dimension.high());
    }

    static double encode_numeric(const Dimension& dimension,
                                 const Encoding& encoding,
                                 const ParameterValue& value) {
        return std::visit(
            [&](const auto& item) -> double {
                using T = std::decay_t<decltype(item)>;
                if constexpr (std::is_same_v<T, IntegerDimension>) {
                    const auto integer = std::get<std::int64_t>(value);
                    if (encoding.kind == EncodingKind::LogInteger) {
                        const std::uint64_t ordinal = integer_ordinal(item, integer);
                        return normalize_log_offset(static_cast<long double>(ordinal) + 0.5L,
                                                    log_integer_domain(item));
                    }
                    return normalized_ordinal(integer_ordinal(item, integer), encoding.count);
                } else if constexpr (std::is_same_v<T, RealDimension>) {
                    const double real = std::get<double>(value);
                    if (encoding.kind == EncodingKind::LogContinuousReal) {
                        return normalize_log_value(static_cast<long double>(real),
                                                   log_real_domain(item));
                    }
                    if (encoding.kind == EncodingKind::ContinuousReal) {
                        return encode_continuous(item, real);
                    }
                    return normalized_ordinal(real_ordinal(item, real, encoding.count),
                                              encoding.count);
                } else {
                    throw std::logic_error("categorical dimension used as TPE numeric dimension");
                }
            },
            dimension);
    }

    static ParameterValue decode_numeric(const Dimension& dimension,
                                         const Encoding& encoding,
                                         double normalized) {
        return std::visit(
            [&](const auto& item) -> ParameterValue {
                using T = std::decay_t<decltype(item)>;
                if constexpr (std::is_same_v<T, IntegerDimension>) {
                    if (encoding.kind == EncodingKind::LogInteger) {
                        return decode_log_integer(item, normalized);
                    }
                    return integer_at(item, decoded_ordinal(normalized, encoding.count));
                } else if constexpr (std::is_same_v<T, RealDimension>) {
                    if (encoding.kind == EncodingKind::LogContinuousReal) {
                        return decode_log_real(item, normalized);
                    }
                    if (encoding.kind == EncodingKind::ContinuousReal) {
                        return decode_continuous(item, normalized);
                    }
                    return real_at(item, decoded_ordinal(normalized, encoding.count));
                } else {
                    throw std::logic_error("categorical dimension used as TPE numeric dimension");
                }
            },
            dimension);
    }

    static std::pair<double, double> numeric_bin(const Dimension& dimension,
                                                 const Encoding& encoding,
                                                 const ParameterValue& value) {
        return std::visit(
            [&](const auto& item) -> std::pair<double, double> {
                using T = std::decay_t<decltype(item)>;
                if constexpr (std::is_same_v<T, IntegerDimension>) {
                    const auto integer = std::get<std::int64_t>(value);
                    if (encoding.kind == EncodingKind::LogInteger) {
                        const auto domain = log_integer_domain(item);
                        const std::uint64_t ordinal = integer_ordinal(item, integer);
                        const long double lower = static_cast<long double>(ordinal);
                        return {
                            normalize_log_offset(lower, domain),
                            normalize_log_offset(lower + 1.0L, domain),
                        };
                    }
                    return normalized_bin(integer_ordinal(item, integer), encoding.count);
                } else if constexpr (std::is_same_v<T, RealDimension>) {
                    const auto ordinal =
                        real_ordinal(item, std::get<double>(value), encoding.count);
                    return normalized_bin(ordinal, encoding.count);
                } else {
                    throw std::logic_error("categorical dimension used as a TPE numeric bin");
                }
            },
            dimension);
    }

    static std::size_t categorical_index(const Dimension& dimension, const ParameterValue& value) {
        return std::visit(
            [&](const auto& item) -> std::size_t {
                using T = std::decay_t<decltype(item)>;
                if constexpr (std::is_same_v<T, BooleanDimension>) {
                    return std::get<bool>(value) ? 1 : 0;
                } else if constexpr (std::is_same_v<T, CategoricalDimension>) {
                    const auto found =
                        std::find(item.choices().begin(), item.choices().end(), value);
                    if (found == item.choices().end()) {
                        throw std::logic_error("TPE categorical value is not a legal choice");
                    }
                    return static_cast<std::size_t>(found - item.choices().begin());
                } else {
                    throw std::logic_error("numeric dimension used as TPE categorical dimension");
                }
            },
            dimension);
    }

    static ParameterValue decode_categorical(const Dimension& dimension, std::size_t index) {
        return std::visit(
            [&](const auto& item) -> ParameterValue {
                using T = std::decay_t<decltype(item)>;
                if constexpr (std::is_same_v<T, BooleanDimension>) {
                    return index != 0;
                } else if constexpr (std::is_same_v<T, CategoricalDimension>) {
                    return item.choices().at(index);
                } else {
                    throw std::logic_error("numeric dimension used as TPE categorical dimension");
                }
            },
            dimension);
    }

    static ParameterValue fixed_value(const Dimension& dimension) {
        return std::visit(
            [](const auto& item) -> ParameterValue {
                using T = std::decay_t<decltype(item)>;
                if constexpr (std::is_same_v<T, IntegerDimension>) {
                    return item.low();
                } else if constexpr (std::is_same_v<T, RealDimension>) {
                    return item.low();
                } else if constexpr (std::is_same_v<T, CategoricalDimension>) {
                    return item.choices().front();
                } else {
                    throw std::logic_error("boolean dimension cannot be fixed");
                }
            },
            dimension);
    }

    DimensionModel build_model(std::size_t column, const Dimension& dimension,
                                      const Encoding& encoding,
                                      const std::vector<ObservationView>& good,
                                      const std::vector<ObservationView>& bad,
                                      const std::vector<double>& good_weights,
                                      const std::vector<double>& bad_weights,
                                      double prior_weight, bool fast_density,
                                      const DimensionModel* cached, bool reuse_good,
                                      bool reuse_bad) {
        DimensionModel model;
        if (encoding.kind == EncodingKind::FixedInteger ||
            encoding.kind == EncodingKind::FixedReal ||
            encoding.kind == EncodingKind::FixedCategorical) {
            return model;
        }

        const std::string name(dimension_name(dimension));
        if (encoding.kind == EncodingKind::Integer || encoding.kind == EncodingKind::LogInteger ||
            encoding.kind == EncodingKind::ContinuousReal ||
            encoding.kind == EncodingKind::LogContinuousReal ||
            encoding.kind == EncodingKind::SteppedReal) {
            std::vector<double> good_values;
            std::vector<double> bad_values;
            good_values.reserve(good.size());
            bad_values.reserve(bad.size());
            if (!reuse_good)
                for (const auto& observation : good)
                    good_values.push_back(encode_numeric(dimension, encoding,
                        observation_value(observation, column, name)));
            if (!reuse_bad)
                for (const auto& observation : bad)
                    bad_values.push_back(encode_numeric(dimension, encoding,
                        observation_value(observation, column, name)));
            model.kind = DimensionModel::Kind::Numeric;
            model.good_numeric = reuse_good ? cached->good_numeric :
                std::make_shared<NumericModel>(good_values, good_weights, prior_weight,
                                               fast_density);
            model.bad_numeric = reuse_bad ? cached->bad_numeric :
                std::make_shared<NumericModel>(bad_values, bad_weights, prior_weight, fast_density);
            return model;
        }

        std::vector<std::size_t> good_values;
        std::vector<std::size_t> bad_values;
        good_values.reserve(good.size());
        bad_values.reserve(bad.size());
        if (!reuse_good)
            for (const auto& observation : good)
                good_values.push_back(categorical_index(dimension,
                    observation_value(observation, column, name)));
        if (!reuse_bad)
            for (const auto& observation : bad)
                bad_values.push_back(categorical_index(dimension,
                    observation_value(observation, column, name)));
        model.kind = DimensionModel::Kind::Categorical;
        model.good_categorical = reuse_good ? cached->good_categorical :
            std::make_shared<CategoricalModel>(static_cast<std::size_t>(encoding.count),
                                               good_values, good_weights, prior_weight);
        model.bad_categorical = reuse_bad ? cached->bad_categorical :
            std::make_shared<CategoricalModel>(static_cast<std::size_t>(encoding.count),
                                               bad_values, bad_weights, prior_weight);
        return model;
    }
};

TpeSampler::TpeSampler(SearchSpace space,
                       std::uint64_t seed,
                       ObjectiveDirection direction,
                       std::uint64_t max_candidates,
                       TpeSamplerConfig config,
                       CandidatePolicy candidate_policy)
    : space_(std::move(space)),
      seed_(seed),
      direction_(direction),
      max_candidates_(max_candidates),
      config_(config),
      candidate_policy_(candidate_policy) {
    validate_config(config_);
    if (direction_ != ObjectiveDirection::Maximize && direction_ != ObjectiveDirection::Minimize) {
        throw std::invalid_argument("TPE objective direction is invalid");
    }
    if (candidate_policy_ != CandidatePolicy::SamplerDefault &&
        candidate_policy_ != CandidatePolicy::WithoutReplacement &&
        candidate_policy_ != CandidatePolicy::Exhaustive) {
        throw std::invalid_argument("TPE candidate policy is invalid");
    }

    std::optional<std::uint64_t> finite_cardinality;
    if (candidate_policy_ != CandidatePolicy::SamplerDefault) {
        finite_cardinality = space_.finite_cardinality();
        if (!finite_cardinality.has_value()) {
            throw std::invalid_argument(
                "finite TPE candidate policy requires a step on every varying real dimension");
        }
        if (max_candidates_ != 0 && max_candidates_ > *finite_cardinality) {
            throw std::invalid_argument(
                "finite TPE candidate budget must not exceed search-space cardinality");
        }
        if (candidate_policy_ == CandidatePolicy::Exhaustive &&
            max_candidates_ != *finite_cardinality) {
            throw std::invalid_argument(
                "exhaustive TPE candidate budget must equal search-space cardinality");
        }
    }
    impl_ = std::make_unique<Impl>(space_, seed_, finite_cardinality);
}

TpeSampler::~TpeSampler() = default;
TpeSampler::TpeSampler(TpeSampler&&) noexcept = default;
TpeSampler& TpeSampler::operator=(TpeSampler&&) noexcept = default;

std::optional<Candidate> TpeSampler::next() {
    return ask();
}

std::optional<Candidate> TpeSampler::ask() {
    std::lock_guard<std::mutex> lock(impl_->mutex_);
    const std::uint64_t generated = impl_->generated_.load(std::memory_order_relaxed);
    const std::uint64_t candidate_id = impl_->next_id_;
    if (max_candidates_ != 0 && generated >= max_candidates_) {
        return std::nullopt;
    }
    if (impl_->finite_space_exhausted()) {
        return std::nullopt;
    }
    if (candidate_id == std::numeric_limits<std::uint64_t>::max())
        throw std::overflow_error("TPE trial IDs exhausted");
    if (candidate_policy_ == CandidatePolicy::SamplerDefault && !impl_->has_varying_dimension_ &&
        generated != 0) {
        return std::nullopt;
    }

    impl_->compact_observations(config_);
    const bool startup =
        impl_->completed_.load(std::memory_order_relaxed) < config_.startup_trials ||
        impl_->history_.empty();
    std::optional<Candidate> candidate;
    if (candidate_policy_ == CandidatePolicy::SamplerDefault) {
        candidate = startup ? std::optional<Candidate>(
                                  impl_->random_candidate(space_, candidate_id))
                            : impl_->tpe_candidate(space_, candidate_id, config_);
    } else if (startup) {
        constexpr std::uint64_t kStartupReservationAttempts = 64;
        for (std::uint64_t attempt = 0; attempt < kStartupReservationAttempts; ++attempt) {
            Candidate proposal = impl_->random_candidate(space_, candidate_id);
            if (impl_->reserve_candidate(space_, proposal)) {
                candidate = std::move(proposal);
                break;
            }
            impl_->duplicate_proposals_skipped_.fetch_add(1, std::memory_order_relaxed);
        }
        if (!candidate.has_value()) {
            candidate = impl_->fallback_candidate(space_, candidate_id);
        }
    } else {
        candidate = impl_->tpe_candidate(space_, candidate_id, config_);
        if (candidate.has_value()) {
            if (!impl_->reserve_candidate(space_, *candidate)) {
                throw std::logic_error("TPE selected a finite candidate that was already reserved");
            }
        } else {
            candidate = impl_->fallback_candidate(space_, candidate_id);
        }
    }
    if (!candidate.has_value()) {
        return std::nullopt;
    }
    if (!space_.is_valid(*candidate)) {
        throw std::logic_error("TPE generated an invalid candidate");
    }

    if (!impl_->register_pending(space_, *candidate)) {
        throw std::logic_error("duplicate TPE candidate id");
    }
    impl_->generated_.store(generated + 1, std::memory_order_relaxed);
    fingerprint_candidate(impl_->fingerprint_, candidate->id, space_.dimensions().size(),
        [&](std::size_t column) {
            return candidate->values.at(std::string(dimension_name(space_.dimensions()[column])));
        });
    ++impl_->attempted_;
    ++impl_->next_id_;
    impl_->outstanding_.fetch_add(1, std::memory_order_relaxed);
    return candidate;
}

void TpeSampler::tell(std::uint64_t candidate_id, double objective_value) {
    if (!std::isfinite(objective_value)) {
        throw std::invalid_argument("TPE objective value must be finite");
    }

    std::lock_guard<std::mutex> lock(impl_->mutex_);
    const auto found = impl_->pending_.find(candidate_id);
    if (found == impl_->pending_.end()) {
        throw std::invalid_argument("TPE candidate id is not outstanding: " +
                                    std::to_string(candidate_id));
    }
    const double score =
        direction_ == ObjectiveDirection::Maximize ? objective_value : -objective_value;
    fingerprint_score(impl_->fingerprint_, candidate_id, objective_value);
    impl_->retain_observation(found->second, score);
    impl_->pending_.erase(found);
    impl_->pending_encodings_.erase(candidate_id);
    impl_->outstanding_.fetch_sub(1, std::memory_order_relaxed);
    impl_->completed_.fetch_add(1, std::memory_order_relaxed);
    impl_->compact_observations(config_);
}

void TpeSampler::abandon(std::uint64_t candidate_id) {
    std::lock_guard<std::mutex> lock(impl_->mutex_);
    const auto found = impl_->pending_.find(candidate_id);
    if (found == impl_->pending_.end()) {
        throw std::invalid_argument("TPE candidate id is not outstanding: " +
                                    std::to_string(candidate_id));
    }
    impl_->pending_.erase(found);
    impl_->pending_encodings_.erase(candidate_id);
    impl_->outstanding_.fetch_sub(1, std::memory_order_relaxed);
}

bool TpeSampler::warm_start(const std::vector<WarmStartObservation>& observations,
                            std::uint64_t replay_batch_size, const std::string& sampler_state) {
    class VectorSource final : public WarmStartSource {
    public:
        VectorSource(const SearchSpace& space, std::vector<WarmStartObservation> values)
            : values_(std::move(values)) {
            for (const auto& dimension : space.dimensions())
                names_.emplace_back(dimension_name(dimension));
            for (const auto& value : values_)
                if (!space.is_valid(value.candidate))
                    throw std::invalid_argument("invalid TPE warm-start observation");
            std::sort(values_.begin(), values_.end(), [](const auto& left, const auto& right) {
                return left.candidate.id < right.candidate.id;
            });
        }
        std::uint64_t size() const noexcept override { return values_.size(); }
        std::uint64_t id(std::uint64_t row) const override { return values_.at(row).candidate.id; }
        ParameterValue parameter(std::uint64_t row, std::size_t column) const override {
            return values_.at(row).candidate.values.at(names_.at(column));
        }
        std::optional<double> objective(std::uint64_t row) const override {
            return values_.at(row).objective;
        }
    private:
        std::vector<WarmStartObservation> values_;
        std::vector<std::string> names_;
    };
    return warm_start(std::make_shared<VectorSource>(space_, observations), replay_batch_size,
                      sampler_state);
}

bool TpeSampler::warm_start(std::shared_ptr<const WarmStartSource> source,
                           std::uint64_t replay_batch_size, const std::string& sampler_state) {
    std::unique_ptr<Impl> previous;
    std::lock_guard<std::mutex> lock(impl_->mutex_);
    if (impl_->generated_ != 0 || impl_->completed_ != 0 || impl_->next_id_ != 0 ||
        !impl_->pending_.empty())
        throw std::logic_error("TPE warm start requires a pristine sampler");
    if (!source || source->size() >= Impl::owned_marker)
        throw std::invalid_argument("invalid TPE warm-start source");
    std::uint64_t next_id = 0;
    for (std::uint64_t row = 0; row < source->size(); ++row) {
        const auto identifier = source->id(row);
        const auto objective = source->objective(row);
        if (identifier < next_id || identifier == std::numeric_limits<std::uint64_t>::max() ||
            (objective && !std::isfinite(*objective)))
            throw std::invalid_argument("invalid TPE warm-start observation");
        for (std::size_t column = 0; column < space_.dimensions().size(); ++column)
            if (!dimension_contains(space_.dimensions()[column], source->parameter(row, column)))
                throw std::invalid_argument("invalid TPE warm-start parameter");
        next_id = identifier + 1;
    }
    const auto policy = candidate_policy_ == CandidatePolicy::Exhaustive
        ? CandidatePolicy::WithoutReplacement : candidate_policy_;
    TpeSampler restored(space_, seed_, direction_, 0, config_, policy);
    (void)replay_batch_size;
    restored.impl_ = std::make_unique<Impl>(space_, continuation_seed(seed_, source->size()),
                                           impl_->finite_cardinality_);
    restored.impl_->source_ = source;
    restored.impl_->source_direction_ = direction_ == ObjectiveDirection::Maximize ? 1.0 : -1.0;
    for (std::uint64_t row = 0; row < source->size(); ++row) {
        if (restored.impl_->reservations_)
            restored.impl_->reserve_candidate(space_, source->observation(space_, row).candidate);
        fingerprint_candidate(restored.impl_->fingerprint_, source->id(row),
            space_.dimensions().size(), [&](std::size_t column) {
                return source->parameter(row, column);
            });
        if (const auto objective = source->objective(row)) {
            fingerprint_score(restored.impl_->fingerprint_, source->id(row), *objective);
            restored.impl_->history_.push_back(row);
            ++restored.impl_->completed_;
            restored.impl_->compact_observations(config_);
        }
    }
    restored.impl_->next_id_ = next_id;
    restored.impl_->attempted_ = source->size();
    const bool checkpoint_restored = restored.impl_->read_state(sampler_state,
        state_signature(space_, seed_, direction_, policy, config_, restored.impl_->numeric_build_),
        space_, config_);
    restored.impl_->generated_ = 0;
    restored.impl_->duplicate_proposals_skipped_.store(0, std::memory_order_relaxed);
    restored.impl_->next_id_ = next_id;
    previous.swap(impl_);
    impl_.swap(restored.impl_);
    return checkpoint_restored;
}

std::string TpeSampler::sampler_state() const {
    std::lock_guard<std::mutex> lock(impl_->mutex_);
    if (!impl_->pending_.empty())
        throw std::logic_error("TPE sampler state requires no outstanding candidates");
    const auto policy = candidate_policy_ == CandidatePolicy::Exhaustive
        ? CandidatePolicy::WithoutReplacement : candidate_policy_;
    return impl_->write_state(
        state_signature(space_, seed_, direction_, policy, config_, impl_->numeric_build_));
}

void TpeSampler::reset() {
    impl_->reset(seed_);
}

std::uint64_t TpeSampler::generated() const noexcept {
    return impl_->generated_.load(std::memory_order_relaxed);
}

std::uint64_t TpeSampler::completed() const noexcept {
    return impl_->completed_.load(std::memory_order_relaxed);
}

std::uint64_t TpeSampler::outstanding() const noexcept {
    return impl_->outstanding_.load(std::memory_order_relaxed);
}

std::size_t TpeSampler::retained_observations() const {
    std::lock_guard<std::mutex> lock(impl_->mutex_);
    return impl_->history_.size() + impl_->older_bad_.size();
}

std::uint64_t TpeSampler::duplicate_proposals_skipped() const noexcept {
    return impl_->duplicate_proposals_skipped_.load(std::memory_order_relaxed);
}

}  // namespace pineforge::hpo
