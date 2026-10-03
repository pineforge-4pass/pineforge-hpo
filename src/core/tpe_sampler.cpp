#include "pineforge/hpo/sampler.hpp"
#include "ordinal_set.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstddef>
#include <limits>
#include <map>
#include <mutex>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <unordered_set>
#include <utility>
#include <vector>

namespace pineforge::hpo {
namespace {

constexpr double kInverse53 = 1.0 / 9007199254740992.0;
constexpr double kLogSqrtTwoPi = 0.91893853320467274178;
constexpr double kSqrtTwo = 1.41421356237309504880;
constexpr double kTwoPi = 6.28318530717958647693;
constexpr std::uint64_t kMaxExactlyRepresentableBins = std::uint64_t{1} << 53U;
constexpr std::uint64_t kMaxEiCandidates = 1'000'000;

std::uint64_t bounded_random(std::mt19937_64& engine, std::uint64_t bound) {
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

double unit_random(std::mt19937_64& engine) {
    return static_cast<double>(engine() >> 11U) * kInverse53;
}

double open_unit_random(std::mt19937_64& engine) {
    return (static_cast<double>(engine() >> 11U) + 0.5) * kInverse53;
}

double standard_normal(std::mt19937_64& engine) {
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

    double sample(std::mt19937_64& engine) const {
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

    std::size_t sample(std::mt19937_64& engine) const {
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

    Impl(const SearchSpace& space,
         std::uint64_t seed,
         std::optional<std::uint64_t> finite_cardinality)
        : engine_(seed), reservoir_engine_(seed ^ 0xd1b54a32d192ed03ULL),
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
        older_bad_seen_ = 0;
        compact_history_ = false;
        cached_models_.clear();
        good_ids_.clear();
        bad_ids_.clear();
        pending_.clear();
        pending_encodings_.clear();
        if (reservations_.has_value()) {
            reservations_->clear();
        }
        fallback_cursor_ = 0;
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
        std::vector<const CompletedObservation*> ranked;
        ranked.reserve(history_.size() + older_bad_.size());
        for (const auto& observation : history_) {
            ranked.push_back(&observation);
        }
        for (const auto& observation : older_bad_)
            ranked.push_back(&observation);
        std::sort(ranked.begin(), ranked.end(), [](const auto* left, const auto* right) {
            if (left->score != right->score) {
                return left->score > right->score;
            }
            return left->candidate.id < right->candidate.id;
        });

        const std::size_t good_count = compact_history_
            ? std::min(history_.size() - 1,
                       gamma_count(completed_.load(std::memory_order_relaxed), config))
            : gamma_count(history_.size(), config);
        std::vector<const Candidate*> good;
        std::vector<const Candidate*> bad;
        good.reserve(good_count);
        bad.reserve(ranked.size() - good_count + pending_.size());
        for (std::size_t i = 0; i < ranked.size(); ++i) {
            (i < good_count ? good : bad).push_back(&ranked[i]->candidate);
        }

        auto chronological = [](const Candidate* left, const Candidate* right) {
            return left->id < right->id;
        };
        std::sort(good.begin(), good.end(), chronological);
        std::sort(bad.begin(), bad.end(), chronological);
        if (config.constant_liar && !compact_history_) {
            for (const auto& [pending_id, candidate] : pending_) {
                (void)pending_id;
                bad.push_back(&candidate);
            }
            std::sort(bad.begin(), bad.end(), chronological);
        }

        const auto good_weights = observation_weights(good.size());
        const auto bad_weights = observation_weights(bad.size());
        std::vector<std::uint64_t> good_ids;
        std::vector<std::uint64_t> bad_ids;
        for (const auto* candidate : good)
            good_ids.push_back(candidate->id);
        for (const auto* candidate : bad)
            bad_ids.push_back(candidate->id);
        const auto model_epoch = completed_.load(std::memory_order_relaxed) / 32;
        const bool reuse_good = good_ids == good_ids_ &&
                                !cached_models_.empty() &&
                                compact_history_ == cached_compact_;
        const bool reuse_bad = (compact_history_ ? model_epoch == cached_epoch_
                                                : bad_ids == bad_ids_) &&
                               !cached_models_.empty() &&
                               compact_history_ == cached_compact_;
        if (!reuse_good || !reuse_bad) {
            std::vector<DimensionModel> updated;
            updated.reserve(space.dimensions().size());
            for (std::size_t index = 0; index < space.dimensions().size(); ++index) {
                updated.push_back(build_model(space.dimensions()[index], encodings_[index],
                    good, bad, good_weights, bad_weights, config.prior_weight, compact_history_,
                    cached_models_.empty() ? nullptr : &cached_models_[index], reuse_good,
                    reuse_bad));
            }
            cached_models_ = std::move(updated);
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
                current_ids.push_back(history_[index].candidate.id);
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
            double log_ratio = 0.0;
            for (std::size_t i = 0; i < space.dimensions().size(); ++i) {
                const auto& dimension = space.dimensions()[i];
                const auto& encoding = encodings_[i];
                const auto& model = models[i];
                ParameterValue value;
                double contribution = 0.0;

                if (model.kind == DimensionModel::Kind::Numeric) {
                    const double sampled = model.good_numeric->sample(engine_);
                    value = decode_numeric(dimension, encoding, sampled);
                    const bool continuous = encoding.kind == EncodingKind::ContinuousReal ||
                                            encoding.kind == EncodingKind::LogContinuousReal;
                    const double legal = compact_history_ && continuous ? sampled :
                        encode_numeric(dimension, encoding, value);
                    if (encoding.kind == EncodingKind::ContinuousReal ||
                        encoding.kind == EncodingKind::LogContinuousReal) {
                        contribution = model.good_numeric->log_density(legal) -
                            model.bad_numeric->pending_log_density(legal, pending_numeric[i]);
                    } else {
                        const auto [lower, upper] = numeric_bin(dimension, encoding, value);
                        contribution = model.good_numeric->log_bin_mass(lower, upper) -
                            model.bad_numeric->pending_log_bin_mass(lower, upper,
                                                                    pending_numeric[i]);
                    }
                } else if (model.kind == DimensionModel::Kind::Categorical) {
                    const std::size_t sampled = model.good_categorical->sample(engine_);
                    value = decode_categorical(dimension, sampled);
                    contribution = model.good_categorical->log_density(sampled) -
                        model.bad_categorical->pending_log_density(sampled, pending_values[i]);
                } else {
                    value = fixed_value(dimension);
                }
                if (!std::isfinite(contribution)) {
                    throw std::logic_error(
                        "TPE produced a non-finite acquisition contribution for dimension: " +
                        std::string(dimension_name(dimension)));
                }
                log_ratio += contribution;
                if (!std::isfinite(log_ratio)) {
                    throw std::logic_error("TPE acquisition log ratio is non-finite");
                }
                values.push_back(std::move(value));
            }
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

    void retain_observation(Candidate candidate, double score, const TpeSamplerConfig& config) {
        history_.push_back({std::move(candidate), score});
        compact_observations(config);
    }

    void compact_observations(const TpeSamplerConfig& config) {
        const std::size_t elite_count = static_cast<std::size_t>(config.gamma_cap);
        const std::size_t recent_count = 64;
        const std::size_t warmup_limit = std::max<std::size_t>(1000, elite_count + recent_count);
        if (!compact_history_ && history_.size() < warmup_limit &&
            generated_.load(std::memory_order_relaxed) < warmup_limit)
            return;
        compact_history_ = true;
        if (history_.size() <= elite_count + recent_count)
            return;
        std::sort(history_.begin(), history_.end(), [](const auto& left, const auto& right) {
            return left.score != right.score ? left.score > right.score
                                            : left.candidate.id < right.candidate.id;
        });
        std::sort(history_.begin() + elite_count, history_.end(),
                  [](const auto& left, const auto& right) {
                      return left.candidate.id > right.candidate.id;
                  });
        const auto retained = elite_count + recent_count;
        for (auto index = history_.size(); index > retained; --index) {
            ++older_bad_seen_;
            if (older_bad_.size() < config.bad_reservoir_size) {
                older_bad_.push_back(std::move(history_[index - 1]));
            } else if (config.bad_reservoir_size != 0) {
                const auto selected = bounded_random(reservoir_engine_, older_bad_seen_);
                if (selected < older_bad_.size())
                    older_bad_[selected] = std::move(history_[index - 1]);
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
    std::mt19937_64 engine_;
    std::mt19937_64 reservoir_engine_;
    std::vector<Encoding> encodings_;
    std::vector<CompletedObservation> history_;
    std::vector<CompletedObservation> older_bad_;
    std::uint64_t older_bad_seen_ = 0;
    bool compact_history_ = false;
    bool cached_compact_ = false;
    std::uint64_t cached_epoch_ = 0;
    std::vector<DimensionModel> cached_models_;
    std::vector<std::uint64_t> good_ids_;
    std::vector<std::uint64_t> bad_ids_;
    std::map<std::uint64_t, Candidate> pending_;
    std::map<std::uint64_t, std::vector<double>> pending_encodings_;
    std::optional<std::uint64_t> finite_cardinality_;
    std::optional<FiniteReservationSet> reservations_;
    std::uint64_t fallback_cursor_ = 0;
    bool has_varying_dimension_ = false;
    std::atomic<std::uint64_t> generated_{0};
    std::atomic<std::uint64_t> completed_{0};
    std::atomic<std::uint64_t> outstanding_{0};
    std::atomic<std::uint64_t> duplicate_proposals_skipped_{0};

private:
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

    static DimensionModel build_model(const Dimension& dimension,
                                      const Encoding& encoding,
                                      const std::vector<const Candidate*>& good,
                                      const std::vector<const Candidate*>& bad,
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
            for (const Candidate* candidate : reuse_good ? std::vector<const Candidate*>{} : good) {
                good_values.push_back(encode_numeric(dimension, encoding, *candidate->find(name)));
            }
            for (const Candidate* candidate : reuse_bad ? std::vector<const Candidate*>{} : bad) {
                bad_values.push_back(encode_numeric(dimension, encoding, *candidate->find(name)));
            }
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
        for (const Candidate* candidate : reuse_good ? std::vector<const Candidate*>{} : good) {
            good_values.push_back(categorical_index(dimension, *candidate->find(name)));
        }
        for (const Candidate* candidate : reuse_bad ? std::vector<const Candidate*>{} : bad) {
            bad_values.push_back(categorical_index(dimension, *candidate->find(name)));
        }
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
    if (max_candidates_ != 0 && generated >= max_candidates_) {
        return std::nullopt;
    }
    if (impl_->finite_space_exhausted()) {
        return std::nullopt;
    }
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
        candidate = startup ? std::optional<Candidate>(impl_->random_candidate(space_, generated))
                            : impl_->tpe_candidate(space_, generated, config_);
    } else if (startup) {
        constexpr std::uint64_t kStartupReservationAttempts = 64;
        for (std::uint64_t attempt = 0; attempt < kStartupReservationAttempts; ++attempt) {
            Candidate proposal = impl_->random_candidate(space_, generated);
            if (impl_->reserve_candidate(space_, proposal)) {
                candidate = std::move(proposal);
                break;
            }
            impl_->duplicate_proposals_skipped_.fetch_add(1, std::memory_order_relaxed);
        }
        if (!candidate.has_value()) {
            candidate = impl_->fallback_candidate(space_, generated);
        }
    } else {
        candidate = impl_->tpe_candidate(space_, generated, config_);
        if (candidate.has_value()) {
            if (!impl_->reserve_candidate(space_, *candidate)) {
                throw std::logic_error("TPE selected a finite candidate that was already reserved");
            }
        } else {
            candidate = impl_->fallback_candidate(space_, generated);
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
    impl_->retain_observation(found->second, score, config_);
    impl_->pending_.erase(found);
    impl_->pending_encodings_.erase(candidate_id);
    impl_->outstanding_.fetch_sub(1, std::memory_order_relaxed);
    impl_->completed_.fetch_add(1, std::memory_order_relaxed);
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
