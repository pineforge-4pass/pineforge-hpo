#pragma once

#include <pineforge/pineforge.h>

#include <array>
#include <cstdint>
#include <limits>
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
    /// Whether ReportSnapshot owns the complete C-ABI trade records (off in the HPO hot loop).
    bool capture_trades = false;
    /// Computes the `returns.bar` statistics from the report's equity curve (see
    /// docs/return-stats.md). Both return-statistics flags false, the default, means no
    /// reduction, no canary and no stored value: the executor behaves as before.
    bool return_stats_bar = false;
    /// Computes the `returns.monthly` statistics; the chart timezone above selects the
    /// calendar (only UTC is defined) and is read from this configuration, never from the
    /// process environment.
    bool return_stats_monthly = false;
    /// Optional instrument metadata, applied after inputs and overrides.
    std::optional<SymbolInfo> symbol_info;
    /// Fixed other-symbol bars loaded once, validated before trials, and shared read-only.
    std::shared_ptr<const SymbolFeeds> symbol_feeds;
};

/// Refuses return statistics in a build whose identity is unbound.
///
/// Return statistics are published only by a build whose identity is bound to the compile
/// commands that produced the reducer (see docs/internal/return-stats-identity.md). When it is
/// not, because of a multi-configuration generator, a compiler launcher, a disabled compilation
/// database, a CMake older than 3.19 and the like, this throws `hpo_toolchain_unavailable`
/// (reason `native_runner`) whose text names the exact unbound reason. Every ordinary run, which
/// requests no return statistics, never calls it and is unaffected. TrialExecutor calls it when a
/// series is requested; the command line calls it earlier, before any plugin or dataset is loaded.
void require_return_stats_identity();

/// Published fields of one return-statistics series, in wire order: count, skipped,
/// periods_per_year, mean, std, sharpe_per_period, skew, kurt_raw, status.
///
/// Every entry is NaN, which the metric layer publishes as null, until the series is computed.
/// The values are the only thing kept: the equity curve they came from is not copied.
struct ReturnStatsFields {
    /// True when the series was requested and computed from a report's equity curve.
    bool computed = false;
    /// Field values in wire order; NaN stands for null.
    std::array<double, 9> values{{
        std::numeric_limits<double>::quiet_NaN(), std::numeric_limits<double>::quiet_NaN(),
        std::numeric_limits<double>::quiet_NaN(), std::numeric_limits<double>::quiet_NaN(),
        std::numeric_limits<double>::quiet_NaN(), std::numeric_limits<double>::quiet_NaN(),
        std::numeric_limits<double>::quiet_NaN(), std::numeric_limits<double>::quiet_NaN(),
        std::numeric_limits<double>::quiet_NaN()}};
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
    /// Optional owned trade records controlled by BacktestConfiguration::capture_trades.
    std::vector<pf_trade_t> trades;
    /// Bar-return statistics, computed before the report was released when requested.
    ReturnStatsFields return_bar;
    /// Monthly-return statistics, computed before the report was released when requested.
    ReturnStatsFields return_monthly;

    /// Resolves a canonical objective path without allocating a string map.
    ///
    /// Public fields under `metrics.all`, `metrics.longs`, `metrics.shorts`, and
    /// `metrics.equity` are supported, along with selected `report.*` counters. Integer metrics
    /// are converted losslessly to double for objective arithmetic. Unknown paths return
    /// `std::nullopt`. `metrics.equity.sharpe_tv` and `metrics.equity.sortino_tv`, the
    /// pre-1.0 engine names of `sharpe_monthly` and `sortino_monthly`, resolve as aliases.
    /// The eighteen names `returns.{bar,monthly}.{count,skipped,periods_per_year,mean,std,
    /// sharpe_per_period,skew,kurt_raw,status}` always resolve; a series that was not computed
    /// (not requested, or no report) reads NaN, which is published as null.
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
    /// Engine-owned failure code; absent for plugins without the optional getter.
    std::optional<std::string> error_code;
    /// Raw engine argument JSON; consumers must validate it before serialization.
    std::optional<std::string> error_args;
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

    /// Runs the exact full-backtest engine path on an immutable bar prefix.
    /// The prefix must contain between one and dataset().size() bars.
    TrialExecutionResult execute_prefix(const ParameterValues& inputs,
                                        const ParameterValues& strategy_overrides,
                                        std::size_t bar_count) const;

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
