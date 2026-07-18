#pragma once

#include <cstddef>
#include <functional>
#include <memory>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

#include "pineforge/hpo/types.hpp"

namespace pineforge::hpo {

/// Metric values keyed by canonical objective path.
using MetricMap = std::unordered_map<std::string, double>;

/// Behavior when an objective expression divides by zero.
enum class DivisionByZeroPolicy {
    Reject,   ///< Return an invalid ExpressionEvaluation.
    Ieee754,  ///< Permit IEEE-754 infinity or NaN, subject to the result policy.
};

/// Behavior when a metric or expression result is not finite.
enum class NonFinitePolicy {
    Reject,  ///< Reject the non-finite value.
    Allow,   ///< Preserve the non-finite value for the caller.
};

/// Policies controlling exceptional arithmetic during metric-expression evaluation.
struct EvaluationPolicy {
    /// Division-by-zero behavior.
    DivisionByZeroPolicy division_by_zero = DivisionByZeroPolicy::Reject;
    /// Behavior for a referenced non-finite metric.
    NonFinitePolicy non_finite_metric = NonFinitePolicy::Reject;
    /// Behavior for a non-finite final expression value.
    NonFinitePolicy non_finite_result = NonFinitePolicy::Reject;
};

/// Stable reason code for an invalid ExpressionEvaluation.
enum class EvaluationError {
    None,             ///< Evaluation succeeded.
    MissingMetric,    ///< A referenced identifier was absent from the metric map.
    NonFiniteMetric,  ///< A referenced metric violated the non-finite metric policy.
    DivisionByZero,   ///< Division by zero was rejected.
    NonFiniteResult,  ///< Evaluation failed or produced a disallowed non-finite result.
};

/// Non-throwing result envelope returned by MetricExpression::evaluate().
struct ExpressionEvaluation {
    /// True only when value is usable under the selected policy.
    bool valid = false;
    /// Evaluated scalar; meaningful only when valid is true.
    double value = 0.0;
    /// Machine-readable failure reason, or EvaluationError::None.
    EvaluationError error = EvaluationError::None;
    /// Human-readable failure detail, empty after successful evaluation.
    std::string diagnostic;
};

/// Parse error raised while compiling a metric expression.
class ExpressionError : public std::runtime_error {
public:
    /// Constructs an error whose `what()` text includes the byte offset.
    ExpressionError(std::string message, std::size_t offset);

    /// Returns the zero-based byte offset in the original expression.
    std::size_t offset() const noexcept { return offset_; }

private:
    std::size_t offset_;
};

namespace detail {
struct ExpressionNode;
}  // namespace detail

/// @brief Parsed metric expression that is compiled once and evaluated repeatedly.
///
/// Supported syntax includes identifiers, finite numeric literals, `+`, `-`, `*`, `/`, unary
/// signs, parentheses, `min(a,b)`, `max(a,b)`, `abs(a)`, and comparisons. Comparisons yield
/// `1.0` when true and `0.0` when false.
class MetricExpression {
public:
    /// Parses and compiles @p source.
    /// @throws ExpressionError when the source is empty or syntactically invalid.
    explicit MetricExpression(std::string source);
    ~MetricExpression();

    MetricExpression(MetricExpression&&) noexcept;
    MetricExpression& operator=(MetricExpression&&) noexcept;
    MetricExpression(const MetricExpression&) = delete;
    MetricExpression& operator=(const MetricExpression&) = delete;

    /// Evaluates the compiled expression without throwing.
    ///
    /// Missing metrics and arithmetic-policy failures are returned in the result envelope.
    ExpressionEvaluation evaluate(const MetricMap& metrics,
                                  const EvaluationPolicy& policy = {}) const noexcept;
    /// Returns the original expression text.
    const std::string& source() const noexcept { return source_; }
    /// Returns unique metric identifiers in first-appearance order.
    const std::vector<std::string>& identifiers() const noexcept { return identifiers_; }

private:
    std::string source_;
    std::unique_ptr<detail::ExpressionNode> root_;
    std::vector<std::string> identifiers_;
};

/// Comparison relation applied by Constraint.
enum class ConstraintRelation {
    LessEqual,     ///< Require `lhs <= rhs + tolerance`.
    GreaterEqual,  ///< Require `lhs >= rhs - tolerance`.
    Equal,         ///< Require `abs(lhs - rhs) <= tolerance`.
};

/// Named scalar feasibility constraint with an absolute non-negative tolerance.
struct Constraint {
    /// User-facing constraint identifier.
    std::string name;
    /// Evaluated left-hand side.
    double lhs = 0.0;
    /// Required comparison relation.
    ConstraintRelation relation = ConstraintRelation::LessEqual;
    /// Configured right-hand side.
    double rhs = 0.0;
    /// Absolute tolerance; negative values are treated as zero.
    double tolerance = 0.0;

    /// Returns whether all operands are finite and the relation holds.
    bool satisfied() const noexcept;

    /// Returns zero when satisfied and the positive violation magnitude otherwise.
    ///
    /// Non-finite input returns positive infinity so an optimizer cannot treat it as feasible.
    double violation() const noexcept;
};

/// Objective values, constraints, and diagnostics produced for one observation.
struct ObjectiveResult {
    /// Objective vector; the current native samplers consume one value.
    std::vector<double> values;
    /// Feasibility constraints evaluated alongside the objective.
    std::vector<Constraint> constraints;
    /// False when the objective adapter could not produce a comparable result.
    bool valid = true;
    /// Human-readable invalid-result detail.
    std::string diagnostic;

    /// Returns true when valid, non-empty, finite-valued, and all constraints pass.
    bool feasible() const noexcept;

    /// Constructs an invalid result carrying @p diagnostic and no objective values.
    static ObjectiveResult invalid(std::string diagnostic);
};

/// Type-erased custom objective accepting an observation and immutable trial context.
template <typename Observation>
using ObjectiveFn = std::function<ObjectiveResult(const Observation&, const TrialContext&)>;

}  // namespace pineforge::hpo
