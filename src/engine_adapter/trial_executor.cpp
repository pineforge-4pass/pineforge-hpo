#include <pineforge/hpo/trial_executor.hpp>

#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string_view>
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

ReportSnapshot copy_report(const pf_report_t& report, bool capture_equity_curve) {
    if (report.total_trades < 0) {
        throw std::runtime_error("strategy returned a negative total_trades value");
    }
    if (report.equity_curve_len < 0) {
        throw std::runtime_error("strategy returned a negative equity_curve_len value");
    }
    if (report.equity_curve_len > 0 && report.equity_curve == nullptr) {
        throw std::runtime_error("strategy returned a null non-empty equity curve");
    }

    const auto curve_size = static_cast<std::uint64_t>(report.equity_curve_len);
    if (curve_size > std::vector<pf_equity_point_t>().max_size()) {
        throw std::runtime_error("strategy equity curve is too large to snapshot");
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
    snapshot.metrics = report.metrics;
    if (capture_equity_curve && report.equity_curve_len > 0) {
        snapshot.equity_curve.assign(report.equity_curve,
                                     report.equity_curve + report.equity_curve_len);
    }
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
    return std::nullopt;
}

TrialExecutor::TrialExecutor(std::shared_ptr<const StrategyPlugin> plugin,
                             std::shared_ptr<const Dataset> dataset,
                             BacktestConfiguration configuration)
    : plugin_(std::move(plugin)),
      dataset_(std::move(dataset)),
      configuration_(std::move(configuration)) {
    if (!plugin_) {
        throw std::invalid_argument("TrialExecutor requires a strategy plugin");
    }
    if (!dataset_) {
        throw std::invalid_argument("TrialExecutor requires a dataset");
    }
    if (dataset_->size() > static_cast<std::size_t>(std::numeric_limits<int>::max())) {
        throw std::invalid_argument("dataset exceeds the PineForge C ABI bar-count limit");
    }
    if (configuration_.magnifier_samples <= 0) {
        throw std::invalid_argument("magnifier_samples must be positive");
    }
    if (!valid_magnifier_distribution(configuration_.magnifier_distribution)) {
        throw std::invalid_argument("invalid magnifier distribution");
    }
}

TrialExecutionResult TrialExecutor::execute(const ParameterValues& inputs,
                                            const ParameterValues& strategy_overrides) const {
    TrialResources resources(*plugin_, plugin_->create_strategy());
    plugin_->set_chart_timezone(resources.strategy(), configuration_.chart_timezone);
    for (const auto& entry : strategy_overrides) {
        plugin_->set_override(resources.strategy(), entry.first, entry.second);
    }
    for (const auto& entry : inputs) {
        plugin_->set_input(resources.strategy(), entry.first, entry.second);
    }

    plugin_->run_backtest_full(resources.strategy(), dataset_->data(),
                               static_cast<int>(dataset_->size()), configuration_.input_timeframe,
                               configuration_.script_timeframe, configuration_.bar_magnifier,
                               configuration_.magnifier_samples,
                               configuration_.magnifier_distribution, &resources.report);

    const std::string engine_error = plugin_->last_error(resources.strategy());
    if (!engine_error.empty()) {
        TrialExecutionResult failed;
        failed.status = TrialExecutionStatus::kEngineError;
        failed.error = engine_error;
        return failed;
    }

    TrialExecutionResult succeeded;
    succeeded.status = TrialExecutionStatus::kSucceeded;
    succeeded.report = copy_report(resources.report, configuration_.capture_equity_curve);
    return succeeded;
}

}  // namespace hpo
}  // namespace pineforge
