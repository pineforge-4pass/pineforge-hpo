#pragma once

#include <pineforge/pineforge.h>

#include <filesystem>
#include <optional>
#include <string>

namespace pineforge {
namespace hpo {

/// Optional instrument metadata applied after trial inputs and runtime overrides.
struct SymbolInfo {
    /// Positive instrument price tick size.
    std::optional<double> mintick;
    /// Positive money-per-price-point multiplier.
    std::optional<double> pointvalue;
    /// Exchange timezone, distinct from the chart timezone; empty means unchanged.
    std::string timezone;
    /// Trading session; empty means unchanged.
    std::string session;
};

/// @brief RAII wrapper around one compiled PineForge strategy plugin.
///
/// The dynamic library is loaded and ABI-validated once. It may serve concurrent calls made with
/// distinct strategy handles, but each `pf_strategy_t` must remain confined to one trial. Reports
/// and strategy handles must be released through this wrapper before the plugin is destroyed.
class StrategyPlugin final {
public:
    /// Loads @p path and resolves the required PineForge strategy symbols.
    /// @throws std::invalid_argument when the path is empty.
    /// @throws std::runtime_error when loading, symbol resolution, or ABI validation fails.
    explicit StrategyPlugin(std::filesystem::path path);
    ~StrategyPlugin();

    StrategyPlugin(const StrategyPlugin&) = delete;
    StrategyPlugin& operator=(const StrategyPlugin&) = delete;
    StrategyPlugin(StrategyPlugin&& other) noexcept;
    StrategyPlugin& operator=(StrategyPlugin&& other) noexcept;

    /// Returns the dynamic-library path supplied at construction.
    const std::filesystem::path& path() const noexcept { return path_; }
    /// Returns the ABI version reported by the validated plugin.
    int abi_version() const noexcept { return abi_version_value_; }

    /// Creates a fresh strategy handle owned by the caller.
    /// @throws std::runtime_error when the plugin returns a null handle.
    pf_strategy_t create_strategy() const;

    /// Releases a handle returned by create_strategy(); safe to call during cleanup.
    void free_strategy(pf_strategy_t strategy) const noexcept;

    /// Applies one serialized Pine input before the backtest starts.
    /// @throws std::invalid_argument for a null handle or an embedded NUL in key/value.
    void set_input(pf_strategy_t strategy, const std::string& key, const std::string& value) const;

    /// Applies one serialized runtime strategy override before the backtest starts.
    /// @throws std::invalid_argument for a null handle or an embedded NUL in key/value.
    void set_override(pf_strategy_t strategy,
                      const std::string& key,
                      const std::string& value) const;

    /// Sets the chart timezone when non-empty and supported by the plugin.
    ///
    /// An empty timezone is a no-op. A non-empty value requires the optional
    /// `strategy_set_chart_timezone` symbol.
    /// @throws std::invalid_argument for a null handle or embedded NUL.
    /// @throws std::runtime_error when the loaded plugin lacks timezone support.
    void set_chart_timezone(pf_strategy_t strategy, const std::string& timezone) const;

    /// Applies mintick, pointvalue, exchange timezone, and session in harness order.
    /// @throws std::runtime_error when a requested optional setter is unavailable.
    /// @throws std::invalid_argument for a null handle, non-positive numbers, or embedded NUL.
    void set_symbol_info(pf_strategy_t strategy, const SymbolInfo& info) const;

    /// Invokes the plugin's full backtest entry point using immutable OHLCV input.
    ///
    /// The report remains caller-owned and must later be released with free_report().
    /// @throws std::invalid_argument for invalid pointers/counts or embedded-NUL timeframes.
    void run_backtest_full(pf_strategy_t strategy,
                           const pf_bar_t* bars,
                           int bar_count,
                           const std::string& input_timeframe,
                           const std::string& script_timeframe,
                           bool bar_magnifier,
                           int magnifier_samples,
                           pf_magnifier_distribution_t magnifier_distribution,
                           pf_report_t* report) const;
    /// Returns the plugin's last error for @p strategy, or an empty string when none exists.
    /// @throws std::invalid_argument for a null handle.
    std::string last_error(pf_strategy_t strategy) const;

    /// Releases report-owned allocations populated by run_backtest_full().
    void free_report(pf_report_t* report) const noexcept;

private:
    using StrategyCreateFn = pf_strategy_t (*)(const char*);
    using StrategyFreeFn = void (*)(pf_strategy_t);
    using StrategySetInputFn = void (*)(pf_strategy_t, const char*, const char*);
    using StrategySetOverrideFn = void (*)(pf_strategy_t, const char*, const char*);
    using StrategySetChartTimezoneFn = void (*)(pf_strategy_t, const char*);
    using StrategySetSymbolDoubleFn = void (*)(pf_strategy_t, double);
    using StrategySetSymbolStringFn = void (*)(pf_strategy_t, const char*);
    using RunBacktestFullFn = void (*)(pf_strategy_t,
                                       pf_bar_t*,
                                       int,
                                       const char*,
                                       const char*,
                                       int,
                                       int,
                                       pf_magnifier_distribution_t,
                                       pf_report_t*);
    using StrategyGetLastErrorFn = const char* (*)(pf_strategy_t);
    using ReportFreeFn = void (*)(pf_report_t*);
    using AbiVersionFn = int (*)();

    void close() noexcept;

    std::filesystem::path path_;
    void* library_handle_ = nullptr;
    int abi_version_value_ = 0;
    StrategyCreateFn strategy_create_ = nullptr;
    StrategyFreeFn strategy_free_ = nullptr;
    StrategySetInputFn strategy_set_input_ = nullptr;
    StrategySetOverrideFn strategy_set_override_ = nullptr;
    StrategySetChartTimezoneFn strategy_set_chart_timezone_ = nullptr;
    StrategySetSymbolDoubleFn strategy_set_syminfo_mintick_ = nullptr;
    StrategySetSymbolDoubleFn strategy_set_syminfo_pointvalue_ = nullptr;
    StrategySetSymbolStringFn strategy_set_syminfo_timezone_ = nullptr;
    StrategySetSymbolStringFn strategy_set_syminfo_session_ = nullptr;
    RunBacktestFullFn run_backtest_full_ = nullptr;
    StrategyGetLastErrorFn strategy_get_last_error_ = nullptr;
    ReportFreeFn report_free_ = nullptr;
    AbiVersionFn pf_abi_version_ = nullptr;
};

}  // namespace hpo
}  // namespace pineforge
