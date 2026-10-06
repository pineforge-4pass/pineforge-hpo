#include <pineforge/hpo/error.hpp>
#include "pineforge/hpo/sampler.hpp"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <ctime>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <type_traits>
#include <unordered_map>
#include <utility>
#include <vector>

#if defined(__clang__)
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdollar-in-identifier-extension"
#endif
#include <dlib/global_optimization.h>
#if defined(__clang__)
#pragma clang diagnostic pop
#endif

namespace pineforge::hpo {
namespace {

std::uint64_t integer_count(const IntegerDimension& dimension) {
    const std::uint64_t span =
        static_cast<std::uint64_t>(dimension.high()) - static_cast<std::uint64_t>(dimension.low());
    const std::uint64_t quotient = span / static_cast<std::uint64_t>(dimension.step());
    if (quotient == std::numeric_limits<std::uint64_t>::max()) {
        throw TypedHpoError<std::overflow_error>("hpo_study_spec_invalid", {{"reason", "sampler"}},
                                                 "integer dimension cardinality exceeds uint64_t");
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
    const long double low = static_cast<long double>(dimension.low());
    const long double high = static_cast<long double>(dimension.high());
    const long double step = *dimension.step();
    const long double span = high - low;
    const long double scaled = std::isfinite(span) ? span / step : high / step - low / step;
    const long double floored = std::floor(scaled);
    if (floored >= static_cast<long double>(std::numeric_limits<std::uint64_t>::max())) {
        throw TypedHpoError<std::overflow_error>("hpo_study_spec_invalid", {{"reason", "sampler"}},
                                                 "real dimension cardinality exceeds uint64_t");
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
        throw TypedHpoError<std::overflow_error>("hpo_study_spec_invalid", {{"reason", "sampler"}},
                                                 "real dimension cardinality exceeds uint64_t");
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

std::int64_t log_integer_at(const IntegerDimension& dimension, double coordinate) {
    if (!std::isfinite(coordinate)) {
        throw TypedHpoError<std::runtime_error>("hpo_invariant", {},
                                                "dlib returned an invalid log-integer coordinate");
    }
    const long double value = std::exp(static_cast<long double>(coordinate));
    if (value <= static_cast<long double>(dimension.low())) {
        return dimension.low();
    }
    if (value >= static_cast<long double>(dimension.high())) {
        return dimension.high();
    }
    const auto rounded = static_cast<std::int64_t>(std::floor(value + 0.5L));
    return std::clamp(rounded, dimension.low(), dimension.high());
}

double log_real_at(const RealDimension& dimension, double coordinate) {
    if (!std::isfinite(coordinate)) {
        throw TypedHpoError<std::runtime_error>("hpo_invariant", {},
                                                "dlib returned an invalid log-real coordinate");
    }
    const double value = static_cast<double>(std::exp(static_cast<long double>(coordinate)));
    return std::clamp(value, dimension.low(), dimension.high());
}

constexpr std::uint64_t kMaxExactlyRepresentableDiscreteValues = std::uint64_t{1} << 53U;

std::uint64_t discrete_coordinate(double value, std::uint64_t count) {
    if (!std::isfinite(value) || count == 0) {
        throw TypedHpoError<std::runtime_error>("hpo_invariant", {},
                                                "dlib returned an invalid discrete coordinate");
    }
    const double rounded = std::round(value);
    if (rounded <= 0.0) {
        return 0;
    }
    const std::uint64_t maximum = count - 1;
    if (rounded >= static_cast<double>(maximum)) {
        return maximum;
    }
    return static_cast<std::uint64_t>(rounded);
}

std::time_t dlib_seed(std::uint64_t seed) noexcept {
    return static_cast<std::time_t>(seed);
}

}  // namespace

class DlibGlobalSampler::Impl {
public:
    struct Encoding {
        enum class Kind {
            FixedInteger,
            IntegerIndex,
            LogIntegerCoordinate,
            FixedReal,
            ContinuousReal,
            LogContinuousReal,
            SteppedRealIndex,
            BooleanIndex,
            FixedCategorical,
            CategoricalIndex,
        };

        Kind kind;
        std::uint64_t count = 0;
    };

    struct PendingRequest {
        std::unique_ptr<dlib::function_evaluation_request> request;
    };

    Impl(const SearchSpace& space, std::uint64_t seed) : seed_(seed) {
        build_encoding(space);
        initialize_search();
    }

    Candidate decode(const SearchSpace& space,
                     std::uint64_t id,
                     const dlib::matrix<double, 0, 1>* point) const {
        Candidate candidate;
        candidate.id = id;
        long coordinate = 0;

        for (std::size_t index = 0; index < space.dimensions().size(); ++index) {
            const auto& dimension = space.dimensions()[index];
            const auto& encoding = encodings_[index];
            ParameterValue value = std::visit(
                [&](const auto& item) -> ParameterValue {
                    using T = std::decay_t<decltype(item)>;
                    if constexpr (std::is_same_v<T, IntegerDimension>) {
                        if (encoding.kind == Encoding::Kind::FixedInteger) {
                            return item.low();
                        }
                        if (encoding.kind == Encoding::Kind::LogIntegerCoordinate) {
                            return log_integer_at(item, (*point)(coordinate++));
                        }
                        const auto ordinal =
                            discrete_coordinate((*point)(coordinate++), encoding.count);
                        return integer_at(item, ordinal);
                    } else if constexpr (std::is_same_v<T, RealDimension>) {
                        if (encoding.kind == Encoding::Kind::FixedReal) {
                            return item.low();
                        }
                        if (encoding.kind == Encoding::Kind::SteppedRealIndex) {
                            const auto ordinal =
                                discrete_coordinate((*point)(coordinate++), encoding.count);
                            return real_at(item, ordinal);
                        }
                        const double sampled = (*point)(coordinate++);
                        if (encoding.kind == Encoding::Kind::LogContinuousReal) {
                            return log_real_at(item, sampled);
                        }
                        return std::clamp(sampled, item.low(), item.high());
                    } else if constexpr (std::is_same_v<T, BooleanDimension>) {
                        return discrete_coordinate((*point)(coordinate++), 2) != 0;
                    } else {
                        if (encoding.kind == Encoding::Kind::FixedCategorical) {
                            return item.choices().front();
                        }
                        const auto ordinal =
                            discrete_coordinate((*point)(coordinate++), encoding.count);
                        return item.choices()[static_cast<std::size_t>(ordinal)];
                    }
                },
                dimension);
            candidate.values.emplace(std::string(dimension_name(dimension)), std::move(value));
        }
        return candidate;
    }

    void reset() {
        std::lock_guard<std::mutex> lock(mutex_);
        pending_.clear();
        search_.reset();
        generated_.store(0, std::memory_order_relaxed);
        completed_.store(0, std::memory_order_relaxed);
        outstanding_.store(0, std::memory_order_relaxed);
        initialize_search();
    }

    std::mutex mutex_;
    std::vector<Encoding> encodings_;
    std::vector<double> lower_bounds_;
    std::vector<double> upper_bounds_;
    std::vector<bool> is_integer_;
    std::unique_ptr<dlib::global_function_search> search_;
    std::unordered_map<std::uint64_t, PendingRequest> pending_;
    std::atomic<std::uint64_t> generated_{0};
    std::atomic<std::uint64_t> completed_{0};
    std::atomic<std::uint64_t> outstanding_{0};

private:
    void append_discrete(std::uint64_t count) {
        if (count < 2) {
            throw TypedHpoError<std::logic_error>(
                "hpo_invariant", {}, "dlib discrete coordinates require at least two values");
        }
        if (count > kMaxExactlyRepresentableDiscreteValues) {
            throw TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "sampler"}},
                "dlib cannot exactly encode a discrete dimension with more than 2^53 values");
        }
        lower_bounds_.push_back(0.0);
        upper_bounds_.push_back(static_cast<double>(count - 1));
        is_integer_.push_back(true);
    }

    void append_continuous(double low, double high) {
        lower_bounds_.push_back(low);
        upper_bounds_.push_back(high);
        is_integer_.push_back(false);
    }

    void build_encoding(const SearchSpace& space) {
        encodings_.reserve(space.dimensions().size());
        for (const auto& dimension : space.dimensions()) {
            std::visit(
                [&](const auto& item) {
                    using T = std::decay_t<decltype(item)>;
                    if constexpr (std::is_same_v<T, IntegerDimension>) {
                        const auto count = integer_count(item);
                        if (count == 1) {
                            encodings_.push_back({Encoding::Kind::FixedInteger, count});
                        } else if (item.log()) {
                            const long double low = item.low();
                            const long double high = item.high();
                            const double transformed_low =
                                static_cast<double>(std::log(low - 0.5L));
                            const double transformed_high =
                                static_cast<double>(std::log(high + 0.5L));
                            if (!(transformed_low < transformed_high)) {
                                throw TypedHpoError<std::invalid_argument>(
                                    "hpo_study_spec_invalid", {{"reason", "sampler"}},
                                    "dlib log integer bounds collapse in double precision: " +
                                        item.name());
                            }
                            append_continuous(transformed_low, transformed_high);
                            encodings_.push_back({Encoding::Kind::LogIntegerCoordinate, count});
                        } else {
                            append_discrete(count);
                            encodings_.push_back({Encoding::Kind::IntegerIndex, count});
                        }
                    } else if constexpr (std::is_same_v<T, RealDimension>) {
                        if (item.low() == item.high()) {
                            encodings_.push_back({Encoding::Kind::FixedReal, 1});
                        } else if (item.log()) {
                            const double transformed_low = std::log(item.low());
                            const double transformed_high = std::log(item.high());
                            if (!(transformed_low < transformed_high)) {
                                throw TypedHpoError<std::invalid_argument>(
                                    "hpo_study_spec_invalid", {{"reason", "sampler"}},
                                    "dlib log real bounds collapse in double precision: " +
                                        item.name());
                            }
                            append_continuous(transformed_low, transformed_high);
                            encodings_.push_back({Encoding::Kind::LogContinuousReal, 0});
                        } else if (item.step().has_value()) {
                            const auto count = real_grid_count(item);
                            if (count == 1) {
                                encodings_.push_back({Encoding::Kind::FixedReal, count});
                            } else {
                                append_discrete(count);
                                encodings_.push_back({Encoding::Kind::SteppedRealIndex, count});
                            }
                        } else {
                            append_continuous(item.low(), item.high());
                            encodings_.push_back({Encoding::Kind::ContinuousReal, 0});
                        }
                    } else if constexpr (std::is_same_v<T, BooleanDimension>) {
                        append_discrete(2);
                        encodings_.push_back({Encoding::Kind::BooleanIndex, 2});
                    } else {
                        const auto count = static_cast<std::uint64_t>(item.choices().size());
                        if (count == 1) {
                            encodings_.push_back({Encoding::Kind::FixedCategorical, count});
                        } else {
                            append_discrete(count);
                            encodings_.push_back({Encoding::Kind::CategoricalIndex, count});
                        }
                    }
                },
                dimension);
        }
    }

    void initialize_search() {
        if (lower_bounds_.empty()) {
            return;
        }
        dlib::matrix<double, 0, 1> lower(static_cast<long>(lower_bounds_.size()));
        dlib::matrix<double, 0, 1> upper(static_cast<long>(upper_bounds_.size()));
        for (std::size_t index = 0; index < lower_bounds_.size(); ++index) {
            lower(static_cast<long>(index)) = lower_bounds_[index];
            upper(static_cast<long>(index)) = upper_bounds_[index];
        }
        dlib::function_spec specification(std::move(lower), std::move(upper), is_integer_);
        search_ = std::make_unique<dlib::global_function_search>(specification);
        search_->set_seed(dlib_seed(seed_));
    }

    std::uint64_t seed_;
};

DlibGlobalSampler::DlibGlobalSampler(SearchSpace space,
                                     std::uint64_t seed,
                                     ObjectiveDirection direction,
                                     std::uint64_t max_candidates)
    : space_(std::move(space)),
      seed_(seed),
      direction_(direction),
      max_candidates_(max_candidates),
      impl_(nullptr) {
    if (seed_ > static_cast<std::uint64_t>(std::numeric_limits<std::int32_t>::max())) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "sampler"}},
            "dlib seed must be in the range [0, 2147483647]");
    }
    impl_ = std::make_unique<Impl>(space_, seed_);
}

DlibGlobalSampler::~DlibGlobalSampler() = default;
DlibGlobalSampler::DlibGlobalSampler(DlibGlobalSampler&&) noexcept = default;
DlibGlobalSampler& DlibGlobalSampler::operator=(DlibGlobalSampler&&) noexcept = default;

std::optional<Candidate> DlibGlobalSampler::next() {
    return ask();
}

std::optional<Candidate> DlibGlobalSampler::ask() {
    std::lock_guard<std::mutex> lock(impl_->mutex_);
    const auto generated = impl_->generated_.load(std::memory_order_relaxed);
    if ((max_candidates_ != 0 && generated >= max_candidates_) ||
        (impl_->search_ == nullptr && generated != 0)) {
        return std::nullopt;
    }

    std::unique_ptr<dlib::function_evaluation_request> request;
    const dlib::matrix<double, 0, 1>* point = nullptr;
    if (impl_->search_ != nullptr) {
        request = std::make_unique<dlib::function_evaluation_request>(impl_->search_->get_next_x());
        point = &request->x();
    }

    Candidate candidate = impl_->decode(space_, generated, point);
    if (!space_.is_valid(candidate)) {
        throw TypedHpoError<std::runtime_error>(
            "hpo_invariant", {}, "dlib produced a candidate outside the search space");
    }
    const auto inserted =
        impl_->pending_.emplace(generated, Impl::PendingRequest{std::move(request)});
    if (!inserted.second) {
        throw TypedHpoError<std::logic_error>("hpo_invariant", {}, "duplicate dlib candidate id");
    }
    impl_->generated_.store(generated + 1, std::memory_order_relaxed);
    impl_->outstanding_.fetch_add(1, std::memory_order_relaxed);
    return candidate;
}

void DlibGlobalSampler::tell(std::uint64_t candidate_id, double objective_value) {
    if (!std::isfinite(objective_value)) {
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "sampler"}},
                                                   "dlib objective value must be finite");
    }

    std::lock_guard<std::mutex> lock(impl_->mutex_);
    const auto found = impl_->pending_.find(candidate_id);
    if (found == impl_->pending_.end()) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "sampler"}},
            "dlib candidate id is not outstanding: " + std::to_string(candidate_id));
    }
    if (found->second.request != nullptr) {
        const double dlib_value =
            direction_ == ObjectiveDirection::Maximize ? objective_value : -objective_value;
        found->second.request->set(dlib_value);
    }
    impl_->pending_.erase(found);
    impl_->outstanding_.fetch_sub(1, std::memory_order_relaxed);
    impl_->completed_.fetch_add(1, std::memory_order_relaxed);
}

void DlibGlobalSampler::abandon(std::uint64_t candidate_id) {
    std::lock_guard<std::mutex> lock(impl_->mutex_);
    const auto found = impl_->pending_.find(candidate_id);
    if (found == impl_->pending_.end()) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "sampler"}},
            "dlib candidate id is not outstanding: " + std::to_string(candidate_id));
    }
    impl_->pending_.erase(found);
    impl_->outstanding_.fetch_sub(1, std::memory_order_relaxed);
}

void DlibGlobalSampler::reset() {
    impl_->reset();
}

std::uint64_t DlibGlobalSampler::generated() const noexcept {
    return impl_->generated_.load(std::memory_order_relaxed);
}

std::uint64_t DlibGlobalSampler::completed() const noexcept {
    return impl_->completed_.load(std::memory_order_relaxed);
}

std::uint64_t DlibGlobalSampler::outstanding() const noexcept {
    return impl_->outstanding_.load(std::memory_order_relaxed);
}

}  // namespace pineforge::hpo
