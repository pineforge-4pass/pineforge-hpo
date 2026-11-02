#if defined(PINEFORGE_HPO_FAKE_ROUTING_PLUGIN)

#include <pineforge/pineforge.h>

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdint>
#include <iomanip>
#include <limits>
#include <map>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
#include <unistd.h>

namespace {

struct Handle {
    explicit Handle(std::uint64_t value) : id(value) {}
    std::uint64_t id;
    std::map<std::string, std::string> inputs;
    std::map<std::string, std::string> overrides;
    std::map<std::string, double> metadata;
    std::string chart_timezone;
    std::string timezone;
    std::string session;
    double mintick = 0;
    double pointvalue = 0;
    std::vector<std::string> feeds;
    std::string error;
    std::string code;
    std::string args;
    bool report_released = false;
    unsigned runs = 0;
};

std::mutex log_mutex;
std::vector<std::string> run_log;
std::vector<std::uint64_t> completed;
std::atomic<std::uint64_t> next_handle{0};
std::atomic<unsigned> admissions{0};
std::atomic<unsigned> live{0};
std::atomic<unsigned> freed{0};
std::atomic<unsigned> validations{0};
std::atomic<unsigned> violations{0};
std::atomic<unsigned> arena_calls{0};
thread_local Handle* current_handle = nullptr;

Handle& handle(pf_strategy_t value) { return *static_cast<Handle*>(value); }

bool fault(const Handle& state, const std::string& mode) {
    const auto market = state.inputs.find("Market");
    const auto selected = state.inputs.find("FaultMarket");
    const auto failure = state.inputs.find("Failure");
    return market != state.inputs.end() && selected != state.inputs.end() &&
           failure != state.inputs.end() && market->second == selected->second &&
           failure->second == mode;
}

double number(const std::map<std::string, std::string>& values, const std::string& name) {
    const auto found = values.find(name);
    return found == values.end() ? 0 : std::stod(found->second);
}

void fail(Handle& state, const char* code, const char* message, const char* args = "{}") {
    state.code = code;
    state.error = message;
    state.args = args;
}

}  // namespace

extern "C" {

PF_API int pf_abi_version() {
    ++admissions;
    return PF_ABI_VERSION;
}

PF_API pf_strategy_t strategy_create(const char*) {
    auto* state = new Handle{next_handle.fetch_add(1)};
    if (current_handle)
        ++violations;
    current_handle = state;
    ++live;
    return state;
}

PF_API void strategy_free(pf_strategy_t value) {
    auto& state = handle(value);
    if (!state.report_released || state.runs > 1 || current_handle != &state)
        ++violations;
    if (!state.inputs.count("Market"))
        ++validations;
    ++freed;
    --live;
    current_handle = nullptr;
    delete &state;
}

PF_API void report_free(pf_report_t* report) {
    if (!current_handle || current_handle->report_released)
        ++violations;
    else
        current_handle->report_released = true;
    delete[] report->equity_curve;
    report->equity_curve = nullptr;
    report->equity_curve_len = 0;
}

PF_API void strategy_set_input(pf_strategy_t value, const char* name, const char* input) {
    handle(value).inputs[name] = input;
}

PF_API int strategy_set_input_checked(pf_strategy_t value, const char* name, const char* input,
                                     char* diagnostic, std::size_t capacity) {
    strategy_set_input(value, name, input);
    auto& state = handle(value);
    if (fault(state, "setting")) {
        fail(state, "setting_rejected", "fixture input rejected",
             "{\"entrypoint\":\"strategy_set_input\"}");
        if (capacity)
            std::snprintf(diagnostic, capacity, "%s", state.error.c_str());
        return PF_SETTINGS_INVALID_ARGUMENT;
    }
    return PF_SETTINGS_OK;
}

PF_API void strategy_set_override(pf_strategy_t value, const char* name, const char* input) {
    handle(value).overrides[name] = input;
}

PF_API void strategy_set_chart_timezone(pf_strategy_t value, const char* timezone) {
    handle(value).chart_timezone = timezone;
}

PF_API void strategy_set_syminfo_metadata(pf_strategy_t value, const char* name, double input) {
    handle(value).metadata[name] = input;
}

PF_API void strategy_set_syminfo_mintick(pf_strategy_t value, double input) {
    handle(value).mintick = input;
}

PF_API void strategy_set_syminfo_pointvalue(pf_strategy_t value, double input) {
    handle(value).pointvalue = input;
}

PF_API void strategy_set_syminfo_timezone(pf_strategy_t value, const char* input) {
    handle(value).timezone = input;
}

PF_API void strategy_set_syminfo_session(pf_strategy_t value, const char* input) {
    handle(value).session = input;
}

PF_API int strategy_set_symbol_facts(pf_strategy_t, const char*, const char*, const char*) {
    return 0;
}

PF_API int strategy_set_symbol_feed(pf_strategy_t value, const char* symbol, const char* timeframe,
                                    const pf_bar_t* bars, const std::int64_t* closes, int count) {
    std::ostringstream out;
    out << "{\"symbol\":" << std::quoted(symbol) << ",\"timeframe\":" << std::quoted(timeframe)
        << ",\"bars\":" << reinterpret_cast<std::uintptr_t>(bars)
        << ",\"closes\":" << reinterpret_cast<std::uintptr_t>(closes)
        << ",\"count\":" << count << '}';
    handle(value).feeds.push_back(out.str());
    return 0;
}

PF_API int strategy_set_scratch_tables_v1(pf_strategy_t, const pf_scratch_tables_v1*) {
    return 0;
}

PF_API int strategy_require_report_scratch_arena_v1(pf_strategy_t value) {
    ++arena_calls;
    auto& state = handle(value);
    if (fault(state, "scratch")) {
        fail(state, "scratch_capacity_exceeded", "fixture scratch exhausted",
             "{\"resource\":\"successor_roots\"}");
        return -1;
    }
    return 0;
}

PF_API void run_backtest_full(pf_strategy_t value, pf_bar_t* bars, int count,
                              const char* input_tf, const char* script_tf, int magnifier,
                              int samples, pf_magnifier_distribution_t distribution,
                              pf_report_t* report) {
    auto& state = handle(value);
    ++state.runs;
    if (state.runs != 1)
        ++violations;
    const auto market = state.inputs.at("Market");
    const auto length = number(state.inputs, "Length");
    std::ostringstream event;
    event << "{\"handle\":" << state.id << ",\"bars\":"
          << reinterpret_cast<std::uintptr_t>(bars) << ",\"count\":" << count
          << ",\"market\":" << std::quoted(market) << ",\"length\":" << length
          << ",\"input_tf\":" << std::quoted(input_tf)
          << ",\"script_tf\":" << std::quoted(script_tf)
          << ",\"chart_timezone\":" << std::quoted(state.chart_timezone)
          << ",\"timezone\":" << std::quoted(state.timezone)
          << ",\"session\":" << std::quoted(state.session)
          << ",\"mintick\":" << state.mintick << ",\"pointvalue\":" << state.pointvalue
          << ",\"qty_step\":" << state.metadata["qty_step"]
          << ",\"mincontract\":" << state.metadata["mincontract"]
          << ",\"magnifier\":" << magnifier << ",\"samples\":" << samples
          << ",\"distribution\":" << static_cast<int>(distribution)
          << ",\"capital\":" << number(state.overrides, "initial_capital") << ",\"feeds\":[";
    for (std::size_t index = 0; index < state.feeds.size(); ++index)
        event << (index ? "," : "") << state.feeds[index];
    event << "]}";
    {
        std::lock_guard<std::mutex> lock(log_mutex);
        run_log.push_back(event.str());
    }
    if (state.inputs.count("NotifyMarket") && market == state.inputs.at("NotifyMarket") &&
        length == number(state.inputs, "NotifyLength")) {
        const char token = 'N';
        if (::write(std::stoi(state.inputs.at("NotifyFd")), &token, 1) != 1)
            fail(state, "strategy_runtime_error", "fixture proposal notification failed");
    }
    if (state.inputs.count("GateMarket") && market == state.inputs.at("GateMarket") &&
        (!state.inputs.count("GateLength") || length == number(state.inputs, "GateLength"))) {
        const int entered = std::stoi(state.inputs.at("EnteredFd"));
        const int release = std::stoi(state.inputs.at("ReleaseFd"));
        char token = 'G';
        if (::write(entered, &token, 1) != 1)
            fail(state, "strategy_runtime_error", "fixture gate notification failed");
        ssize_t received;
        do {
            received = ::read(release, &token, 1);
        } while (received < 0 && errno == EINTR);
        if (received != 1)
            fail(state, "strategy_runtime_error", "fixture gate release failed");
    }
    // A perturbation, not a wait or a limit: it only changes which worker finishes first.
    // No assertion depends on its length; ordering that tests assert is enforced by the
    // descriptor handshake above, so a slower or faster machine cannot change a verdict.
    if (state.inputs.count("DelayMarket") && market == state.inputs.at("DelayMarket"))
        std::this_thread::sleep_for(std::chrono::milliseconds(
            static_cast<int>(number(state.inputs, "DelayMs"))));
    double score = length + bars[0].close + bars[count - 1].close;
    report->equity_curve = new pf_equity_point_t[1]{};
    report->equity_curve_len = 1;
    report->total_trades = 1;
    report->input_bars_processed = count;
    report->script_bars_processed = count;
    report->input_tf_seconds = std::stoi(input_tf) * 60;
    report->script_tf_seconds = std::stoi(script_tf) * 60;
    report->script_tf_ratio = 1;
    if (fault(state, "engine")) {
        score = 1e12;
        fail(state, "strategy_runtime_error", "");
    }
    if (fault(state, "report")) {
        score = 1e12;
        report->total_trades = -1;
    }
    if (fault(state, "objective"))
        score = std::numeric_limits<double>::quiet_NaN();
    report->net_profit = score;
    report->metrics.all.net_profit = score;
    {
        std::lock_guard<std::mutex> lock(log_mutex);
        completed.push_back(state.id);
    }
}

PF_API const char* strategy_get_last_error(pf_strategy_t value) {
    return handle(value).error.c_str();
}
PF_API const char* strategy_get_last_error_code(pf_strategy_t value) {
    return handle(value).code.c_str();
}
PF_API const char* strategy_get_last_error_args(pf_strategy_t value) {
    return handle(value).args.c_str();
}
PF_API int strategy_last_run_status(pf_strategy_t value) {
    return handle(value).code.empty() ? 0 : 1;
}

// Test observation exports. They are not engine capabilities and are never resolved by HPO.
PF_API const char* routing_snapshot() {
    static thread_local std::string bytes;
    std::lock_guard<std::mutex> lock(log_mutex);
    std::ostringstream out;
    out << "{\"admissions\":" << admissions << ",\"created\":" << next_handle
        << ",\"freed\":" << freed << ",\"live\":" << live << ",\"validations\":" << validations
        << ",\"violations\":" << violations << ",\"arena_calls\":" << arena_calls << ",\"runs\":[";
    for (std::size_t index = 0; index < run_log.size(); ++index)
        out << (index ? "," : "") << run_log[index];
    out << "],\"completed\":[";
    for (std::size_t index = 0; index < completed.size(); ++index)
        out << (index ? "," : "") << completed[index];
    out << "]}";
    bytes = out.str();
    return bytes.c_str();
}

PF_API void routing_clear_events() {
    std::lock_guard<std::mutex> lock(log_mutex);
    run_log.clear();
    completed.clear();
}

}  // extern "C"

#else

// Exercise the production coordinator through internal C++ interfaces only.
#define main pineforge_hpo_native_entry
#include "../src/cli/main.cpp"
#undef main

#include <dlfcn.h>
#include <sys/wait.h>

#include <cstring>
#include <iterator>
#include <set>

namespace {

namespace cli = pineforge::hpo::cli;
namespace detail = pineforge::hpo::detail;
using Path = std::filesystem::path;
using Json = detail::Json;

void require(bool condition, const std::string& message) {
    if (!condition)
        throw std::runtime_error(message);
}

std::int64_t integer(const Json& value, const std::string& name) {
    return detail::json_integer(detail::field(value, name));
}

std::string string(const Json& value, const std::string& name) {
    return detail::field(value, name).text();
}

bool boolean(const Json& value, const std::string& name) {
    const auto& field = detail::field(value, name);
    require(field.kind == Json::Kind::Bool, "expected boolean field " + name);
    return field.value == "true";
}

std::string read(const Path& path) {
    std::ifstream input(path, std::ios::binary);
    require(bool(input), "cannot read test output");
    return {std::istreambuf_iterator<char>(input), {}};
}

void write(const Path& path, const std::string& bytes) {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    output.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    output.close();
    require(bool(output), "cannot write test fixture");
}

class Observations final {
public:
    explicit Observations(const Path& plugin) : library_(::dlopen(plugin.c_str(), RTLD_NOW)) {
        require(library_ != nullptr, "cannot observe fake plugin");
        snapshot_ = reinterpret_cast<const char* (*)()>(::dlsym(library_, "routing_snapshot"));
        clear_ = reinterpret_cast<void (*)()>(::dlsym(library_, "routing_clear_events"));
        require(snapshot_ && clear_, "fake observation exports missing");
    }
    ~Observations() { ::dlclose(library_); }
    Json snapshot() const { return detail::parse_json(snapshot_()); }
    void clear() const { clear_(); }

private:
    void* library_;
    const char* (*snapshot_)();
    void (*clear_)();
};

class Environment final {
public:
    Environment(const char* name, const char* value) : name_(name) {
        if (const auto* previous = std::getenv(name))
            previous_ = previous;
        if (value)
            require(::setenv(name, value, 1) == 0, "cannot set fixture environment");
        else
            require(::unsetenv(name) == 0, "cannot clear fixture environment");
    }
    ~Environment() {
        if (previous_)
            ::setenv(name_.c_str(), previous_->c_str(), 1);
        else
            ::unsetenv(name_.c_str());
    }

private:
    std::string name_;
    std::optional<std::string> previous_;
};

class Pipe final {
public:
    Pipe() { require(::pipe(descriptors_) == 0, "cannot create fixture pipe"); }
    ~Pipe() { for (const int descriptor : descriptors_) ::close(descriptor); }
    int reader() const { return descriptors_[0]; }
    int writer() const { return descriptors_[1]; }
    void receive() const {
        struct pollfd descriptor{reader(), POLLIN, 0};
        require(::poll(&descriptor, 1, 5000) == 1, "fixture gate did not arrive");
        char token;
        require(::read(reader(), &token, 1) == 1, "fixture gate was empty");
    }
    void release() const {
        const char token = 'G';
        require(::write(writer(), &token, 1) == 1, "cannot release fixture gate");
    }

private:
    int descriptors_[2];
};

template <typename Function>
void refusal(Function&& function, const std::string& code) {
    try {
        function();
    } catch (const pfh::HpoError& error) {
        require(error.code() == code, "unexpected refusal code: " + error.code());
        return;
    }
    throw std::runtime_error("expected refusal " + code);
}

void tape(const Path& path, int price, int count, bool binary, const std::string& symbol,
          const std::string& timeframe) {
    std::ostringstream csv;
    csv << "timestamp,open,high,low,close,volume\n";
    std::string bytes;
    for (int index = 0; index < count; ++index) {
        const double value = price + index;
        const std::int64_t timestamp = static_cast<std::int64_t>(index) * 60'000;
        csv << timestamp << ',' << value << ',' << value << ',' << value << ',' << value
            << ",1\n";
        // Independent on-disk encoding of five binary64 values followed by signed milliseconds.
        for (const double field : {value, value, value, value, 1.0}) {
            std::uint64_t bits;
            std::memcpy(&bits, &field, sizeof(bits));
            for (unsigned shift = 0; shift < 64; shift += 8)
                bytes.push_back(static_cast<char>((bits >> shift) & 255));
        }
        for (unsigned shift = 0; shift < 64; shift += 8)
            bytes.push_back(static_cast<char>(
                (static_cast<std::uint64_t>(timestamp) >> shift) & 255));
    }
    write(path, binary ? bytes : csv.str());
    if (binary) {
        write(path.string() + ".json",
              "{\"format\":\"pf_bar_t/v1\",\"record_size\":48,\"symbol\":\"" + symbol +
              "\",\"tf\":\"" + timeframe + "\",\"count\":" + std::to_string(count) +
              ",\"first_ts\":0,\"last_ts\":" + std::to_string((count - 1) * 60'000) +
              ",\"sha256\":\"" + detail::sha256(bytes) + "\"}");
    }
}

struct Fixture {
    std::shared_ptr<const detail::AdmittedDataCatalogue> catalogue;
    std::map<std::string, unsigned> loads;
    std::shared_ptr<const pfh::SymbolFeeds> a_feeds;
    std::shared_ptr<const pfh::SymbolFeeds> b_feeds;
    detail::ChartDataDeclaration a;
    detail::ChartDataDeclaration b;

    Fixture(const Path& directory, bool a_binary = false, bool b_binary = false)
        : a{"zeta", "asset-a", "1", "1", "UTC",
            {"asset-a", "synthetic", "units", 0.5, 2, 3, "UTC", "24x7"},
            directory / (a_binary ? "a.bin" : "a.csv"),
            a_binary ? detail::ChartDataFormat::Binary : detail::ChartDataFormat::Csv,
            pfh::BinaryVerification::Full},
          b{"alpha", "asset-b", "5", "5", "Etc/UTC",
            {"asset-b", "synthetic", "units", 0.25, 4, 5, "Etc/UTC", "0900-1700"},
            directory / (b_binary ? "b.bin" : "b.csv"),
            b_binary ? detail::ChartDataFormat::Binary : detail::ChartDataFormat::Csv,
            pfh::BinaryVerification::Full} {
        std::filesystem::create_directories(directory);
        tape(a.path, 10, 3, a_binary, a.symbol, a.input_tf);
        tape(b.path, 100, 5, b_binary, b.symbol, b.input_tf);
        const auto make_feeds = [](const std::string& symbol, double price) {
            pfh::SymbolFeed feed;
            feed.timeframe = "1";
            pf_bar_t bar{};
            bar.open = bar.high = bar.low = bar.close = price;
            bar.volume = 1;
            feed.bars.push_back(bar);
            feed.close_ms.push_back(60'000);
            feed.source_values_sha256 = detail::sha256(symbol);
            pfh::SymbolData data;
            data.symbol = symbol;
            data.facts.push_back({"currency", std::string("units")});
            data.feeds.push_back(std::move(feed));
            return std::make_shared<const pfh::SymbolFeeds>(pfh::SymbolFeeds{std::move(data)});
        };
        a_feeds = make_feeds("request-a", 7);
        b_feeds = make_feeds("request-b", 19);
        auto union_feeds = std::make_shared<pfh::SymbolFeeds>(*a_feeds);
        union_feeds->insert(union_feeds->end(), b_feeds->begin(), b_feeds->end());
        detail::DataContextSettings settings{
            "artifact", std::string(64, 'a'), {}, {}, "fixture", "full_input", "none", union_feeds};
        // Dataset declarations, choices and the explicit map deliberately disagree in order.
        const detail::SymbolBindingDeclaration binding{
            "Market", detail::SymbolBindingRole::Chart, {a.symbol, b.symbol},
            {{b.symbol, b.id}, {a.symbol, a.id}}};
        catalogue = std::make_shared<const detail::AdmittedDataCatalogue>(
            std::vector<detail::ChartDataDeclaration>{b, a},
            std::vector<std::string>{b.id, a.id},
            std::vector<detail::SymbolBindingDeclaration>{binding}, settings,
            std::set<std::string>{"fixture"}, [&](const detail::ChartDataDeclaration& declaration) {
                ++loads[declaration.id];
                return detail::load_bound_chart(declaration);
            });
    }

    const detail::BoundTrialContext& context(const std::string& symbol) const {
        return catalogue->context(catalogue->resolve_choice("Market", symbol));
    }

    pfh::BacktestConfiguration configuration(const detail::BoundTrialContext& context) const {
        const auto& declaration = context.identity;
        const auto& facts = declaration.syminfo;
        pfh::BacktestConfiguration configuration;
        configuration.input_timeframe = declaration.input_tf;
        configuration.script_timeframe = declaration.script_tf;
        configuration.chart_timezone = declaration.chart_timezone;
        configuration.symbol_info = pfh::SymbolInfo{
            facts.mintick, facts.pointvalue, facts.mincontract, facts.timezone, facts.session};
        configuration.symbol_feeds = declaration.symbol == a.symbol ? a_feeds : b_feeds;
        configuration.capture_equity_curve = false;
        configuration.bar_magnifier = declaration.symbol == b.symbol;
        configuration.magnifier_samples = declaration.symbol == a.symbol ? 3 : 7;
        configuration.magnifier_distribution = declaration.symbol == a.symbol
            ? PF_MAGNIFIER_UNIFORM : PF_MAGNIFIER_TRIANGLE;
        return configuration;
    }
};

cli::PreparedStudy study(const Fixture& fixture,
                         std::shared_ptr<const pfh::StrategyPlugin> plugin,
                         cli::Options options = {}) {
    if (options.dimensions.empty()) {
        options.dimensions.emplace_back(pfh::IntegerDimension("Length", 1, 2));
        options.dimensions.emplace_back(pfh::CategoricalDimension(
            "Market", {fixture.a.symbol, fixture.b.symbol}));
    }
    if (options.objective.empty())
        options.objective = "metrics.all.net_profit";
    options.seed = 17;
    if (!options.max_trials)
        options.max_trials = 4;
    if (!options.batch_size && options.schedule != "rolling")
        options.batch_size = 4;
    options.strategy_overrides = {{"initial_capital", "1000"}};
    pfh::SearchSpace space(options.dimensions);
    const auto recorded = detail::recorded_space(space, options.objective, "maximize",
                                                 options.constraints);
    options.space_json = std::make_shared<const std::string>(detail::dump_json(recorded));
    options.space_hash = detail::space_hash(recorded);
    options.context_selector = std::make_shared<const cli::ContextSelector>(
        fixture.catalogue, space, "Market",
        [&](const detail::BoundTrialContext& context) { return fixture.configuration(context); });
    if (options.sampler == "candidates") {
        std::string list;
        for (std::uint64_t index = 0; index < options.max_trials; ++index) {
            const auto candidate = space.candidate_at(index % *space.finite_cardinality(), index);
            Json row = detail::object_json();
            for (const auto& value : candidate.values)
                row.members[value.first] = detail::scalar_json(value.second);
            list += detail::dump_json(row) + "\n";
        }
        options.candidate_list = std::make_shared<const detail::CandidateList>(
            detail::parse_candidate_list(list, space, options.fixed_inputs));
    }
    if (options.sampler == "sobol") {
        const pfh::SobolSampler probe(space, options.seed,
            *detail::sobol_scramble_from_name(options.sobol_scramble), 0, options.max_trials);
        options.sobol_descriptor = std::make_shared<const detail::SobolDescriptor>(
            detail::make_sobol_descriptor(probe, options.space_hash, std::nullopt));
    }
    const auto selector = options.context_selector;
    const auto cardinality = space.finite_cardinality();
    pfh::Pruner pruner(options.pruner, options.pruner_rungs, options.pruner_eta, false);
    pfh::MetricExpression objective(options.objective);
    std::vector<pfh::MetricExpression> constraints;
    for (const auto& expression : options.constraints)
        constraints.emplace_back(expression);
    return {std::move(options), std::move(space), cardinality, std::move(pruner),
            std::move(objective), std::move(constraints), {}, std::move(plugin),
            fixture.context(fixture.a.symbol).view.owner, {}, selector};
}

struct Run {
    cli::StudyResult result;
    std::string bytes;
    Json document;
};

Run run(const cli::PreparedStudy& prepared) {
    Run result;
    cli::StudyCallbacks callbacks;
    callbacks.publish = [&](const detail::ResultDocument& document, const cli::StudyStatistics&) {
        std::ostringstream output;
        document.write(output);
        result.bytes = output.str();
    };
    result.result = cli::run_study(prepared, callbacks);
    result.document = detail::parse_json(result.bytes, result.bytes.size());
    return result;
}

void identity(const Json& row, const Fixture& fixture) {
    const auto market = string(detail::field(row, "parameters"), "Market");
    const auto& expected = fixture.context(market);
    require(string(row, "symbol") == market && string(row, "dataset_id") == expected.identity.id &&
                string(row, "data_context_sha256") == fixture.catalogue->data_context_sha256(),
            "row identity disagrees with its candidate and admitted owner");
    require(!row.find("quote_currency"), "unverified money unit was emitted");
}

void clean(const Observations& observations) {
    const auto snapshot = observations.snapshot();
    require(integer(snapshot, "live") == 0 && integer(snapshot, "violations") == 0 &&
                integer(snapshot, "created") == integer(snapshot, "freed"),
            "report/handle lifetime or fresh-handle contract failed");
}

void observed_routes(const Json& snapshot, const Fixture& fixture, bool prefixes) {
    std::set<std::int64_t> handles;
    std::map<std::pair<std::string, int>, std::vector<int>> calls;
    for (const auto& event : detail::field(snapshot, "runs").items) {
        const auto market = string(event, "market");
        const auto& context = fixture.context(market);
        const auto& declaration = context.identity;
        const auto& facts = declaration.syminfo;
        const auto expected_feeds = market == fixture.a.symbol ? fixture.a_feeds : fixture.b_feeds;
        require(handles.insert(integer(event, "handle")).second,
                "a strategy handle executed more than one trial or prefix");
        require(integer(event, "bars") == static_cast<std::int64_t>(
                    reinterpret_cast<std::uintptr_t>(context.view.data())),
                "executor did not borrow the selected chart's exact pointer");
        require(string(event, "input_tf") == declaration.input_tf &&
                    string(event, "script_tf") == declaration.script_tf &&
                    string(event, "chart_timezone") == declaration.chart_timezone &&
                    string(event, "timezone") == facts.timezone &&
                    string(event, "session") == facts.session &&
                    detail::field(event, "mintick").real() == facts.mintick &&
                    detail::field(event, "pointvalue").real() == facts.pointvalue &&
                    detail::field(event, "qty_step").real() == facts.mincontract &&
                    detail::field(event, "mincontract").real() == facts.mincontract &&
                    integer(event, "magnifier") == (market == fixture.a.symbol ? 0 : 1) &&
                    integer(event, "samples") == (market == fixture.a.symbol ? 3 : 7) &&
                    integer(event, "distribution") == (market == fixture.a.symbol
                        ? PF_MAGNIFIER_UNIFORM : PF_MAGNIFIER_TRIANGLE) &&
                    integer(event, "capital") == 1000,
                "selected context settings or trial overrides were not forwarded");
        const auto& feeds = detail::field(event, "feeds").items;
        require(feeds.size() == expected_feeds->size(), "request-feed set leaked across contexts");
        const auto& expected_feed = expected_feeds->front().feeds.front();
        require(string(feeds.front(), "symbol") == expected_feeds->front().symbol &&
                    integer(feeds.front(), "bars") == static_cast<std::int64_t>(
                        reinterpret_cast<std::uintptr_t>(expected_feed.bars.data())) &&
                    integer(feeds.front(), "closes") == static_cast<std::int64_t>(
                        reinterpret_cast<std::uintptr_t>(expected_feed.close_ms.data())) &&
                    integer(feeds.front(), "count") == 1,
                "selected request feed did not retain its admitted storage");
        calls[{market, static_cast<int>(integer(event, "length"))}].push_back(
            static_cast<int>(integer(event, "count")));
    }
    require(calls.size() == 4, "interleaved candidates were lost or repeated");
    for (auto& entry : calls) {
        auto& counts = entry.second;
        std::sort(counts.begin(), counts.end());
        const bool a = entry.first.first == fixture.a.symbol;
        const std::vector<int> expected = prefixes
            ? (a ? std::vector<int>{2, 3} : std::vector<int>{3, 5})
            : (a ? std::vector<int>{3} : std::vector<int>{5});
        require(counts == expected, "prefix plan used another context's bar counts");
    }
}

void routes_and_admission(const Fixture& fixture,
                          const std::shared_ptr<const pfh::StrategyPlugin>& plugin,
                          const Observations& observations) {
    std::string single;
    const auto admission_count = integer(observations.snapshot(), "admissions");
    for (const bool prefixes : {false, true}) {
        for (const unsigned workers : {1u, 4u}) {
            observations.clear();
            cli::Options options;
            options.workers = workers;
            if (prefixes) {
                // This internal fake context exercises the mechanism, without admitting a
                // public comparison or pruning policy.
                options.pruner = pfh::PrunerKind::Median;
                options.pruner_name = "median";
                options.pruner_rungs = {0.5};
            }
            const auto before = observations.snapshot();
            const auto result = run(study(fixture, plugin, options));
            require(result.result.exit_code == 0 && result.result.trials_completed == 4 &&
                        result.result.best_candidate &&
                        std::get<std::string>(result.result.best_candidate->values.at("Market")) ==
                            fixture.b.symbol,
                    "interleaved study did not rank the correct complete vector");
            const auto& rows = detail::field(result.document, "trials").items;
            require(rows.size() == 4, "interleaved study lost terminal rows");
            for (std::size_t index = 0; index < rows.size(); ++index) {
                const auto& row = rows[index];
                identity(row, fixture);
                const auto market = string(detail::field(row, "parameters"), "Market");
                const auto& view = fixture.context(market).view;
                const auto length = integer(detail::field(row, "parameters"), "Length");
                require(market == (index % 2 ? fixture.b.symbol : fixture.a.symbol),
                        "grid choice order no longer interleaves the two contexts");
                require(detail::field(row, "objective").real() ==
                            length + view.data()[0].close + view.data()[view.size() - 1].close,
                        "score disagrees with independent selected-tape arithmetic");
                require(integer(detail::field(row, "backtest"), "input_bars_processed") ==
                            static_cast<std::int64_t>(view.size()),
                        "full evaluation used another chart's length");
                if (prefixes) {
                    const auto prefix = market == fixture.a.symbol ? 2 : 3;
                    require(integer(detail::field(row, "pruning"), "bars_processed_total") ==
                                static_cast<std::int64_t>(view.size()) + prefix,
                            "prefix and full counts did not use the same context");
                }
            }
            if (!prefixes) {
                if (single.empty())
                    single = result.bytes;
                else
                    require(single == result.bytes, "worker count changed ordered study bytes");
            }
            const auto after = observations.snapshot();
            observed_routes(after, fixture, prefixes);
            const auto runs = prefixes ? 8 : 4;
            require(integer(after, "created") - integer(before, "created") == runs + 2 &&
                        integer(after, "validations") - integer(before, "validations") == 2,
                    "validation handles were retained or repeated in the trial hot loop");
            require(integer(after, "admissions") == admission_count,
                    "a context or prefix reloaded the strategy plugin");
            clean(observations);
        }
    }
    require(fixture.loads.size() == 2 && fixture.loads.at(fixture.a.id) == 1 &&
                fixture.loads.at(fixture.b.id) == 1,
            "repeated choices or prefix reruns reloaded an admitted tape");
}

void failure_isolation(const Fixture& fixture,
                       const std::shared_ptr<const pfh::StrategyPlugin>& plugin,
                       const Observations& observations) {
    for (const auto* mode : {"setting", "engine", "report", "scratch"}) {
        for (const unsigned workers : {1u, 4u}) {
            Environment arena(
                "PF_SCRATCH_ARENA_FD", std::string(mode) == "scratch" ? "1" : nullptr);
            observations.clear();
            cli::Options options;
            options.workers = workers;
            options.fixed_inputs = {{"FaultMarket", fixture.b.symbol}, {"Failure", mode}};
            const auto result = run(study(fixture, plugin, options));
            require(result.result.exit_code == 0 && result.result.trials_completed == 4 &&
                        result.result.best_candidate &&
                        std::get<std::string>(result.result.best_candidate->values.at("Market")) ==
                            fixture.a.symbol,
                    "failed context ranked or contaminated the succeeding context");
            const auto& rows = detail::field(result.document, "trials").items;
            require(rows.size() == 4, "failure study lost terminal rows");
            for (const auto& row : rows) {
                identity(row, fixture);
                const auto market = string(detail::field(row, "parameters"), "Market");
                if (market == fixture.a.symbol) {
                    require(string(row, "status") == "ok", "healthy context inherited an error");
                    require(detail::field(row, "objective").real() < 1000,
                            "healthy context inherited the failed report");
                } else {
                    const bool engine = std::string(mode) == "engine" ||
                                        std::string(mode) == "scratch";
                    require(string(row, "status") == (engine ? "engine_error" : "trial_error") &&
                                detail::field(row, "objective").kind == Json::Kind::Null &&
                                !boolean(row, "feasible"),
                            "failed setting, report or engine result became rankable");
                    const std::string code = std::string(mode) == "setting" ? "setting_rejected" :
                        std::string(mode) == "report" ? "hpo_report_invalid" :
                        std::string(mode) == "scratch" ? "scratch_capacity_exceeded" :
                                                       "strategy_runtime_error";
                    require(string(row, "failure_code") == code,
                            "context failure lost the original engine or adapter code");
                    const auto& arguments = detail::field(row, "failure_args");
                    if (std::string(mode) == "scratch")
                        require(string(arguments, "resource") == "successor_roots",
                                "scratch failure lost its engine arguments");
                    else if (std::string(mode) == "setting")
                        require(string(arguments, "entrypoint") == "strategy_set_input",
                                "setting failure lost its engine arguments");
                    require(string(row, "failure_origin") ==
                                (std::string(mode) == "report" ? "hpo" : "engine"),
                            "failure origin was attributed to the other execution layer");
                }
            }
            if (std::string(mode) == "scratch")
                require(integer(observations.snapshot(), "arena_calls") > 4,
                        "scratch failure bypassed main-thread admission or fresh-handle admission");
            clean(observations);
        }
    }
}

void sampler_batches(const Fixture& fixture,
                     const std::shared_ptr<const pfh::StrategyPlugin>& plugin,
                     const Observations& observations) {
    for (const auto* sampler : {"grid", "random", "tpe", "dlib_global", "candidates", "sobol"}) {
        for (const bool finite_tpe : {false, true}) {
            if (finite_tpe && std::string(sampler) != "tpe")
                continue;
            std::string reference;
            for (const unsigned workers : {1u, 2u, 4u}) {
                observations.clear();
                cli::Options options;
                options.sampler = sampler;
                options.workers = workers;
                options.batch_size = 4;
                options.batch_lag = 1;
                options.max_trials = 12;
                options.tpe_config.startup_trials = 4;
                options.fixed_inputs = {{"DelayMarket", fixture.b.symbol}, {"DelayMs", "3"}};
                options.dimensions = {
                    pfh::IntegerDimension("Length", 1, 6),
                    pfh::CategoricalDimension("Market", {fixture.a.symbol, fixture.b.symbol})};
                if (finite_tpe)
                    options.candidate_policy = pfh::CandidatePolicy::Exhaustive;
                const auto result = run(study(fixture, plugin, options));
                require(result.result.exit_code == 0 && result.result.trials_completed == 12,
                        std::string(sampler) + " did not execute its declared budget");
                const auto& rows = detail::field(result.document, "trials");
                require(rows.items.size() == 12, "sampler lost trial rows");
                for (const auto& row : rows.items)
                    identity(row, fixture);
                const auto values = detail::dump_json(rows);
                if (reference.empty())
                    reference = values;
                else
                    require(reference == values,
                            std::string(sampler) + " changed trial values across worker counts");
                if (std::string(sampler) == "grid" || finite_tpe) {
                    std::set<std::string> vectors;
                    for (const auto& row : rows.items)
                        vectors.insert(detail::dump_json(detail::field(row, "parameters")));
                    require(vectors.size() == 12 &&
                                boolean(result.document, "full_parameter_coverage") &&
                                boolean(result.document, "exhaustive_equivalent"),
                            "finite study did not cover complete vectors including the context");
                }
                clean(observations);
            }
        }
    }
}

void rolling_completion(const Fixture& fixture,
                        const std::shared_ptr<const pfh::StrategyPlugin>& plugin,
                        const Observations& observations) {
    for (const bool stop : {false, true}) {
        observations.clear();
        Pipe entered;
        Pipe release;
        Pipe proposed_later;
        cli::Options options;
        options.schedule = "rolling";
        options.workers = 2;
        options.max_trials = 12;
        options.fixed_inputs = {
            {"DelayMarket", fixture.b.symbol}, {"DelayMs", "20"},
            {"GateMarket", fixture.b.symbol}, {"GateLength", "1"},
            {"EnteredFd", std::to_string(entered.writer())},
            {"ReleaseFd", std::to_string(release.reader())},
            {"NotifyMarket", fixture.b.symbol}, {"NotifyLength", "2"},
            {"NotifyFd", std::to_string(proposed_later.writer())}};
        options.dimensions = {
            pfh::IntegerDimension("Length", 1, 6),
            pfh::CategoricalDimension("Market", {fixture.a.symbol, fixture.b.symbol})};
        if (stop) {
            options.no_improvement_trials = 2;
            options.objective = "-metrics.all.net_profit";
        }
        const auto prepared = study(fixture, plugin, options);
        auto evaluation = std::async(std::launch::async, [&] { return run(prepared); });
        try {
            entered.receive();
            // With two slots, the later B proposal is possible only after A's first and second
            // trials were fed back. Keep the first B held until that proposal enters the engine.
            proposed_later.receive();
        } catch (...) {
            release.release();
            evaluation.wait();
            throw;
        }
        release.release();
        const auto result = evaluation.get();
        const auto& rows = detail::field(result.document, "trials").items;
        const auto& order = detail::field(result.document, "completion_order").items;
        const auto& prefixes = detail::field(result.document, "completed_before").items;
        require(!rows.empty() && order.size() == rows.size() && prefixes.size() == rows.size(),
                "rolling drain lost a terminal row or proposal prefix");
        require(order.size() >= 4 && detail::json_integer(order[0]) == 0 &&
                    detail::json_integer(order[1]) == 2 &&
                    detail::json_integer(prefixes[3]) == 2,
                "rolling completion order or proposal prefix violated the held-context gate");
        std::set<std::int64_t> ids;
        std::uint64_t previous_prefix = 0;
        for (std::size_t index = 0; index < rows.size(); ++index) {
            const auto& row = rows[index];
            identity(row, fixture);
            require(integer(row, "trial_id") == static_cast<std::int64_t>(index),
                    "rolling output lost ordered trial IDs");
            const auto prefix = static_cast<std::uint64_t>(detail::json_integer(prefixes[index]));
            require(prefix >= previous_prefix && prefix <= index,
                    "rolling proposal saw an impossible completion prefix");
            if (index < options.workers)
                require(prefix == 0, "initial rolling slots saw feedback before admission");
            else
                require(prefix > 0, "rolling refill did not see any completed trial");
            for (std::size_t seen = 0; seen < prefix; ++seen)
                require(detail::json_integer(order[seen]) < static_cast<std::int64_t>(index),
                        "proposal prefix contains a trial not yet proposed");
            previous_prefix = prefix;
            require(ids.insert(detail::json_integer(order[index])).second,
                    "rolling completion log repeated a trial");
        }
        const auto snapshot = observations.snapshot();
        require(detail::field(snapshot, "runs").items.size() == rows.size(),
                "rolling executed a trial twice or omitted execution");
        std::set<std::pair<std::string, std::int64_t>> vectors;
        for (const auto& event : detail::field(snapshot, "runs").items) {
            const auto market = string(event, "market");
            require(vectors.emplace(market, integer(event, "length")).second,
                    "rolling duplicated a complete candidate");
            require(integer(event, "bars") == static_cast<std::int64_t>(
                        reinterpret_cast<std::uintptr_t>(fixture.context(market).view.data())),
                    "rolling slot refill borrowed the preceding slot's chart");
        }
        if (stop) {
            require(string(result.document, "stop_reason") == "no_improvement" &&
                        integer(detail::field(result.document, "early_stop"),
                                "drained_through_trial_id") ==
                            static_cast<std::int64_t>(rows.size() - 1),
                    "rolling patience did not stop and drain its proposal frontier");
        } else {
            require(rows.size() == 12, "rolling budget was not completed");
        }
        clean(observations);
    }
}

// Overwrites the first byte (the size is unchanged) and moves the modification time, so an
// integrity check that compares either size or time must notice. Deterministic: not a wait.
void mutate_mapping(const Path& path) {
    std::fstream output(path, std::ios::in | std::ios::out | std::ios::binary);
    output.put('\x01');
    output.close();
    require(bool(output), "cannot mutate held fixture mapping");
    std::filesystem::last_write_time(
        path, std::filesystem::last_write_time(path) + std::chrono::seconds(2));
}

void storage_formats_and_refusals(
    const Path& directory, const std::shared_ptr<const pfh::StrategyPlugin>& plugin,
    const Observations& observations) {
    for (const bool a_binary : {false, true}) {
        for (const bool b_binary : {false, true}) {
            Fixture fixture(directory / (std::to_string(a_binary) + std::to_string(b_binary)),
                            a_binary, b_binary);
            observations.clear();
            const auto result = run(study(fixture, plugin));
            require(result.result.trials_completed == 4 && result.result.exit_code == 0,
                    "CSV, binary or mixed catalogue did not execute");
            observed_routes(observations.snapshot(), fixture, false);
            fixture.catalogue->verify_unchanged();
            clean(observations);
        }
    }
    for (const auto* damage : {"sidecar", "digest", "order", "symbol", "timeframe"}) {
        Fixture fixture(directory / damage, true, true);
        const auto path = fixture.b.path;
        const auto sidecar_path = Path(path.string() + ".json");
        auto sidecar = detail::parse_json(read(sidecar_path));
        if (std::string(damage) == "sidecar")
            sidecar.members.erase("count");
        else if (std::string(damage) == "digest")
            sidecar.members["sha256"] = Json::string(std::string(64, '0'));
        else if (std::string(damage) == "symbol")
            sidecar.members["symbol"] = Json::string(fixture.a.symbol);
        else if (std::string(damage) == "timeframe")
            sidecar.members["tf"] = Json::string(fixture.a.input_tf);
        else {
            auto bytes = read(path);
            const auto first = bytes.substr(0, 48);
            bytes.replace(0, 48, bytes.substr(48, 48));
            bytes.replace(48, 48, first);
            write(path, bytes);
            sidecar.members["first_ts"] = Json::number("60000");
            sidecar.members["sha256"] = Json::string(detail::sha256(bytes));
        }
        write(sidecar_path, detail::dump_json(sidecar));
        const detail::SymbolBindingDeclaration binding{
            "Market", detail::SymbolBindingRole::Chart, {fixture.a.symbol, fixture.b.symbol},
            {{fixture.b.symbol, fixture.b.id}, {fixture.a.symbol, fixture.a.id}}};
        const detail::DataContextSettings settings{
            "artifact", std::string(64, 'a'), {}, {}, "fixture", "full_input", "none", nullptr};
        refusal([&] {
            detail::AdmittedDataCatalogue(
                {fixture.a, fixture.b}, {fixture.a.id, fixture.b.id}, {binding}, settings,
                {"fixture"});
        }, "hpo_dataset_invalid");
    }
}

void all_mapping_guards(const Path& directory,
                        const std::shared_ptr<const pfh::StrategyPlugin>& plugin,
                        const Observations& observations) {
    for (const bool after_progress : {false, true}) {
        Fixture fixture(directory / (after_progress ? "after-progress" : "held"), true, true);
        const auto& non_last = fixture.catalogue->contexts().front();
        require(non_last.id != fixture.catalogue->contexts().back().id &&
                    non_last.identity.symbol == fixture.b.symbol,
                "mutation fixture must target the non-last admitted context");
        Pipe entered;
        Pipe released;
        cli::Options options;
        options.max_trials = 1;  // Only the other chart executes; this mapping stays unsampled.
        options.batch_size = 1;
        options.trials_file = directory / (after_progress ? "after.jsonl" : "held.jsonl");
        if (!after_progress)
            options.fixed_inputs = {{"GateMarket", fixture.a.symbol},
                                    {"EnteredFd", std::to_string(entered.writer())},
                                    {"ReleaseFd", std::to_string(released.reader())}};
        const auto prepared = study(fixture, plugin, options);
        bool published = false;
        cli::StudyCallbacks callbacks;
        callbacks.publish = [&](const detail::ResultDocument&, const cli::StudyStatistics&) {
            published = true;
        };
        if (after_progress) {
            callbacks.before_publish = [&] {
                require(!read(options.trials_file).empty(),
                        "mutation did not occur after the trial's progress row");
                mutate_mapping(non_last.identity.path);
            };
            refusal([&] { cli::run_study(prepared, callbacks); }, "hpo_dataset_invalid");
        } else {
            auto evaluation = std::async(std::launch::async, [&] {
                refusal([&] { cli::run_study(prepared, callbacks); }, "hpo_dataset_invalid");
            });
            try {
                entered.receive();
                mutate_mapping(non_last.identity.path);
            } catch (...) {
                released.release();
                evaluation.wait();
                throw;
            }
            released.release();
            evaluation.get();
        }
        require(!published, "changed non-last input published a final success");
        fixture.context(fixture.a.symbol).view.owner->verify_unchanged();
        clean(observations);
    }
}

void all_output_aliases(const Path& directory,
                        const std::shared_ptr<const pfh::StrategyPlugin>& plugin,
                        const Observations& observations) {
    Fixture fixture(directory, true, true);
    const auto inputs = fixture.catalogue->binary_inputs();
    require(inputs.size() == 4, "binary alias inventory omitted an input or sidecar");
    unsigned index = 0;
    for (const auto& input : inputs) {
        for (const bool hardlink : {false, true}) {
            const auto alias = directory / ("alias-" + std::to_string(index++));
            if (hardlink)
                std::filesystem::create_hard_link(input, alias);
            const auto output = hardlink ? alias : input;
            for (const int destination : {0, 1, 2}) {
                cli::Options options;
                options.trials_file = directory / "untouched.jsonl";
                write(options.trials_file, "unchanged");
                if (destination == 0)
                    options.output = output;
                else if (destination == 1)
                    options.trials_file = output;
                else
                    options.scheduler_stats = output;
                const auto before = read(input);
                const auto handles = integer(observations.snapshot(), "created");
                const auto prepared = study(fixture, plugin, options);
                refusal([&] { run(prepared); }, "hpo_cli_usage");
                require(read(input) == before &&
                            read(directory / "untouched.jsonl") == "unchanged" &&
                            integer(observations.snapshot(), "created") == handles,
                        "alias refusal opened an output or created a validation handle");
            }
        }
    }
    clean(observations);
}

class OutputFile final {
public:
    explicit OutputFile(const Path& path)
        : descriptor_(::open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600)) {
        require(descriptor_ >= 0, "cannot open progress fixture");
    }
    ~OutputFile() { ::close(descriptor_); }
    int descriptor() const { return descriptor_; }

private:
    int descriptor_;
};

class CaptureStdout final {
public:
    CaptureStdout() : previous_(std::cout.rdbuf(output_.rdbuf())) {}
    ~CaptureStdout() { std::cout.rdbuf(previous_); }
    std::string bytes() const { return output_.str(); }

private:
    std::ostringstream output_;
    std::streambuf* previous_;
};

std::vector<Json> lines(const std::string& bytes) {
    std::istringstream input(bytes);
    std::vector<Json> result;
    std::string line;
    while (std::getline(input, line))
        result.push_back(detail::parse_json(line, line.size()));
    return result;
}

void row_space_identity(const Json& row, const Fixture& fixture, cli::RowSpace row_space) {
    identity(row, fixture);
    require((row.find("space") != nullptr) == (row_space == cli::RowSpace::kFull) &&
                (row.find("space_hash") != nullptr) == (row_space != cli::RowSpace::kNone),
            "context identity changed the row-space policy");
}

void output_paths(const Path& directory, const Fixture& fixture,
                  const std::shared_ptr<const pfh::StrategyPlugin>& plugin,
                  const Observations& observations) {
    std::filesystem::create_directories(directory);
    unsigned invocation = 0;
    for (const auto* retention : {"all", "best-k", "none"}) {
        for (const auto row_space :
             {cli::RowSpace::kFull, cli::RowSpace::kHash, cli::RowSpace::kNone}) {
            for (const auto* mode : {"ok", "engine", "setting", "report", "scratch", "objective",
                                     "constraint", "constraint_error", "pruned"}) {
                for (const bool file : {false, true}) {
                    Environment arena(
                        "PF_SCRATCH_ARENA_FD", std::string(mode) == "scratch" ? "1" : nullptr);
                    const auto stem = directory / std::to_string(invocation++);
                    const auto progress_path = Path(stem.string() + ".progress");
                    OutputFile progress(progress_path);
                    cli::Options options;
                    options.workers = 4;
                    options.trials_out = retention;
                    options.best_k = 4;
                    options.row_space = row_space;
                    options.progress_fd = progress.descriptor();
                    options.trials_file = stem.string() + ".jsonl";
                    if (file)
                        options.output = stem.string() + ".json";
                    if (std::string(mode) == "engine" || std::string(mode) == "setting" ||
                        std::string(mode) == "report" || std::string(mode) == "scratch" ||
                        std::string(mode) == "objective")
                        options.fixed_inputs = {
                            {"FaultMarket", fixture.b.symbol}, {"Failure", mode}};
                    if (std::string(mode) == "constraint")
                        options.constraints = {"metrics.all.net_profit < 100"};
                    if (std::string(mode) == "constraint_error")
                        options.constraints = {"1 / (metrics.all.net_profit - 205)"};
                    if (std::string(mode) == "pruned") {
                        options.pruner = pfh::PrunerKind::Median;
                        options.pruner_name = "median";
                        options.pruner_rungs = {0.5};
                    }
                    auto prepared = study(fixture, plugin, options);
                    if (std::string(mode) == "pruned") {
                        prepared.pruner.observe({1000.0});
                        prepared.pruner.observe({1000.0});
                    }
                    std::string bytes;
                    {
                        CaptureStdout capture;
                        cli::StudyCallbacks callbacks;
                        callbacks.publish = [&](const detail::ResultDocument& document,
                                                const cli::StudyStatistics& statistics) {
                            cli::publish(prepared.options, document, statistics);
                        };
                        const auto result = cli::run_study(prepared, callbacks);
                        require(result.trials_completed == 4 &&
                                    result.exit_code == (std::string(mode) == "pruned" ? 2 : 0),
                                "terminal output study did not complete its selected contexts");
                        bytes = capture.bytes();
                    }
                    if (file)
                        require(read(options.output) == bytes, "stdout and file bytes differ");
                    const auto document = detail::parse_json(bytes, bytes.size());
                    require(detail::field(document, "dataset_id").kind == Json::Kind::Null &&
                                detail::dump_json(detail::field(document, "dataset_ids")) ==
                                    "[\"alpha\",\"zeta\"]" &&
                                string(document, "data_context_sha256") ==
                                    fixture.catalogue->data_context_sha256() &&
                                detail::dump_json(detail::field(
                                    detail::field(document, "applied_runtime"), "data_context")) ==
                                    fixture.catalogue->canonical_descriptor(),
                            "bound result lost its full catalogue provenance");
                    const auto& rows = detail::field(document, "trials").items;
                    for (const auto& row : rows)
                        row_space_identity(row, fixture, row_space);
                    if (const auto* summary = document.find("summary")) {
                        for (const auto& row : detail::field(*summary, "best_k").items)
                            row_space_identity(row, fixture, row_space);
                    }
                    const auto streamed_bytes = read(options.trials_file);
                    require(streamed_bytes == read(progress_path),
                            "progress and trial-file rows disagree");
                    const auto streamed = lines(streamed_bytes);
                    require(streamed.size() == 4, "stream lost a failed or discarded terminal row");
                    for (const auto& row : streamed) {
                        row_space_identity(row, fixture, row_space);
                        const auto market = string(detail::field(row, "parameters"), "Market");
                        std::string expected = std::string(mode) == "pruned" ? "pruned" : "ok";
                        if (market == fixture.b.symbol) {
                            if (std::string(mode) == "engine" || std::string(mode) == "scratch")
                                expected = "engine_error";
                            else if (std::string(mode) == "setting" ||
                                     std::string(mode) == "report")
                                expected = "trial_error";
                            else if (std::string(mode) == "objective")
                                expected = "objective_error";
                            else if (std::string(mode) == "constraint")
                                expected = "constraint_violation";
                            else if (std::string(mode) == "constraint_error" &&
                                     integer(detail::field(row, "parameters"), "Length") == 1)
                                expected = "constraint_error";
                        }
                        require(string(row, "status") == expected,
                                "streamed terminal status belongs to another context");
                    }
                    if (std::string(retention) == "all")
                        require(rows.size() == 4, "all retention lost terminal rows");
                    else if (std::string(retention) == "none")
                        require(rows.empty(), "none retention materialized trial rows");
                    clean(observations);
                }
            }
        }
    }
}

void pending_and_skipped_records(
    const Fixture& fixture, const std::shared_ptr<const pfh::StrategyPlugin>& plugin) {
    cli::Options options;
    options.trials_out = "none";
    const auto prepared = study(fixture, plugin, options);
    cli::TrialArchive archive(prepared.options, prepared.space, true);
    cli::RunState state(prepared.options, cli::RunState::Clock::now(), archive,
                        [] { return true; }, {}, {});
    for (std::uint64_t ordinal = 0; ordinal < 4; ++ordinal) {
        auto record = cli::make_trial_record(
            prepared.space.candidate_at(ordinal, ordinal), prepared.options);
        require(record.status == "pending" && record.data_context.has_value(),
                "record was constructed before selecting its context");
        require(!state.begin(record), "cancelled queued record began execution");
        state.skip(record.trial_id);
        identity(detail::parse_json(cli::render_trial(record)), fixture);
    }
    state.shutdown();
    require(archive.completed == 0, "never-started records became terminal output");
}

void timeout_publication(const Path& directory, const Fixture& fixture,
                         const std::shared_ptr<const pfh::StrategyPlugin>& plugin) {
    std::filesystem::create_directories(directory);
    unsigned invocation = 0;
    for (const auto* retention : {"all", "best-k", "none"}) {
        for (const auto row_space :
             {cli::RowSpace::kFull, cli::RowSpace::kHash, cli::RowSpace::kNone}) {
            for (const bool wall : {false, true}) {
                const auto stem = directory / std::to_string(invocation++);
                const auto result_path = Path(stem.string() + ".json");
                const auto progress_path = Path(stem.string() + ".progress");
                const auto trial_path = Path(stem.string() + ".jsonl");
                const auto child = ::fork();
                require(child >= 0, "cannot fork timeout fixture");
                if (child == 0) {
                    try {
                        Pipe entered;
                        Pipe release;
                        OutputFile progress(progress_path);
                        cli::Options options;
                        options.sampler = "candidates";
                        options.max_trials = 2;
                        options.trials_out = retention;
                        options.row_space = row_space;
                        options.progress_fd = progress.descriptor();
                        options.trials_file = trial_path;
                        if (wall) {
                            options.max_wall_seconds = 0.1;
                            options.wall_grace_seconds = 0;
                        } else {
                            options.trial_timeout_seconds = 0.1;
                        }
                        const auto market = wall ? fixture.a.symbol : fixture.b.symbol;
                        options.fixed_inputs = {{"GateMarket", market},
                            {"EnteredFd", std::to_string(entered.writer())},
                            {"ReleaseFd", std::to_string(release.reader())}};
                        auto prepared = study(fixture, plugin, options);
                        const auto preceding = wall ? fixture.b.symbol : fixture.a.symbol;
                        prepared.options.candidate_list =
                            std::make_shared<const detail::CandidateList>(
                                detail::parse_candidate_list(
                                    "{\"Length\":1,\"Market\":\"" + preceding + "\"}\n" +
                                    "{\"Length\":1,\"Market\":\"" + market + "\"}\n",
                                    prepared.space, prepared.options.fixed_inputs));
                        cli::StudyCallbacks callbacks;
                        callbacks.timeout_publish = [&](const detail::ResultDocument& document) {
                            std::ostringstream bytes;
                            document.write(bytes);
                            write(result_path, bytes.str());
                        };
                        callbacks.timeout_exit = [](std::exception_ptr) { ::_exit(3); };
                        callbacks.timeout_failure = [](const std::exception&) { ::_exit(90); };
                        cli::run_study(prepared, callbacks);
                        ::_exit(91);
                    } catch (...) {
                        ::_exit(92);
                    }
                }
                int status;
                require(::waitpid(child, &status, 0) == child && WIFEXITED(status) &&
                            WEXITSTATUS(status) == 3,
                        "timeout fixture did not publish through the production watchdog");
                const auto bytes = read(result_path);
                const auto document = detail::parse_json(bytes, bytes.size());
                require(string(document, "stop_reason") == (wall ? "deadline" : "trial_timeout") &&
                            integer(document, "trials_completed") == 2,
                        "timeout publication lost the active trial");
                for (const auto& row : detail::field(document, "trials").items) {
                    row_space_identity(row, fixture, row_space);
                    require(string(row, "status") ==
                                (integer(row, "trial_id") == 1 ? "trial_timeout" : "ok"),
                            "timeout record inherited a preceding terminal status");
                }
                if (const auto* summary = document.find("summary")) {
                    for (const auto& row : detail::field(*summary, "best_k").items)
                        row_space_identity(row, fixture, row_space);
                }
                const auto streamed = lines(read(trial_path));
                require(streamed.size() == 2 && read(trial_path) == read(progress_path),
                        "watchdog did not flush the identified timeout row");
                row_space_identity(streamed.front(), fixture, row_space);
                row_space_identity(streamed.back(), fixture, row_space);
                require(string(streamed.front(), "status") == "ok" &&
                            string(streamed.back(), "status") == "trial_timeout",
                        "timeout stream contains the wrong terminal record");
            }
        }
    }
}

void timeout_mapping_guard(const Path& directory,
                           const std::shared_ptr<const pfh::StrategyPlugin>& plugin) {
    std::filesystem::create_directories(directory);
    const auto child = ::fork();
    require(child >= 0, "cannot fork integrity timeout fixture");
    const auto success = directory / "success.json";
    const auto failure = directory / "failure.json";
    if (child == 0) {
        try {
            Fixture fixture(directory, true, true);
            Pipe entered;
            Pipe release;
            cli::Options options;
            options.max_trials = 1;
            options.trial_timeout_seconds = 0.05;
            options.trials_file = directory / "trials.jsonl";
            options.fixed_inputs = {{"GateMarket", fixture.a.symbol},
                {"EnteredFd", std::to_string(entered.writer())},
                {"ReleaseFd", std::to_string(release.reader())}};
            const auto prepared = study(fixture, plugin, options);
            cli::StudyCallbacks callbacks;
            callbacks.before_publish = [&] {
                require(!read(options.trials_file).empty(),
                        "timeout mutation must follow its progress row");
                const auto& first = fixture.catalogue->contexts().front();
                require(first.id != fixture.catalogue->contexts().back().id,
                        "timeout mutation did not target a non-last mapping");
                mutate_mapping(first.identity.path);
            };
            callbacks.timeout_publish = [&](const detail::ResultDocument& document) {
                std::ostringstream bytes;
                document.write(bytes);
                write(success, bytes.str());
            };
            callbacks.timeout_exit = [](std::exception_ptr) { ::_exit(3); };
            callbacks.timeout_failure = [&](const std::exception& error) {
                write(failure, detail::failure_document(error, 3));
                ::_exit(17);
            };
            cli::run_study(prepared, callbacks);
            ::_exit(91);
        } catch (...) {
            ::_exit(92);
        }
    }
    int status;
    require(::waitpid(child, &status, 0) == child && WIFEXITED(status) &&
                WEXITSTATUS(status) == 17 && !std::filesystem::exists(success),
            "changed non-last mapping published a timeout result");
    const auto document = detail::parse_json(read(failure));
    require(string(detail::field(document, "failure"), "code") == "hpo_dataset_invalid",
            "timeout integrity failure lost its dataset diagnostic");
}

void unbound_bytes(const Path& directory, const Path& existing_plugin) {
    std::filesystem::create_directories(directory);
    const auto csv = directory / "bars.csv";
    tape(csv, 10, 2, false, "unused", "1");
    // Source-derived bytes of the existing adapter fixture: Length=1 and default capital give
    // 1 + 100000/1000 = 101. The unchanged writer emits these counters and this metric in order.
    // The space hash is the independently hashed, hand-written canonical space, without an LF.
    const std::string space =
        R"({"objective":{"constraints":[],"direction":"maximize",)"
        R"("expression":"metrics.all.net_profit"},"parameters":{"Length":{"high":1,)"
        R"("kind":"integer","log":false,"low":1,"step":1}}})";
    const std::string hash = "2b794142e6de848190e89f060259924a23eff23bd835f5443a7ad892dd88fe51";
    const std::string base_row =
        R"({"trial_id": 0, "status": "ok", "feasible": true, "objective": 101, )"
        R"("total_trades": 1, "net_profit": 101, "backtest": {"input_bars_processed": 2, )"
        R"("script_bars_processed": 1, "input_tf_seconds": 60, "script_tf_seconds": 300, )"
        R"("script_tf_ratio": 5, "needs_aggregation": true, "magnifier_sample_ticks_total": 72}, )"
        R"("parameters": {"Length": 1}, "constraint_values": [], )"
        R"("metrics": {"metrics.all.net_profit": 101}, "error": "", "failure_code": null, )"
        R"("failure_args": null, "failure_origin": null)";
    for (const auto row_space :
         {cli::RowSpace::kFull, cli::RowSpace::kHash, cli::RowSpace::kNone}) {
        std::string row = base_row;
        if (row_space != cli::RowSpace::kNone) {
            row += ", \"space_hash_version\": 1, \"space_hash\": \"" + hash + '"';
            if (row_space == cli::RowSpace::kFull)
                row += ", \"space\": " + space;
        }
        row += '}';
        const std::string expected =
            "{\n"
            "  \"schema_version\": 1,\n"
            "  \"space_hash_version\": 1,\n"
            "  \"space_hash\": \"" + hash + "\",\n"
            "  \"space\": " + space + ",\n"
            "  \"pineforge_hpo_version\": \"" PINEFORGE_HPO_VERSION "\",\n"
            "  \"sampler_implementation\": \"pineforge_grid_v2_finite\",\n"
            "  \"ok\": true,\n"
            "  \"artifact_key\": \"\",\n"
            "  \"sampler\": \"grid\",\n"
            "  \"candidate_policy\": \"sampler_default\",\n"
            "  \"candidate_policy_implementation\": \"sampler_default\",\n"
            "  \"seed\": 17,\n"
            "  \"tpe_history_switch\": null,\n"
            "  \"sampler_config\": {},\n"
            "  \"direction\": \"maximize\",\n"
            "  \"batch_size\": 1,\n"
            "  \"batch_lag\": 0,\n"
            "  \"replay_contract\": \"ordered_batches_v1\",\n"
            "  \"continuation_contract\": \"ordered_batches_v1\",\n"
            "  \"pruner\": \"none\",\n"
            "  \"pruner_eta\": 2,\n"
            "  \"pruner_rungs\": [0.25, 0.5],\n"
            "  \"search_space_finite\": true,\n"
            "  \"search_space_cardinality_overflow\": false,\n"
            "  \"search_space_cardinality\": 1,\n"
            "  \"trials_requested\": 1,\n"
            "  \"trials_completed\": 1,\n"
            "  \"unique_candidates_attempted\": 1,\n"
            "  \"duplicate_proposals_skipped\": 0,\n"
            "  \"remaining_candidates\": 0,\n"
            "  \"search_space_exhausted\": true,\n"
            "  \"stop_reason\": \"search_space_exhausted\",\n"
            "  \"full_parameter_coverage\": true,\n"
            "  \"exhaustive_equivalent\": true,\n"
            "  \"best_trial_id\": 0,\n"
            "  \"best_value\": 101,\n"
            "  \"trials\": [\n"
            "    " + row + "\n"
            "  ],\n"
            "  \"trials_out\":\"all\",\n"
            "  \"best_k\":10\n"
            "}\n";
        cli::Options options;
        options.strategy = existing_plugin;
        options.ohlcv = csv;
        options.output = directory / ("unbound-" + std::to_string(static_cast<int>(row_space)) +
                                       ".json");
        options.trials_file = options.output.string() + ".jsonl";
        options.seed = 17;
        options.max_trials = 1;
        options.batch_size = 1;
        options.row_space = row_space;
        options.objective = "metrics.all.net_profit";
        options.input_timeframe = "1";
        options.script_timeframe = "5";
        options.bar_magnifier = true;
        options.magnifier_samples = 6;
        options.magnifier_distribution = PF_MAGNIFIER_TRIANGLE;
        options.dimensions.emplace_back(pfh::IntegerDimension("Length", 1, 1));
        auto prepared = cli::prepare(options);
        require(!prepared.context_selector && !prepared.options.context_selector,
                "ordinary admission constructed a bound selector");
        const auto progress_path = Path(options.output.string() + ".progress");
        OutputFile progress(progress_path);
        prepared.options.progress_fd = progress.descriptor();
        std::string bytes;
        {
            CaptureStdout capture;
            cli::StudyCallbacks callbacks;
            callbacks.before_publish = [&] { prepared.dataset->verify_unchanged(); };
            callbacks.publish = [&](const detail::ResultDocument& document,
                                    const cli::StudyStatistics& statistics) {
                cli::publish(prepared.options, document, statistics);
            };
            const auto result = cli::run_study(
                prepared, pfh::DatasetView(prepared.dataset), callbacks);
            require(result.exit_code == 0, "existing unbound fixture did not succeed");
            bytes = capture.bytes();
        }
        require(bytes == expected && read(options.output) == expected,
                "unbound document bytes differ from the source-derived golden");
        require(read(options.trials_file) == row + "\n" && read(progress_path) == row + "\n",
                "unbound streamed bytes differ from the source-derived golden");
        require(bytes.find("data_context") == std::string::npos &&
                    bytes.find("dataset_id") == std::string::npos,
                "unbound output gained a context field");
    }
}

void selector_refusals(const Fixture& fixture,
                       const std::shared_ptr<const pfh::StrategyPlugin>& plugin) {
    const auto prepared = study(fixture, plugin);
    auto incomplete = prepared.space.candidate_at(0, 0);
    incomplete.values.erase("Length");
    refusal([&] { cli::make_trial_record(incomplete, prepared.options); },
            "hpo_study_spec_invalid");
    const pfh::SearchSpace reversed({
        pfh::IntegerDimension("Length", 1, 2),
        pfh::CategoricalDimension("Market", {fixture.b.symbol, fixture.a.symbol})});
    refusal([&] {
        cli::ContextSelector selector(fixture.catalogue, reversed, "Market",
            [&](const detail::BoundTrialContext& context) {
                return fixture.configuration(context);
            });
    }, "hpo_study_spec_invalid");
    refusal([&] {
        cli::ContextSelector selector(fixture.catalogue, prepared.space, "Market",
            [&](const detail::BoundTrialContext& context) {
                auto configuration = fixture.configuration(context);
                configuration.symbol_info->pointvalue = 17;
                return configuration;
            });
    }, "hpo_study_spec_invalid");
    auto unbound = study(fixture, plugin);
    unbound.context_selector.reset();
    refusal([&] { cli::run_study(unbound, cli::StudyCallbacks{}); }, "hpo_invariant");
}

}  // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 4, "usage: symbol routing test <routing-plugin> <adapter-plugin> <output>");
        const Path directory = Path(argv[3]) / std::to_string(::getpid());
        std::filesystem::create_directories(directory);
        const Environment temporary("TMPDIR", directory.c_str());
        const Environment unjailed("PF_SCRATCH_ARENA_FD", nullptr);
        Observations observations(argv[1]);
        require(integer(observations.snapshot(), "admissions") == 0,
                "observation loading unexpectedly admitted the native plugin");
        const auto plugin = std::make_shared<const pfh::StrategyPlugin>(argv[1]);
        require(integer(observations.snapshot(), "admissions") == 1,
                "native plugin admission was not counted exactly once");
        Fixture fixture(directory / "charts");
        selector_refusals(fixture, plugin);
        routes_and_admission(fixture, plugin, observations);
        failure_isolation(fixture, plugin, observations);
        storage_formats_and_refusals(directory / "formats", plugin, observations);
        all_output_aliases(directory / "aliases", plugin, observations);
        all_mapping_guards(directory / "mutation", plugin, observations);
        sampler_batches(fixture, plugin, observations);
        rolling_completion(fixture, plugin, observations);
        output_paths(directory / "outputs", fixture, plugin, observations);
        pending_and_skipped_records(fixture, plugin);
        timeout_publication(directory / "timeouts", fixture, plugin);
        timeout_mapping_guard(directory / "timeout-mutation", plugin);
        unbound_bytes(directory / "unbound", argv[2]);
        clean(observations);
        require(integer(observations.snapshot(), "admissions") == 1,
                "a later study reloaded the native plugin");
        std::cout << "PASS native context routing, reporting, lifetime and integrity\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}

#endif
