#include "pineforge/hpo/search_space.hpp"
#include <pineforge/hpo/error.hpp>
#include "portable_grid.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <set>
#include <sstream>
#include <stdexcept>
#include <type_traits>
#include <utility>

namespace pineforge::hpo {
namespace {

void require_name(const std::string& name) {
    if (name.empty()) {
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "search_space"}},
                                                   "dimension name must not be empty");
    }
}

bool same_parameter_value(const ParameterValue& left, const ParameterValue& right) noexcept {
    if (left.index() != right.index()) {
        return false;
    }
    return left == right;
}

bool finite_parameter_value(const ParameterValue& value) noexcept {
    const auto* real = std::get_if<double>(&value);
    return real == nullptr || std::isfinite(*real);
}

std::string expected_type_name(DimensionKind kind) {
    switch (kind) {
    case DimensionKind::Integer:
        return "integer";
    case DimensionKind::Real:
        return "real";
    case DimensionKind::Boolean:
        return "boolean";
    case DimensionKind::Categorical:
        return "one of the categorical choices";
    }
    return "valid";
}

std::uint64_t integer_count(const IntegerDimension& dimension) {
    const std::uint64_t span =
        static_cast<std::uint64_t>(dimension.high()) - static_cast<std::uint64_t>(dimension.low());
    const std::uint64_t quotient = span / static_cast<std::uint64_t>(dimension.step());
    if (quotient == std::numeric_limits<std::uint64_t>::max()) {
        throw TypedHpoError<std::overflow_error>(
            "hpo_study_spec_invalid", {{"reason", "search_space"}},
            "integer dimension cardinality exceeds uint64_t: " + dimension.name());
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
    return detail::portable_grid_count(dimension.low(), dimension.high(), *dimension.step());
}

double real_at(const RealDimension& dimension, std::uint64_t index, std::uint64_t count) {
    double decoded =
        detail::math::fma(static_cast<double>(index), *dimension.step(), dimension.low());
    if (index + 1 == count && decoded > dimension.high() &&
        decoded <=
            detail::math::nextafter(dimension.high(), std::numeric_limits<double>::infinity())) {
        decoded = dimension.high();
    }
    if (!std::isfinite(decoded) || decoded < dimension.low() || decoded > dimension.high()) {
        throw TypedHpoError<std::logic_error>(
            "hpo_invariant", {}, "real grid decoder escaped dimension bounds: " + dimension.name());
    }
    return decoded;
}

void validate_real_grid_injective(const RealDimension& dimension, std::uint64_t count) {
    constexpr std::uint64_t kMaxExactlyRepresentableOrdinals = std::uint64_t{1} << 53U;
    if (count > kMaxExactlyRepresentableOrdinals) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "search_space"}},
            "stepped real dimension has more than 2^53 exactly indexable values: " +
                dimension.name());
    }
    if (count <= 1) {
        return;
    }

    constexpr std::uint64_t kExactValidationLimit = 1'000'000;
    if (count <= kExactValidationLimit) {
        double previous = real_at(dimension, 0, count);
        for (std::uint64_t index = 1; index < count; ++index) {
            const double current = real_at(dimension, index, count);
            // serialize_parameter_value uses max_digits10, so distinct finite
            // binary64 values necessarily have distinct round-trippable ABI text.
            if (!(current > previous)) {
                throw TypedHpoError<std::invalid_argument>(
                    "hpo_study_spec_invalid", {{"reason", "search_space"}},
                    "stepped real dimension has grid points that collapse to the same ABI value: " +
                        dimension.name());
            }
            previous = current;
        }
        return;
    }

    // For very large one-dimensional grids, avoid an O(count) validation.
    // A step wider than the largest endpoint ULP proves that rounding remains
    // strictly increasing throughout the interval, because binary64 spacing
    // is monotone with absolute magnitude.
    const double first = real_at(dimension, 0, count);
    const double last = real_at(dimension, count - 1, count);
    const double edge = std::abs(first) >= std::abs(last) ? first : last;
    const double next_up = detail::math::nextafter(edge, std::numeric_limits<double>::infinity());
    const double next_down =
        detail::math::nextafter(edge, -std::numeric_limits<double>::infinity());
    const double gap_up =
        std::isfinite(next_up) ? static_cast<double>(next_up) - static_cast<double>(edge)
                               : 0.0;
    const double gap_down = std::isfinite(next_down) ? static_cast<double>(edge) -
                                                                static_cast<double>(next_down)
                                                          : 0.0;
    const double largest_gap = std::max(std::abs(gap_up), std::abs(gap_down));
    if (static_cast<double>(*dimension.step()) <= largest_gap) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "search_space"}},
            "stepped real grid is too large to prove unique binary64 ABI values; use integer "
            "ticks or a larger step: " +
                dimension.name());
    }
}

std::optional<std::uint64_t> dimension_cardinality(const Dimension& dimension) {
    return std::visit(
        [](const auto& item) -> std::optional<std::uint64_t> {
            using T = std::decay_t<decltype(item)>;
            if constexpr (std::is_same_v<T, IntegerDimension>) {
                return integer_count(item);
            } else if constexpr (std::is_same_v<T, RealDimension>) {
                if (item.low() == item.high()) {
                    return 1;
                }
                if (!item.step().has_value()) {
                    return std::nullopt;
                }
                return real_grid_count(item);
            } else if constexpr (std::is_same_v<T, BooleanDimension>) {
                return 2;
            } else {
                return static_cast<std::uint64_t>(item.choices().size());
            }
        },
        dimension);
}

ParameterValue dimension_value_at(const Dimension& dimension,
                                  std::uint64_t index,
                                  std::uint64_t count) {
    return std::visit(
        [&](const auto& item) -> ParameterValue {
            using T = std::decay_t<decltype(item)>;
            if constexpr (std::is_same_v<T, IntegerDimension>) {
                return integer_at(item, index);
            } else if constexpr (std::is_same_v<T, RealDimension>) {
                if (item.low() == item.high()) {
                    return item.low();
                }
                return real_at(item, index, count);
            } else if constexpr (std::is_same_v<T, BooleanDimension>) {
                return index != 0;
            } else {
                return item.choices().at(static_cast<std::size_t>(index));
            }
        },
        dimension);
}

std::uint64_t dimension_ordinal(const Dimension& dimension,
                                const ParameterValue& value,
                                std::uint64_t count) {
    return std::visit(
        [&](const auto& item) -> std::uint64_t {
            using T = std::decay_t<decltype(item)>;
            if constexpr (std::is_same_v<T, IntegerDimension>) {
                const auto integer = std::get<std::int64_t>(value);
                const std::uint64_t offset =
                    static_cast<std::uint64_t>(integer) - static_cast<std::uint64_t>(item.low());
                return offset / static_cast<std::uint64_t>(item.step());
            } else if constexpr (std::is_same_v<T, RealDimension>) {
                const double real = std::get<double>(value);
                if (item.low() == item.high()) {
                    if (serialize_parameter_value(real) != serialize_parameter_value(item.low())) {
                        throw TypedHpoError<std::invalid_argument>(
                            "hpo_study_spec_invalid", {{"reason", "search_space"}},
                            "real parameter is not the canonical fixed "
                            "value: " +
                                item.name());
                    }
                    return 0;
                }
                const double rounded = detail::pair_round(
                    detail::grid_coordinate(real, item.low(), *item.step()));
                if (!std::isfinite(rounded) || rounded < 0.0 ||
                    rounded >= static_cast<double>(count)) {
                    throw TypedHpoError<std::invalid_argument>(
                        "hpo_study_spec_invalid", {{"reason", "search_space"}},
                        "real parameter is outside its finite grid: " + item.name());
                }
                const auto ordinal = static_cast<std::uint64_t>(rounded);
                const double canonical = real_at(item, ordinal, count);
                if (serialize_parameter_value(real) != serialize_parameter_value(canonical)) {
                    throw TypedHpoError<std::invalid_argument>(
                        "hpo_study_spec_invalid", {{"reason", "search_space"}},
                        "real parameter is not a canonical grid value: " + item.name());
                }
                return ordinal;
            } else if constexpr (std::is_same_v<T, BooleanDimension>) {
                return std::get<bool>(value) ? 1 : 0;
            } else {
                const auto found = std::find(item.choices().begin(), item.choices().end(), value);
                if (found == item.choices().end()) {
                    throw TypedHpoError<std::invalid_argument>(
                        "hpo_study_spec_invalid", {{"reason", "search_space"}},
                        "categorical parameter is not a declared choice: " + item.name());
                }
                return static_cast<std::uint64_t>(found - item.choices().begin());
            }
        },
        dimension);
}

}  // namespace

IntegerDimension::IntegerDimension(
    std::string name, std::int64_t low, std::int64_t high, std::int64_t step, bool log)
    : name_(std::move(name)), low_(low), high_(high), step_(step), log_(log) {
    require_name(name_);
    if (low_ > high_) {
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "search_space"}},
                                                   "integer dimension low must not exceed high");
    }
    if (step_ <= 0) {
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "search_space"}},
                                                   "integer dimension step must be positive");
    }
    if (log_ && (low_ <= 0 || high_ <= 0)) {
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "search_space"}},
                                                   "log integer dimension bounds must be positive");
    }
    if (log_ && step_ != 1) {
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "search_space"}},
                                                   "log integer dimension requires step = 1");
    }
}

bool IntegerDimension::contains(const ParameterValue& value) const noexcept {
    const auto* integer = std::get_if<std::int64_t>(&value);
    if (integer == nullptr || *integer < low_ || *integer > high_) {
        return false;
    }
    const std::uint64_t delta =
        static_cast<std::uint64_t>(*integer) - static_cast<std::uint64_t>(low_);
    return delta % static_cast<std::uint64_t>(step_) == 0;
}

RealDimension::RealDimension(
    std::string name, double low, double high, std::optional<double> step, bool log)
    : name_(std::move(name)), low_(low), high_(high), step_(step), log_(log) {
    require_name(name_);
    if (!std::isfinite(low_) || !std::isfinite(high_)) {
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "search_space"}},
                                                   "real dimension bounds must be finite");
    }
    if (low_ > high_) {
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "search_space"}},
                                                   "real dimension low must not exceed high");
    }
    if (step_.has_value() && (!std::isfinite(*step_) || *step_ <= 0.0)) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "search_space"}},
            "real dimension step must be finite and positive");
    }
    if (step_.has_value() && low_ < high_ && low_ + *step_ == low_) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "search_space"}},
            "real dimension step is too small to produce a distinct double value");
    }
    if (log_ && (low_ <= 0.0 || high_ <= 0.0)) {
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "search_space"}},
                                                   "log real dimension bounds must be positive");
    }
    if (log_ && step_.has_value()) {
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "search_space"}},
                                                   "log real dimension does not support a step");
    }
    if (step_.has_value() && low_ < high_) {
        validate_real_grid_injective(*this, real_grid_count(*this));
    }
}

bool RealDimension::contains(const ParameterValue& value) const noexcept {
    const auto* real = std::get_if<double>(&value);
    if (real == nullptr || !std::isfinite(*real)) {
        return false;
    }

    const double scale = std::max({1.0, std::abs(low_), std::abs(high_)});
    const double bounds_tolerance = 16.0 * std::numeric_limits<double>::epsilon() * scale;
    if (*real < low_ - bounds_tolerance || *real > high_ + bounds_tolerance) {
        return false;
    }
    if (!step_.has_value()) {
        return true;
    }

    const auto coordinate = detail::grid_coordinate(*real, low_, *step_);
    const double index = coordinate.high + coordinate.low;
    if (!std::isfinite(index)) {
        return false;
    }
    const double nearest = detail::pair_round(coordinate);
    const double step_tolerance =
        64.0 * std::numeric_limits<double>::epsilon() * std::max(1.0, std::abs(index));
    return std::abs(index - nearest) <= step_tolerance;
}

BooleanDimension::BooleanDimension(std::string name) : name_(std::move(name)) {
    require_name(name_);
}

bool BooleanDimension::contains(const ParameterValue& value) const noexcept {
    return std::holds_alternative<bool>(value);
}

CategoricalDimension::CategoricalDimension(std::string name, std::vector<ParameterValue> choices)
    : name_(std::move(name)), choices_(std::move(choices)) {
    require_name(name_);
    if (choices_.empty()) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "search_space"}},
            "categorical dimension must have at least one choice");
    }
    std::set<std::string> abi_values;
    for (std::size_t i = 0; i < choices_.size(); ++i) {
        if (!finite_parameter_value(choices_[i])) {
            throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                       {{"reason", "search_space"}},
                                                       "categorical choices must be finite");
        }
        for (std::size_t j = 0; j < i; ++j) {
            if (same_parameter_value(choices_[i], choices_[j])) {
                throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                           {{"reason", "search_space"}},
                                                           "categorical choices must be unique");
            }
        }
        if (!abi_values.insert(serialize_parameter_value(choices_[i])).second) {
            throw TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "search_space"}},
                "categorical choices must be unique after strategy-ABI serialization");
        }
    }
}

bool CategoricalDimension::contains(const ParameterValue& value) const noexcept {
    return std::any_of(choices_.begin(), choices_.end(), [&](const ParameterValue& choice) {
        return same_parameter_value(choice, value);
    });
}

DimensionKind dimension_kind(const Dimension& dimension) noexcept {
    return std::visit(
        [](const auto& item) {
            using T = std::decay_t<decltype(item)>;
            if constexpr (std::is_same_v<T, IntegerDimension>) {
                return DimensionKind::Integer;
            } else if constexpr (std::is_same_v<T, RealDimension>) {
                return DimensionKind::Real;
            } else if constexpr (std::is_same_v<T, BooleanDimension>) {
                return DimensionKind::Boolean;
            } else {
                return DimensionKind::Categorical;
            }
        },
        dimension);
}

std::string_view dimension_name(const Dimension& dimension) noexcept {
    return std::visit([](const auto& item) -> std::string_view { return item.name(); }, dimension);
}

bool dimension_contains(const Dimension& dimension, const ParameterValue& value) noexcept {
    return std::visit([&](const auto& item) { return item.contains(value); }, dimension);
}

SearchSpace::SearchSpace(std::vector<Dimension> dimensions) {
    dimensions_.reserve(dimensions.size());
    for (auto& dimension : dimensions) {
        add(std::move(dimension));
    }
}

void SearchSpace::add(Dimension dimension) {
    const std::string_view name = dimension_name(dimension);
    if (find(name) != nullptr) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "search_space"}},
            "duplicate dimension name: " + std::string(name));
    }
    dimensions_.push_back(std::move(dimension));
}

const Dimension* SearchSpace::find(std::string_view name) const noexcept {
    for (const auto& dimension : dimensions_) {
        if (dimension_name(dimension) == name) {
            return &dimension;
        }
    }
    return nullptr;
}

std::vector<ValidationIssue> SearchSpace::validate(const Candidate& candidate,
                                                   bool reject_unknown) const {
    std::vector<ValidationIssue> issues;
    for (const auto& dimension : dimensions_) {
        const std::string name(dimension_name(dimension));
        const ParameterValue* value = candidate.find(name);
        if (value == nullptr) {
            issues.push_back({name, "missing required parameter"});
            continue;
        }
        if (!dimension_contains(dimension, *value)) {
            std::ostringstream message;
            message << "expected " << expected_type_name(dimension_kind(dimension)) << ", got "
                    << parameter_type_name(parameter_type(*value));
            issues.push_back({name, message.str()});
        }
    }

    if (reject_unknown) {
        for (const auto& [name, value] : candidate.values) {
            (void)value;
            if (find(name) == nullptr) {
                issues.push_back({name, "unknown parameter"});
            }
        }
    }
    return issues;
}

bool SearchSpace::is_valid(const Candidate& candidate, bool reject_unknown) const {
    return validate(candidate, reject_unknown).empty();
}

std::map<std::string, std::string> SearchSpace::serialize_candidate(const Candidate& candidate,
                                                                    bool reject_unknown) const {
    const auto issues = validate(candidate, reject_unknown);
    if (!issues.empty()) {
        std::ostringstream message;
        message << "candidate validation failed";
        for (const auto& issue : issues) {
            message << "; " << issue.parameter << ": " << issue.message;
        }
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "search_space"}}, message.str());
    }

    std::map<std::string, std::string> serialized;
    for (const auto& [name, value] : candidate.values) {
        serialized.emplace(name, serialize_parameter_value(value));
    }
    return serialized;
}

std::optional<std::uint64_t> SearchSpace::finite_cardinality() const {
    std::uint64_t total = 1;
    for (const auto& dimension : dimensions_) {
        const auto count = dimension_cardinality(dimension);
        if (!count.has_value()) {
            return std::nullopt;
        }
        if (total > std::numeric_limits<std::uint64_t>::max() / *count) {
            throw TypedHpoError<std::overflow_error>(
                "hpo_study_spec_invalid", {{"reason", "search_space"}},
                "finite search-space cardinality exceeds uint64_t");
        }
        total *= *count;
    }
    return total;
}

Candidate SearchSpace::candidate_at(std::uint64_t ordinal, std::uint64_t id) const {
    const auto total = finite_cardinality();
    if (!total.has_value()) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "search_space"}},
            "finite search-space indexing requires a step on every varying real dimension");
    }
    if (ordinal >= *total) {
        throw TypedHpoError<std::out_of_range>("hpo_invariant", {},
                                               "finite search-space ordinal is out of range");
    }

    Candidate candidate;
    candidate.id = id;
    std::uint64_t remaining = ordinal;
    for (std::size_t position = dimensions_.size(); position > 0; --position) {
        const auto& dimension = dimensions_[position - 1];
        const std::uint64_t count = *dimension_cardinality(dimension);
        const std::uint64_t index = remaining % count;
        remaining /= count;
        candidate.values.emplace(std::string(dimension_name(dimension)),
                                 dimension_value_at(dimension, index, count));
    }
    return candidate;
}

std::uint64_t SearchSpace::candidate_ordinal(const Candidate& candidate) const {
    const auto total = finite_cardinality();
    if (!total.has_value()) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "search_space"}},
            "finite search-space indexing requires a step on every varying real dimension");
    }

    const auto issues = validate(candidate);
    if (!issues.empty()) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "search_space"}},
            "cannot index an invalid search-space candidate");
    }

    std::uint64_t ordinal = 0;
    for (const auto& dimension : dimensions_) {
        const std::uint64_t count = *dimension_cardinality(dimension);
        const std::string name(dimension_name(dimension));
        const std::uint64_t index = dimension_ordinal(dimension, *candidate.find(name), count);
        ordinal = ordinal * count + index;
    }
    if (ordinal >= *total) {
        throw TypedHpoError<std::logic_error>(
            "hpo_invariant", {}, "finite search-space encoder produced an out-of-range ordinal");
    }
    return ordinal;
}

}  // namespace pineforge::hpo
