#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <variant>

namespace pineforge::hpo {

/// Scalar parameter value accepted by search spaces and the PineForge strategy ABI.
using ParameterValue = std::variant<std::int64_t, double, bool, std::string>;

/// Runtime discriminator for a ParameterValue alternative.
enum class ParameterType {
    Integer,  ///< Signed 64-bit integer.
    Real,     ///< IEEE-754 binary64 real.
    Boolean,  ///< Boolean value.
    String,   ///< UTF-8 string value.
};

/// Returns the discriminator of @p value.
ParameterType parameter_type(const ParameterValue& value) noexcept;

/// Returns a stable lowercase name for @p type.
const char* parameter_type_name(ParameterType type) noexcept;

/// Produces the exact text passed to `strategy_set_input` or `strategy_set_override`.
///
/// Real values use enough digits for a binary64 round trip. Non-finite real values are
/// rejected because the engine input grammar has no portable representation for them.
///
/// @throws std::invalid_argument if @p value is a non-finite real.
std::string serialize_parameter_value(const ParameterValue& value);

/// Complete parameter assignment proposed by a sampler.
struct Candidate {
    /// Sampler-assigned identifier used to correlate an ask with its result.
    std::uint64_t id = 0;

    /// Parameter values keyed by search-space dimension name.
    std::map<std::string, ParameterValue> values;

    /// Returns the named value, or `nullptr` when the candidate does not contain it.
    ///
    /// The returned pointer remains valid until this candidate's map is mutated or destroyed.
    const ParameterValue* find(const std::string& name) const noexcept;
};

/// Metadata supplied to a custom objective while evaluating one trial.
struct TrialContext {
    /// Scheduler-level trial identifier.
    std::uint64_t trial_id = 0;

    /// Non-owning candidate pointer; may be `nullptr` when no candidate is associated.
    const Candidate* candidate = nullptr;
};

}  // namespace pineforge::hpo
