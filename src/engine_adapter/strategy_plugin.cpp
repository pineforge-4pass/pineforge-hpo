#include <pineforge/hpo/error.hpp>
#include <pineforge/hpo/strategy_plugin.hpp>

#include "../core/json.hpp"

#include <array>
#include <charconv>
#include <cmath>
#include <iomanip>
#include <limits>
#include <locale>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#if defined(__unix__) || defined(__APPLE__)
#include <dlfcn.h>
#endif

namespace pineforge {
namespace hpo {
namespace {

#if defined(__unix__) || defined(__APPLE__)

std::string dynamic_loader_error() {
    const char* error = ::dlerror();
    return error != nullptr ? std::string(error) : std::string("unknown dynamic-loader error");
}

template <typename Function>
Function load_required_symbol(void* library, const std::filesystem::path& path, const char* name) {
    ::dlerror();
    void* const symbol = ::dlsym(library, name);
    const char* const error = ::dlerror();
    if (error != nullptr || symbol == nullptr) {
        throw TypedHpoError<std::runtime_error>(
            "hpo_plugin_invalid", {{"reason", "symbols"}},
            "strategy plugin '" + path.string() + "' is missing required symbol '" + name + "': " +
                (error != nullptr ? std::string(error) : std::string("symbol resolved to null")));
    }
    return reinterpret_cast<Function>(symbol);
}

template <typename Function>
Function load_optional_symbol(void* library, const char* name) noexcept {
    ::dlerror();
    void* const symbol = ::dlsym(library, name);
    const char* const error = ::dlerror();
    if (error != nullptr || symbol == nullptr) {
        return nullptr;
    }
    return reinterpret_cast<Function>(symbol);
}

#endif

void reject_embedded_null(const std::string& value, const char* label) {
    if (value.find('\0') != std::string::npos) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "input"}},
            std::string(label) + " contains an embedded NUL byte");
    }
}

void check_settings(const StrategyPlugin& plugin,
                    pf_strategy_t strategy,
                    int status,
                    const char* entrypoint,
                    const char* message) {
    if (status == PF_SETTINGS_OK)
        return;
    const auto code = strategy ? plugin.last_error_code(strategy) : std::nullopt;
    if (code && !code->empty())
        throw EngineError(code, plugin.last_error_args(strategy), message);
    if (status == PF_SETTINGS_INVALID_ARGUMENT)
        throw TypedHpoError<>("setting_rejected", {{"entrypoint", entrypoint}}, message,
                              FailureOrigin::Engine);
    throw TypedHpoError<>(status == PF_SETTINGS_UNSUPPORTED ? "setting_unsupported"
                                                           : "engine_unclassified_error",
                          {}, message, FailureOrigin::Engine);
}

[[noreturn]] void selected_unavailable(const char* reason, const std::string& message) {
    throw TypedHpoError<>("hpo_walkforward_unavailable", {{"reason", reason}}, message);
}

void require_handle(pf_strategy_t strategy) {
    if (!strategy)
        throw TypedHpoError<>("hpo_invariant", {}, "selected execution requires a strategy handle");
}

void check_selected_setter(int status, const char* entrypoint, const char* contract_reason) {
    if (status == 0)
        return;
    const std::string message =
        std::string(entrypoint) + " refused with status " + std::to_string(status);
    if (status == -4)
        throw EngineError(std::string("out_of_memory"), std::string("{}"), message);
    if (status == -2)
        selected_unavailable(contract_reason, message);
    throw TypedHpoError<>("hpo_invariant", {}, message);
}

}  // namespace

EngineVersion validate_selected_engine_version(
    const std::optional<pf_version_t>& numbers,
    const std::optional<std::string>& full,
    const std::optional<std::string>& expected_manifest_version) {
    if (!numbers || !full) {
        selected_unavailable("engine_version_missing",
                             std::string("missing embedded engine metadata:") +
                                 (!numbers ? " pf_version_get" : "") +
                                 (!full ? " pf_version_string" : ""));
    }
    // Match the generator's MAJOR.MINOR.PATCH[-rc.N][-D-gSHA][-dirty] form.
    static const std::regex pattern(
        R"((0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*))"
        R"((-rc\.([1-9][0-9]*))?(-([0-9]+)-g[0-9a-fA-F]+)?(-dirty)?)");
    std::smatch match;
    if (!std::regex_match(*full, match, pattern))
        selected_unavailable("engine_version_invalid", "invalid embedded engine version: " + *full);
    const auto component = [&](std::size_t index) {
        const std::string text = match[index].str();
        std::uint32_t value = 0;
        const auto parsed = std::from_chars(text.data(), text.data() + text.size(), value);
        if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size() ||
            value > static_cast<std::uint32_t>(std::numeric_limits<int>::max()))
            selected_unavailable("engine_version_invalid", "engine version component overflow");
        return value;
    };
    EngineVersion version;
    version.major = component(1);
    version.minor = component(2);
    version.patch = component(3);
    if (numbers->major < 0 || numbers->minor < 0 || numbers->patch < 0 ||
        version.major != static_cast<std::uint32_t>(numbers->major) ||
        version.minor != static_cast<std::uint32_t>(numbers->minor) ||
        version.patch != static_cast<std::uint32_t>(numbers->patch))
        selected_unavailable("engine_version_invalid",
                             "pf_version_get disagrees with pf_version_string");
    if (version.major != 1 || version.minor < 6 ||
        (version.minor == 6 && version.patch < 2) || match[4].matched)
        selected_unavailable("engine_revision",
                             "selected execution requires stable engine 1.x >= 1.6.2: " + *full);
    if (expected_manifest_version && *expected_manifest_version != *full)
        selected_unavailable("engine_identity",
                             "embedded engine version differs from the expected manifest version");
    version.full = *full;
    version.commit_sha = numbers->commit_sha ? numbers->commit_sha : "";
    return version;
}

StrategyPlugin::StrategyPlugin(std::filesystem::path path) : path_(std::move(path)) {
#if defined(__unix__) || defined(__APPLE__)
    if (path_.empty()) {
        throw TypedHpoError<std::invalid_argument>("hpo_plugin_invalid", {{"reason", "path"}},
                                                   "strategy plugin path is empty");
    }
    library_handle_ = ::dlopen(path_.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (library_handle_ == nullptr) {
        throw TypedHpoError<std::runtime_error>(
            "hpo_plugin_invalid", {{"reason", "load"}},
            "cannot load strategy plugin '" + path_.string() + "': " + dynamic_loader_error());
    }

    try {
        strategy_create_ =
            load_required_symbol<StrategyCreateFn>(library_handle_, path_, "strategy_create");
        strategy_free_ =
            load_required_symbol<StrategyFreeFn>(library_handle_, path_, "strategy_free");
        strategy_set_input_ =
            load_required_symbol<StrategySetInputFn>(library_handle_, path_, "strategy_set_input");
        strategy_set_override_ = load_required_symbol<StrategySetOverrideFn>(
            library_handle_, path_, "strategy_set_override");
        strategy_set_chart_timezone_ = load_optional_symbol<StrategySetChartTimezoneFn>(
            library_handle_, "strategy_set_chart_timezone");
        strategy_set_syminfo_mintick_ = load_optional_symbol<StrategySetSymbolDoubleFn>(
            library_handle_, "strategy_set_syminfo_mintick");
        strategy_set_syminfo_pointvalue_ = load_optional_symbol<StrategySetSymbolDoubleFn>(
            library_handle_, "strategy_set_syminfo_pointvalue");
        strategy_set_syminfo_metadata_ = load_optional_symbol<StrategySetSymbolMetadataFn>(
            library_handle_, "strategy_set_syminfo_metadata");
        strategy_set_syminfo_timezone_ = load_optional_symbol<StrategySetSymbolStringFn>(
            library_handle_, "strategy_set_syminfo_timezone");
        strategy_set_syminfo_session_ = load_optional_symbol<StrategySetSymbolStringFn>(
            library_handle_, "strategy_set_syminfo_session");
        strategy_set_symbol_facts_ = load_optional_symbol<StrategySetSymbolFactsFn>(
            library_handle_, "strategy_set_symbol_facts");
        strategy_set_symbol_feed_ = load_optional_symbol<StrategySetSymbolFeedFn>(
            library_handle_, "strategy_set_symbol_feed");
        run_backtest_full_ =
            load_required_symbol<RunBacktestFullFn>(library_handle_, path_, "run_backtest_full");
        strategy_get_last_error_ = load_required_symbol<StrategyGetLastErrorFn>(
            library_handle_, path_, "strategy_get_last_error");
        strategy_get_last_error_code_ = load_optional_symbol<StrategyGetLastErrorFn>(
            library_handle_, "strategy_get_last_error_code");
        strategy_get_last_error_args_ = load_optional_symbol<StrategyGetLastErrorFn>(
            library_handle_, "strategy_get_last_error_args");
        strategy_last_run_status_ = load_optional_symbol<StrategyLastRunStatusFn>(
            library_handle_, "strategy_last_run_status");
        strategy_create_checked_ = load_optional_symbol<StrategyCreateCheckedFn>(
            library_handle_, "strategy_create_checked");
        strategy_set_input_checked_ = load_optional_symbol<StrategySetCheckedFn>(
            library_handle_, "strategy_set_input_checked");
        strategy_set_override_checked_ = load_optional_symbol<StrategySetCheckedFn>(
            library_handle_, "strategy_set_override_checked");
        pf_selected_window_version_ = load_optional_symbol<ProtocolVersionFn>(
            library_handle_, "pf_selected_window_version");
        strategy_set_selected_window_v1_ = load_optional_symbol<SetSelectedWindowFn>(
            library_handle_, "strategy_set_selected_window_v1");
        strategy_selected_window_counts_v1_ = load_optional_symbol<SelectedWindowCountsFn>(
            library_handle_, "strategy_selected_window_counts_v1");
        pf_execution_observer_version_ = load_optional_symbol<ProtocolVersionFn>(
            library_handle_, "pf_execution_observer_version");
        strategy_set_execution_observer_v1_ = load_optional_symbol<SetExecutionObserverFn>(
            library_handle_, "strategy_set_execution_observer_v1");
        strategy_execution_observation_v1_ = load_optional_symbol<ExecutionObservationFn>(
            library_handle_, "strategy_execution_observation_v1");
        pf_version_get_ = load_optional_symbol<VersionGetFn>(library_handle_, "pf_version_get");
        pf_version_string_ =
            load_optional_symbol<VersionStringFn>(library_handle_, "pf_version_string");
        strategy_get_effective_settings_ = load_optional_symbol<EffectiveSettingsFn>(
            library_handle_, "strategy_get_effective_settings");
        report_free_ = load_required_symbol<ReportFreeFn>(library_handle_, path_, "report_free");
        pf_abi_version_ =
            load_required_symbol<AbiVersionFn>(library_handle_, path_, "pf_abi_version");

        abi_version_value_ = pf_abi_version_();
        if (abi_version_value_ != PF_ABI_VERSION) {
            throw TypedHpoError<std::runtime_error>(
                "hpo_plugin_invalid", {{"reason", "abi"}},
                "strategy plugin '" + path_.string() + "' has PineForge ABI version " +
                    std::to_string(abi_version_value_) + ", expected " +
                    std::to_string(PF_ABI_VERSION));
        }
    } catch (...) {
        close();
        throw;
    }
#else
    (void)path_;
    throw TypedHpoError<std::runtime_error>(
        "hpo_plugin_invalid", {{"reason", "platform"}},
        "StrategyPlugin currently supports only Unix dynamic libraries");
#endif
}

StrategyPlugin::~StrategyPlugin() {
    close();
}

StrategyPlugin::StrategyPlugin(StrategyPlugin&& other) noexcept
    : path_(std::move(other.path_)),
      library_handle_(other.library_handle_),
      abi_version_value_(other.abi_version_value_),
      strategy_create_(other.strategy_create_),
      strategy_free_(other.strategy_free_),
      strategy_set_input_(other.strategy_set_input_),
      strategy_set_override_(other.strategy_set_override_),
      strategy_set_chart_timezone_(other.strategy_set_chart_timezone_),
      strategy_set_syminfo_mintick_(other.strategy_set_syminfo_mintick_),
      strategy_set_syminfo_pointvalue_(other.strategy_set_syminfo_pointvalue_),
      strategy_set_syminfo_metadata_(other.strategy_set_syminfo_metadata_),
      strategy_set_syminfo_timezone_(other.strategy_set_syminfo_timezone_),
      strategy_set_syminfo_session_(other.strategy_set_syminfo_session_),
      strategy_set_symbol_facts_(other.strategy_set_symbol_facts_),
      strategy_set_symbol_feed_(other.strategy_set_symbol_feed_),
      run_backtest_full_(other.run_backtest_full_),
      strategy_get_last_error_(other.strategy_get_last_error_),
      strategy_get_last_error_code_(other.strategy_get_last_error_code_),
      strategy_get_last_error_args_(other.strategy_get_last_error_args_),
      strategy_last_run_status_(other.strategy_last_run_status_),
      strategy_create_checked_(other.strategy_create_checked_),
      strategy_set_input_checked_(other.strategy_set_input_checked_),
      strategy_set_override_checked_(other.strategy_set_override_checked_),
      report_free_(other.report_free_),
      pf_abi_version_(other.pf_abi_version_),
      pf_selected_window_version_(other.pf_selected_window_version_),
      strategy_set_selected_window_v1_(other.strategy_set_selected_window_v1_),
      strategy_selected_window_counts_v1_(other.strategy_selected_window_counts_v1_),
      pf_execution_observer_version_(other.pf_execution_observer_version_),
      strategy_set_execution_observer_v1_(other.strategy_set_execution_observer_v1_),
      strategy_execution_observation_v1_(other.strategy_execution_observation_v1_),
      pf_version_get_(other.pf_version_get_),
      pf_version_string_(other.pf_version_string_),
      strategy_get_effective_settings_(other.strategy_get_effective_settings_) {
    other.library_handle_ = nullptr;
    other.abi_version_value_ = 0;
    other.strategy_create_ = nullptr;
    other.strategy_free_ = nullptr;
    other.strategy_set_input_ = nullptr;
    other.strategy_set_override_ = nullptr;
    other.strategy_set_chart_timezone_ = nullptr;
    other.strategy_set_syminfo_mintick_ = nullptr;
    other.strategy_set_syminfo_pointvalue_ = nullptr;
    other.strategy_set_syminfo_metadata_ = nullptr;
    other.strategy_set_syminfo_timezone_ = nullptr;
    other.strategy_set_syminfo_session_ = nullptr;
    other.strategy_set_symbol_facts_ = nullptr;
    other.strategy_set_symbol_feed_ = nullptr;
    other.run_backtest_full_ = nullptr;
    other.strategy_get_last_error_ = nullptr;
    other.strategy_get_last_error_code_ = nullptr;
    other.strategy_get_last_error_args_ = nullptr;
    other.strategy_last_run_status_ = nullptr;
    other.strategy_create_checked_ = nullptr;
    other.strategy_set_input_checked_ = nullptr;
    other.strategy_set_override_checked_ = nullptr;
    other.report_free_ = nullptr;
    other.pf_abi_version_ = nullptr;
    other.pf_selected_window_version_ = nullptr;
    other.strategy_set_selected_window_v1_ = nullptr;
    other.strategy_selected_window_counts_v1_ = nullptr;
    other.pf_execution_observer_version_ = nullptr;
    other.strategy_set_execution_observer_v1_ = nullptr;
    other.strategy_execution_observation_v1_ = nullptr;
    other.pf_version_get_ = nullptr;
    other.pf_version_string_ = nullptr;
    other.strategy_get_effective_settings_ = nullptr;
}

StrategyPlugin& StrategyPlugin::operator=(StrategyPlugin&& other) noexcept {
    if (this == &other) {
        return *this;
    }
    close();
    path_ = std::move(other.path_);
    library_handle_ = other.library_handle_;
    abi_version_value_ = other.abi_version_value_;
    strategy_create_ = other.strategy_create_;
    strategy_free_ = other.strategy_free_;
    strategy_set_input_ = other.strategy_set_input_;
    strategy_set_override_ = other.strategy_set_override_;
    strategy_set_chart_timezone_ = other.strategy_set_chart_timezone_;
    strategy_set_syminfo_mintick_ = other.strategy_set_syminfo_mintick_;
    strategy_set_syminfo_pointvalue_ = other.strategy_set_syminfo_pointvalue_;
    strategy_set_syminfo_metadata_ = other.strategy_set_syminfo_metadata_;
    strategy_set_syminfo_timezone_ = other.strategy_set_syminfo_timezone_;
    strategy_set_syminfo_session_ = other.strategy_set_syminfo_session_;
    strategy_set_symbol_facts_ = other.strategy_set_symbol_facts_;
    strategy_set_symbol_feed_ = other.strategy_set_symbol_feed_;
    run_backtest_full_ = other.run_backtest_full_;
    strategy_get_last_error_ = other.strategy_get_last_error_;
    strategy_get_last_error_code_ = other.strategy_get_last_error_code_;
    strategy_get_last_error_args_ = other.strategy_get_last_error_args_;
    strategy_last_run_status_ = other.strategy_last_run_status_;
    strategy_create_checked_ = other.strategy_create_checked_;
    strategy_set_input_checked_ = other.strategy_set_input_checked_;
    strategy_set_override_checked_ = other.strategy_set_override_checked_;
    report_free_ = other.report_free_;
    pf_abi_version_ = other.pf_abi_version_;
    pf_selected_window_version_ = other.pf_selected_window_version_;
    strategy_set_selected_window_v1_ = other.strategy_set_selected_window_v1_;
    strategy_selected_window_counts_v1_ = other.strategy_selected_window_counts_v1_;
    pf_execution_observer_version_ = other.pf_execution_observer_version_;
    strategy_set_execution_observer_v1_ = other.strategy_set_execution_observer_v1_;
    strategy_execution_observation_v1_ = other.strategy_execution_observation_v1_;
    pf_version_get_ = other.pf_version_get_;
    pf_version_string_ = other.pf_version_string_;
    strategy_get_effective_settings_ = other.strategy_get_effective_settings_;

    other.library_handle_ = nullptr;
    other.abi_version_value_ = 0;
    other.strategy_create_ = nullptr;
    other.strategy_free_ = nullptr;
    other.strategy_set_input_ = nullptr;
    other.strategy_set_override_ = nullptr;
    other.strategy_set_chart_timezone_ = nullptr;
    other.strategy_set_syminfo_mintick_ = nullptr;
    other.strategy_set_syminfo_pointvalue_ = nullptr;
    other.strategy_set_syminfo_metadata_ = nullptr;
    other.strategy_set_syminfo_timezone_ = nullptr;
    other.strategy_set_syminfo_session_ = nullptr;
    other.strategy_set_symbol_facts_ = nullptr;
    other.strategy_set_symbol_feed_ = nullptr;
    other.run_backtest_full_ = nullptr;
    other.strategy_get_last_error_ = nullptr;
    other.strategy_get_last_error_code_ = nullptr;
    other.strategy_get_last_error_args_ = nullptr;
    other.strategy_last_run_status_ = nullptr;
    other.strategy_create_checked_ = nullptr;
    other.strategy_set_input_checked_ = nullptr;
    other.strategy_set_override_checked_ = nullptr;
    other.report_free_ = nullptr;
    other.pf_abi_version_ = nullptr;
    other.pf_selected_window_version_ = nullptr;
    other.strategy_set_selected_window_v1_ = nullptr;
    other.strategy_selected_window_counts_v1_ = nullptr;
    other.pf_execution_observer_version_ = nullptr;
    other.strategy_set_execution_observer_v1_ = nullptr;
    other.strategy_execution_observation_v1_ = nullptr;
    other.pf_version_get_ = nullptr;
    other.pf_version_string_ = nullptr;
    other.strategy_get_effective_settings_ = nullptr;
    return *this;
}

pf_strategy_t StrategyPlugin::create_strategy() const {
    pf_strategy_t strategy = nullptr;
    if (strategy_create_checked_) {
        std::array<char, 4096> error{};
        const auto status =
            strategy_create_checked_(nullptr, &strategy, error.data(), error.size());
        if (status != PF_SETTINGS_OK) {
            if (strategy)
                free_strategy(strategy);
            throw TypedHpoError<>("hpo_strategy_create_failed", {}, error.data());
        }
    } else {
        strategy = strategy_create_(nullptr);
    }
    if (strategy == nullptr) {
        throw TypedHpoError<std::runtime_error>(
            "hpo_strategy_create_failed", {},
            "strategy_create returned null for plugin '" + path_.string() + "'");
    }
    return strategy;
}

void StrategyPlugin::free_strategy(pf_strategy_t strategy) const noexcept {
    if (strategy_free_ != nullptr) {
        strategy_free_(strategy);
    }
}

void StrategyPlugin::set_input(pf_strategy_t strategy,
                               const std::string& key,
                               const std::string& value) const {
    if (strategy == nullptr) {
        throw TypedHpoError<std::invalid_argument>("hpo_invariant", {},
                                                   "cannot set input on a null strategy handle");
    }
    reject_embedded_null(key, "input key");
    reject_embedded_null(value, "input value");
    if (strategy_set_input_checked_) {
        std::array<char, 4096> error{};
        const auto status = strategy_set_input_checked_(strategy, key.c_str(), value.c_str(),
                                                        error.data(), error.size());
        check_settings(*this, strategy, status, "strategy_set_input", error.data());
    } else {
        strategy_set_input_(strategy, key.c_str(), value.c_str());
    }
}

void StrategyPlugin::set_override(pf_strategy_t strategy,
                                  const std::string& key,
                                  const std::string& value) const {
    if (strategy == nullptr) {
        throw TypedHpoError<std::invalid_argument>("hpo_invariant", {},
                                                   "cannot set override on a null strategy handle");
    }
    reject_embedded_null(key, "override key");
    reject_embedded_null(value, "override value");
    if (strategy_set_override_checked_) {
        std::array<char, 4096> error{};
        const auto status = strategy_set_override_checked_(strategy, key.c_str(), value.c_str(),
                                                           error.data(), error.size());
        check_settings(*this, strategy, status, "strategy_set_override", error.data());
    } else {
        strategy_set_override_(strategy, key.c_str(), value.c_str());
    }
}

void StrategyPlugin::set_chart_timezone(pf_strategy_t strategy, const std::string& timezone) const {
    if (timezone.empty()) {
        return;
    }
    if (strategy == nullptr) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_invariant", {}, "cannot set chart timezone on a null strategy handle");
    }
    reject_embedded_null(timezone, "chart timezone");
    if (strategy_set_chart_timezone_ == nullptr) {
        throw TypedHpoError<std::runtime_error>(
            "hpo_plugin_invalid", {{"reason", "setter_missing"}},
            "strategy plugin '" + path_.string() + "' does not support a chart timezone");
    }
    strategy_set_chart_timezone_(strategy, timezone.c_str());
}

void StrategyPlugin::set_symbol_info(pf_strategy_t strategy, const SymbolInfo& info) const {
    if (strategy == nullptr)
        throw TypedHpoError<std::invalid_argument>(
            "hpo_invariant", {}, "cannot set symbol info on a null strategy handle");
    for (const auto& value : {info.mintick, info.pointvalue, info.mincontract}) {
        if (value && (!std::isfinite(*value) || *value <= 0.0))
            throw TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "backtest"}},
                "symbol numbers must be finite and positive");
    }
    reject_embedded_null(info.timezone, "symbol timezone");
    reject_embedded_null(info.session, "symbol session");
    if (info.mincontract && !strategy_set_syminfo_metadata_) {
        throw TypedHpoError<std::runtime_error>(
            "hpo_plugin_invalid", {{"reason", "setter_missing"}},
            "strategy plugin does not support requested symbol info: syminfo.mincontract "
            "needs strategy_set_syminfo_metadata (lot-size grid key qty_step)");
    }
    if ((info.mintick && !strategy_set_syminfo_mintick_) ||
        (info.pointvalue && !strategy_set_syminfo_pointvalue_) ||
        (!info.timezone.empty() && !strategy_set_syminfo_timezone_) ||
        (!info.session.empty() && !strategy_set_syminfo_session_)) {
        throw TypedHpoError<std::runtime_error>(
            "hpo_plugin_invalid", {{"reason", "setter_missing"}},
            "strategy plugin does not support requested symbol info");
    }
    if (info.mincontract) {
        strategy_set_syminfo_metadata_(strategy, "qty_step", *info.mincontract);
        strategy_set_syminfo_metadata_(strategy, "mincontract", *info.mincontract);
    }
    if (info.mintick)
        strategy_set_syminfo_mintick_(strategy, *info.mintick);
    if (info.pointvalue)
        strategy_set_syminfo_pointvalue_(strategy, *info.pointvalue);
    if (!info.timezone.empty())
        strategy_set_syminfo_timezone_(strategy, info.timezone.c_str());
    if (!info.session.empty())
        strategy_set_syminfo_session_(strategy, info.session.c_str());
}

void StrategyPlugin::run_backtest_full(pf_strategy_t strategy,
                                       const pf_bar_t* bars,
                                       int bar_count,
                                       const std::string& input_timeframe,
                                       const std::string& script_timeframe,
                                       bool bar_magnifier,
                                       int magnifier_samples,
                                       pf_magnifier_distribution_t magnifier_distribution,
                                       pf_report_t* report) const {
    if (strategy == nullptr) {
        throw TypedHpoError<std::invalid_argument>("hpo_invariant", {},
                                                   "cannot run a null strategy handle");
    }
    if (bar_count < 0 || (bar_count > 0 && bars == nullptr)) {
        throw TypedHpoError<std::invalid_argument>("hpo_invariant", {},
                                                   "invalid OHLCV array passed to strategy plugin");
    }
    if (report == nullptr) {
        throw TypedHpoError<std::invalid_argument>("hpo_invariant", {}, "report pointer is null");
    }
    reject_embedded_null(input_timeframe, "input timeframe");
    reject_embedded_null(script_timeframe, "script timeframe");

    // The engine consumes bars as immutable input. Its legacy public ABI has a
    // non-const pointer, so the const_cast is kept at this single boundary.
    run_backtest_full_(strategy, const_cast<pf_bar_t*>(bars), bar_count, input_timeframe.c_str(),
                       script_timeframe.c_str(), bar_magnifier ? 1 : 0, magnifier_samples,
                       magnifier_distribution, report);
}

void StrategyPlugin::set_symbol_feeds(pf_strategy_t strategy, const SymbolFeeds& symbols) const {
    if (symbols.empty())
        return;
    if (!strategy_set_symbol_facts_ || !strategy_set_symbol_feed_)
        throw TypedHpoError<std::runtime_error>(
            "hpo_plugin_invalid", {{"reason", "setter_missing"}},
            "--symbol-feeds: strategy library has no " +
                std::string(!strategy_set_symbol_facts_ ? "strategy_set_symbol_facts " : "") +
                (!strategy_set_symbol_feed_ ? "strategy_set_symbol_feed" : "") +
                "; other symbols' bars cannot be installed (engine 1.0.0 or later)");
    if (!strategy)
        throw TypedHpoError<std::invalid_argument>("hpo_invariant", {},
                                                   "--symbol-feeds: null strategy handle");
    const auto refused = [&](const std::string& what) {
        const auto error = last_error(strategy);
        throw EngineError(
            last_error_code(strategy), last_error_args(strategy),
            "--symbol-feeds: the engine refused " + what + (error.empty() ? "" : ": " + error));
    };
    for (const auto& symbol : symbols) {
        reject_embedded_null(symbol.symbol, "symbol key");
        for (const auto& fact : symbol.facts) {
            std::string value;
            if (const auto* number = std::get_if<double>(&fact.value)) {
                std::ostringstream text;
                text.imbue(std::locale::classic());
                text << std::setprecision(17) << *number;
                value = text.str();
            } else {
                value = std::get<std::string>(fact.value);
            }
            reject_embedded_null(fact.field, "symbol fact");
            reject_embedded_null(value, "symbol fact value");
            if (strategy_set_symbol_facts_(strategy, symbol.symbol.c_str(), fact.field.c_str(),
                                           value.c_str()) != 0)
                refused("the " + fact.field + " of " + symbol.symbol);
        }
        for (const auto& feed : symbol.feeds) {
            if (feed.bars.size() != feed.close_ms.size() ||
                feed.bars.size() > static_cast<std::size_t>(INT32_MAX))
                refused("the feed " + symbol.symbol + "@" + feed.timeframe +
                        " (invalid bar/close count)");
            reject_embedded_null(feed.timeframe, "symbol timeframe");
            if (strategy_set_symbol_feed_(strategy, symbol.symbol.c_str(), feed.timeframe.c_str(),
                    feed.bars.data(), feed.close_ms.data(),
                    static_cast<std::int32_t>(feed.bars.size())) != 0)
                refused("the feed " + symbol.symbol + "@" + feed.timeframe);
        }
    }
}

std::string StrategyPlugin::last_error(pf_strategy_t strategy) const {
    if (strategy == nullptr) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_invariant", {}, "cannot read the error from a null strategy handle");
    }
    const char* const error = strategy_get_last_error_(strategy);
    return error != nullptr ? std::string(error) : std::string();
}

void StrategyPlugin::free_report(pf_report_t* report) const noexcept {
    if (report_free_ != nullptr) {
        report_free_(report);
    }
}

std::optional<std::string> StrategyPlugin::last_error_code(pf_strategy_t strategy) const {
    if (!strategy_get_last_error_code_)
        return std::nullopt;
    const auto* code = strategy_get_last_error_code_(strategy);
    return code ? std::optional<std::string>(code) : std::nullopt;
}

std::optional<std::string> StrategyPlugin::last_error_args(pf_strategy_t strategy) const {
    if (!strategy_get_last_error_args_)
        return std::nullopt;
    const auto* args = strategy_get_last_error_args_(strategy);
    return args ? std::optional<std::string>(args) : std::nullopt;
}

std::optional<std::int32_t> StrategyPlugin::last_run_status(pf_strategy_t strategy) const {
    if (!strategy_last_run_status_)
        return std::nullopt;
    return strategy_last_run_status_(strategy);
}

bool StrategyPlugin::selected_window_available() const noexcept {
    return pf_selected_window_version_ && strategy_set_selected_window_v1_ &&
        strategy_selected_window_counts_v1_ && pf_execution_observer_version_ &&
        strategy_set_execution_observer_v1_ && strategy_execution_observation_v1_;
}

void StrategyPlugin::require_selected_window(
    const std::optional<std::string>& expected_manifest_version) const {
    const std::array<std::pair<bool, const char*>, 6> exports{{
        {pf_selected_window_version_ != nullptr, "pf_selected_window_version"},
        {strategy_set_selected_window_v1_ != nullptr, "strategy_set_selected_window_v1"},
        {strategy_selected_window_counts_v1_ != nullptr, "strategy_selected_window_counts_v1"},
        {pf_execution_observer_version_ != nullptr, "pf_execution_observer_version"},
        {strategy_set_execution_observer_v1_ != nullptr, "strategy_set_execution_observer_v1"},
        {strategy_execution_observation_v1_ != nullptr, "strategy_execution_observation_v1"}}};
    std::string first;
    std::string missing;
    for (const auto& entry : exports) {
        if (!entry.first) {
            if (first.empty())
                first = "missing_" + std::string(entry.second);
            missing += " " + std::string(entry.second);
        }
    }
    if (!missing.empty())
        selected_unavailable(first.c_str(), "missing selected execution exports:" + missing);
    if (pf_selected_window_version_() != 1)
        selected_unavailable("selected_window_version", "selected-window ABI version is not 1");
    if (pf_execution_observer_version_() != 1)
        selected_unavailable("execution_observer_version",
                             "execution-observer ABI version is not 1");
    (void)engine_version(expected_manifest_version);
}

EngineVersion StrategyPlugin::engine_version(
    const std::optional<std::string>& expected_manifest_version) const {
    const std::optional<pf_version_t> numbers =
        pf_version_get_ ? std::optional<pf_version_t>(pf_version_get_()) : std::nullopt;
    const char* const full = pf_version_string_ ? pf_version_string_() : nullptr;
    return validate_selected_engine_version(
        numbers, full ? std::optional<std::string>(full) : std::nullopt,
        expected_manifest_version);
}

void StrategyPlugin::set_selected_window(pf_strategy_t strategy, std::int64_t start_ms,
                                          std::int64_t end_ms) const {
    require_handle(strategy);
    if (!strategy_set_selected_window_v1_)
        require_selected_window();
    const pf_selected_window_config_v1 config{
        sizeof(pf_selected_window_config_v1), 1, start_ms, end_ms};
    check_selected_setter(strategy_set_selected_window_v1_(strategy, &config),
                          "strategy_set_selected_window_v1", "selected_window_contract");
}

void StrategyPlugin::clear_selected_window(pf_strategy_t strategy) const {
    require_handle(strategy);
    if (!strategy_set_selected_window_v1_)
        require_selected_window();
    check_selected_setter(strategy_set_selected_window_v1_(strategy, nullptr),
                          "strategy_set_selected_window_v1(clear)", "selected_window_contract");
}

pf_selected_window_counts_v1 StrategyPlugin::selected_window_counts(pf_strategy_t strategy) const {
    require_handle(strategy);
    if (!strategy_selected_window_counts_v1_)
        require_selected_window();
    pf_selected_window_counts_v1 counts{};
    counts.struct_size = sizeof(counts);
    counts.version = 1;
    const int status = strategy_selected_window_counts_v1_(strategy, &counts);
    if (status != 0 || counts.struct_size != sizeof(counts) || counts.version != 1)
        throw TypedHpoError<>("hpo_invariant", {},
                              "selected counts refused or changed header: " +
                                  std::to_string(status));
    return counts;
}

void StrategyPlugin::set_execution_observer(pf_strategy_t strategy,
                                            const pf_execution_observer_v1* observer) const {
    require_handle(strategy);
    if (!strategy_set_execution_observer_v1_)
        require_selected_window();
    check_selected_setter(strategy_set_execution_observer_v1_(strategy, observer),
                          "strategy_set_execution_observer_v1", "execution_observer_contract");
}

pf_execution_observation_v1 StrategyPlugin::execution_observation(pf_strategy_t strategy) const {
    require_handle(strategy);
    if (!strategy_execution_observation_v1_)
        require_selected_window();
    pf_execution_observation_v1 observation{};
    observation.struct_size = sizeof(observation);
    observation.version = 1;
    const int status = strategy_execution_observation_v1_(strategy, &observation);
    if (status != 0 || observation.struct_size != sizeof(observation) || observation.version != 1)
        throw TypedHpoError<>("hpo_invariant", {},
                              "execution observation refused or changed header: " +
                                  std::to_string(status));
    return observation;
}

std::optional<std::string> StrategyPlugin::effective_settings(pf_strategy_t strategy) const {
    require_handle(strategy);
    if (!strategy_get_effective_settings_)
        return std::nullopt;
    constexpr std::size_t limit = 16 * 1024 * 1024;
    std::size_t required = 0;
    std::array<char, 4096> error{};
    if (strategy_get_effective_settings_(strategy, nullptr, 0, &required, error.data(),
                                         error.size()) != PF_SETTINGS_BUFFER_TOO_SMALL ||
        required == 0 || required > limit)
        return std::nullopt;
    std::vector<char> buffer(required);
    if (strategy_get_effective_settings_(strategy, buffer.data(), buffer.size(), &required,
                                         error.data(), error.size()) != PF_SETTINGS_OK ||
        required == 0 || required > buffer.size() || buffer[required - 1] != '\0')
        return std::nullopt;
    const std::string text(buffer.data(), required - 1);
    try {
        const auto receipt = detail::parse_json(text, limit);
        if (receipt.kind != detail::Json::Kind::Object)
            return std::nullopt;
        const auto* version = receipt.find("version");
        if (!version || version->kind != detail::Json::Kind::Number || version->real() != 1)
            return std::nullopt;
    } catch (const HpoError&) {
        return std::nullopt;
    }
    return text;
}

void StrategyPlugin::close() noexcept {
#if defined(__unix__) || defined(__APPLE__)
    if (library_handle_ != nullptr) {
        ::dlclose(library_handle_);
        library_handle_ = nullptr;
    }
#endif
}

}  // namespace hpo
}  // namespace pineforge
