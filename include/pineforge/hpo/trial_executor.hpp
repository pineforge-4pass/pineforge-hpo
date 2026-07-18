#pragma once

#include <pineforge/pineforge.h>

#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/strategy_plugin.hpp>

namespace pineforge {
namespace hpo {

/// Serialized Pine input or runtime-override values keyed by strategy parameter name.
using ParameterValues = std::map<std::string, std::string>;

/// Immutable engine settings applied to every execution owned by a TrialExecutor.
struct BacktestConfiguration {
    /// Timeframe represented by the input Dataset bars.
    std::string input_timeframe;
    /// Pine strategy execution timeframe; may differ from the input timeframe.
    std::string script_timeframe;
    /// Optional chart timezone applied through the strategy plugin.
    std::string chart_timezone;
    /// Enables PineForge bar-magnifier sampling.
    bool bar_magnifier = false;
    /// Positive number of magnifier samples per sub-bar.
    int magnifier_samples = 4;
    /// Distribution used to place magnifier samples.
    pf_magnifier_distribution_t magnifier_distribution = PF_MAGNIFIER_ENDPOINTS;

    /// Whether ReportSnapshot owns a copy of the report equity curve.
    ///
    /// Scalar-only HPO can disable this copy. The default retains it for portfolio and custom
    /// objective consumers.
    bool capture_equity_curve = true;
};

/// @brief Owning report snapshot detached from strategy and C-ABI report lifetimes.
///
/// When requested, the equity curve supports independent sleeve alignment and approximate
/// portfolio aggregation. It is not a shared-account simulation: completed reports cannot
/// reconstruct shared cash, margin admission, or cross-strategy order ordering.
struct ReportSnapshot {
    /// Number of completed trades.
    std::int32_t total_trades = 0;
    /// Report-level net profit in account currency.
    double net_profit = 0.0;

    /// Number of input-timeframe bars processed by the engine.
    std::int64_t input_bars_processed = 0;
    /// Number of script-timeframe bars processed by the strategy.
    std::int64_t script_bars_processed = 0;
    /// Number of requested secondary security feeds.
    std::int64_t security_feeds_total = 0;
    /// Number of complete secondary-feed observations.
    std::int64_t security_complete_total = 0;
    /// Number of partial secondary-feed observations.
    std::int64_t security_partial_total = 0;
    /// Total magnifier sub-bars processed.
    std::int64_t magnifier_sub_bars_total = 0;
    /// Total synthetic magnifier ticks sampled.
    std::int64_t magnifier_sample_ticks_total = 0;

    /// Input timeframe duration in seconds.
    std::int32_t input_tf_seconds = 0;
    /// Script timeframe duration in seconds.
    std::int32_t script_tf_seconds = 0;
    /// Integer aggregation ratio reported by the engine.
    std::int32_t script_tf_ratio = 0;
    /// Whether the engine aggregated input bars for script execution.
    bool needs_aggregation = false;
    /// Whether bar magnification was active.
    bool bar_magnifier_enabled = false;

    /// Complete detached PineForge metrics structure.
    pf_metrics_t metrics{};
    /// Optional owned copy controlled by BacktestConfiguration::capture_equity_curve.
    std::vector<pf_equity_point_t> equity_curve;

    /// Resolves a canonical objective path without allocating a string map.
    ///
    /// Public fields under `metrics.all`, `metrics.longs`, `metrics.shorts`, and
    /// `metrics.equity` are supported, along with selected `report.*` counters. Integer metrics
    /// are converted losslessly to double for objective arithmetic. Unknown paths return
    /// `std::nullopt`.
    std::optional<double> metric(std::string_view path) const noexcept;
};

/// Terminal status of one native strategy execution.
enum class TrialExecutionStatus : std::uint8_t {
    kSucceeded = 0,   ///< Backtest completed and a detached snapshot is available.
    kEngineError = 1,  ///< Strategy reported an error; no comparable snapshot is available.
};

/// Result envelope returned by TrialExecutor::execute().
struct TrialExecutionResult {
    /// Machine-readable terminal status.
    TrialExecutionStatus status = TrialExecutionStatus::kEngineError;
    /// Strategy error text when status is TrialExecutionStatus::kEngineError.
    std::string error;
    /// Detached report populated when execution succeeds.
    ReportSnapshot report;

    /// Returns whether status is TrialExecutionStatus::kSucceeded.
    bool succeeded() const noexcept { return status == TrialExecutionStatus::kSucceeded; }
};

/// @brief Deterministic one-shot adapter from serialized parameters to a detached report.
///
/// execute() is const and may be called concurrently: every call creates a fresh strategy handle
/// while all calls share only the immutable plugin function table and Dataset. Report-owned
/// resources are released before the strategy handle on every path.
class TrialExecutor final {
public:
    /// Binds a validated plugin, immutable dataset, and execution configuration.
    ///
    /// @throws std::invalid_argument for null dependencies, an ABI-incompatible dataset size,
    /// non-positive magnifier samples, or an unknown magnifier distribution.
    TrialExecutor(std::shared_ptr<const StrategyPlugin> plugin,
                  std::shared_ptr<const Dataset> dataset,
                  BacktestConfiguration configuration = {});

    /// Runs one fresh strategy instance with overrides applied before ordinary inputs.
    ///
    /// Strategy-reported errors are returned as TrialExecutionStatus::kEngineError. Adapter and
    /// plugin contract violations may throw.
    TrialExecutionResult execute(const ParameterValues& inputs,
                                 const ParameterValues& strategy_overrides = {}) const;

    /// Returns the immutable per-execution configuration.
    const BacktestConfiguration& configuration() const noexcept { return configuration_; }
    /// Returns the shared immutable dataset.
    const Dataset& dataset() const noexcept { return *dataset_; }
    /// Returns the loaded strategy plugin.
    const StrategyPlugin& plugin() const noexcept { return *plugin_; }

private:
    std::shared_ptr<const StrategyPlugin> plugin_;
    std::shared_ptr<const Dataset> dataset_;
    BacktestConfiguration configuration_;
};

}  // namespace hpo
}  // namespace pineforge
