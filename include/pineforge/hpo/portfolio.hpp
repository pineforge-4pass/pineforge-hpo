#pragma once

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

#include "pineforge/hpo/objective.hpp"

namespace pineforge::hpo {

/// One timestamped point on an account-level equity curve.
struct AccountEquityPoint {
    /// Unix timestamp in milliseconds.
    std::int64_t timestamp_ms = 0;
    /// Total account equity at the timestamp.
    double equity = 0.0;
};

/// Requested capital allocation to one strategy and market sleeve.
struct Allocation {
    /// Stable strategy identifier from the study definition.
    std::string strategy_id;
    /// Stable market or dataset identifier from the study definition.
    std::string market_id;
    /// Portfolio weight assigned to the sleeve.
    double weight = 0.0;
};

/// Compact start/end summary for one independently evaluated portfolio sleeve.
struct SleeveSummary {
    /// Stable strategy identifier.
    std::string strategy_id;
    /// Stable market or dataset identifier.
    std::string market_id;
    /// Portfolio weight used for this sleeve.
    double weight = 0.0;
    /// Sleeve equity at the beginning of the observation window.
    double initial_equity = 0.0;
    /// Sleeve equity at the end of the observation window.
    double final_equity = 0.0;
};

/// @brief Account-level observation supplied to a custom portfolio objective.
///
/// This type is deliberately independent of `pf_report_t`: a portfolio evaluator materializes
/// only the account-level fields requested by its objective. It is an observation contract, not
/// proof of shared cash, margin admission, or cross-strategy order sequencing.
struct PortfolioObservation {
    /// Time-aligned account equity series.
    std::vector<AccountEquityPoint> account_equity;
    /// Account returns aligned to the evaluator's chosen interval.
    std::vector<double> account_returns;
    /// Allocations that produced this observation.
    std::vector<Allocation> allocations;
    /// Per-sleeve start/end summaries.
    std::vector<SleeveSummary> sleeves;
    /// Objective-specific account features keyed by registered name.
    std::unordered_map<std::string, double> features;
    /// Evaluator-defined portfolio turnover over the observation window.
    double turnover = 0.0;
    /// Evaluator-defined amount of capital deployed.
    double capital_used = 0.0;
};

/// Custom objective function specialized for PortfolioObservation.
using PortfolioObjectiveFn = ObjectiveFn<PortfolioObservation>;

}  // namespace pineforge::hpo
