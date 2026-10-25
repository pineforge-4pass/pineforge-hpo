#pragma once

#include <pineforge/pineforge.h>
#include <pineforge/execution_observer.h>
#include <pineforge/selected_window.h>
#include <pineforge/hpo/symbol_feeds.hpp>

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>

namespace pineforge {
namespace hpo {

/// Embedded runtime identity admitted for selected-window execution.
struct EngineVersion {
    /// Numeric major release from the plugin's own metadata.
    std::uint32_t major = 0;
    /// Numeric minor release from the plugin's own metadata.
    std::uint32_t minor = 0;
    /// Numeric patch release from the plugin's own metadata.
    std::uint32_t patch = 0;
    /// Complete version, including any git-describe suffix.
    std::string full;
    /// Commit reported by the plugin; empty when the runtime does not know it.
    std::string commit_sha;
};

/// Parses and cross-checks embedded metadata without loading a plugin or consulting build headers.
///
/// Requires stable engine 1.x at least 1.6.2. Parses `X.Y.Z[-rc.N][-D-gSHA][-dirty]`;
/// dirty suffixes are admitted with or without git distance, but release candidates are refused.
/// A supplied manifest version must match the full embedded string exactly.
/// Missing, malformed, unsupported and mismatched identities raise
/// `hpo_walkforward_unavailable` with the corresponding engine reason.
EngineVersion validate_selected_engine_version(
    const std::optional<pf_version_t>& numbers,
    const std::optional<std::string>& full,
    const std::optional<std::string>& expected_manifest_version = std::nullopt);

/// Optional instrument metadata applied after trial inputs and runtime overrides.
struct SymbolInfo {
    /// Positive instrument price tick size.
    std::optional<double> mintick;
    /// Positive money-per-price-point multiplier.
    std::optional<double> pointvalue;
    /// Instrument lot-size grid (TradingView `syminfo.mincontract`); unset means no grid.
    /// Applied to the engine as the metadata key `qty_step`, and as `mincontract`.
    std::optional<double> mincontract;
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
    /// Prefers optional checked creation and otherwise uses the legacy factory.
    /// @throws std::runtime_error when the plugin returns a null handle.
    pf_strategy_t create_strategy() const;

    /// Releases a handle returned by create_strategy(); safe to call during cleanup.
    void free_strategy(pf_strategy_t strategy) const noexcept;

    /// Applies one serialized Pine input before the backtest starts.
    /// Uses the optional checked setter when exported; refuses rejected settings.
    /// @throws std::invalid_argument for a null handle or an embedded NUL in key/value.
    /// @throws std::runtime_error for an engine-rejected setting.
    void set_input(pf_strategy_t strategy, const std::string& key, const std::string& value) const;

    /// Applies one serialized runtime strategy override before the backtest starts.
    /// Uses the optional checked setter when exported; refuses rejected settings.
    /// @throws std::invalid_argument for a null handle or an embedded NUL in key/value.
    /// @throws std::runtime_error for an engine-rejected setting.
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

    /// Applies the lot grid (metadata keys `qty_step`, then `mincontract`) first, then mintick,
    /// pointvalue, exchange timezone, and session in their existing order.
    /// @throws std::runtime_error when a requested optional setter is unavailable; a
    ///         `mincontract` is never applied silently as no grid.
    /// @throws std::invalid_argument for a null handle, non-positive numbers, or embedded NUL.
    void set_symbol_info(pf_strategy_t strategy, const SymbolInfo& info) const;

    /// Installs fixed other-symbol facts and bars into a fresh strategy handle.
    /// @throws std::runtime_error naming a missing setter or refused symbol/timeframe.
    void set_symbol_feeds(pf_strategy_t strategy, const SymbolFeeds& symbols) const;

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

    /// Returns an engine code, or nullopt when the optional getter is unavailable.
    std::optional<std::string> last_error_code(pf_strategy_t strategy) const;
    /// Returns raw engine argument JSON, or nullopt when its getter is unavailable.
    std::optional<std::string> last_error_args(pf_strategy_t strategy) const;
    /// Returns the optional run status; status 1 denotes a failed execution.
    std::optional<std::int32_t> last_run_status(pf_strategy_t strategy) const;

    /// Returns whether all six selected-window/observer exports exist; does not admit a revision.
    bool selected_window_available() const noexcept;

    /// Requires all six exports in protocol order, ABI versions 1, then an admitted engine.
    /// The diagnostic lists every absent protocol export; the reason names the first.
    /// @throws HpoError with code `hpo_walkforward_unavailable` on admission failure.
    void require_selected_window(
        const std::optional<std::string>& expected_manifest_version = std::nullopt) const;

    /// Reads and validates this plugin's embedded runtime identity, optionally against a manifest.
    /// @throws HpoError with code `hpo_walkforward_unavailable` for invalid metadata or revision.
    EngineVersion engine_version(
        const std::optional<std::string>& expected_manifest_version = std::nullopt) const;

    /// Configures the next selected run's half-open interval using the engine's v1 descriptor.
    /// Setter -2 is unavailable, -4 is engine `out_of_memory`; other refusals are invariants.
    /// Callers outside `SelectedEvaluation::evaluate` must call require_selected_window() first;
    /// this method does not enforce admission when its own symbol exists.
    void set_selected_window(pf_strategy_t strategy, std::int64_t start_ms,
                             std::int64_t end_ms) const;
    /// Clears pending selection; subject to the same checked status rules as its setter.
    /// Callers outside `SelectedEvaluation::evaluate` must call require_selected_window() first;
    /// this method does not enforce admission when its own symbol exists.
    void clear_selected_window(pf_strategy_t strategy) const;
    /// Reads native counts with initialized and checked engine headers.
    /// Any getter refusal, including -2 after a successful run, is `hpo_invariant`.
    /// Callers outside `SelectedEvaluation::evaluate` must call require_selected_window() first;
    /// this method does not enforce admission when its own symbol exists.
    pf_selected_window_counts_v1 selected_window_counts(pf_strategy_t strategy) const;
    /// Registers an observer, or removes it when @p observer is null.
    /// Borrowed callback/context lifetime follows the engine header, including removal refusal.
    /// Setter -2 is unavailable, -4 is engine `out_of_memory`; other refusals are invariants.
    /// Callers outside `SelectedEvaluation::evaluate` must call require_selected_window() first;
    /// this method does not enforce admission when its own symbol exists.
    void set_execution_observer(pf_strategy_t strategy,
                                const pf_execution_observer_v1* observer) const;
    /// Reads the execution snapshot; a refusal or changed header is `hpo_invariant`.
    /// Callers outside `SelectedEvaluation::evaluate` must call require_selected_window() first;
    /// this method does not enforce admission when its own symbol exists.
    pf_execution_observation_v1 execution_observation(pf_strategy_t strategy) const;
    /// Returns optional version-1 effective-settings JSON using the bounded two-call protocol.
    /// An absent getter or unusable receipt is nullopt, never a settings attestation.
    std::optional<std::string> effective_settings(pf_strategy_t strategy) const;

    /// Releases report-owned allocations populated by run_backtest_full().
    void free_report(pf_report_t* report) const noexcept;

private:
    using StrategyCreateFn = pf_strategy_t (*)(const char*);
    using StrategyFreeFn = void (*)(pf_strategy_t);
    using StrategySetInputFn = void (*)(pf_strategy_t, const char*, const char*);
    using StrategySetOverrideFn = void (*)(pf_strategy_t, const char*, const char*);
    using StrategySetChartTimezoneFn = void (*)(pf_strategy_t, const char*);
    using StrategySetSymbolDoubleFn = void (*)(pf_strategy_t, double);
    using StrategySetSymbolMetadataFn = void (*)(pf_strategy_t, const char*, double);
    using StrategySetSymbolStringFn = void (*)(pf_strategy_t, const char*);
    using StrategySetSymbolFactsFn = int (*)(pf_strategy_t, const char*, const char*, const char*);
    using StrategySetSymbolFeedFn = int (*)(pf_strategy_t, const char*, const char*,
                                           const pf_bar_t*, const std::int64_t*, std::int32_t);
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
    using StrategyLastRunStatusFn = int (*)(pf_strategy_t);
    using StrategyCreateCheckedFn = int (*)(const char*, pf_strategy_t*, char*, std::size_t);
    using StrategySetCheckedFn =
        int (*)(pf_strategy_t, const char*, const char*, char*, std::size_t);
    using ReportFreeFn = void (*)(pf_report_t*);
    using AbiVersionFn = int (*)();
    using ProtocolVersionFn = std::uint32_t (*)();
    using SetSelectedWindowFn = int (*)(pf_strategy_t, const pf_selected_window_config_v1*);
    using SelectedWindowCountsFn = int (*)(pf_strategy_t, pf_selected_window_counts_v1*);
    using SetExecutionObserverFn = int (*)(pf_strategy_t, const pf_execution_observer_v1*);
    using ExecutionObservationFn = int (*)(pf_strategy_t, pf_execution_observation_v1*);
    using VersionGetFn = pf_version_t (*)();
    using VersionStringFn = const char* (*)();
    using EffectiveSettingsFn =
        int (*)(pf_strategy_t, char*, std::size_t, std::size_t*, char*, std::size_t);

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
    StrategySetSymbolMetadataFn strategy_set_syminfo_metadata_ = nullptr;
    StrategySetSymbolStringFn strategy_set_syminfo_timezone_ = nullptr;
    StrategySetSymbolStringFn strategy_set_syminfo_session_ = nullptr;
    StrategySetSymbolFactsFn strategy_set_symbol_facts_ = nullptr;
    StrategySetSymbolFeedFn strategy_set_symbol_feed_ = nullptr;
    RunBacktestFullFn run_backtest_full_ = nullptr;
    StrategyGetLastErrorFn strategy_get_last_error_ = nullptr;
    StrategyGetLastErrorFn strategy_get_last_error_code_ = nullptr;
    StrategyGetLastErrorFn strategy_get_last_error_args_ = nullptr;
    StrategyLastRunStatusFn strategy_last_run_status_ = nullptr;
    StrategyCreateCheckedFn strategy_create_checked_ = nullptr;
    StrategySetCheckedFn strategy_set_input_checked_ = nullptr;
    StrategySetCheckedFn strategy_set_override_checked_ = nullptr;
    ReportFreeFn report_free_ = nullptr;
    AbiVersionFn pf_abi_version_ = nullptr;
    ProtocolVersionFn pf_selected_window_version_ = nullptr;
    SetSelectedWindowFn strategy_set_selected_window_v1_ = nullptr;
    SelectedWindowCountsFn strategy_selected_window_counts_v1_ = nullptr;
    ProtocolVersionFn pf_execution_observer_version_ = nullptr;
    SetExecutionObserverFn strategy_set_execution_observer_v1_ = nullptr;
    ExecutionObservationFn strategy_execution_observation_v1_ = nullptr;
    VersionGetFn pf_version_get_ = nullptr;
    VersionStringFn pf_version_string_ = nullptr;
    EffectiveSettingsFn strategy_get_effective_settings_ = nullptr;
};

}  // namespace hpo
}  // namespace pineforge
