#include <pineforge/pineforge.h>

#if defined(PINEFORGE_HPO_FAKE_PLUGIN)

#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

struct FakeStrategy {
    std::map<std::string, std::string> inputs;
    std::map<std::string, std::string> overrides;
    std::string chart_timezone;
    std::string error;
    std::string error_code;
    std::string error_args;
    int run_status = 0;
    int runs = 0;
    int syminfo_stage = 0;
    bool syminfo_order_error = false;
    double mintick = 0.01;
    double pointvalue = 1.0;
    std::string symbol_timezone;
    std::string symbol_session;
    std::map<std::string, double> metadata;
    std::map<std::string, std::string> symbol_facts;
    std::map<std::string, std::vector<pf_bar_t>> symbol_feeds;
};

// Every syminfo metadata call of every handle, "key=value" in call order, for the adapter test.
std::mutex metadata_mutex;
std::vector<std::string> metadata_log;

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
    fake(strategy)->syminfo_order_error |=
        fake(strategy)->syminfo_stage != 0 || !fake(strategy)->metadata.empty();
    fake(strategy)->inputs[key != nullptr ? key : ""] = value != nullptr ? value : "";
}

PF_API void strategy_set_override(pf_strategy_t strategy, const char* key, const char* value) {
    fake(strategy)->syminfo_order_error |=
        fake(strategy)->syminfo_stage != 0 || !fake(strategy)->metadata.empty();
    fake(strategy)->overrides[key != nullptr ? key : ""] = value != nullptr ? value : "";
}

#if !defined(PINEFORGE_HPO_FAKE_PLUGIN_NO_TIMEZONE)
PF_API void strategy_set_chart_timezone(pf_strategy_t strategy, const char* timezone) {
    fake(strategy)->chart_timezone = timezone != nullptr ? timezone : "";
}
#endif

#if !defined(PINEFORGE_HPO_FAKE_PLUGIN_NO_METADATA)
// Lot-grid metadata comes after inputs/overrides and before mintick and the other setters.
PF_API void strategy_set_syminfo_metadata(pf_strategy_t strategy, const char* key, double value) {
    auto* state = fake(strategy);
    state->syminfo_order_error |= state->syminfo_stage != 0;
    state->metadata[key != nullptr ? key : ""] = value;
    char text[40];
    std::snprintf(text, sizeof(text), "=%.17g", value);
    const std::lock_guard<std::mutex> lock(metadata_mutex);
    metadata_log.push_back(std::string(key != nullptr ? key : "") + text);
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

#if !defined(PINEFORGE_HPO_FAKE_PLUGIN_NO_METADATA)
PF_API int strategy_set_symbol_facts(pf_strategy_t strategy, const char* key,
                                     const char* field, const char* value) {
    if (std::strcmp(key, "REFUSE") == 0) {
        fake(strategy)->error = "deliberate symbol facts refusal";
        return -1;
    }
    fake(strategy)->symbol_facts[std::string(key) + ":" + field] = value;
    return 0;
}

PF_API int strategy_set_symbol_feed(pf_strategy_t strategy, const char* key, const char* timeframe,
                                    const pf_bar_t* bars, const int64_t* closes, int count) {
    if (std::strcmp(key, "REFUSE_FEED") == 0) {
        fake(strategy)->error = "deliberate symbol feed refusal";
        return -1;
    }
    if (count && (!bars || !closes))
        return -1;
    auto& target = fake(strategy)->symbol_feeds[std::string(key) + "@" + timeframe];
    for (int index = 0; index < count; ++index) {
        if (closes[index] <= bars[index].timestamp)
            return -1;
        target.push_back(bars[index]);
    }
    return 0;
}
#endif

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
    state->error_code.clear();
    state->error_args.clear();
    state->run_status = 0;
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

    const auto mode = state->inputs.find("FailureMode");
    if (mode != state->inputs.end()) {
        allocate_curve(report, 1, 100.0);
        report->net_profit = 1e12;
        state->run_status = 1;
        state->error_code = "strategy_runtime_error";
        state->error_args = "{}";
        if (mode->second == "unicode") {
            state->error = std::string(4094, 'x') + "€" + std::string(5000, 'y');
        } else if (mode->second == "forged") {
            state->error = "std::bad_alloc native engine fault --symbol-feeds: rejected";
        } else if (mode->second == "unknown_code") {
            state->error_code = "future_engine_failure";
            state->error_args = "{\"z\":null,\"bool\":true,\"n\":1.00,\"a\":\"value\"}";
        } else if (mode->second == "nested_args") {
            state->error_args = "{\"reason\":{\"untrusted\":true}}";
        } else if (mode->second == "array_args") {
            state->error_args = "[]";
        } else if (mode->second == "invalid_args") {
            state->error_args = "{invalid json}";
        } else if (mode->second == "nonfinite_args") {
            state->error_args = "{\"number\":1e999}";
        } else if (mode->second == "duplicate_args") {
            state->error_args = "{\"reason\":\"first\",\"reason\":\"second\"}";
        } else if (mode->second == "large_args") {
            state->error_args = "{\"reason\":\"" + std::string(20000, 'x') + "\"}";
        } else if (mode->second == "code_only") {
            state->run_status = 0;
        } else if (mode->second == "status_only") {
            state->error_code.clear();
            state->error_args.clear();
        } else if (mode->second == "invalid_report") {
            state->run_status = 0;
            state->error_code.clear();
            state->error_args.clear();
            report->total_trades = -1;
        } else if (mode->second == "text_only") {
            state->run_status = 0;
            state->error_code.clear();
            state->error = "text failure";
        }
        return;
    }

    const bool prefix_test = parse_or(state->inputs, "BatchPrefixTest", 0.0) == 1.0;
    if ((!prefix_test && bar_count != 2) || std::strcmp(input_timeframe, "1") != 0 ||
        std::strcmp(script_timeframe, state->symbol_feeds.empty() ? "5" : "1") != 0 ||
        bar_magnifier != 1 || magnifier_samples != 6 ||
        magnifier_distribution != PF_MAGNIFIER_TRIANGLE) {
        state->error = "backtest configuration was not forwarded";
        return;
    }

    const int length = static_cast<int>(parse_or(state->inputs, "Length", 0.0));
    if (prefix_test && parse_or(state->inputs, "BatchPrefixJitter", 1.0) == 1.0) {
        std::this_thread::sleep_for(std::chrono::milliseconds((length % 7) + 1));
    }
    if (bar_count > parse_or(state->inputs, "FailAfterBars", 1e9)) {
        state->error = "deliberate later-rung engine failure";
        return;
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
    const auto qty_step = state->metadata.find("qty_step");
    const auto mincontract = state->metadata.find("mincontract");
    const double metadata_bonus =
        (qty_step == state->metadata.end() ? 0.0 : qty_step->second * 1'000.0) +
        (mincontract == state->metadata.end() ? 0.0 : mincontract->second * 10.0);
    double feed_bonus = 0.0;
    for (const auto& feed : state->symbol_feeds) {
        for (const auto& bar : feed.second)
            feed_bonus += bar.close;
    }
    const double score = static_cast<double>(length) + initial_capital / 1'000.0 + timezone_bonus +
        symbol_bonus + metadata_bonus + feed_bonus;

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

#if defined(PINEFORGE_HPO_FAKE_PLUGIN_CODED) || defined(PINEFORGE_HPO_FAKE_PLUGIN_STATUS)
PF_API int strategy_last_run_status(pf_strategy_t strategy) {
    return strategy ? (!fake(strategy)->error.empty() ? 1 : fake(strategy)->run_status) : 1;
}
#endif

#if defined(PINEFORGE_HPO_FAKE_PLUGIN_CODED)
PF_API const char* strategy_get_last_error_code(pf_strategy_t strategy) {
    if (!strategy)
        return nullptr;
    const auto* state = fake(strategy);
    return !state->error_code.empty() ? state->error_code.c_str()
           : !state->error.empty()    ? "strategy_runtime_error"
                                      : "";
}

PF_API const char* strategy_get_last_error_args(pf_strategy_t strategy) {
    if (!strategy)
        return nullptr;
    const auto* state = fake(strategy);
    return !state->error_args.empty() ? state->error_args.c_str()
           : !state->error.empty()    ? "{}"
                                      : "";
}

PF_API int strategy_create_checked(const char*,
                                   pf_strategy_t* out,
                                   char* error,
                                   std::size_t capacity) {
    *out = nullptr;
    if (std::getenv("PFH_TEST_CREATE_REFUSED")) {
        if (error && capacity)
            std::snprintf(error, capacity, "deliberate checked create refusal");
        return PF_SETTINGS_EXCEPTION;
    }
    *out = strategy_create(nullptr);
    if (error && capacity)
        error[0] = '\0';
    return PF_SETTINGS_OK;
}

int checked_refusal(
    pf_strategy_t strategy, const char* entrypoint, int status, char* error, std::size_t capacity) {
    auto* state = fake(strategy);
    state->error = "deliberate checked setting refusal";
    state->error_code = "setting_rejected";
    state->error_args = std::string("{\"entrypoint\":\"") + entrypoint + "\",\"reason\":\"" +
                        (status == PF_SETTINGS_UNSUPPORTED ? "unknown_key" : "unparseable_value") +
                        "\"}";
    if (error && capacity)
        std::snprintf(error, capacity, "%s", state->error.c_str());
    return status;
}

PF_API int strategy_set_input_checked(
    pf_strategy_t strategy, const char* key, const char* value, char* error, std::size_t capacity) {
    if (std::strcmp(key, "Unknown") == 0 || std::strcmp(key, "length") == 0)
        return checked_refusal(strategy, "strategy_set_input", PF_SETTINGS_UNSUPPORTED, error,
                               capacity);
    if (std::strcmp(key, "Length") == 0 || std::strcmp(key, "FixedNumber") == 0) {
        char* end = nullptr;
        const auto number = std::strtod(value, &end);
        if (end == value || *end || !std::isfinite(number))
            return checked_refusal(strategy, "strategy_set_input", PF_SETTINGS_INVALID_ARGUMENT,
                                   error, capacity);
    }
    strategy_set_input(strategy, key, value);
    if (error && capacity)
        error[0] = '\0';
    return PF_SETTINGS_OK;
}

PF_API int strategy_set_override_checked(
    pf_strategy_t strategy, const char* key, const char* value, char* error, std::size_t capacity) {
    if (std::strcmp(key, "unknown_override") == 0)
        return checked_refusal(strategy, "strategy_set_override", PF_SETTINGS_UNSUPPORTED, error,
                               capacity);
    if (std::strcmp(key, "commission_type") == 0 && std::strcmp(value, "percent") != 0)
        return checked_refusal(strategy, "strategy_set_override", PF_SETTINGS_INVALID_ARGUMENT,
                               error, capacity);
    strategy_set_override(strategy, key, value);
    if (error && capacity)
        error[0] = '\0';
    return PF_SETTINGS_OK;
}
#endif

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
PF_API int fake_metadata_count(void) {
    const std::lock_guard<std::mutex> lock(metadata_mutex);
    return static_cast<int>(metadata_log.size());
}
// Valid until the next metadata call or reset: the vector can reallocate.
PF_API const char* fake_metadata_entry(int index) {
    const std::lock_guard<std::mutex> lock(metadata_mutex);
    return index >= 0 && static_cast<std::size_t>(index) < metadata_log.size()
               ? metadata_log[static_cast<std::size_t>(index)].c_str()
               : "";
}
PF_API void fake_metadata_reset(void) {
    const std::lock_guard<std::mutex> lock(metadata_mutex);
    metadata_log.clear();
}

}  // extern "C"

#else

#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/error.hpp>
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
        metadata_count_ = load("fake_metadata_count");
        metadata_reset_ = reinterpret_cast<void (*)()>(load_symbol("fake_metadata_reset"));
        metadata_entry_ =
            reinterpret_cast<const char* (*)(int)>(load_symbol("fake_metadata_entry"));
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
    /// The syminfo metadata calls the fake saw, "key=value" in call order.
    std::vector<std::string> metadata() const {
        std::vector<std::string> calls;
        for (int index = 0; index < metadata_count_(); ++index) {
            calls.emplace_back(metadata_entry_(index));
        }
        return calls;
    }
    void reset_metadata() const { metadata_reset_(); }

private:
    using Counter = int (*)();

    void* load_symbol(const char* name) {
        void* const symbol = ::dlsym(handle_, name);
        if (symbol == nullptr) {
            throw std::runtime_error(std::string("fake plugin is missing counter ") + name);
        }
        return symbol;
    }

    Counter load(const char* name) { return reinterpret_cast<Counter>(load_symbol(name)); }

    void* handle_ = nullptr;
    Counter active_handles_ = nullptr;
    Counter outstanding_reports_ = nullptr;
    Counter lifetime_violations_ = nullptr;
    Counter reused_handle_runs_ = nullptr;
    Counter metadata_count_ = nullptr;
    void (*metadata_reset_)() = nullptr;
    const char* (*metadata_entry_)(int) = nullptr;
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

BacktestConfiguration lot_grid_configuration(const pineforge::hpo::SymbolInfo& info) {
    BacktestConfiguration configuration = test_configuration();
    configuration.chart_timezone.clear();
    configuration.symbol_info = info;
    return configuration;
}

void test_lot_grid_metadata(const std::filesystem::path& plugin_path) {
    auto plugin = std::make_shared<const StrategyPlugin>(plugin_path);
    FakeCounters counters(plugin_path);

    // Inputs and overrides first; then qty_step, mincontract (same value), then mintick and the
    // rest. The fake flags any other order, which fails the trial.
    pineforge::hpo::SymbolInfo full;
    full.mintick = 0.5;
    full.pointvalue = 2.0;
    full.mincontract = 0.25;
    full.timezone = "UTC";
    full.session = "24x7";
    counters.reset_metadata();
    TrialExecutor with_grid(plugin, test_dataset(), lot_grid_configuration(full));
    const auto gridded = with_grid.execute({{"Length", "14"}}, {{"initial_capital", "100000"}});
    require(gridded.succeeded(), "lot-grid trial failed: " + gridded.error);
    const std::vector<std::string> expected = {"qty_step=0.25", "mincontract=0.25"};
    require(counters.metadata() == expected,
            "mincontract was not applied as qty_step then mincontract, once each");
    // 14 + 100 + (0.5 * 100 + 2 + 10 + 20) + 0.25 * 1000 + 0.25 * 10 = 448.5
    require_near(gridded.report.net_profit, 448.5, "lot-grid trial result is wrong");

    // Only the grid: metadata is applied and no other symbol setter is called.
    pineforge::hpo::SymbolInfo only_grid;
    only_grid.mincontract = 0.125;
    counters.reset_metadata();
    TrialExecutor grid_only(plugin, test_dataset(), lot_grid_configuration(only_grid));
    const auto lone = grid_only.execute({{"Length", "14"}}, {{"initial_capital", "100000"}});
    require(lone.succeeded(), "grid-only trial failed: " + lone.error);
    const std::vector<std::string> grid_only_calls = {"qty_step=0.125", "mincontract=0.125"};
    require(counters.metadata() == grid_only_calls,
            "grid-only symbol info applied the wrong metadata");
    require_near(lone.report.net_profit, 14.0 + 100.0 + 125.0 + 1.25, "grid-only result is wrong");

    // No mincontract: no metadata call at all, the other setters unchanged.
    pineforge::hpo::SymbolInfo no_grid = full;
    no_grid.mincontract.reset();
    counters.reset_metadata();
    TrialExecutor without_grid(plugin, test_dataset(), lot_grid_configuration(no_grid));
    const auto plain = without_grid.execute({{"Length", "14"}}, {{"initial_capital", "100000"}});
    require(plain.succeeded(), "gridless trial failed: " + plain.error);
    require(counters.metadata().empty(), "a metadata call was made without mincontract");
    require_near(plain.report.net_profit, 14.0 + 100.0 + 82.0, "gridless result is wrong");

    // Invalid grids never reach the plugin.
    for (const double bad : {0.0, -0.25, std::nan(""), HUGE_VAL, -HUGE_VAL}) {
        pineforge::hpo::SymbolInfo invalid = full;
        invalid.mincontract = bad;
        counters.reset_metadata();
        TrialExecutor rejected(plugin, test_dataset(), lot_grid_configuration(invalid));
        require_throws_containing([&] { (void)rejected.execute({{"Length", "14"}}); },
                                  "finite and positive", "invalid mincontract was accepted");
        require(counters.metadata().empty(), "an invalid mincontract reached the plugin");
    }
    require(counters.active_handles() == 0, "strategy handle leaked after lot-grid trials");
    require(counters.lifetime_violations() == 0, "lot-grid failure broke teardown ordering");
}

void test_missing_metadata_symbol(const std::filesystem::path& plugin_path) {
    auto plugin = std::make_shared<const StrategyPlugin>(plugin_path);
    FakeCounters counters(plugin_path);

    // Without a mincontract the plugin keeps working with every other symbol field.
    pineforge::hpo::SymbolInfo info;
    info.mintick = 0.5;
    info.pointvalue = 2.0;
    info.timezone = "UTC";
    info.session = "24x7";
    TrialExecutor legacy(plugin, test_dataset(), lot_grid_configuration(info));
    const auto result = legacy.execute({{"Length", "14"}}, {{"initial_capital", "100000"}});
    require(result.succeeded(),
            "plugin without metadata failed without a mincontract: " + result.error);
    require_near(result.report.net_profit, 14.0 + 100.0 + 82.0,
                 "plugin without metadata returned the wrong result");

    // With a mincontract it fails loudly instead of running without the grid.
    info.mincontract = 0.25;
    TrialExecutor gridded(plugin, test_dataset(), lot_grid_configuration(info));
    require_throws_containing([&] { (void)gridded.execute({{"Length", "14"}}); },
                              "strategy_set_syminfo_metadata",
                              "mincontract ran silently without a plugin lot-size setter");
    require_throws_containing([&] { (void)gridded.execute({{"Length", "14"}}); }, "mincontract",
                              "the missing-setter error does not name syminfo.mincontract");
    require(counters.active_handles() == 0,
            "strategy handle leaked after missing-metadata failure");
    require(counters.lifetime_violations() == 0,
            "missing-metadata failure broke teardown ordering");
}

void test_optional_failure_symbols(const std::filesystem::path& legacy_path,
                                   const std::filesystem::path& coded_path,
                                   const std::filesystem::path& status_path) {
    using pineforge::hpo::StrategyPlugin;
    using pineforge::hpo::TrialExecutor;
    FakeCounters counters(coded_path);
    StrategyPlugin source(coded_path);
    auto moved = std::make_shared<StrategyPlugin>(std::move(source));
    TrialExecutor executor(moved, test_dataset(), test_configuration());
    const auto failed = executor.execute({{"Length", "14"}, {"FailureMode", "silent"}});
    require(!failed.succeeded() && failed.error.empty(), "silent runtime error was scored");
    require(failed.error_code == "strategy_runtime_error" && failed.error_args == "{}",
            "move construction lost the optional failure getters");
    require(failed.report.net_profit == 0, "failed partial report was snapshotted");
    require_throws_containing([&] { (void)executor.execute({{"Unknown", "14"}}); },
                              "checked setting", "move lost checked input setter");
    auto assigned = std::make_shared<StrategyPlugin>(legacy_path);
    *assigned = std::move(*moved);
    TrialExecutor reassigned(assigned, test_dataset(), test_configuration());
    require(reassigned.execute({{"Length", "14"}, {"FailureMode", "code_only"}}).error_code ==
                "strategy_runtime_error",
            "move assignment lost the optional code getter");
    *assigned = StrategyPlugin(legacy_path);
    const auto legacy = reassigned.execute({{"Length", "14"}, {"FailureMode", "text_only"}});
    require(!legacy.succeeded() && !legacy.error_code && !legacy.error_args,
            "legacy plugin did not preserve null metadata after move assignment");
    *assigned = StrategyPlugin(status_path);
    const auto status = reassigned.execute({{"Length", "14"}, {"FailureMode", "silent"}});
    require(!status.succeeded() && !status.error_code && !status.error_args,
            "status-only plugin did not detect the silent failure");
    require(counters.active_handles() == 0 && counters.outstanding_reports() == 0,
            "failed run or checked setter leaked a handle or report");
    require(counters.lifetime_violations() == 0, "failure broke report-before-handle teardown");
}

}  // namespace

int main(int argc, char** argv) {
    try {
        require(argc >= 2 && argc <= 6,
                "usage: test_engine_adapter <fake-plugin> [fake-plugin-without-timezone "
                "[fake-plugin-without-metadata]]");
        test_dataset_loader();
        test_trial_executor(argv[1]);
        test_lot_grid_metadata(argv[1]);
        if (argc >= 3) {
            test_missing_timezone_symbol(argv[2]);
        }
        if (argc >= 4) {
            test_missing_metadata_symbol(argv[3]);
        }
        if (argc == 6) {
            test_optional_failure_symbols(argv[1], argv[4], argv[5]);
        }
        std::cout << "engine adapter tests passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "engine adapter test failure: " << error.what() << '\n';
        return 1;
    }
}

#endif
