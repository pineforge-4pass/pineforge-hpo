#if defined(PINEFORGE_HPO_FAKE_SELECTED)

#include <pineforge/execution_observer.h>
#include <pineforge/selected_window.h>

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <map>
#include <set>
#include <sstream>
#include <string>

namespace {

struct SelectedFake {
    pf_execution_observer_v1 observer{};
    pf_selected_window_config_v1 selection{};
    std::string error;
    std::string code;
    std::int32_t status = 0;
    bool selected = false;
    bool ran = false;
};

std::set<std::string> selected_faults;
std::map<std::string, std::int64_t> selected_counters;
std::string selected_events;
const pf_bar_t* selected_bars = nullptr;

void event(const char* name) {
    ++selected_counters[name];
    if (!selected_events.empty())
        selected_events += ',';
    selected_events += name;
}

bool hit(const char* name) {
    if (!selected_faults.count(name))
        return false;
    event(name);
    return true;
}

SelectedFake& selected_fake(pf_strategy_t strategy) {
    return *static_cast<SelectedFake*>(strategy);
}

int setter_status(const char* kind) {
    for (const int status : {-1, -2, -3, -4, -99}) {
        const std::string fault = std::string(kind) + "_" + std::to_string(status);
        if (hit(fault.c_str()))
            return status;
    }
    return 0;
}

pf_execution_observation_v1 fake_observation() {
    pf_execution_observation_v1 result{sizeof(pf_execution_observation_v1), 1,
                                       41, 73, 41, 4, 0, 4, 1};
    if (hit("zero_run_generation"))
        result.run_generation = 0;
    if (hit("zero_attempt_serial"))
        result.attempt_serial = 0;
    if (hit("zero_attempt_generation"))
        result.attempt_generation = 0;
    if (hit("boundary_generation_mismatch"))
        ++result.run_generation;
    if (hit("boundary_serial_mismatch"))
        ++result.attempt_serial;
    if (hit("phase_wrong"))
        result.phase = 3;
    if (hit("outcome_wrong"))
        result.attempt_outcome = 2;
    if (hit("boundary_not_delivered"))
        result.boundary_delivered = 0;
    if (hit("boundary_delivered_twice"))
        result.boundary_delivered = 2;
    if (hit("fault_set"))
        result.fault_stage = 2;
    return result;
}

void dispatch_boundary(SelectedFake& state) {
    pf_execution_boundary_v1 boundary{sizeof(pf_execution_boundary_v1), 1, 41, 73};
    pf_boundary_receipt_v1 receipt{sizeof(pf_boundary_receipt_v1), 1, 41, UINT32_MAX, 0, 0, 0};
    if (hit("callback_throws"))
        boundary.version = 9;  // Throws inside the host callback's protected validation block.
    if (hit("callback_receipt_mutated"))
        receipt.version = 9;
    if (hit("callback_boundary_zero"))
        boundary.run_generation = 0;
    event("callback_dispatch");
    const int status = state.observer.before_results(state.observer.context, &boundary, &receipt);
    if (status == 0 && receipt.struct_size == sizeof(receipt) && receipt.version == 1 &&
        receipt.run_generation == boundary.run_generation && receipt.frame_bytes == 0 &&
        receipt.handed_bytes == 0 && receipt.export_requested == 0 && receipt.reserved == 0) {
        event("valid_receipt");
    } else {
        event("callback_refused");
        state.error = "fake observed a callback failure";
        state.code = "strategy_runtime_error";
        state.status = 1;
    }
}

}  // namespace

extern "C" {

PF_API void fake_selected_reset(const char* faults) {
    selected_faults.clear();
    selected_counters.clear();
    selected_events.clear();
    selected_bars = nullptr;
    std::istringstream input(faults ? faults : "");
    std::string fault;
    while (std::getline(input, fault, ','))
        selected_faults.insert(fault);
}

PF_API std::int64_t fake_selected_counter(const char* name) {
    return selected_counters[name];
}

PF_API const char* fake_selected_events() { return selected_events.c_str(); }
PF_API const pf_bar_t* fake_selected_bars() { return selected_bars; }

PF_API pf_strategy_t strategy_create(const char*) {
    event("create");
    return new SelectedFake;
}

PF_API void strategy_free(pf_strategy_t strategy) {
    auto& state = selected_fake(strategy);
    if (selected_counters["free_report"] != 1)
        event("bad_free_order");
    if (state.selected)
        event("selection_at_destroy");
    if (state.observer.before_results) {
        // Character access is legal for any object; ASan detects a context freed too soon.
        const volatile unsigned char alive =
            *static_cast<const unsigned char*>(state.observer.context);
        (void)alive;
        event("context_alive_at_destroy");
    }
    event("destroy");
    delete &state;
}

PF_API void strategy_set_chart_timezone(pf_strategy_t, const char* value) {
    event(std::strcmp(value, "UTC") == 0 ? "timezone" : "wrong_timezone");
}

PF_API void strategy_set_override(pf_strategy_t, const char* key, const char* value) {
    event(std::strcmp(key, "initial_capital") == 0 && std::strcmp(value, "1000") == 0
              ? "override" : "wrong_override");
}

PF_API void strategy_set_input(pf_strategy_t strategy, const char* key, const char* value) {
    event(std::strcmp(key, "Length") == 0 && std::strcmp(value, "7") == 0
              ? "input" : "wrong_input");
    if (hit("setting_error")) {
        selected_fake(strategy).error = "latched setting failure";
        selected_fake(strategy).code = "setting_rejected";
    }
}

PF_API void strategy_set_syminfo_metadata(pf_strategy_t, const char* key, double value) {
    event(value == 0.5 ? key : "wrong_metadata");
}
PF_API void strategy_set_syminfo_mintick(pf_strategy_t, double value) {
    event(value == 0.25 ? "mintick" : "wrong_mintick");
}
PF_API void strategy_set_syminfo_pointvalue(pf_strategy_t, double value) {
    event(value == 2 ? "pointvalue" : "wrong_pointvalue");
}
PF_API void strategy_set_syminfo_timezone(pf_strategy_t, const char* value) {
    event(std::strcmp(value, "UTC") == 0 ? "symbol_timezone" : "wrong_symbol_timezone");
}
PF_API void strategy_set_syminfo_session(pf_strategy_t, const char* value) {
    event(std::strcmp(value, "24x7") == 0 ? "session" : "wrong_session");
}
PF_API int strategy_set_symbol_facts(pf_strategy_t, const char*, const char*, const char*) {
    event("symbol_fact");
    return 0;
}
PF_API int strategy_set_symbol_feed(pf_strategy_t, const char*, const char*, const pf_bar_t*,
                                    const std::int64_t*, std::int32_t) {
    event("symbol_feed");
    return 0;
}

#if !defined(PINEFORGE_HPO_FAKE_SELECTED_OMIT) || PINEFORGE_HPO_FAKE_SELECTED_OMIT != 1
PF_API std::uint32_t pf_selected_window_version() {
    return hit("wrong_selected_version") ? 2 : 1;
}
#endif

#if !defined(PINEFORGE_HPO_FAKE_SELECTED_OMIT) || PINEFORGE_HPO_FAKE_SELECTED_OMIT != 2
PF_API int strategy_set_selected_window_v1(pf_strategy_t strategy,
                                          const pf_selected_window_config_v1* config) {
    auto& state = selected_fake(strategy);
    if (!config) {
        event("clear");
        if (hit("clear_refused"))
            return -3;
        state.selected = false;
        return 0;
    }
    event("select");
    if (const int status = setter_status("selection"))
        return status;
    if (config->struct_size != sizeof(*config) || config->version != 1 ||
        config->start_ms != 1'704'067'320'000LL || config->end_ms != 1'704'067'560'000LL) {
        event("wrong_selection");
        return -1;
    }
    state.selection = *config;
    state.selected = true;
    return 0;
}
#endif

#if !defined(PINEFORGE_HPO_FAKE_SELECTED_OMIT) || PINEFORGE_HPO_FAKE_SELECTED_OMIT != 3
PF_API int strategy_selected_window_counts_v1(pf_strategy_t,
                                             pf_selected_window_counts_v1* out) {
    event("counts");
    if (hit("counts_minus2"))
        return -2;
    if (out->struct_size != sizeof(*out) || out->version != 1)
        return -1;
    const auto observation = fake_observation();
    *out = {sizeof(*out), 1, observation.run_generation, observation.attempt_serial,
            observation.attempt_generation, 6, 3, 2, 1, 4, 2};
    if (hit("counts_header_size"))
        --out->struct_size;
    if (hit("counts_header_version"))
        out->version = 2;
    if (hit("run_generation_mismatch"))
        ++out->run_generation;
    if (hit("attempt_serial_mismatch"))
        ++out->attempt_serial;
    if (hit("attempt_generation_mismatch"))
        ++out->attempt_generation;
    if (hit("fed_input_wrong"))
        ++out->fed_input_bars;
    if (hit("fed_script_wrong"))
        ++out->fed_script_bars;
    if (hit("preroll_input_wrong"))
        ++out->preroll_input_bars;
    if (hit("preroll_script_wrong"))
        ++out->preroll_script_bars;
    if (hit("window_input_wrong"))
        ++out->window_input_bars;
    if (hit("window_script_wrong"))
        ++out->window_script_bars;
    return 0;
}
#endif

#if !defined(PINEFORGE_HPO_FAKE_SELECTED_OMIT) || PINEFORGE_HPO_FAKE_SELECTED_OMIT != 4
PF_API std::uint32_t pf_execution_observer_version() {
    return hit("wrong_observer_version") ? 2 : 1;
}
#endif

#if !defined(PINEFORGE_HPO_FAKE_SELECTED_OMIT) || PINEFORGE_HPO_FAKE_SELECTED_OMIT != 5
PF_API int strategy_set_execution_observer_v1(pf_strategy_t strategy,
                                             const pf_execution_observer_v1* observer) {
    auto& state = selected_fake(strategy);
    if (!observer) {
        event("remove");
        if (hit("remove_refused"))
            return -3;
        state.observer = {};
        return 0;
    }
    event("observe");
    if (const int status = setter_status("observer"))
        return status;
    if (observer->struct_size != sizeof(*observer) || observer->version != 1 ||
        !observer->before_results || !observer->context)
        return -1;
    state.observer = *observer;
    return 0;
}
#endif

#if !defined(PINEFORGE_HPO_FAKE_SELECTED_OMIT) || PINEFORGE_HPO_FAKE_SELECTED_OMIT != 6
PF_API int strategy_execution_observation_v1(pf_strategy_t, pf_execution_observation_v1* out) {
    event("observation");
    if (hit("observation_refused"))
        return -1;
    if (out->struct_size != sizeof(*out) || out->version != 1)
        return -1;
    *out = fake_observation();
    if (hit("observation_header_size"))
        --out->struct_size;
    if (hit("observation_header_version"))
        out->version = 2;
    return 0;
}
#endif

#if !defined(PINEFORGE_HPO_FAKE_SELECTED_NO_VERSION)
PF_API pf_version_t pf_version_get() {
    if (hit("engine_revision_old"))
        return {1, 6, 1, "fixture"};
    if (hit("engine_revision_rc"))
        return {1, 6, 2, "fixture"};
    if (hit("engine_revision_rc_above_floor"))
        return {1, 7, 0, "fixture"};
    return {1, 7, hit("numeric_version_mismatch") ? 1 : 0, "fixture"};
}
PF_API const char* pf_version_string() {
    if (hit("engine_string_missing"))
        return nullptr;
    if (hit("engine_revision_old"))
        return "1.6.1";
    if (hit("engine_revision_rc"))
        return "1.6.2-rc.1";
    if (hit("engine_revision_rc_above_floor"))
        return "1.7.0-rc.1";
    if (hit("engine_string_invalid"))
        return "1.7.0-malformed";
    return "1.7.0";
}
#endif

#if !defined(PINEFORGE_HPO_FAKE_SELECTED_NO_SETTINGS)
PF_API int strategy_get_effective_settings(pf_strategy_t, char* json, std::size_t capacity,
                                           std::size_t* required, char*, std::size_t) {
    const std::string text = hit("settings_bad_json") ? "{bad}" :
        hit("settings_bad_version") ? "{\"version\":2}" :
        "{\"version\":1,\"inputs\":[{\"key\":\"Length\",\"value\":\"7\"}]}";
    *required = text.size() + 1;
    if (!json) {
        event("settings_query");
        if (hit("settings_huge"))
            *required = 16 * 1024 * 1024 + 1;
        if (hit("settings_zero"))
            *required = 0;
        return hit("settings_query_status") ? PF_SETTINGS_OK : PF_SETTINGS_BUFFER_TOO_SMALL;
    }
    event("settings_read");
    if (hit("settings_read_status"))
        return PF_SETTINGS_BUFFER_TOO_SMALL;
    if (capacity < *required)
        return PF_SETTINGS_BUFFER_TOO_SMALL;
    std::memcpy(json, text.c_str(), *required);
    if (hit("settings_no_nul"))
        json[*required - 1] = 'x';
    if (hit("settings_size_changed"))
        *required = capacity + 1;
    return PF_SETTINGS_OK;
}
#endif

PF_API void run_backtest_full(pf_strategy_t strategy, pf_bar_t* bars, int count,
                              const char* input, const char* script, int magnifier, int samples,
                              pf_magnifier_distribution_t distribution, pf_report_t* report) {
    auto& state = selected_fake(strategy);
    event("run");
    selected_bars = bars;
    if (state.ran || !state.selected || !state.observer.before_results || count != 6 ||
        std::strcmp(input, "1") != 0 || std::strcmp(script, "2") != 0 || magnifier != 0 ||
        samples != 4 || distribution != PF_MAGNIFIER_ENDPOINTS) {
        state.error = "selected execution configuration was not forwarded";
        state.code = "strategy_runtime_error";
        event("bad_run");
        return;
    }
    state.ran = true;
    report->total_trades = 1;
    report->trades_len = 1;
    report->trades = new pf_trade_t[1]{};
    report->trades[0].pnl = 12.5;
    report->trades[0].open_at_end = 1;
    report->net_profit = 12.5;
    report->metrics.all.net_profit = 12.5;
    report->input_bars_processed = hit("processed_input_wrong") ? 4 : 6;
    report->script_bars_processed = hit("processed_script_wrong") ? 2 : 3;
    report->equity_curve_len = hit("curve_short") ? 2 : 3;
    report->equity_curve = new pf_equity_point_t[3]{};
    report->equity_curve[0].equity = 1000;
    report->equity_curve[1].equity = 1005;
    report->equity_curve[2].equity = 1012.5;
    event("allocate_report");
    if (!hit("never_callback")) {
        dispatch_boundary(state);
        if (hit("duplicate_callback"))
            dispatch_boundary(state);
    }
    if (hit("last_error_text"))
        state.error = "post-return engine error";
    if (hit("last_error_code"))
        state.code = "strategy_runtime_error";
    if (hit("last_error_status"))
        state.status = 1;
    if (hit("unknown_run_status"))
        state.status = 99;
    if (hit("poison_report"))
        report->trades_len = -1;  // A premature snapshot raises hpo_report_invalid instead.
}

PF_API const char* strategy_get_last_error(pf_strategy_t strategy) {
    return selected_fake(strategy).error.c_str();
}
PF_API const char* strategy_get_last_error_code(pf_strategy_t strategy) {
    return selected_fake(strategy).code.c_str();
}
PF_API const char* strategy_get_last_error_args(pf_strategy_t) { return "{}"; }
PF_API int strategy_last_run_status(pf_strategy_t strategy) {
    return selected_fake(strategy).status;
}
PF_API void report_free(pf_report_t* report) {
    event("free_report");
    delete[] report->trades;
    delete[] report->equity_curve;
    *report = {};
    // Exactly one selected handle is in flight; the event order is checked by the test.
}
PF_API int pf_abi_version() { return PF_ABI_VERSION; }

}  // extern "C"

#else

#include <pineforge/hpo/error.hpp>
#include <pineforge/hpo/selected_evaluation.hpp>

#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <dlfcn.h>
#include <filesystem>
#include <functional>
#include <iostream>
#include <limits>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <variant>
#include <vector>

namespace {

using namespace pineforge::hpo;

void require(bool value, const std::string& message) {
    if (!value)
        throw std::runtime_error(message);
}

void check_plugin_path(const char* value) {
    const auto refuse_named = [](const std::filesystem::path& path) {
        std::vector<std::string> parts;
        for (const auto& part : path)
            parts.push_back(part.string());
        for (std::size_t index = 0; index < parts.size(); ++index) {
            const auto& part = parts[index];
            const bool environment = part.size() >= 4 && part.substr(part.size() - 4) == ".env";
            const bool private_config = part == ".config" && index + 1 < parts.size() &&
                (parts[index + 1] == "pineforge" ||
                 (parts[index + 1] == "zsh" && index + 2 < parts.size() &&
                  parts[index + 2] == "keys"));
            require(part != ".ssh" && part != ".aws" && part != ".codex" &&
                        !environment && !private_config,
                    "refused sensitive plugin path by name: " + path.string());
        }
    };
    const auto supplied = std::filesystem::absolute(value);
    refuse_named(supplied);
    refuse_named(std::filesystem::canonical(supplied));
}

class Fixture final {
public:
    explicit Fixture(const char* path) : library_(::dlopen(path, RTLD_NOW | RTLD_LOCAL)) {
        require(library_ != nullptr, "cannot open selected fixture");
        reset = symbol<void (*)(const char*)>("fake_selected_reset");
        counter = symbol<std::int64_t (*)(const char*)>("fake_selected_counter");
        events = symbol<const char* (*)()>("fake_selected_events");
        bars = symbol<const pf_bar_t* (*)()>("fake_selected_bars");
    }
    ~Fixture() { ::dlclose(library_); }
    Fixture(const Fixture&) = delete;
    Fixture& operator=(const Fixture&) = delete;

    void (*reset)(const char*) = nullptr;
    std::int64_t (*counter)(const char*) = nullptr;
    const char* (*events)() = nullptr;
    const pf_bar_t* (*bars)() = nullptr;

private:
    template <typename Function>
    Function symbol(const char* name) {
        ::dlerror();
        void* const value = ::dlsym(library_, name);
        require(value != nullptr && ::dlerror() == nullptr,
                std::string("missing fixture: ") + name);
        return reinterpret_cast<Function>(value);
    }
    void* library_;
};

std::string expect_failure(const std::function<void()>& work, const std::string& code,
                           const std::string& reason = "",
                           FailureOrigin origin = FailureOrigin::Hpo) {
    try {
        work();
    } catch (const HpoError& error) {
        require(error.code() == code, "wrong code: " + error.code() + " for " + error.what());
        require(error.origin() == origin, "wrong failure origin");
        if (!reason.empty()) {
            const auto found = error.args().find("reason");
            require(found != error.args().end(), "missing typed reason");
            const auto* actual = std::get_if<std::string>(&found->second.value());
            require(actual && *actual == reason, "wrong typed reason");
        }
        return error.what();
    }
    throw std::runtime_error("false pass: work returned success instead of " + code + "/" + reason);
}

BacktestConfiguration configuration() {
    BacktestConfiguration result;
    result.input_timeframe = "1";
    result.script_timeframe = "2";
    result.chart_timezone = "UTC";
    // The helper must force both captures on even when the ordinary hot-loop flags are off.
    result.capture_equity_curve = false;
    result.capture_trades = false;
    SymbolInfo info;
    info.mincontract = 0.5;
    info.mintick = 0.25;
    info.pointvalue = 2;
    info.timezone = "UTC";
    info.session = "24x7";
    result.symbol_info = info;
    return result;
}

std::array<pf_bar_t, 8> input_bars() {
    std::array<pf_bar_t, 8> result{};
    for (std::size_t index = 0; index < result.size(); ++index) {
        auto& bar = result[index];
        bar.open = 100;
        bar.high = 102;
        bar.low = 98;
        bar.close = 101;
        bar.volume = 10;
        bar.timestamp = 1'704'067'140'000LL + static_cast<std::int64_t>(index) * 60'000;
    }
    return result;
}

SelectedEvaluationResult evaluate(const StrategyPlugin& plugin,
                                   const std::array<pf_bar_t, 8>& bars,
                                   const BacktestConfiguration& config = configuration()) {
    // Six retained rows start at the second row of a larger immutable owner.
    return SelectedEvaluation::evaluate(plugin, bars.data() + 1, 6,
        1'704'067'320'000LL, 1'704'067'560'000LL, {6, 3, 2, 1, 4, 2},
        {{"Length", "7"}}, {{"initial_capital", "1000"}}, config, std::string("1.7.0"));
}

void require_cleanup(Fixture& fixture, bool ran = true) {
    // A false pass would leak a handle/report or hide missing remove/clear calls.
    require(fixture.counter("create") == 1 && fixture.counter("destroy") == 1,
            "evaluation did not create/destroy exactly one handle");
    require(fixture.counter("run") == (ran ? 1 : 0), "unexpected run count");
    require(fixture.counter("bad_free_order") == 0 && fixture.counter("free_report") == 1,
            "report release was missing, repeated, or after the handle");
    require(fixture.counter("selection_at_destroy") == (fixture.counter("clear_refused") ? 1 : 0),
            "successful cleanup left pending selection installed");
    require(fixture.counter("context_alive_at_destroy") ==
                (fixture.counter("remove_refused") ? 1 : 0),
            "successful cleanup left an observer installed");
    require(fixture.counter("remove") == 1 && fixture.counter("clear") == 1,
            "observer/selection cleanup was skipped");
    const std::string log = fixture.events();
    require(log.find("remove,clear") != std::string::npos ||
                log.find("remove,remove_refused,clear") != std::string::npos,
            "observer must be removed before selection is cleared");
    const auto freed = log.find("free_report");
    const auto destroyed = log.find("destroy");
    require(freed != std::string::npos && destroyed != std::string::npos && freed < destroyed,
            "report was not freed before its handle");
    require(fixture.counter("bad_run") == 0, "fixture refused forwarded configuration");
}

void require_not_started(Fixture& fixture, const std::string& row) {
    require(fixture.counter("create") == 0, row + ": refusal created a handle");
    // Every fake setter and work entry records an event, including wrong setter arguments.
    const std::string log = fixture.events();
    require(log.empty(), row + ": refusal called a setter or started work: " + log);
}

void test_version_parser() {
    // Literal outcomes catch lexical comparisons, parity-era admission and build-header metadata.
    for (const auto& row : std::vector<std::pair<pf_version_t, std::string>>{
             {{1, 6, 2, ""}, "1.6.2"}, {{1, 6, 12, ""}, "1.6.12"}, {{1, 7, 0, ""}, "1.7.0"},
             {{1, 10, 0, ""}, "1.10.0"}, {{1, 6, 2, ""}, "1.6.2-dirty"},
             {{1, 7, 0, ""}, "1.7.0-dirty"}, {{1, 7, 0, "abc"}, "1.7.0-2-gabc"},
             {{1, 7, 0, "abc"}, "1.7.0-2-gabc-dirty"}}) {
        const auto result = validate_selected_engine_version(row.first, row.second, row.second);
        require(result.major == 1 && result.minor == static_cast<std::uint32_t>(row.first.minor) &&
                    result.patch == static_cast<std::uint32_t>(row.first.patch) &&
                    result.full == row.second && result.commit_sha == row.first.commit_sha,
                "accepted version was not parsed numerically");
    }
    const auto refused = [](pf_version_t numbers, const std::string& full, const char* reason) {
        expect_failure([&] { (void)validate_selected_engine_version(numbers, full); },
                       "hpo_walkforward_unavailable", reason);
    };
    refused({1, 6, 1, ""}, "1.6.1", "engine_revision");
    refused({1, 6, 1, ""}, "1.6.1-dirty", "engine_revision");
    refused({1, 5, 999, ""}, "1.5.999", "engine_revision");
    refused({1, 6, 2, ""}, "1.6.2-rc.1", "engine_revision");
    refused({1, 6, 2, ""}, "1.6.2-rc.1-dirty", "engine_revision");
    refused({1, 7, 0, ""}, "1.7.0-rc.1", "engine_revision");
    refused({1, 7, 0, ""}, "1.7.0-rc.1-dirty", "engine_revision");
    refused({1, 7, 0, ""}, "1.7.0-rc.1-2-gabc", "engine_revision");
    refused({1, 7, 0, ""}, "1.7.0-rc.1-2-gabc-dirty", "engine_revision");
    refused({2, 0, 0, ""}, "2.0.0", "engine_revision");
    refused({1, 7, 1, ""}, "1.7.0", "engine_version_invalid");
    refused({-1, 7, 0, ""}, "1.7.0", "engine_version_invalid");
    for (const char* text : {"", "v1.7.0", "1.07.0", "1.7", "1.7.0extra", "1.7.0-rc.",
                             "1.7.0-1-gZZZ", "1.7.0-gZZZ", "1.7.0-dirty-dirty",
                             "1.7.0-2-gabc-dirty-dirty", "999999999999.7.0"})
        refused({1, 7, 0, ""}, text, "engine_version_invalid");
    expect_failure([] {
        (void)validate_selected_engine_version(pf_version_t{1, 7, 0, ""}, "1.7.0", "1.6.2");
    }, "hpo_walkforward_unavailable", "engine_identity");
    expect_failure([] {
        (void)validate_selected_engine_version(
            pf_version_t{1, 7, 0, ""}, "1.7.0-dirty", "1.7.0");
    }, "hpo_walkforward_unavailable", "engine_identity");
    expect_failure([] { (void)validate_selected_engine_version(std::nullopt, "1.7.0"); },
                   "hpo_walkforward_unavailable", "engine_version_missing");
    expect_failure([] {
        (void)validate_selected_engine_version(pf_version_t{1, 7, 0, ""}, std::nullopt);
    }, "hpo_walkforward_unavailable", "engine_version_missing");
}

void test_default_fake(const char* path) {
    // A false pass would grant the default fake selected capability or hide omissions.
    StrategyPlugin plugin(path);
    require(!plugin.selected_window_available(), "default fake unexpectedly exports selected ABI");
    const auto message = expect_failure([&] { plugin.require_selected_window("wrong"); },
        "hpo_walkforward_unavailable", "missing_pf_selected_window_version");
    std::size_t position = 0;
    for (const char* name : {"pf_selected_window_version", "strategy_set_selected_window_v1",
                            "strategy_selected_window_counts_v1", "pf_execution_observer_version",
                            "strategy_set_execution_observer_v1",
                            "strategy_execution_observation_v1"}) {
        const auto found = message.find(name, position);
        require(found != std::string::npos, "missing diagnostic export or wrong protocol order");
        position = found + std::string(name).size();
    }
    const auto bars = input_bars();
    expect_failure([&] { (void)evaluate(plugin, bars); }, "hpo_walkforward_unavailable",
                   "missing_pf_selected_window_version");
    const auto handle = plugin.create_strategy();
    require(!plugin.effective_settings(handle), "absent getter became a settings attestation");
    plugin.free_strategy(handle);
}

void test_missing_export(const char* path, const char* reason) {
    // Each separately linked omission must be exercised; an all-missing fake cannot prove this.
    Fixture fixture(path);
    fixture.reset("");
    StrategyPlugin plugin(path);
    expect_failure([&] { plugin.require_selected_window("wrong"); },
                   "hpo_walkforward_unavailable", reason);
    require(fixture.counter("create") == 0, "missing export created a handle");
}

void test_admission(const char* path, const char* no_version) {
    Fixture fixture(path);
    StrategyPlugin plugin(path);
    for (const auto& row : std::vector<std::pair<const char*, const char*>>{
             {"wrong_selected_version", "selected_window_version"},
             {"wrong_observer_version", "execution_observer_version"},
             {"wrong_selected_version,wrong_observer_version", "selected_window_version"},
             {"wrong_observer_version,engine_revision_old", "execution_observer_version"},
             {"engine_revision_old", "engine_revision"},
             {"engine_revision_rc", "engine_revision"},
             {"engine_revision_rc_above_floor", "engine_revision"},
             {"numeric_version_mismatch", "engine_version_invalid"},
             {"engine_string_invalid", "engine_version_invalid"},
             {"engine_string_missing", "engine_version_missing"}}) {
        // A false pass would skip the fault, reorder admission or create a handle too early.
        fixture.reset(row.first);
        expect_failure([&] { plugin.require_selected_window(); },
                       "hpo_walkforward_unavailable", row.second);
        require(fixture.counter("create") == 0, "admission created a handle");
        const std::string faults = row.first;
        const std::string first = faults.substr(0, faults.find(','));
        require(fixture.counter(first.c_str()) != 0, "admission fault was never injected");
        if (faults == "wrong_selected_version,wrong_observer_version")
            require(fixture.counter("wrong_observer_version") == 0,
                    "observer version was queried before the selected ABI refusal");
        if (faults == "wrong_observer_version,engine_revision_old")
            require(fixture.counter("engine_revision_old") == 0,
                    "engine revision was queried before the observer ABI refusal");
    }
    fixture.reset("");
    expect_failure([&] { plugin.require_selected_window("1.6.2"); },
                   "hpo_walkforward_unavailable", "engine_identity");
    StrategyPlugin missing(no_version);
    expect_failure([&] { missing.require_selected_window(); },
                   "hpo_walkforward_unavailable", "engine_version_missing");
}

void test_success(const char* path) {
    Fixture fixture(path);
    StrategyPlugin original(path);
    StrategyPlugin moved(std::move(original));
    StrategyPlugin plugin(path);
    plugin = std::move(moved);
    require(plugin.selected_window_available(), "move lost optional protocol bindings");
    const auto bars = input_bars();
    for (int repeat = 0; repeat != 2; ++repeat) {
        fixture.reset("");
        const auto result = evaluate(plugin, bars);
        // A false pass would ignore setters, omit full capture, rebase metrics, or reuse a handle.
        require(result.report.net_profit == 12.5 && result.report.metrics.all.net_profit == 12.5 &&
                    result.report.trades.size() == 1 && result.report.trades[0].pnl == 12.5 &&
                    result.report.trades[0].open_at_end == 1 &&
                    result.report.equity_curve.size() == 3 &&
                    result.report.equity_curve[2].equity == 1012.5,
                "selected snapshot is not the detached complete engine report");
        require(result.counts.run_generation == 41 && result.observation.attempt_serial == 73 &&
                    result.boundary.run_generation == 41 && result.boundary.attempt_serial == 73,
                "result identities were not retained");
        require(result.effective_settings && result.effective_settings->find("\"7\"") !=
                    std::string::npos, "effective settings were not read");
        require(fixture.bars() == bars.data() + 1, "retained OHLCV was copied or offset lost");
        require(fixture.counter("valid_receipt") == 1 && fixture.counter("callback_dispatch") == 1,
                "callback did not replace the sentinel with a valid non-exporting receipt");
        const std::string log = fixture.events();
        const std::string prefix =
            "create,timezone,override,input,qty_step,mincontract,mintick,pointvalue,"
            "symbol_timezone,session,settings_query,settings_read,select,observe,run";
        require(log.find(prefix) == 0, "setter/receipt/selection order drifted");
        require_cleanup(fixture);
    }  // The caller releases each returned window before starting the next.

    fixture.reset("");
    auto with_feeds = configuration();
    SymbolData symbol;
    symbol.symbol = "TEST:OTHER";
    symbol.facts.push_back({"timezone", std::string("UTC")});
    SymbolFeed feed;
    feed.timeframe = "1";
    feed.bars.push_back(bars[1]);
    feed.close_ms.push_back(bars[1].timestamp + 60'000);
    symbol.feeds.push_back(std::move(feed));
    with_feeds.symbol_feeds = std::make_shared<const SymbolFeeds>(SymbolFeeds{std::move(symbol)});
    const auto result = evaluate(plugin, bars, with_feeds);
    const std::string feed_log = fixture.events();
    require(result.report.net_profit == 12.5 && fixture.counter("symbol_fact") == 1 &&
                fixture.counter("symbol_feed") == 1 &&
                feed_log.find("session,symbol_fact,symbol_feed,settings_query") !=
                    std::string::npos,
            "false pass: symbol feeds were skipped or installed after the settings receipt");
    require_cleanup(fixture);
}

void test_request_validation(const char* path) {
    Fixture fixture(path);
    StrategyPlugin plugin(path);
    const auto bars = input_bars();
    struct Request {
        const pf_bar_t* pointer;
        std::size_t count;
        std::int64_t start_ms;
        std::int64_t end_ms;
        SelectedExpectedCounts expected;
        BacktestConfiguration config;
    };
    const Request valid{bars.data() + 1, 6, 1'704'067'320'000LL, 1'704'067'560'000LL,
                        {6, 3, 2, 1, 4, 2}, configuration()};
    const auto run_request = [&](const Request& request) {
        return SelectedEvaluation::evaluate(
            plugin, request.pointer, request.count, request.start_ms, request.end_ms,
            request.expected, {{"Length", "7"}}, {{"initial_capital", "1000"}},
            request.config, std::string("1.7.0"));
    };
    // The same baseline used by every invalid row must first complete real helper execution.
    fixture.reset("");
    (void)run_request(valid);
    require_cleanup(fixture);

    struct InvalidRequest {
        const char* name;
        const char* diagnostic;
        std::function<void(Request&)> change;
    };
    const std::vector<InvalidRequest> rows{
        {"null_bars", "invalid retained input pointer/count",
         [](Request& request) { request.pointer = nullptr; }},
        {"zero_count", "invalid retained input pointer/count",
         [](Request& request) {
             request.count = 0;
             request.expected = {};
         }},
        {"equal_bounds", "invalid planned selected bounds",
         [](Request& request) { request.start_ms = request.end_ms; }},
        {"reversed_bounds", "invalid planned selected bounds",
         [](Request& request) { request.start_ms = request.end_ms + 1; }},
        {"start_below_json_safe", "invalid planned selected bounds",
         [](Request& request) { request.start_ms = -9'007'199'254'740'992LL; }},
        {"end_above_json_safe", "invalid planned selected bounds",
         [](Request& request) { request.end_ms = 9'007'199'254'740'992LL; }},
        {"fed_input_not_count", "inconsistent planned selected counts",
         [](Request& request) { request.expected = {7, 3, 2, 1, 5, 2}; }},
        {"preroll_input_above_fed", "inconsistent planned selected counts",
         [](Request& request) {
             // Match the unsigned subtraction so removing this clause reaches handle creation.
             request.expected = {6, 3, 7, 1, std::numeric_limits<std::uint64_t>::max(), 2};
         }},
        {"window_input_difference", "inconsistent planned selected counts",
         [](Request& request) { request.expected = {6, 3, 2, 1, 5, 2}; }},
        {"preroll_script_above_fed", "inconsistent planned selected counts",
         [](Request& request) {
             // 3 - UINT64_MAX wraps to 4, below the later INT64_MAX guard.
             request.expected = {6, 3, 2, std::numeric_limits<std::uint64_t>::max(), 4, 4};
         }},
        {"window_script_difference", "inconsistent planned selected counts",
         [](Request& request) { request.expected = {6, 3, 2, 1, 4, 3}; }},
        {"fed_script_above_count", "inconsistent planned selected counts",
         [](Request& request) { request.expected = {6, 7, 2, 1, 4, 6}; }},
        {"zero_magnifier_samples", "magnifier_samples must be positive",
         [](Request& request) { request.config.magnifier_samples = 0; }},
        {"negative_magnifier_samples", "magnifier_samples must be positive",
         [](Request& request) { request.config.magnifier_samples = -1; }},
        {"invalid_magnifier_distribution", "invalid magnifier distribution",
         [](Request& request) {
             // 6 is representable by this enum's range but is not a named distribution.
             request.config.magnifier_distribution = static_cast<pf_magnifier_distribution_t>(6);
         }}};
    for (const auto& row : rows) {
        fixture.reset("");
        auto request = valid;
        row.change(request);
        const auto message = expect_failure([&] { (void)run_request(request); },
                                            "hpo_invariant", "", FailureOrigin::Hpo);
        require(message == std::string("selected evaluation: ") + row.diagnostic,
                std::string(row.name) + ": an unrelated invariant hid the missing clause");
        require_not_started(fixture, row.name);
    }
    // No row claims the dominated window_script_bars >= INT64_MAX clause: all earlier
    // predicates being false implies window_script_bars <= fed_script_bars <= count <= INT_MAX.
}

void test_return_statistics(const char* path) {
    Fixture fixture(path);
    StrategyPlugin plugin(path);
    const auto bars = input_bars();
    std::optional<std::string> identity_refusal;
    try {
        require_return_stats_identity();
    } catch (const HpoError& error) {
        require(error.code() == "hpo_toolchain_unavailable" && error.origin() == FailureOrigin::Hpo,
                "unexpected return-statistics identity failure");
        const auto reason = error.args().find("reason");
        require(reason != error.args().end(), "identity refusal has no typed reason");
        const auto* value = std::get_if<std::string>(&reason->second.value());
        require(value && *value == "native_runner", "unexpected identity refusal reason");
        identity_refusal = error.what();
    }
    const bool contraction_free = !identity_refusal && return_stats_contraction_free();
    struct StatsRequest {
        const char* name;
        bool bar;
        bool monthly;
        std::uint32_t blocks;
    };
    for (const auto& row : std::vector<StatsRequest>{
             {"bar", true, false, 0}, {"monthly", false, true, 0},
             {"pbo_blocks", false, false, 2}}) {
        fixture.reset("");
        auto config = configuration();
        config.return_stats_bar = row.bar;
        config.return_stats_monthly = row.monthly;
        config.pbo_blocks = row.blocks;
        // Use the same build-admission functions as the helper; every branch asserts its result.
        if (identity_refusal) {
            const auto message = expect_failure([&] { (void)evaluate(plugin, bars, config); },
                "hpo_toolchain_unavailable", "native_runner", FailureOrigin::Hpo);
            require(message == *identity_refusal, "helper did not propagate the identity refusal");
            require_not_started(fixture, row.name);
            std::cout << "selected return statistics " << row.name << ": identity refused\n";
        } else if (!contraction_free) {
            const auto message = expect_failure([&] { (void)evaluate(plugin, bars, config); },
                                                "hpo_invariant", "", FailureOrigin::Hpo);
            require(message == "selected evaluation: return statistics need a build without "
                               "floating-point contraction",
                    "an unrelated invariant hid the contraction refusal");
            require_not_started(fixture, row.name);
            std::cout << "selected return statistics " << row.name << ": contraction refused\n";
        } else {
            const auto result = evaluate(plugin, bars, config);
            require(result.report.return_bar.computed == row.bar &&
                        result.report.return_monthly.computed == row.monthly,
                    std::string(row.name) + ": requested return series was not captured");
            require(result.counts.window_script_bars == 2 && result.report.equity_curve.size() == 3,
                    "return statistics did not use the selected three-point curve");
            if (row.blocks != 0) {
                // K=2 intervals, S=2: the smallest half holds 1 interval, below the required 2.
                // A skipped reduction leaves NotRequested, so it cannot satisfy this assertion.
                const auto& layout = result.report.return_block_layout;
                const auto& moments = result.report.return_blocks;
                require(!layout.usable && layout.blocks == 0 && layout.intervals == 0 &&
                            layout.n_b.empty() && layout.boundary_time_ms.empty() &&
                            moments.status == ReturnBlocksStatus::UnusableLayout &&
                            moments.count.empty() && moments.sum.empty() && moments.m2.empty(),
                        "short selected curve must compute UnusableLayout with no block arrays");
            } else {
                require(result.report.return_blocks.status == ReturnBlocksStatus::NotRequested,
                        "unrequested blocks were reduced");
            }
            require_cleanup(fixture);
            std::cout << "selected return statistics " << row.name << ": accepted\n";
        }
    }
}

void test_faults(const char* path) {
    Fixture fixture(path);
    StrategyPlugin plugin(path);
    const auto bars = input_bars();
    struct Fault {
        const char* name;
        const char* code;
        const char* reason;
        FailureOrigin origin;
        bool ran;
    };
    const std::vector<Fault> setter_faults{
        {"selection_-1", "hpo_invariant", "", FailureOrigin::Hpo, false},
        {"selection_-2", "hpo_walkforward_unavailable", "selected_window_contract",
         FailureOrigin::Hpo, false},
        {"selection_-3", "hpo_invariant", "", FailureOrigin::Hpo, false},
        {"selection_-4", "out_of_memory", "", FailureOrigin::Engine, false},
        {"selection_-99", "hpo_invariant", "", FailureOrigin::Hpo, false},
        {"observer_-1", "hpo_invariant", "", FailureOrigin::Hpo, false},
        {"observer_-2", "hpo_walkforward_unavailable", "execution_observer_contract",
         FailureOrigin::Hpo, false},
        {"observer_-3", "hpo_invariant", "", FailureOrigin::Hpo, false},
        {"observer_-4", "out_of_memory", "", FailureOrigin::Engine, false},
        {"observer_-99", "hpo_invariant", "", FailureOrigin::Hpo, false},
        {"setting_error", "setting_rejected", "", FailureOrigin::Engine, false}};
    for (const auto& fault : setter_faults) {
        fixture.reset(fault.name);
        int scored = 0;
        expect_failure([&] { (void)evaluate(plugin, bars); ++scored; },
                       fault.code, fault.reason, fault.origin);
        require(scored == 0 && fixture.counter(fault.name) == 1, "setter fault not exercised");
        require_cleanup(fixture, fault.ran);
    }
    for (const char* name : {
             "counts_minus2", "counts_header_size", "counts_header_version",
             "never_callback", "duplicate_callback", "callback_throws", "callback_receipt_mutated",
             "callback_boundary_zero", "observation_refused", "observation_header_size",
             "observation_header_version", "run_generation_mismatch", "attempt_serial_mismatch",
             "attempt_generation_mismatch", "zero_run_generation", "zero_attempt_serial",
             "zero_attempt_generation", "boundary_generation_mismatch", "boundary_serial_mismatch",
             "boundary_not_delivered", "boundary_delivered_twice", "phase_wrong", "outcome_wrong",
             "fault_set", "fed_input_wrong", "fed_script_wrong", "preroll_input_wrong",
             "preroll_script_wrong", "window_input_wrong", "window_script_wrong", "curve_short",
             "processed_input_wrong", "processed_script_wrong", "unknown_run_status"}) {
        // Poisoning report capture makes a premature copy a different typed failure, never a pass.
        fixture.reset((std::string(name) + ",poison_report").c_str());
        int scored = 0;
        const auto message =
            expect_failure([&] { (void)evaluate(plugin, bars); ++scored; }, "hpo_invariant");
        require(scored == 0 && fixture.counter(name) > 0 && fixture.counter("poison_report") == 1,
                std::string("fault was not injected or was scored: ") + name);
        if (std::string(name).find("zero_") == 0)
            require(message.find("identities differ or are zero") != std::string::npos,
                    "a later invariant hid a missing nonzero identity check");
        if (std::string(name) == "duplicate_callback" || std::string(name) == "callback_throws" ||
            std::string(name) == "callback_receipt_mutated" ||
            std::string(name) == "callback_boundary_zero") {
            require(fixture.counter("callback_refused") == 1,
                    "host callback did not catch failure");
            require(fixture.counter("observation") == 0 && fixture.counter("counts") == 0,
                    "callback failure reached result getters");
        }
        require_cleanup(fixture);
    }
    for (const auto& row : std::vector<std::pair<const char*, const char*>>{
             {"last_error_text", ""}, {"last_error_code", "strategy_runtime_error"},
             {"last_error_status", ""}}) {
        fixture.reset((std::string(row.first) + ",poison_report").c_str());
        // Engine failures must win over invalid reports and missing callbacks.
        expect_failure([&] { (void)evaluate(plugin, bars); },
                       row.second, "", FailureOrigin::Engine);
        require(fixture.counter(row.first) == 1 && fixture.counter("observation") == 0,
                "post-return engine error was not checked first");
        require_cleanup(fixture);
    }
    fixture.reset("last_error_code,never_callback,poison_report");
    expect_failure([&] { (void)evaluate(plugin, bars); }, "strategy_runtime_error", "",
                   FailureOrigin::Engine);
    require_cleanup(fixture);
    fixture.reset("poison_report");
    expect_failure([&] { (void)evaluate(plugin, bars); }, "hpo_report_invalid");
    require(fixture.counter("poison_report") == 1, "snapshot-failure fault was not injected");
    require_cleanup(fixture);
}

void test_cleanup_failures(const char* path) {
    Fixture fixture(path);
    StrategyPlugin plugin(path);
    const auto bars = input_bars();
    for (const char* fault : {"remove_refused", "clear_refused"}) {
        // A false pass would return a score after refusal or reverse report/handle teardown.
        fixture.reset(fault);
        expect_failure([&] { (void)evaluate(plugin, bars); }, "hpo_invariant");
        require(fixture.counter(fault) == 1, "cleanup refusal not injected");
        if (std::string(fault) == "remove_refused")
            require(fixture.counter("context_alive_at_destroy") == 1,
                    "borrowed context did not survive failed removal");
        require_cleanup(fixture);
    }
    fixture.reset("last_error_code,remove_refused,clear_refused,poison_report");
    expect_failure([&] { (void)evaluate(plugin, bars); }, "strategy_runtime_error", "",
                   FailureOrigin::Engine);
    require_cleanup(fixture);
    fixture.reset("callback_throws,remove_refused,clear_refused,poison_report");
    const auto message = expect_failure([&] { (void)evaluate(plugin, bars); }, "hpo_invariant");
    require(message.find("boundary or receipt") != std::string::npos,
            "cleanup or engine failure hid the retained callback exception");
    require_cleanup(fixture);
}

void test_settings(const char* path, const char* no_settings) {
    Fixture fixture(path);
    StrategyPlugin plugin(path);
    const auto bars = input_bars();
    for (const char* fault : {"settings_huge", "settings_zero", "settings_query_status",
                             "settings_read_status", "settings_bad_json", "settings_bad_version",
                             "settings_no_nul", "settings_size_changed"}) {
        // A false pass would attest malformed/oversized receipts or skip the two-call protocol.
        fixture.reset(fault);
        const auto result = evaluate(plugin, bars);
        require(!result.effective_settings && fixture.counter(fault) > 0,
                "unusable settings receipt became an attestation or fault was not injected");
        if (std::string(fault) == "settings_huge" || std::string(fault) == "settings_zero" ||
            std::string(fault) == "settings_query_status")
            require(fixture.counter("settings_read") == 0,
                    "invalid size/status reached allocation");
        require_cleanup(fixture);
    }
    Fixture absent(no_settings);
    absent.reset("");
    StrategyPlugin without(no_settings);
    const auto result = evaluate(without, bars);
    require(!result.effective_settings, "absent settings getter became an attestation");
    require_cleanup(absent);
}

}  // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 11,
                "expected default, selected, six omissions, no-version and no-settings");
        for (int index = 1; index < argc; ++index)
            std::cout << "will load plugin: " << argv[index] << '\n';
        std::cout.flush();
        for (int index = 1; index < argc; ++index)
            check_plugin_path(argv[index]);
        test_version_parser();
        test_default_fake(argv[1]);
        const std::array<const char*, 6> reasons{{
            "missing_pf_selected_window_version", "missing_strategy_set_selected_window_v1",
            "missing_strategy_selected_window_counts_v1", "missing_pf_execution_observer_version",
            "missing_strategy_set_execution_observer_v1",
            "missing_strategy_execution_observation_v1"}};
        for (std::size_t index = 0; index < reasons.size(); ++index)
            test_missing_export(argv[3 + index], reasons[index]);
        test_admission(argv[2], argv[9]);
        test_success(argv[2]);
        test_request_validation(argv[2]);
        test_return_statistics(argv[2]);
        test_faults(argv[2]);
        test_cleanup_failures(argv[2]);
        test_settings(argv[2], argv[10]);
        std::cout << "selected evaluation tests passed\n";
        return EXIT_SUCCESS;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return EXIT_FAILURE;
    }
}

#endif
