#pragma once

#include <pineforge/hpo/trial_executor.hpp>

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>

namespace pineforge {
namespace hpo {

/// Already-planned retained primary counts; no planning is performed by SelectedEvaluation.
struct SelectedExpectedCounts {
    /// Retained input rows fed to the engine, including preroll.
    std::uint64_t fed_input_bars = 0;
    /// Retained script groups fed to the engine, including preroll.
    std::uint64_t fed_script_bars = 0;
    /// Retained input rows before the selected start.
    std::uint64_t preroll_input_bars = 0;
    /// Used preroll script groups (the planner's `used_script_bars`).
    std::uint64_t preroll_script_bars = 0;
    /// Selected input rows in the half-open window.
    std::uint64_t window_input_bars = 0;
    /// Selected script groups in the half-open window.
    std::uint64_t window_script_bars = 0;
};

/// One fully checked selected run, detached from the engine handle and report.
struct SelectedEvaluationResult {
    /// Full owning snapshot, always including resident trades and the equity curve.
    ReportSnapshot report;
    /// Native counts correlated against the observation and supplied plan.
    pf_selected_window_counts_v1 counts{};
    /// Native execution identities and successful Results state.
    pf_execution_observation_v1 observation{};
    /// Boundary captured by the one successful non-exporting callback.
    pf_execution_boundary_v1 boundary{};
    /// Optional effective-settings JSON from this handle, unavailable when nullopt.
    std::optional<std::string> effective_settings;
};

/// Dedicated one-shot OOS execution on a fresh handle using full report mode.
///
/// The worker must have at most one selected evaluation in flight. The input pointer is borrowed
/// immutable retained OHLCV; its owner must outlive evaluate(). The returned report uses
/// O(window bars + trades) memory. Stream its window row, trades and equity to the caller's sink,
/// then release it before the next evaluation. This helper retains nothing between calls.
class SelectedEvaluation final {
public:
    /// Runs exactly once using already-planned bounds, retained rows and expected counts.
    ///
    /// Settings use the ordinary trial setter sequence. The optional manifest version is checked
    /// against embedded metadata before handle creation. Capture flags are forced on; optional
    /// return-statistics reductions are supported with the same identity checks as ordinary trials.
    /// No objective is scored here; only a successfully checked snapshot is returned.
    /// @throws HpoError for unavailable capabilities, engine failures or contract violations.
    static SelectedEvaluationResult evaluate(
        const StrategyPlugin& plugin, const pf_bar_t* bars, std::size_t count,
        std::int64_t start_ms, std::int64_t end_ms, const SelectedExpectedCounts& expected,
        const ParameterValues& inputs, const ParameterValues& strategy_overrides = {},
        const BacktestConfiguration& configuration = {},
        const std::optional<std::string>& expected_manifest_version = std::nullopt);
};

}  // namespace hpo
}  // namespace pineforge
