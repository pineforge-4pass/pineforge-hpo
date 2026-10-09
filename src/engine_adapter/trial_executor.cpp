#include <pineforge/hpo/error.hpp>
#include <pineforge/hpo/return_stats.hpp>
#include <pineforge/hpo/trial_executor.hpp>

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string_view>
#include <type_traits>
#include <utility>

namespace pineforge {
namespace hpo {
namespace {

std::optional<double> trade_metric(const pf_trade_stats_t& metrics,
                                   std::string_view name) noexcept {
    if (name == "num_trades")
        return static_cast<double>(metrics.num_trades);
    if (name == "num_wins")
        return static_cast<double>(metrics.num_wins);
    if (name == "num_losses")
        return static_cast<double>(metrics.num_losses);
    if (name == "num_even")
        return static_cast<double>(metrics.num_even);
    if (name == "percent_profitable")
        return metrics.percent_profitable;
    if (name == "net_profit")
        return metrics.net_profit;
    if (name == "net_profit_pct")
        return metrics.net_profit_pct;
    if (name == "gross_profit")
        return metrics.gross_profit;
    if (name == "gross_profit_pct")
        return metrics.gross_profit_pct;
    if (name == "gross_loss")
        return metrics.gross_loss;
    if (name == "gross_loss_pct")
        return metrics.gross_loss_pct;
    if (name == "profit_factor")
        return metrics.profit_factor;
    if (name == "avg_trade")
        return metrics.avg_trade;
    if (name == "avg_trade_pct")
        return metrics.avg_trade_pct;
    if (name == "avg_win")
        return metrics.avg_win;
    if (name == "avg_win_pct")
        return metrics.avg_win_pct;
    if (name == "avg_loss")
        return metrics.avg_loss;
    if (name == "avg_loss_pct")
        return metrics.avg_loss_pct;
    if (name == "ratio_avg_win_avg_loss")
        return metrics.ratio_avg_win_avg_loss;
    if (name == "largest_win")
        return metrics.largest_win;
    if (name == "largest_win_pct")
        return metrics.largest_win_pct;
    if (name == "largest_loss")
        return metrics.largest_loss;
    if (name == "largest_loss_pct")
        return metrics.largest_loss_pct;
    if (name == "commission_paid")
        return metrics.commission_paid;
    if (name == "expectancy")
        return metrics.expectancy;
    if (name == "max_consecutive_wins") {
        return static_cast<double>(metrics.max_consecutive_wins);
    }
    if (name == "max_consecutive_losses") {
        return static_cast<double>(metrics.max_consecutive_losses);
    }
    if (name == "avg_bars_in_trade")
        return metrics.avg_bars_in_trade;
    if (name == "avg_bars_in_wins")
        return metrics.avg_bars_in_wins;
    if (name == "avg_bars_in_losses")
        return metrics.avg_bars_in_losses;
    return std::nullopt;
}

std::optional<double> equity_metric(const pf_equity_stats_t& metrics,
                                    std::string_view name) noexcept {
    if (name == "max_equity_drawdown")
        return metrics.max_equity_drawdown;
    if (name == "max_equity_drawdown_pct")
        return metrics.max_equity_drawdown_pct;
    if (name == "max_equity_runup")
        return metrics.max_equity_runup;
    if (name == "max_equity_runup_pct")
        return metrics.max_equity_runup_pct;
    if (name == "buy_hold_return")
        return metrics.buy_hold_return;
    if (name == "buy_hold_return_pct")
        return metrics.buy_hold_return_pct;
    // Engine 1.0 renamed sharpe_tv/sortino_tv; the pre-1.0 names stay accepted aliases.
    if (name == "sharpe_monthly" || name == "sharpe_tv")
        return metrics.sharpe_monthly;
    if (name == "sortino_monthly" || name == "sortino_tv")
        return metrics.sortino_monthly;
    if (name == "sharpe_bar")
        return metrics.sharpe_bar;
    if (name == "sortino_bar")
        return metrics.sortino_bar;
    if (name == "cagr")
        return metrics.cagr;
    if (name == "calmar")
        return metrics.calmar;
    if (name == "recovery_factor")
        return metrics.recovery_factor;
    if (name == "time_in_market_pct")
        return metrics.time_in_market_pct;
    if (name == "open_pl")
        return metrics.open_pl;
    return std::nullopt;
}

bool strip_prefix(std::string_view* value, std::string_view prefix) noexcept {
    if (value->size() < prefix.size() || value->substr(0, prefix.size()) != prefix) {
        return false;
    }
    value->remove_prefix(prefix.size());
    return true;
}

void store_series(ReturnStatsFields* target, const ReturnSeriesStats& stats) noexcept {
    target->computed = true;
    for (std::size_t index = 0; index < kReturnStatisticCount; ++index)
        target->values[index] = stats.value(static_cast<ReturnStatistic>(index));
}

// Reads the engine-owned canonical curve in place, for the requested series only. It runs while
// the C-ABI report is still alive; only the resulting numbers are kept in the snapshot.
void compute_report_return_stats(const pf_report_t& report,
                                 const BacktestConfiguration& configuration,
                                 ReportSnapshot* snapshot) {
    static_assert(std::is_same<decltype(pf_equity_point_t::equity), double>::value,
                  "the reducer reads the curve equity as binary64");
    static_assert(std::is_integral<decltype(pf_equity_point_t::time_ms)>::value &&
                      std::is_signed<decltype(pf_equity_point_t::time_ms)>::value &&
                      sizeof(pf_equity_point_t::time_ms) == sizeof(std::int64_t),
                  "the reducer reads the curve time as a signed 64-bit integer");
    EquityPointsView view;
    view.records = report.equity_curve;
    view.count = static_cast<std::size_t>(report.equity_curve_len);
    view.stride = sizeof(pf_equity_point_t);
    view.time_offset = offsetof(pf_equity_point_t, time_ms);
    view.equity_offset = offsetof(pf_equity_point_t, equity);
    ReturnStatsRequest request;
    request.bar = configuration.return_stats_bar;
    request.monthly = configuration.return_stats_monthly;
    request.chart_timezone = configuration.chart_timezone;
    const ReturnStatsResult result = compute_return_stats(view, request);
    if (result.bar)
        store_series(&snapshot->return_bar, *result.bar);
    if (result.monthly)
        store_series(&snapshot->return_monthly, *result.monthly);
}

ReportSnapshot copy_report(const pf_report_t& report, const BacktestConfiguration& configuration) {
    const bool capture_equity_curve = configuration.capture_equity_curve;
    const bool capture_trades = configuration.capture_trades;
    if (report.total_trades < 0) {
        throw TypedHpoError<std::runtime_error>("hpo_report_invalid", {},
                                                "strategy returned a negative total_trades value");
    }
    if (report.equity_curve_len < 0) {
        throw TypedHpoError<std::runtime_error>(
            "hpo_report_invalid", {}, "strategy returned a negative equity_curve_len value");
    }
    if (report.equity_curve_len > 0 && report.equity_curve == nullptr) {
        throw TypedHpoError<std::runtime_error>("hpo_report_invalid", {},
                                                "strategy returned a null non-empty equity curve");
    }

    const auto curve_size = static_cast<std::uint64_t>(report.equity_curve_len);
    if (curve_size > std::vector<pf_equity_point_t>().max_size()) {
        throw TypedHpoError<std::runtime_error>("hpo_report_invalid", {},
                                                "strategy equity curve is too large to snapshot");
    }

    ReportSnapshot snapshot;
    snapshot.total_trades = static_cast<std::int32_t>(report.total_trades);
    snapshot.net_profit = report.net_profit;
    snapshot.input_bars_processed = report.input_bars_processed;
    snapshot.script_bars_processed = report.script_bars_processed;
    snapshot.security_feeds_total = report.security_feeds_total;
    snapshot.security_complete_total = report.security_complete_total;
    snapshot.security_partial_total = report.security_partial_total;
    snapshot.magnifier_sub_bars_total = report.magnifier_sub_bars_total;
    snapshot.magnifier_sample_ticks_total = report.magnifier_sample_ticks_total;
    snapshot.input_tf_seconds = report.input_tf_seconds;
    snapshot.script_tf_seconds = report.script_tf_seconds;
    snapshot.script_tf_ratio = report.script_tf_ratio;
    snapshot.needs_aggregation = report.needs_aggregation != 0;
    snapshot.bar_magnifier_enabled = report.bar_magnifier_enabled != 0;
    std::memcpy(&snapshot.metrics, &report.metrics, sizeof(snapshot.metrics));
    if (capture_trades && report.total_trades > 0) {
        if (!report.trades) {
            throw TypedHpoError<std::runtime_error>("hpo_report_invalid", {},
                                                    "strategy returned null non-empty trades");
        }
        snapshot.trades.resize(static_cast<std::size_t>(report.total_trades));
        std::memcpy(snapshot.trades.data(), report.trades,
                    snapshot.trades.size() * sizeof(pf_trade_t));
    }
    if (capture_equity_curve && report.equity_curve_len > 0) {
        snapshot.equity_curve.assign(report.equity_curve,
                                     report.equity_curve + report.equity_curve_len);
    }
    if (configuration.return_stats_bar || configuration.return_stats_monthly)
        compute_report_return_stats(report, configuration, &snapshot);
    return snapshot;
}

bool valid_magnifier_distribution(pf_magnifier_distribution_t distribution) noexcept {
    switch (distribution) {
    case PF_MAGNIFIER_UNIFORM:
    case PF_MAGNIFIER_COSINE:
    case PF_MAGNIFIER_TRIANGLE:
    case PF_MAGNIFIER_ENDPOINTS:
    case PF_MAGNIFIER_FRONT_LOADED:
    case PF_MAGNIFIER_BACK_LOADED:
        return true;
    }
    return false;
}

// One resource owner makes the required teardown order structural rather than
// dependent on local declaration order: report arrays first, handle second.
class TrialResources final {
public:
    TrialResources(const StrategyPlugin& plugin, pf_strategy_t strategy)
        : plugin_(plugin), strategy_(strategy) {}

    ~TrialResources() {
        plugin_.free_report(&report);
        plugin_.free_strategy(strategy_);
    }

    TrialResources(const TrialResources&) = delete;
    TrialResources& operator=(const TrialResources&) = delete;

    pf_strategy_t strategy() const noexcept { return strategy_; }

    pf_report_t report{};

private:
    const StrategyPlugin& plugin_;
    pf_strategy_t strategy_;
};

}  // namespace

std::optional<double> ReportSnapshot::metric(std::string_view path) const noexcept {
    if (path == "total_trades" || path == "report.total_trades") {
        return static_cast<double>(total_trades);
    }
    if (path == "net_profit" || path == "report.net_profit")
        return net_profit;
    if (path == "input_bars_processed" || path == "report.input_bars_processed") {
        return static_cast<double>(input_bars_processed);
    }
    if (path == "script_bars_processed" || path == "report.script_bars_processed") {
        return static_cast<double>(script_bars_processed);
    }
    if (path == "magnifier_sample_ticks_total" || path == "report.magnifier_sample_ticks_total") {
        return static_cast<double>(magnifier_sample_ticks_total);
    }

    std::string_view leaf = path;
    if (strip_prefix(&leaf, "metrics.all."))
        return trade_metric(metrics.all, leaf);
    leaf = path;
    if (strip_prefix(&leaf, "metrics.longs."))
        return trade_metric(metrics.longs, leaf);
    leaf = path;
    if (strip_prefix(&leaf, "metrics.shorts."))
        return trade_metric(metrics.shorts, leaf);
    leaf = path;
    if (strip_prefix(&leaf, "metrics.equity."))
        return equity_metric(metrics.equity, leaf);
    // Only a name that matched nothing above reaches the return statistics, so the existing
    // metrics pay nothing for them.
    if (const auto resolved = parse_return_stats_metric(path)) {
        const ReturnStatsFields& series =
            resolved->series == ReturnSeries::Bar ? return_bar : return_monthly;
        return series.values[static_cast<std::size_t>(resolved->statistic)];
    }
    return std::nullopt;
}

TrialExecutor::TrialExecutor(std::shared_ptr<const StrategyPlugin> plugin,
                             std::shared_ptr<const Dataset> dataset,
                             BacktestConfiguration configuration)
    : plugin_(std::move(plugin)),
      dataset_(std::move(dataset)),
      configuration_(std::move(configuration)) {
    if (!plugin_) {
        throw TypedHpoError<std::invalid_argument>("hpo_invariant", {},
                                                   "TrialExecutor requires a strategy plugin");
    }
    if (!dataset_) {
        throw TypedHpoError<std::invalid_argument>("hpo_invariant", {},
                                                   "TrialExecutor requires a dataset");
    }
    if (dataset_->size() > static_cast<std::size_t>(std::numeric_limits<int>::max())) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_dataset_invalid", {}, "dataset exceeds the PineForge C ABI bar-count limit");
    }
    if (configuration_.magnifier_samples <= 0) {
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "backtest"}},
                                                   "magnifier_samples must be positive");
    }
    if (!valid_magnifier_distribution(configuration_.magnifier_distribution)) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "backtest"}}, "invalid magnifier distribution");
    }
    if (configuration_.return_stats_bar || configuration_.return_stats_monthly) {
        // Fail-closed sanity probe of the reducer's own build (multiply-add not contracted). It
        // is not an attestation; the statistics identity and the spot proofs bind the build.
        if (!return_stats_contraction_free())
            throw TypedHpoError<std::logic_error>(
                "hpo_invariant", {},
                "return statistics need a build without floating-point contraction");
    }
    if (configuration_.symbol_feeds && !configuration_.symbol_feeds->empty()) {
        TrialResources validation(*plugin_, plugin_->create_strategy());
        plugin_->set_symbol_feeds(validation.strategy(), *configuration_.symbol_feeds);
        const auto error = plugin_->last_error(validation.strategy());
        const auto code = plugin_->last_error_code(validation.strategy());
        if (!error.empty() || (code && !code->empty()) ||
            plugin_->last_run_status(validation.strategy()) == 1)
            throw EngineError(code, plugin_->last_error_args(validation.strategy()),
                              "--symbol-feeds: " + error);
        const auto canonical = [](std::string timeframe) {
            if (timeframe == "D" || timeframe == "W" || timeframe == "M" || timeframe == "S")
                timeframe = "1" + timeframe;
            return timeframe;
        };
        const auto has_bars = std::any_of(configuration_.symbol_feeds->begin(),
            configuration_.symbol_feeds->end(), [](const auto& symbol) {
                return !symbol.feeds.empty();
            });
        if (has_bars && !configuration_.script_timeframe.empty() &&
            canonical(configuration_.input_timeframe) != canonical(configuration_.script_timeframe))
            throw TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "symbol_feeds"}},
                "--symbol-feeds: request.security of another symbol "
                "needs the chart's own bars as input; input '" +
                    configuration_.input_timeframe + "' aggregated to chart '" +
                    configuration_.script_timeframe + "' is not supported");
    }
}

TrialExecutionResult TrialExecutor::execute(const ParameterValues& inputs,
                                            const ParameterValues& strategy_overrides) const {
    return execute_prefix(inputs, strategy_overrides, dataset_->size());
}

TrialExecutionResult TrialExecutor::execute_prefix(const ParameterValues& inputs,
                                                   const ParameterValues& strategy_overrides,
                                                   std::size_t bar_count) const {
    if (bar_count == 0 || bar_count > dataset_->size())
        throw TypedHpoError<std::invalid_argument>("hpo_invariant", {},
                                                   "prefix size must be within the dataset");
    TrialResources resources(*plugin_, plugin_->create_strategy());
    plugin_->set_chart_timezone(resources.strategy(), configuration_.chart_timezone);
    for (const auto& entry : strategy_overrides) {
        plugin_->set_override(resources.strategy(), entry.first, entry.second);
    }
    for (const auto& entry : inputs) {
        plugin_->set_input(resources.strategy(), entry.first, entry.second);
    }
    if (configuration_.symbol_info)
        plugin_->set_symbol_info(resources.strategy(), *configuration_.symbol_info);
    if (configuration_.symbol_feeds)
        plugin_->set_symbol_feeds(resources.strategy(), *configuration_.symbol_feeds);

    plugin_->run_backtest_full(resources.strategy(), dataset_->data(),
                               static_cast<int>(bar_count), configuration_.input_timeframe,
                               configuration_.script_timeframe, configuration_.bar_magnifier,
                               configuration_.magnifier_samples,
                               configuration_.magnifier_distribution, &resources.report);

    const std::string engine_error = plugin_->last_error(resources.strategy());
    const auto engine_code = plugin_->last_error_code(resources.strategy());
    if (!engine_error.empty() || (engine_code && !engine_code->empty()) ||
        plugin_->last_run_status(resources.strategy()) == 1) {
        TrialExecutionResult failed;
        failed.status = TrialExecutionStatus::kEngineError;
        failed.error = engine_error;
        failed.error_code = engine_code && !engine_code->empty() ? engine_code : std::nullopt;
        failed.error_args = plugin_->last_error_args(resources.strategy());
        return failed;
    }

    TrialExecutionResult succeeded;
    succeeded.status = TrialExecutionStatus::kSucceeded;
    succeeded.report = copy_report(resources.report, configuration_);
    return succeeded;
}

}  // namespace hpo
}  // namespace pineforge
