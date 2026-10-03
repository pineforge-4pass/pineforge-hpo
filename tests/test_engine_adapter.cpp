#include <pineforge/pineforge.h>

#if defined(PINEFORGE_HPO_FAKE_PLUGIN)

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <thread>

namespace {

struct FakeStrategy {
    std::map<std::string, std::string> inputs;
    std::map<std::string, std::string> overrides;
    std::string chart_timezone;
    std::string error;
    int runs = 0;
    int syminfo_stage = 0;
    bool syminfo_order_error = false;
    double mintick = 0.01;
    double pointvalue = 1.0;
    std::string symbol_timezone;
    std::string symbol_session;
};

std::atomic<int> active_handles{0};
std::atomic<int> outstanding_reports{0};
std::atomic<int> lifetime_violations{0};
std::atomic<int> reused_handle_runs{0};

FakeStrategy* fake(pf_strategy_t strategy) {
    return static_cast<FakeStrategy*>(strategy);
}

double parse_or(const std::map<std::string, std::string>& values,
                const std::string& key,
                double fallback) {
    const auto found = values.find(key);
    return found == values.end() ? fallback : std::strtod(found->second.c_str(), nullptr);
}

void allocate_curve(pf_report_t* report, int64_t count, double base) {
    report->equity_curve = new pf_equity_point_t[static_cast<std::size_t>(count)];
    report->equity_curve_len = count;
    for (int64_t index = 0; index < count; ++index) {
        report->equity_curve[index].time_ms = 1'700'000'000'000LL + index * 60'000;
        report->equity_curve[index].equity = base + static_cast<double>(index);
        report->equity_curve[index].open_profit = 0.25 * static_cast<double>(index);
    }
    ++outstanding_reports;
}

}  // namespace

extern "C" {

PF_API pf_strategy_t strategy_create(const char*) {
    ++active_handles;
    return new FakeStrategy();
}

PF_API void strategy_free(pf_strategy_t strategy) {
    if (strategy == nullptr) {
        return;
    }
    if (outstanding_reports.load() != 0) {
        ++lifetime_violations;
    }
    --active_handles;
    delete fake(strategy);
}

PF_API void strategy_set_input(pf_strategy_t strategy, const char* key, const char* value) {
    fake(strategy)->syminfo_order_error |= fake(strategy)->syminfo_stage != 0;
    fake(strategy)->inputs[key != nullptr ? key : ""] = value != nullptr ? value : "";
}

PF_API void strategy_set_override(pf_strategy_t strategy, const char* key, const char* value) {
    fake(strategy)->syminfo_order_error |= fake(strategy)->syminfo_stage != 0;
    fake(strategy)->overrides[key != nullptr ? key : ""] = value != nullptr ? value : "";
}

#if !defined(PINEFORGE_HPO_FAKE_PLUGIN_NO_TIMEZONE)
PF_API void strategy_set_chart_timezone(pf_strategy_t strategy, const char* timezone) {
    fake(strategy)->chart_timezone = timezone != nullptr ? timezone : "";
}
#endif

PF_API void strategy_set_syminfo_mintick(pf_strategy_t strategy, double value) {
    auto* state = fake(strategy);
    state->syminfo_order_error |= state->syminfo_stage != 0;
    state->syminfo_stage = 1;
    state->mintick = value;
}

PF_API void strategy_set_syminfo_pointvalue(pf_strategy_t strategy, double value) {
    auto* state = fake(strategy);
    state->syminfo_order_error |= state->syminfo_stage > 1;
    state->syminfo_stage = 2;
    state->pointvalue = value;
}

PF_API void strategy_set_syminfo_timezone(pf_strategy_t strategy, const char* value) {
    auto* state = fake(strategy);
    state->syminfo_order_error |= state->syminfo_stage > 2;
    state->syminfo_stage = 3;
    state->symbol_timezone = value;
}

PF_API void strategy_set_syminfo_session(pf_strategy_t strategy, const char* value) {
    auto* state = fake(strategy);
    state->syminfo_order_error |= state->syminfo_stage > 3;
    state->syminfo_stage = 4;
    state->symbol_session = value;
}

PF_API void run_backtest_full(pf_strategy_t strategy,
                              pf_bar_t*,
                              int bar_count,
                              const char* input_timeframe,
                              const char* script_timeframe,
                              int bar_magnifier,
                              int magnifier_samples,
                              pf_magnifier_distribution_t magnifier_distribution,
                              pf_report_t* report) {
    FakeStrategy* const state = fake(strategy);
    state->error.clear();
    ++state->runs;
    if (state->runs != 1) {
        ++reused_handle_runs;
        state->error = "strategy handle reused";
        return;
    }

    const auto fail = state->inputs.find("fail");
    if (fail != state->inputs.end() && fail->second == "true") {
        allocate_curve(report, 1, 100.0);
        state->error = "deliberate fake engine failure";
        return;
    }

    const bool prefix_test = parse_or(state->inputs, "BatchPrefixTest", 0.0) == 1.0;
    if ((!prefix_test && bar_count != 2) || std::strcmp(input_timeframe, "1") != 0 ||
        std::strcmp(script_timeframe, "5") != 0 || bar_magnifier != 1 || magnifier_samples != 6 ||
        magnifier_distribution != PF_MAGNIFIER_TRIANGLE) {
        state->error = "backtest configuration was not forwarded";
        return;
    }

    const int length = static_cast<int>(parse_or(state->inputs, "Length", 0.0));
    if (prefix_test) {
        std::this_thread::sleep_for(std::chrono::milliseconds((length % 7) + 1));
    }
    if (state->syminfo_order_error) {
        state->error = "symbol setters did not follow inputs/overrides and harness order";
        return;
    }
    if (length == parse_or(state->inputs, "HangAtLength", -1.0)) {
        for (;;) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(
        static_cast<int>(parse_or(state->inputs, "DelayMs", 0.0))));
    const double initial_capital = parse_or(state->overrides, "initial_capital", 100'000.0);
    const double timezone_bonus = state->chart_timezone == "Asia/Taipei" ? 1'000.0 : 0.0;
    const double symbol_bonus = state->syminfo_stage == 0 ? 0.0 :
        state->mintick * 100.0 + state->pointvalue +
        (state->symbol_timezone == "UTC" ? 10.0 : 0.0) +
        (state->symbol_session == "24x7" ? 20.0 : 0.0);
    const double score = static_cast<double>(length) + initial_capital / 1'000.0 + timezone_bonus +
        symbol_bonus;

    report->total_trades = length;
    report->net_profit = score;
    report->input_bars_processed = bar_count;
    report->script_bars_processed = 1;
    report->security_feeds_total = 7;
    report->security_complete_total = 5;
    report->security_partial_total = 2;
    report->magnifier_sub_bars_total = 12;
    report->magnifier_sample_ticks_total = 72;
    report->input_tf_seconds = 60;
    report->script_tf_seconds = 300;
    report->script_tf_ratio = 5;
    report->needs_aggregation = 1;
    report->bar_magnifier_enabled = 1;

    report->metrics.all.num_trades = length;
    report->metrics.all.net_profit = score;
    report->metrics.all.profit_factor = 2.5;
    report->metrics.longs.num_trades = length - 1;
    report->metrics.longs.net_profit = score - 10.0;
    report->metrics.shorts.num_trades = 1;
    report->metrics.shorts.net_profit = 10.0;
    report->metrics.equity.sharpe_monthly = score / 10.0;
    report->metrics.equity.sortino_monthly = score / 20.0;
    report->metrics.equity.max_equity_drawdown = 12.5;
    report->metrics.equity.open_pl = 3.5;
    allocate_curve(report, 2, initial_capital);
}

PF_API const char* strategy_get_last_error(pf_strategy_t strategy) {
    return strategy != nullptr ? fake(strategy)->error.c_str() : nullptr;
}

PF_API void report_free(pf_report_t* report) {
    if (report == nullptr || report->equity_curve == nullptr) {
        return;
    }
    delete[] report->equity_curve;
    report->equity_curve = nullptr;
    report->equity_curve_len = 0;
    --outstanding_reports;
}

PF_API int pf_abi_version(void) {
    return PF_ABI_VERSION;
}

PF_API int fake_active_handles(void) {
    return active_handles.load();
}
PF_API int fake_outstanding_reports(void) {
    return outstanding_reports.load();
}
PF_API int fake_lifetime_violations(void) {
    return lifetime_violations.load();
}
PF_API int fake_reused_handle_runs(void) {
    return reused_handle_runs.load();
}

}  // extern "C"

#else

#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/strategy_plugin.hpp>
#include <pineforge/hpo/trial_executor.hpp>

#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#if defined(__unix__) || defined(__APPLE__)
#include <dlfcn.h>
#endif

namespace {

using pineforge::hpo::BacktestConfiguration;
using pineforge::hpo::Dataset;
using pineforge::hpo::ParameterValues;
using pineforge::hpo::StrategyPlugin;
using pineforge::hpo::TrialExecutor;

static_assert(std::is_same<decltype(std::declval<const Dataset&>().data()), const pf_bar_t*>::value,
              "Dataset must expose immutable bars");

void require(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

void require_near(double actual, double expected, const std::string& message) {
    if (std::fabs(actual - expected) > 1e-12) {
        throw std::runtime_error(message + ": expected " + std::to_string(expected) + ", got " +
                                 std::to_string(actual));
    }
}

void require_metric(const pineforge::hpo::ReportSnapshot& report,
                    const std::string& path,
                    double expected,
                    const std::string& message) {
    const auto value = report.metric(path);
    require(value.has_value(), "missing metric path: " + path);
    require_near(*value, expected, message);
}

template <typename Function>
void require_throws_containing(Function&& function,
                               const std::string& expected,
                               const std::string& message) {
    try {
        function();
    } catch (const std::exception& error) {
        require(std::string(error.what()).find(expected) != std::string::npos,
                message + ": unexpected exception: " + error.what());
        return;
    }
    throw std::runtime_error(message + ": no exception was thrown");
}

class TemporaryCsv final {
public:
    explicit TemporaryCsv(const std::string& contents) {
        const auto nonce = std::chrono::high_resolution_clock::now().time_since_epoch().count();
        path_ = std::filesystem::temp_directory_path() /
                ("pineforge-hpo-engine-adapter-" + std::to_string(nonce) + ".csv");
        std::ofstream output(path_, std::ios::binary);
        if (!output) {
            throw std::runtime_error("cannot create temporary CSV");
        }
        output << contents;
        output.close();
        if (!output) {
            throw std::runtime_error("cannot write temporary CSV");
        }
    }

    ~TemporaryCsv() {
        std::error_code ignored;
        std::filesystem::remove(path_, ignored);
    }

    const std::filesystem::path& path() const noexcept { return path_; }

private:
    std::filesystem::path path_;
};

class FakeCounters final {
public:
    explicit FakeCounters(const std::filesystem::path& path) {
#if defined(__unix__) || defined(__APPLE__)
        handle_ = ::dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
        if (handle_ == nullptr) {
            throw std::runtime_error("cannot open fake plugin for counters");
        }
        active_handles_ = load("fake_active_handles");
        outstanding_reports_ = load("fake_outstanding_reports");
        lifetime_violations_ = load("fake_lifetime_violations");
        reused_handle_runs_ = load("fake_reused_handle_runs");
#else
        (void)path;
        throw std::runtime_error("fake plugin counters require dlopen");
#endif
    }

    ~FakeCounters() {
#if defined(__unix__) || defined(__APPLE__)
        if (handle_ != nullptr) {
            ::dlclose(handle_);
        }
#endif
    }

    int active_handles() const { return active_handles_(); }
    int outstanding_reports() const { return outstanding_reports_(); }
    int lifetime_violations() const { return lifetime_violations_(); }
    int reused_handle_runs() const { return reused_handle_runs_(); }

private:
    using Counter = int (*)();

    Counter load(const char* name) {
        void* const symbol = ::dlsym(handle_, name);
        if (symbol == nullptr) {
            throw std::runtime_error(std::string("fake plugin is missing counter ") + name);
        }
        return reinterpret_cast<Counter>(symbol);
    }

    void* handle_ = nullptr;
    Counter active_handles_ = nullptr;
    Counter outstanding_reports_ = nullptr;
    Counter lifetime_violations_ = nullptr;
    Counter reused_handle_runs_ = nullptr;
};

void test_dataset_loader() {
    TemporaryCsv csv(
        "\"volume\", close, timestamp, open, ignored, low, high\n"
        "\"10\", 101, 1700000000000, 100, x, 99, 102\n"
        "11.5, 102.5, 1700000060000, 101.5, y, 100.5, 103.5\n");
    const Dataset dataset = Dataset::load_csv(csv.path());
    require(dataset.size() == 2, "CSV loader returned the wrong number of bars");
    require_near(dataset.bars()[0].open, 100.0, "integer-form CSV open mapping failed");
    require_near(dataset.bars()[0].high, 102.0, "integer-form CSV high mapping failed");
    require_near(dataset.bars()[0].low, 99.0, "integer-form CSV low mapping failed");
    require_near(dataset.bars()[0].close, 101.0, "integer-form CSV close mapping failed");
    require_near(dataset.bars()[0].volume, 10.0, "integer-form CSV volume mapping failed");
    require(dataset.bars()[1].timestamp == 1'700'000'060'000LL, "CSV timestamp mapping failed");

    std::vector<pf_bar_t> unordered(2);
    unordered[0].timestamp = 20;
    unordered[1].timestamp = 10;
    require_throws_containing([&] { Dataset invalid(std::move(unordered)); }, "strictly increasing",
                              "Dataset accepted non-monotonic timestamps");
}

BacktestConfiguration test_configuration() {
    BacktestConfiguration configuration;
    configuration.input_timeframe = "1";
    configuration.script_timeframe = "5";
    configuration.chart_timezone = "Asia/Taipei";
    configuration.bar_magnifier = true;
    configuration.magnifier_samples = 6;
    configuration.magnifier_distribution = PF_MAGNIFIER_TRIANGLE;
    return configuration;
}

std::shared_ptr<const Dataset> test_dataset() {
    std::vector<pf_bar_t> bars(2);
    bars[0] = pf_bar_t{100.0, 102.0, 99.0, 101.0, 10.0, 1'700'000'000'000LL};
    bars[1] = pf_bar_t{101.0, 103.0, 100.0, 102.0, 11.0, 1'700'000'060'000LL};
    return std::make_shared<const Dataset>(std::move(bars));
}

void require_all_metric_paths(const pineforge::hpo::ReportSnapshot& report) {
    const std::vector<std::string> trade_fields = {
        "num_trades",
        "num_wins",
        "num_losses",
        "num_even",
        "percent_profitable",
        "net_profit",
        "net_profit_pct",
        "gross_profit",
        "gross_profit_pct",
        "gross_loss",
        "gross_loss_pct",
        "profit_factor",
        "avg_trade",
        "avg_trade_pct",
        "avg_win",
        "avg_win_pct",
        "avg_loss",
        "avg_loss_pct",
        "ratio_avg_win_avg_loss",
        "largest_win",
        "largest_win_pct",
        "largest_loss",
        "largest_loss_pct",
        "commission_paid",
        "expectancy",
        "max_consecutive_wins",
        "max_consecutive_losses",
        "avg_bars_in_trade",
        "avg_bars_in_wins",
        "avg_bars_in_losses",
    };
    for (const std::string& section : {"all", "longs", "shorts"}) {
        for (const std::string& field : trade_fields) {
            const std::string path = "metrics." + section + "." + field;
            require(report.metric(path).has_value(), "missing metric path: " + path);
        }
    }

    const std::vector<std::string> equity_fields = {
        "max_equity_drawdown",
        "max_equity_drawdown_pct",
        "max_equity_runup",
        "max_equity_runup_pct",
        "buy_hold_return",
        "buy_hold_return_pct",
        "sharpe_monthly",
        "sortino_monthly",
        "sharpe_bar",
        "sortino_bar",
        "cagr",
        "calmar",
        "recovery_factor",
        "time_in_market_pct",
        "open_pl",
    };
    for (const std::string& field : equity_fields) {
        const std::string path = "metrics.equity." + field;
        require(report.metric(path).has_value(), "missing metric path: " + path);
    }
}

void test_trial_executor(const std::filesystem::path& plugin_path) {
    auto plugin = std::make_shared<const StrategyPlugin>(plugin_path);
    FakeCounters counters(plugin_path);
    TrialExecutor executor(plugin, test_dataset(), test_configuration());

    const auto first = executor.execute({{"Length", "14"}}, {{"initial_capital", "200000"}});
    require(first.succeeded(), "successful fake trial failed: " + first.error);
    require(first.report.total_trades == 14, "input override was not applied");
    require_near(first.report.net_profit, 1'214.0, "trial score inputs were not forwarded");
    require_metric(first.report, "metrics.all.net_profit", 1'214.0, "all net-profit path is wrong");
    require_metric(first.report, "metrics.all.num_trades", 14.0, "all trade-count path is wrong");
    require_metric(first.report, "metrics.equity.sharpe_monthly", 121.4, "Sharpe path is wrong");
    require_metric(first.report, "metrics.equity.sortino_monthly", 60.7, "Sortino path is wrong");
    // The pre-1.0 spellings stay accepted aliases of the renamed engine fields.
    require_metric(first.report, "metrics.equity.sharpe_tv", 121.4, "Sharpe alias is wrong");
    require_metric(first.report, "metrics.equity.sortino_tv", 60.7, "Sortino alias is wrong");
    require_metric(first.report, "metrics.equity.max_equity_drawdown", 12.5,
                   "drawdown path is wrong");
    require(!first.report.metric("metrics.equity.not_a_metric").has_value(),
            "unknown metric path unexpectedly resolved");
    require(first.report.equity_curve.size() == 2, "equity curve was not snapshotted");
    require_near(first.report.equity_curve[1].equity, 200'001.0,
                 "equity curve was copied incorrectly");
    require_all_metric_paths(first.report);

    const auto second = executor.execute({{"Length", "20"}}, {{"initial_capital", "100000"}});
    require(second.succeeded(), "second trial did not use a fresh strategy handle");
    require_near(second.report.net_profit, 1'120.0, "second trial result is wrong");

    const auto full_prefix = executor.execute_prefix(
        {{"Length", "14"}}, {{"initial_capital", "200000"}}, executor.dataset().size());
    require(full_prefix.succeeded(), "full-size prefix failed");
    require(full_prefix.report.net_profit == first.report.net_profit,
            "full-size prefix changed the objective");
    require(full_prefix.report.total_trades == first.report.total_trades,
            "full-size prefix changed trades");
    const auto partial = executor.execute_prefix(
        {{"Length", "14"}, {"BatchPrefixTest", "1"}}, {{"initial_capital", "200000"}}, 1);
    require(partial.succeeded(), "partial prefix failed");
    require(partial.report.input_bars_processed == 1, "prefix did not limit input bars");
    require(partial.report.net_profit == first.report.net_profit,
            "prefix did not apply runtime settings");
    require_throws_containing([&] { (void)executor.execute_prefix({}, {}, 0); },
                              "prefix size", "empty prefix was accepted");
    require_throws_containing([&] {
        (void)executor.execute_prefix({}, {}, executor.dataset().size() + 1);
    }, "prefix size", "oversized prefix was accepted");

    auto scalar_configuration = test_configuration();
    scalar_configuration.capture_equity_curve = false;
    TrialExecutor scalar_executor(plugin, test_dataset(), scalar_configuration);
    const auto scalar = scalar_executor.execute({{"Length", "12"}});
    require(scalar.succeeded(), "scalar-only trial failed");
    require(scalar.report.equity_curve.empty(), "scalar-only trial copied an unused equity curve");
    require_metric(scalar.report, "metrics.all.net_profit", 1'112.0,
                   "disabling curve capture changed scalar metrics");

    const auto failed = executor.execute({{"fail", "true"}});
    require(!failed.succeeded(), "fake engine failure was reported as success");
    require(failed.error == "deliberate fake engine failure",
            "engine error text was not propagated");

    require(counters.active_handles() == 0, "strategy handle leaked after trial");
    require(counters.outstanding_reports() == 0, "engine report leaked after trial");
    require(counters.lifetime_violations() == 0, "strategy was freed before its report arrays");
    require(counters.reused_handle_runs() == 0, "a strategy handle was reused across trials");
}

void test_missing_timezone_symbol(const std::filesystem::path& plugin_path) {
    auto plugin = std::make_shared<const StrategyPlugin>(plugin_path);
    FakeCounters counters(plugin_path);
    BacktestConfiguration legacy_configuration = test_configuration();
    legacy_configuration.chart_timezone.clear();
    TrialExecutor legacy_executor(plugin, test_dataset(), legacy_configuration);
    const auto legacy = legacy_executor.execute({{"Length", "14"}});
    require(legacy.succeeded(), "plugin without optional timezone failed in legacy UTC mode");
    require_near(legacy.report.net_profit, 114.0,
                 "plugin without optional timezone returned the wrong result");

    TrialExecutor timezone_executor(plugin, test_dataset(), test_configuration());
    require_throws_containing([&] { (void)timezone_executor.execute({{"Length", "14"}}); },
                              "does not support a chart timezone",
                              "non-empty chart timezone did not require plugin support");
    require(counters.active_handles() == 0,
            "strategy handle leaked after missing-timezone failure");
    require(counters.lifetime_violations() == 0,
            "missing-timezone failure broke teardown ordering");
}

}  // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 2 || argc == 3,
                "usage: test_engine_adapter <fake-plugin> [fake-plugin-without-timezone]");
        test_dataset_loader();
        test_trial_executor(argv[1]);
        if (argc == 3) {
            test_missing_timezone_symbol(argv[2]);
        }
        std::cout << "engine adapter tests passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "engine adapter test failure: " << error.what() << '\n';
        return 1;
    }
}

#endif
