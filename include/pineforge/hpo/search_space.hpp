#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <variant>
#include <vector>

#include "pineforge/hpo/types.hpp"

namespace pineforge::hpo {

/// Concrete dimension category stored by Dimension.
enum class DimensionKind {
    Integer,      ///< Discrete signed-integer lattice.
    Real,         ///< Continuous or stepped binary64 interval.
    Boolean,      ///< The two values `false` and `true`.
    Categorical,  ///< Explicit ordered set of scalar choices.
};

/// Named signed-integer dimension with inclusive bounds and a positive step.
class IntegerDimension {
public:
    /// Constructs an integer dimension.
    ///
    /// Log dimensions require positive bounds and `step == 1`. Adaptive and random
    /// samplers use the expanded latent interval `[ln(low - 0.5), ln(high + 0.5)]`;
    /// finite grid enumeration remains literal.
    ///
    /// @throws std::invalid_argument for an empty name, reversed bounds, a non-positive
    /// step, or an invalid log configuration.
    IntegerDimension(std::string name,
                     std::int64_t low,
                     std::int64_t high,
                     std::int64_t step = 1,
                     bool log = false);

    /// Returns the unique dimension name.
    const std::string& name() const noexcept { return name_; }
    /// Returns the inclusive lower bound.
    std::int64_t low() const noexcept { return low_; }
    /// Returns the inclusive upper bound.
    std::int64_t high() const noexcept { return high_; }
    /// Returns the positive lattice step.
    std::int64_t step() const noexcept { return step_; }
    /// Returns whether adaptive samplers operate in logarithmic latent coordinates.
    bool log() const noexcept { return log_; }
    /// Tests both the scalar type and membership in this dimension's integer lattice.
    bool contains(const ParameterValue& value) const noexcept;

private:
    std::string name_;
    std::int64_t low_;
    std::int64_t high_;
    std::int64_t step_;
    bool log_;
};

/// Named binary64 dimension with inclusive bounds and an optional finite-grid step.
class RealDimension {
public:
    /// Constructs a real dimension.
    ///
    /// Log dimensions require positive bounds and no step. Values remain in original units
    /// at the public Candidate and strategy-ABI boundaries.
    ///
    /// @throws std::invalid_argument for an empty name, non-finite or reversed bounds, a
    /// non-positive/non-finite step, or an invalid log configuration.
    RealDimension(std::string name,
                  double low,
                  double high,
                  std::optional<double> step = std::nullopt,
                  bool log = false);

    /// Returns the unique dimension name.
    const std::string& name() const noexcept { return name_; }
    /// Returns the inclusive lower bound.
    double low() const noexcept { return low_; }
    /// Returns the inclusive upper bound.
    double high() const noexcept { return high_; }
    /// Returns the finite-grid step, or `std::nullopt` for a continuous dimension.
    const std::optional<double>& step() const noexcept { return step_; }
    /// Returns whether adaptive samplers operate in logarithmic latent coordinates.
    bool log() const noexcept { return log_; }
    /// Tests both the scalar type and interval/lattice membership.
    bool contains(const ParameterValue& value) const noexcept;

private:
    std::string name_;
    double low_;
    double high_;
    std::optional<double> step_;
    bool log_;
};

/// Named dimension whose legal values are exactly `false` and `true`.
class BooleanDimension {
public:
    /// @throws std::invalid_argument if @p name is empty.
    explicit BooleanDimension(std::string name);

    /// Returns the unique dimension name.
    const std::string& name() const noexcept { return name_; }
    /// Returns true only when @p value contains a Boolean alternative.
    bool contains(const ParameterValue& value) const noexcept;

private:
    std::string name_;
};

/// Named dimension backed by an ordered, non-empty set of scalar choices.
class CategoricalDimension {
public:
    /// Constructs a categorical dimension.
    ///
    /// @throws std::invalid_argument for an empty name, no choices, duplicate choices,
    /// non-finite real choices, or choices that serialize to the same strategy-ABI text.
    CategoricalDimension(std::string name, std::vector<ParameterValue> choices);

    /// Returns the unique dimension name.
    const std::string& name() const noexcept { return name_; }
    /// Returns choices in their declared finite-grid order.
    const std::vector<ParameterValue>& choices() const noexcept { return choices_; }
    /// Tests exact type-and-value membership in the declared choices.
    bool contains(const ParameterValue& value) const noexcept;

private:
    std::string name_;
    std::vector<ParameterValue> choices_;
};

/// Closed variant over every supported search-space dimension type.
using Dimension =
    std::variant<IntegerDimension, RealDimension, BooleanDimension, CategoricalDimension>;

/// Returns the concrete category stored in @p dimension.
DimensionKind dimension_kind(const Dimension& dimension) noexcept;
/// Returns the name of the concrete @p dimension.
std::string_view dimension_name(const Dimension& dimension) noexcept;
/// Dispatches a type-aware membership test to the concrete @p dimension.
bool dimension_contains(const Dimension& dimension, const ParameterValue& value) noexcept;

/// One candidate-validation failure tied to a parameter name.
struct ValidationIssue {
    /// Dimension name, or the offending unknown candidate key.
    std::string parameter;
    /// Human-readable validation failure.
    std::string message;
};

/// @brief Ordered product of named dimensions with validation and finite-space codecs.
///
/// Dimension names are unique. Declaration order is significant for mixed-radix finite
/// enumeration: the last declared dimension varies fastest.
class SearchSpace {
public:
    /// Constructs an empty search space whose finite cardinality is one.
    SearchSpace() = default;

    /// Constructs a search space in declaration order.
    /// @throws std::invalid_argument if dimension names are duplicated.
    explicit SearchSpace(std::vector<Dimension> dimensions);

    /// Appends a dimension while preserving declaration order.
    /// @throws std::invalid_argument if its name is already present.
    void add(Dimension dimension);
    /// Returns all dimensions in declaration order.
    const std::vector<Dimension>& dimensions() const noexcept { return dimensions_; }
    /// Returns the named dimension, or `nullptr` when it is not declared.
    const Dimension* find(std::string_view name) const noexcept;

    /// Returns every missing, mistyped, off-grid, or optionally unknown parameter issue.
    std::vector<ValidationIssue> validate(const Candidate& candidate,
                                          bool reject_unknown = true) const;
    /// Returns whether validate() would produce no issues for the same arguments.
    bool is_valid(const Candidate& candidate, bool reject_unknown = true) const;

    /// Validates and serializes a candidate for the PineForge strategy ABI.
    ///
    /// @throws std::invalid_argument containing all validation failures when the candidate
    /// is invalid.
    std::map<std::string, std::string> serialize_candidate(const Candidate& candidate,
                                                           bool reject_unknown = true) const;

    /// Returns the exact Cartesian-product cardinality when every dimension is finite.
    ///
    /// A varying real dimension without a step returns `std::nullopt`. Fixed real dimensions
    /// have cardinality one without requiring a step.
    ///
    /// @throws std::overflow_error if a dimension or product exceeds `std::uint64_t`.
    /// @throws std::invalid_argument if a stepped-real grid cannot map injectively to ABI
    /// values.
    std::optional<std::uint64_t> finite_cardinality() const;

    /// Decodes a mixed-radix ordinal into its canonical finite-space candidate.
    ///
    /// The last declared dimension varies fastest, matching GridSampler enumeration order.
    /// @throws std::invalid_argument if the space is continuous.
    /// @throws std::out_of_range if @p ordinal is outside the finite space.
    Candidate candidate_at(std::uint64_t ordinal, std::uint64_t id = 0) const;

    /// Encodes a canonical finite-space candidate into its mixed-radix ordinal.
    ///
    /// Only canonical grid values are accepted, preserving a one-to-one mapping between
    /// ordinals and strategy-ABI parameter vectors.
    /// @throws std::invalid_argument if the space is continuous or the candidate is not a
    /// complete canonical finite-grid assignment.
    std::uint64_t candidate_ordinal(const Candidate& candidate) const;

private:
    std::vector<Dimension> dimensions_;
};

}  // namespace pineforge::hpo
