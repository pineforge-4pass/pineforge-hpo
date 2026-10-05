#include <pineforge/hpo/strategy_plugin.hpp>

#include <cmath>
#include <iomanip>
#include <locale>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>

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
        throw std::runtime_error(
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
        throw std::invalid_argument(std::string(label) + " contains an embedded NUL byte");
    }
}

}  // namespace

StrategyPlugin::StrategyPlugin(std::filesystem::path path) : path_(std::move(path)) {
#if defined(__unix__) || defined(__APPLE__)
    if (path_.empty()) {
        throw std::invalid_argument("strategy plugin path is empty");
    }
    library_handle_ = ::dlopen(path_.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (library_handle_ == nullptr) {
        throw std::runtime_error("cannot load strategy plugin '" + path_.string() +
                                 "': " + dynamic_loader_error());
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
        report_free_ = load_required_symbol<ReportFreeFn>(library_handle_, path_, "report_free");
        pf_abi_version_ =
            load_required_symbol<AbiVersionFn>(library_handle_, path_, "pf_abi_version");

        abi_version_value_ = pf_abi_version_();
        if (abi_version_value_ != PF_ABI_VERSION) {
            throw std::runtime_error("strategy plugin '" + path_.string() +
                                     "' has PineForge ABI version " +
                                     std::to_string(abi_version_value_) + ", expected " +
                                     std::to_string(PF_ABI_VERSION));
        }
    } catch (...) {
        close();
        throw;
    }
#else
    (void)path_;
    throw std::runtime_error("StrategyPlugin currently supports only Unix dynamic libraries");
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
      report_free_(other.report_free_),
      pf_abi_version_(other.pf_abi_version_) {
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
    other.report_free_ = nullptr;
    other.pf_abi_version_ = nullptr;
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
    report_free_ = other.report_free_;
    pf_abi_version_ = other.pf_abi_version_;

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
    other.report_free_ = nullptr;
    other.pf_abi_version_ = nullptr;
    return *this;
}

pf_strategy_t StrategyPlugin::create_strategy() const {
    pf_strategy_t strategy = strategy_create_(nullptr);
    if (strategy == nullptr) {
        throw std::runtime_error("strategy_create returned null for plugin '" + path_.string() +
                                 "'");
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
        throw std::invalid_argument("cannot set input on a null strategy handle");
    }
    reject_embedded_null(key, "input key");
    reject_embedded_null(value, "input value");
    strategy_set_input_(strategy, key.c_str(), value.c_str());
}

void StrategyPlugin::set_override(pf_strategy_t strategy,
                                  const std::string& key,
                                  const std::string& value) const {
    if (strategy == nullptr) {
        throw std::invalid_argument("cannot set override on a null strategy handle");
    }
    reject_embedded_null(key, "override key");
    reject_embedded_null(value, "override value");
    strategy_set_override_(strategy, key.c_str(), value.c_str());
}

void StrategyPlugin::set_chart_timezone(pf_strategy_t strategy, const std::string& timezone) const {
    if (timezone.empty()) {
        return;
    }
    if (strategy == nullptr) {
        throw std::invalid_argument("cannot set chart timezone on a null strategy handle");
    }
    reject_embedded_null(timezone, "chart timezone");
    if (strategy_set_chart_timezone_ == nullptr) {
        throw std::runtime_error("strategy plugin '" + path_.string() +
                                 "' does not support a chart timezone");
    }
    strategy_set_chart_timezone_(strategy, timezone.c_str());
}

void StrategyPlugin::set_symbol_info(pf_strategy_t strategy, const SymbolInfo& info) const {
    if (strategy == nullptr)
        throw std::invalid_argument("cannot set symbol info on a null strategy handle");
    for (const auto& value : {info.mintick, info.pointvalue, info.mincontract}) {
        if (value && (!std::isfinite(*value) || *value <= 0.0))
            throw std::invalid_argument("symbol numbers must be finite and positive");
    }
    reject_embedded_null(info.timezone, "symbol timezone");
    reject_embedded_null(info.session, "symbol session");
    if (info.mincontract && !strategy_set_syminfo_metadata_) {
        throw std::runtime_error(
            "strategy plugin does not support requested symbol info: syminfo.mincontract "
            "needs strategy_set_syminfo_metadata (lot-size grid key qty_step)");
    }
    if ((info.mintick && !strategy_set_syminfo_mintick_) ||
        (info.pointvalue && !strategy_set_syminfo_pointvalue_) ||
        (!info.timezone.empty() && !strategy_set_syminfo_timezone_) ||
        (!info.session.empty() && !strategy_set_syminfo_session_)) {
        throw std::runtime_error("strategy plugin does not support requested symbol info");
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
        throw std::invalid_argument("cannot run a null strategy handle");
    }
    if (bar_count < 0 || (bar_count > 0 && bars == nullptr)) {
        throw std::invalid_argument("invalid OHLCV array passed to strategy plugin");
    }
    if (report == nullptr) {
        throw std::invalid_argument("report pointer is null");
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
        throw std::runtime_error("--symbol-feeds: strategy library has no " +
            std::string(!strategy_set_symbol_facts_ ? "strategy_set_symbol_facts " : "") +
            (!strategy_set_symbol_feed_ ? "strategy_set_symbol_feed" : "") +
            "; other symbols' bars cannot be installed (engine 1.0.0 or later)");
    if (!strategy)
        throw std::invalid_argument("--symbol-feeds: null strategy handle");
    const auto refused = [&](const std::string& what) {
        const auto error = last_error(strategy);
        throw std::runtime_error("--symbol-feeds: the engine refused " + what +
                                 (error.empty() ? "" : ": " + error));
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
        throw std::invalid_argument("cannot read the error from a null strategy handle");
    }
    const char* const error = strategy_get_last_error_(strategy);
    return error != nullptr ? std::string(error) : std::string();
}

void StrategyPlugin::free_report(pf_report_t* report) const noexcept {
    if (report_free_ != nullptr) {
        report_free_(report);
    }
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
