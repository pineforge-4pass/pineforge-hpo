// Exercise internal admission and the production coordinator without public activation.
#define main pineforge_hpo_native_entry
#include "../src/cli/main.cpp"
#undef main

#include <dlfcn.h>

#include <atomic>
#include <iterator>
#include <set>

namespace {

namespace cli = pineforge::hpo::cli;
namespace detail = pineforge::hpo::detail;
using Json = detail::Json;
using Path = std::filesystem::path;

void require(bool condition, const std::string& message) {
    if (!condition)
        throw std::runtime_error(message);
}

std::string read(const Path& path) {
    std::ifstream input(path, std::ios::binary);
    require(bool(input), "cannot read continuation fixture");
    return {std::istreambuf_iterator<char>(input), {}};
}

void write(const Path& path, const std::string& bytes) {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    output.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    output.close();
    require(bool(output), "cannot write continuation fixture");
}

std::int64_t integer(const Json& value, const std::string& name) {
    return detail::json_integer(detail::field(value, name));
}

std::string string(const Json& value, const std::string& name) {
    return detail::field(value, name).text();
}

template <typename Function>
void rejected(Function&& function, const std::string& cause = {}) {
    try {
        function();
    } catch (const detail::WarmStartError& error) {
        require(error.code() == "hpo_warm_start_rejected" && error.args().empty(),
                "warm refusal lost its registered failure metadata");
        require(std::string(error.what()).find(cause) != std::string::npos,
                "warm refusal did not identify the expected incompatibility");
        return;
    }
    throw std::runtime_error("incompatible continuation was accepted");
}

class Observations final {
public:
    explicit Observations(const Path& plugin) : library_(::dlopen(plugin.c_str(), RTLD_NOW)) {
        require(library_ != nullptr, "cannot observe routing plugin");
        snapshot_ = reinterpret_cast<const char* (*)()>(::dlsym(library_, "routing_snapshot"));
        clear_ = reinterpret_cast<void (*)()>(::dlsym(library_, "routing_clear_events"));
        require(snapshot_ && clear_, "routing observation exports missing");
    }
    ~Observations() { ::dlclose(library_); }
    Json snapshot() const { return detail::parse_json(snapshot_()); }
    void clear() const { clear_(); }

private:
    void* library_;
    const char* (*snapshot_)();
    void (*clear_)();
};

class ScratchEnvironment final {
public:
    ScratchEnvironment() {
        if (const auto* value = std::getenv("PF_SCRATCH_ARENA_FD"))
            previous_ = value;
        require(::unsetenv("PF_SCRATCH_ARENA_FD") == 0, "cannot clear fixture scratch input");
    }
    ~ScratchEnvironment() {
        if (previous_)
            ::setenv("PF_SCRATCH_ARENA_FD", previous_->c_str(), 1);
    }

private:
    std::optional<std::string> previous_;
};

struct Counters {
    unsigned loads = 0;
    std::atomic<unsigned> owners{0};
    std::vector<std::weak_ptr<const pfh::Dataset>> observed;
};

struct TrackedOwner {
    std::shared_ptr<const pfh::Dataset> data;
    std::shared_ptr<Counters> counters;
    TrackedOwner(std::shared_ptr<const pfh::Dataset> value, std::shared_ptr<Counters> counts)
        : data(std::move(value)), counters(std::move(counts)) { ++counters->owners; }
    ~TrackedOwner() { --counters->owners; }
};

struct Fixture {
    std::shared_ptr<Counters> counters = std::make_shared<Counters>();
    std::vector<detail::ChartDataDeclaration> declarations;
    std::vector<std::string> references{"second", "first"};
    std::vector<detail::SymbolBindingDeclaration> bindings{
        {"Market", detail::SymbolBindingRole::Chart, {"asset-a", "asset-b"},
         {{"asset-b", "second"}, {"asset-a", "first"}}}};
    detail::DataContextSettings settings{
        "artifact", std::string(64, 'a'),
        {{"Count", std::int64_t{2}}, {"Enabled", true}, {"Label", std::string("α")},
         {"Threshold", 0.5}},
        {{"slippage", std::int64_t{0}}}, "exact_timestamps", "full_input", "none", nullptr};
    std::shared_ptr<const detail::AdmittedDataCatalogue> catalogue;

    explicit Fixture(const Path& directory) {
        std::filesystem::create_directories(directory);
        for (const auto& item : std::vector<std::pair<std::string, int>>{
                 {"second", 2}, {"first", 1}}) {
            const auto symbol = item.first == "first" ? "asset-a" : "asset-b";
            const auto path = directory / (item.first + ".csv");
            tape(path, item.second);
            declarations.push_back({
                item.first, symbol, "60", "60", "UTC",
                {symbol, "synthetic", "units", 1, 1, 1, "UTC", "24x7"},
                path, detail::ChartDataFormat::Csv, pfh::BinaryVerification::Full});
        }
        catalogue = admit(declarations, references, bindings, settings);
    }

    static void tape(const Path& path, int price) {
        const auto value = std::to_string(price);
        write(path, "timestamp,open,high,low,close,volume\n0," + value + "," + value + "," +
                    value + "," + value + "," + value + "\n");
    }

    std::shared_ptr<const detail::AdmittedDataCatalogue> admit(
        const std::vector<detail::ChartDataDeclaration>& datasets,
        const std::vector<std::string>& ids,
        const std::vector<detail::SymbolBindingDeclaration>& mapping,
        const detail::DataContextSettings& configuration) const {
        return std::make_shared<const detail::AdmittedDataCatalogue>(
            datasets, ids, mapping, configuration, std::set<std::string>{"exact_timestamps"},
            [&](const detail::ChartDataDeclaration& declaration) {
                ++counters->loads;
                auto tracked = std::make_shared<TrackedOwner>(
                    detail::load_bound_chart(declaration), counters);
                std::shared_ptr<const pfh::Dataset> owner(tracked, tracked->data.get());
                counters->observed.emplace_back(owner);
                return owner;
            });
    }
};

cli::PreparedStudy study(const std::shared_ptr<const detail::AdmittedDataCatalogue>& catalogue,
                         std::shared_ptr<const pfh::StrategyPlugin> plugin,
                         cli::Options options = {},
                         std::shared_ptr<const pfh::SymbolFeeds> feeds = nullptr) {
    options.dimensions = {pfh::IntegerDimension("Length", 1, 2),
                          pfh::CategoricalDimension(
                              "Market", {std::string("asset-a"), std::string("asset-b")})};
    options.objective = "metrics.all.net_profit";
    options.seed = 17;
    options.fixed_inputs = {
        {"Count", "2"}, {"Enabled", "true"}, {"Label", "α"}, {"Threshold", "0.5"}};
    options.strategy_overrides = {{"slippage", "0"}};
    if (!options.max_trials)
        options.max_trials = 4;
    if (!options.batch_size)
        options.batch_size = 2;
    options.tpe_config.startup_trials = 2;
    options.tpe_config.ei_candidates = 8;
    options.tpe_config.max_threads = 1;
    pfh::SearchSpace space(options.dimensions);
    const auto recorded = detail::recorded_space(space, options.objective, "maximize", {});
    options.space_json = std::make_shared<const std::string>(detail::dump_json(recorded));
    options.space_hash = detail::space_hash(recorded);
    options.context_selector = std::make_shared<const cli::ContextSelector>(
        catalogue, space, catalogue->chart_input_title(), [feeds](const auto& context) {
            const auto& declared = context.identity;
            const auto& facts = declared.syminfo;
            pfh::BacktestConfiguration configuration;
            configuration.input_timeframe = declared.input_tf;
            configuration.script_timeframe = declared.script_tf;
            configuration.chart_timezone = declared.chart_timezone;
            configuration.symbol_info = pfh::SymbolInfo{
                facts.mintick, facts.pointvalue, facts.mincontract, facts.timezone, facts.session};
            configuration.capture_equity_curve = false;
            configuration.symbol_feeds = feeds;
            return configuration;
        });
    const auto selector = options.context_selector;
    const auto cardinality = space.finite_cardinality();
    pfh::Pruner pruner(pfh::PrunerKind::None, options.pruner_rungs, options.pruner_eta, false);
    pfh::MetricExpression objective(options.objective);
    return {std::move(options), std::move(space), cardinality, std::move(pruner),
            std::move(objective), {}, {}, std::move(plugin),
            catalogue->contexts().front().view.owner, {}, selector};
}

Json run(const cli::PreparedStudy& prepared) {
    std::string bytes;
    cli::StudyCallbacks callbacks;
    callbacks.publish = [&](const detail::ResultDocument& document, const cli::StudyStatistics&) {
        std::ostringstream output;
        document.write(output);
        bytes = output.str();
    };
    const auto result = cli::run_study(prepared, callbacks);
    require(result.exit_code == 0, "bound component study did not succeed");
    return detail::parse_json(bytes, bytes.size());
}

void clean(const Observations& observations) {
    const auto counts = observations.snapshot();
    require(integer(counts, "live") == 0 && integer(counts, "violations") == 0 &&
                integer(counts, "created") == integer(counts, "freed"),
            "trial retained a handle/report or released them in the wrong order");
}

Json parent_for(const Json& source, const detail::AdmittedDataCatalogue& catalogue) {
    auto document = source;
    const detail::ContinuationDataContext context(catalogue.canonical_descriptor());
    document.members["data_context_sha256"] = Json::string(catalogue.data_context_sha256());
    auto ids = detail::array_json();
    for (const auto& item : catalogue.contexts())
        ids.items.push_back(Json::string(item.identity.id));
    document.members["dataset_ids"] = std::move(ids);
    document.members["applied_runtime"].members["data_context"] =
        detail::parse_json(
            catalogue.canonical_descriptor(), catalogue.canonical_descriptor().size());
    for (auto& row : document.members["trials"].items)
        context.annotate_row(row);
    return document;
}

std::string json_form(const Json& document, const std::string& form) {
    if (form == "result")
        return detail::dump_json(document);
    if (form == "array")
        return detail::dump_json(detail::field(document, "trials"));
    std::string bytes;
    for (const auto& row : detail::field(document, "trials").items)
        bytes += detail::dump_json(row) + "\n";
    return bytes;
}

void json_contracts(const Path& directory, const Fixture& fixture, const Json& parent,
                    const pfh::SearchSpace& space, const Json& recorded) {
    const detail::ContinuationDataContext expected(fixture.catalogue->canonical_descriptor());
    require(expected.canonical_descriptor() == read(directory / "descriptor.json") &&
                expected.data_context_sha256() == read(directory / "context.sha256"),
            "native catalogue bytes differ from the independent descriptor oracle");
    require(detail::dump_json(parent) == read(directory / "parent.json"),
            "complete-result JSON bytes differ from the independent canonical fixture");
    const auto path = directory / "json-parent";
    for (const std::string form : {"result", "array", "jsonl"}) {
        write(path, json_form(parent, form));
        const auto history = detail::load_warm_history(path, space, recorded, &expected);
        require(history.size() == 2 && history.tried_count() == 2 && history.next_id == 20 &&
                    history.completed == 2 && history.feasible == 2,
                "JSON continuation lost non-contiguous IDs or complete vectors");
        rejected([&] { detail::load_warm_history(path, space, recorded); });
        for (const std::string label : {"symbol", "dataset_id", "data_context_sha256"}) {
            auto forged = parent;
            forged.members["trials"].items.front().members[label] = Json::string("forged");
            write(path, json_form(forged, form));
            rejected([&] { detail::load_warm_history(path, space, recorded, &expected); });
            forged.members["trials"].items.front().members.erase(label);
            write(path, json_form(forged, form));
            rejected([&] { detail::load_warm_history(path, space, recorded, &expected); });
        }
        auto swapped = parent;
        auto& first = swapped.members["trials"].items.front();
        first.members["symbol"] = Json::string("asset-b");
        first.members["dataset_id"] = Json::string("second");
        write(path, json_form(swapped, form));
        rejected([&] { detail::load_warm_history(path, space, recorded, &expected); });

        // Each variant is admitted from real declarations, including the unsampled tape.
        for (unsigned axis = 0; axis < 6; ++axis) {
            auto declarations = fixture.declarations;
            auto settings = fixture.settings;
            if (axis == 0) {
                declarations.front().path = directory / "unsampled-changed.csv";
                Fixture::tape(declarations.front().path, 3);
            } else if (axis == 1) {
                declarations.front().syminfo.session = "0900-1700";
            } else if (axis == 2) {
                settings.fixed_inputs["Count"] = std::int64_t{3};
            } else if (axis == 3) {
                settings.overrides["slippage"] = std::int64_t{1};
            } else if (axis == 4) {
                settings.plugin_sha256 = std::string(64, 'b');
            } else {
                settings.warmup = "other";
            }
            const auto catalogue = fixture.admit(
                declarations, fixture.references, fixture.bindings, settings);
            const detail::ContinuationDataContext other(catalogue->canonical_descriptor());
            write(path, json_form(parent, form));
            rejected([&] { detail::load_warm_history(path, space, recorded, &other); },
                     "data_context_sha256");
            rejected([&] {
                detail::load_warm_history(directory / "bound.bin", space, recorded, &other);
            }, "data_context_sha256");
        }
        auto relocated = fixture.declarations;
        for (auto& declaration : relocated) {
            const auto price = declaration.id == "first" ? 1 : 2;
            declaration.path = directory / (declaration.id + "-relocated.csv");
            Fixture::tape(declaration.path, price);
        }
        const auto catalogue = fixture.admit(
            relocated, fixture.references, fixture.bindings, fixture.settings);
        const detail::ContinuationDataContext moved(catalogue->canonical_descriptor());
        write(path, json_form(parent, form));
        require(detail::load_warm_history(path, space, recorded, &moved).next_id == 20,
                "identical data relocated to another path was refused");
    }
    for (const std::string name : {"dataset_ids", "data_context_sha256", "applied_runtime"}) {
        auto stripped = parent;
        stripped.members.erase(name);
        write(path, detail::dump_json(stripped));
        rejected([&] { detail::load_warm_history(path, space, recorded, &expected); });
    }
    for (const auto* ids : {"[\"first\"]", "[\"first\",\"first\"]", "[\"first\",\"unknown\"]"}) {
        auto incomplete = parent;
        incomplete.members["dataset_ids"] = detail::parse_json(ids);
        write(path, detail::dump_json(incomplete));
        rejected([&] { detail::load_warm_history(path, space, recorded, &expected); });
    }
    auto contradictory = parent;
    contradictory.members["applied_runtime"].members["data_context"]
        .members["fixed_inputs"].members["Enabled"] = Json::boolean(false);
    write(path, detail::dump_json(contradictory));
    rejected([&] { detail::load_warm_history(path, space, recorded, &expected); });
    auto ancestor = parent;
    ancestor.members["warm_start_trials"] = detail::array_json();
    ancestor.members["warm_start_trials"].items.push_back(ancestor.members["trials"].items.front());
    ancestor.members["trials"].items.erase(ancestor.members["trials"].items.begin());
    ancestor.members["trials_completed"] = Json::number("1");
    ancestor.members["warm_start"] = detail::object_json();
    ancestor.members["warm_start"].members["trials"] = Json::number("1");
    write(path, detail::dump_json(ancestor));
    require(detail::load_warm_history(path, space, recorded, &expected).size() == 2,
            "complete ancestor history was lost");
    ancestor.members["warm_start_trials"].items.front().members["dataset_id"] =
        Json::string("second");
    write(path, detail::dump_json(ancestor));
    rejected([&] { detail::load_warm_history(path, space, recorded, &expected); });
}

void mapping_and_role_changes(const Path& directory, const Fixture& fixture, const Json& parent,
                              const pfh::SearchSpace& space, const Json& recorded) {
    auto declarations = fixture.declarations;
    auto alias = declarations.front();
    alias.id = "alternate";
    declarations.push_back(alias);
    auto references = fixture.references;
    references.push_back(alias.id);
    const auto original = fixture.admit(
        declarations, references, fixture.bindings, fixture.settings);
    auto bindings = fixture.bindings;
    bindings.front().datasets.front().second = alias.id;
    const auto remapped = fixture.admit(declarations, references, bindings, fixture.settings);
    const auto path = directory / "mapping-parent.json";
    write(path, detail::dump_json(parent_for(parent, *original)));
    const detail::ContinuationDataContext original_context(original->canonical_descriptor());
    const detail::ContinuationDataContext remapped_context(remapped->canonical_descriptor());
    require(detail::load_warm_history(path, space, recorded, &original_context).size() == 2,
            "valid mapping parent was refused");
    rejected([&] { detail::load_warm_history(path, space, recorded, &remapped_context); });
    const auto binary_path = directory / "mapping-parent.bin";
    const auto mapping_history = detail::load_warm_history(
        path, space, recorded, &original_context);
    detail::encode_warm_history(
        binary_path, space, recorded, mapping_history, 0, &original_context);
    rejected([&] {
        detail::load_warm_history(binary_path, space, recorded, &remapped_context);
    });

    auto request = fixture.bindings.front();
    request.input_title = "Reference";
    request.role = detail::SymbolBindingRole::Request;
    bindings = fixture.bindings;
    bindings.push_back(request);
    const auto roles = fixture.admit(
        fixture.declarations, fixture.references, bindings, fixture.settings);
    bindings.front().role = detail::SymbolBindingRole::Request;
    bindings.back().role = detail::SymbolBindingRole::Chart;
    const auto exchanged = fixture.admit(
        fixture.declarations, fixture.references, bindings, fixture.settings);
    auto dimensions = space.dimensions();
    dimensions.emplace_back(pfh::CategoricalDimension(
        "Reference", {std::string("asset-a"), std::string("asset-b")}));
    const pfh::SearchSpace role_space(std::move(dimensions));
    const auto role_recorded = detail::recorded_space(
        role_space, "metrics.all.net_profit", "maximize", {});
    auto role_parent = parent_for(parent, *roles);
    role_parent.members["space"] = role_recorded;
    for (auto& row : role_parent.members["trials"].items) {
        row.members["parameters"].members["Reference"] = Json::string("asset-a");
        row.members["space"] = role_recorded;
    }
    write(path, detail::dump_json(role_parent));
    const detail::ContinuationDataContext roles_context(roles->canonical_descriptor());
    const detail::ContinuationDataContext exchanged_context(exchanged->canonical_descriptor());
    require(detail::load_warm_history(path, role_space, role_recorded, &roles_context).size() == 2,
            "valid chart and request role parent was refused");
    rejected([&] {
        detail::load_warm_history(path, role_space, role_recorded, &exchanged_context);
    });
    const auto role_history = detail::load_warm_history(
        path, role_space, role_recorded, &roles_context);
    detail::encode_warm_history(
        binary_path, role_space, role_recorded, role_history, 0, &roles_context);
    rejected([&] {
        detail::load_warm_history(binary_path, role_space, role_recorded, &exchanged_context);
    });
}

// This is the previous reader's flag admission block, kept unchanged. It reads the
// actual file header; a legacy control must pass before the bound file is tried.
void legacy_reader_flags(const Path& path) {
    const detail::WarmMapping mapping(path);
    require(mapping.size >= 80, "truncated legacy reader fixture");
    const auto* header = mapping.bytes;
    const auto flags = detail::warm_integer<std::uint16_t>(header + 10);
    if ((flags & ~1U) != 0 ||
        detail::warm_integer<std::uint32_t>(header + 44) != 0)
        throw pfh::TypedHpoError<std::runtime_error>("hpo_study_spec_invalid",
                                                    {{"reason", "study"}},
                                                    "unsupported binary block flags");
}

void binary_contracts(const Path& directory, const Fixture& fixture,
                      const pfh::SearchSpace& space, const Json& recorded) {
    const detail::ContinuationDataContext expected(fixture.catalogue->canonical_descriptor());
    const auto history = detail::load_warm_history(
        directory / "parent.json", space, recorded, &expected);
    const auto output = directory / "encoded.bin";
    detail::encode_warm_history(output, space, recorded, history, 0, &expected);
    require(read(output) == read(directory / "bound.bin"),
            "native bound writer differs from independent wire bytes");
    legacy_reader_flags(directory / "legacy.bin");
    legacy_reader_flags(directory / "legacy-feeds.bin");
    bool old_refused = false;
    try {
        legacy_reader_flags(output);
    } catch (const pfh::HpoError& error) {
        old_refused = std::string(error.what()) == "unsupported binary block flags";
    }
    require(old_refused, "unchanged old reader flag logic accepted the bound file");
    const auto binary = detail::load_warm_history(output, space, recorded, &expected);
    require(binary.binary && binary.records.empty() && binary.observations.empty() &&
                binary.next_id == 20 && binary.size() == 2,
            "binary continuation retained per-row JSON or lost IDs");
    auto decoded = detail::array_json();
    for (std::uint64_t index = 0; index < binary.size(); ++index)
        decoded.items.push_back(binary.record(space, index));
    require(detail::dump_json(decoded) == read(directory / "decoded.json"),
            "binary decoded JSON bytes differ from independent expected rows");
    rejected([&] { detail::load_warm_history(output, space, recorded); });
    rejected([&] {
        detail::load_warm_history(directory / "legacy.bin", space, recorded, &expected);
    });
    rejected([&] { detail::encode_warm_history(output, space, recorded, history, 0); });
    detail::encode_warm_history(output, space, recorded, history, 1, &expected);
    const auto chunked = detail::load_warm_history(output, space, recorded, &expected);
    require(chunked.size() == 2 && chunked.next_id == 20, "concatenated bound blocks lost rows");
    auto other_descriptor = detail::parse_json(expected.canonical_descriptor());
    other_descriptor.members["datasets"].members["second"].members["values_sha256"] =
        Json::string(std::string(64, 'b'));
    const detail::ContinuationDataContext other(detail::dump_json(other_descriptor));
    rejected([&] { detail::load_warm_history(output, space, recorded, &other); });
    const auto malformed = directory / "malformed.bin";
    const auto golden = read(directory / "bound.bin");
    for (const std::size_t offset : {10u, 12u, 16u, 88u, 92u}) {
        auto bytes = golden;
        bytes[offset] ^= static_cast<char>(128);
        write(malformed, bytes);
        rejected([&] { detail::load_warm_history(malformed, space, recorded, &expected); });
    }
    for (std::size_t size = 88; size < 124; ++size) {
        write(malformed, golden.substr(0, size));
        rejected([&] { detail::load_warm_history(malformed, space, recorded, &expected); });
    }
    write(malformed, golden + read(directory / "legacy.bin"));
    rejected([&] { detail::load_warm_history(malformed, space, recorded, &expected); });
    auto with_feeds = recorded;
    with_feeds.members["symbol_feeds"] = detail::parse_json(read(directory / "feeds.json"));
    require(detail::load_warm_history(
                directory / "legacy-feeds.bin", space, with_feeds).next_id == 20,
            "legacy feed extension no longer admits the independent control");
    const auto dual = detail::load_warm_history(
        directory / "dual.bin", space, with_feeds, &expected);
    require(dual.next_id == 20, "matching dual extensions were refused");
    with_feeds.members["symbol_feeds"].members["symbols"].members["reference"]
        .members["facts"].members["currency"] = Json::string("different");
    rejected([&] {
        detail::load_warm_history(directory / "dual.bin", space, with_feeds, &expected);
    });
    rejected([&] { detail::load_warm_history(directory / "dual.bin", space, recorded, &other); });

    auto settings = fixture.settings;
    pfh::SymbolData reference;
    reference.symbol = "reference";
    reference.facts = {{"currency", std::string("units")}};
    reference.feeds.push_back(detail::load_symbol_feed(
        fixture.declarations.back().path, reference.symbol, "60"));
    settings.symbol_feeds = std::make_shared<const pfh::SymbolFeeds>(
        pfh::SymbolFeeds{std::move(reference)});
    const auto feed_catalogue = fixture.admit(
        fixture.declarations, fixture.references, fixture.bindings, settings);
    const detail::ContinuationDataContext feed_context(feed_catalogue->canonical_descriptor());
    auto feed_recorded = recorded;
    feed_recorded.members["symbol_feeds"] = detail::symbol_feeds_record(*settings.symbol_feeds);
    const auto feed_parent = parent_for(
        detail::parse_json(read(directory / "parent.json")), *feed_catalogue);
    const auto feed_json = directory / "feed-context.json";
    write(feed_json, detail::dump_json(feed_parent));
    const auto feed_history = detail::load_warm_history(
        feed_json, space, feed_recorded, &feed_context);
    detail::encode_warm_history(output, space, feed_recorded, feed_history, 0, &feed_context);
    const auto bytes = read(output);
    require(detail::warm_integer<std::uint16_t>(
                reinterpret_cast<const unsigned char*>(bytes.data()) + 10) == 2 &&
                detail::load_warm_history(output, space, feed_recorded, &feed_context).size() == 2,
            "verified full context did not replace the otherwise required feed block");
    rejected([&] { detail::load_warm_history(output, space, feed_recorded, &expected); });

    auto legacy_parent = detail::parse_json(read(directory / "parent.json"));
    legacy_parent.members.erase("applied_runtime");
    legacy_parent.members.erase("data_context_sha256");
    legacy_parent.members.erase("dataset_ids");
    for (auto& row : legacy_parent.members["trials"].items) {
        row.members.erase("symbol");
        row.members.erase("dataset_id");
        row.members.erase("data_context_sha256");
    }
    write(feed_json, detail::dump_json(legacy_parent));
    const auto legacy = detail::load_warm_history(feed_json, space, recorded);
    detail::encode_warm_history(output, space, recorded, legacy, 0);
    require(read(output) == read(directory / "legacy.bin"),
            "unbound native writer changed the independent no-binding bytes");
}

std::set<std::string> vectors(const Json& rows) {
    std::set<std::string> result;
    for (const auto& row : rows.items)
        result.insert(detail::dump_json(detail::field(row, "parameters")));
    return result;
}

void grid_and_random(const Path& directory, const Fixture& fixture,
                     const std::shared_ptr<const pfh::StrategyPlugin>& plugin) {
    const auto full = run(study(fixture.catalogue, plugin));
    const std::set<std::string> reference{
        R"({"Length":1,"Market":"asset-a"})", R"({"Length":1,"Market":"asset-b"})",
        R"({"Length":2,"Market":"asset-a"})", R"({"Length":2,"Market":"asset-b"})"};
    require(vectors(detail::field(full, "trials")) == reference,
            "complete grid differs from the independently listed four vectors");
    for (const auto* parent : {"parent.json", "bound.bin"}) {
        cli::Options options;
        options.warm_start = directory / parent;
        options.max_trials = 2;
        options.candidate_policy = pfh::CandidatePolicy::Exhaustive;
        const auto continued = run(study(fixture.catalogue, plugin, options));
        auto combined = detail::field(continued, "warm_start_trials");
        const auto& rows = detail::field(continued, "trials");
        combined.items.insert(combined.items.end(), rows.items.begin(), rows.items.end());
        require(combined.items.size() == 4 && vectors(combined) == reference,
                "partial grid plus continuation did not cover the four complete vectors");
        require(integer(rows.items[0], "trial_id") == 20 &&
                    integer(rows.items[1], "trial_id") == 21 &&
                    string(rows.items[0], "dataset_id") == "second" &&
                    string(rows.items[1], "dataset_id") == "second",
                "grid continued IDs or chart labels disagree with the remaining vectors");
    }
    cli::Options options;
    options.sampler = "random";
    options.max_trials = 6;
    options.warm_start = directory / "parent.json";
    const auto prepared = study(fixture.catalogue, plugin, options);
    const detail::ContinuationDataContext context(fixture.catalogue->canonical_descriptor());
    const auto recorded = detail::parse_json(*prepared.options.space_json);
    const auto history = detail::load_warm_history(options.warm_start, prepared.space, recorded,
                                                   &context);
    pfh::RandomSampler reference_sampler(
        prepared.space, pfh::continuation_seed(17, history.size()));
    const auto continued = run(prepared);
    std::uint64_t next_id = 20;
    for (const auto& row : detail::field(continued, "trials").items) {
        auto expected = reference_sampler.next();
        while (expected && history.contains(*expected))
            expected = reference_sampler.next();
        require(bool(expected), "seeded random reference stopped unexpectedly");
        auto values = detail::object_json();
        for (const auto& item : expected->values)
            values.members[item.first] = detail::scalar_json(item.second);
        require(detail::dump_json(values) == detail::dump_json(detail::field(row, "parameters")),
                "random warm continuation did not use its derived seed and parent exclusion");
        require(detail::json_id(detail::field(row, "trial_id")) == next_id++,
                "random continuation reused a parent trial ID");
    }
}

void tpe_contracts(const Path& directory, const Fixture& fixture,
                   const std::shared_ptr<const pfh::StrategyPlugin>& plugin,
                   const Observations& observations) {
    cli::Options options;
    options.sampler = "tpe";
    options.max_trials = 2;
    const auto prepared = study(fixture.catalogue, plugin, options);
    const auto parent = run(prepared);
    const auto path = directory / "tpe-parent.json";
    write(path, detail::dump_json(parent));
    const auto recorded = detail::parse_json(*prepared.options.space_json);
    const detail::ContinuationDataContext context(fixture.catalogue->canonical_descriptor());
    const auto history = detail::load_warm_history(path, prepared.space, recorded, &context);
    require(!history.sampler_state.empty(), "TPE parent did not retain a checkpoint");
    const auto binary_path = directory / "tpe-parent.bin";
    detail::encode_warm_history(binary_path, prepared.space, recorded, history, 1, &context);
    options.max_trials = 3;
    for (const auto& source : {path, binary_path}) {
        options.warm_start = source;
        const auto continued = run(study(fixture.catalogue, plugin, options));
        require(string(continued, "warm_start_model") == "restored_sampler_state",
                "matching TPE checkpoint was not restored after context verification");
        auto uninterrupted_options = options;
        uninterrupted_options.warm_start.clear();
        uninterrupted_options.max_trials = 5;
        const auto uninterrupted = run(study(fixture.catalogue, plugin, uninterrupted_options));
        const auto& expected_rows = detail::field(uninterrupted, "trials").items;
        const auto& actual_rows = detail::field(continued, "trials").items;
        for (std::size_t index = 0; index < actual_rows.size(); ++index) {
            require(detail::dump_json(detail::field(actual_rows[index], "parameters")) ==
                        detail::dump_json(detail::field(expected_rows[index + 2], "parameters")),
                    "matching TPE checkpoint changed the continued proposal sequence");
        }
    }
    const auto binary = detail::load_warm_history(binary_path, prepared.space, recorded, &context);
    auto descriptor = detail::parse_json(context.canonical_descriptor());
    descriptor.members["datasets"].members["second"].members["values_sha256"] =
        Json::string(std::string(64, 'b'));
    const detail::ContinuationDataContext other(detail::dump_json(descriptor));
    for (const auto* imported : {&history, &binary}) {
        pfh::TpeSampler sampler(prepared.space, 17, pfh::ObjectiveDirection::Maximize,
                                3, prepared.options.tpe_config);
        const auto pristine = sampler.sampler_state();
        rejected([&] {
            cli::restore_context_tpe_history(
                sampler, *imported, prepared.space, other, 2, imported->sampler_state);
        });
        require(sampler.completed() == 0 && sampler.generated() == 0 &&
                    sampler.retained_observations() == 0 && sampler.sampler_state() == pristine,
                "data mismatch trained or restored the real TPE sampler");
        require(cli::restore_context_tpe_history(
                    sampler, *imported, prepared.space, context, 2, imported->sampler_state),
                "checkpoint bytes used by the mismatch test were not otherwise restorable");
        require(sampler.completed() == history.completed,
                "matching restore did not import completed observations");
    }
    auto without_state = parent;
    without_state.members.erase("tpe_sampler_state");
    const auto rebuild_path = directory / "tpe-history.json";
    write(rebuild_path, detail::dump_json(without_state));
    options.warm_start = rebuild_path;
    const auto first = run(study(fixture.catalogue, plugin, options));
    const auto second = run(study(fixture.catalogue, plugin, options));
    require(string(first, "warm_start_model") == "rebuilt_history" &&
                detail::dump_json(detail::field(first, "trials")) ==
                    detail::dump_json(detail::field(second, "trials")),
            "history-only TPE rebuild was not deterministic for the same context");

    auto changed = fixture.declarations;
    changed.front().syminfo.mintick = 0.5;
    const auto changed_catalogue = fixture.admit(
        changed, fixture.references, fixture.bindings, fixture.settings);
    for (const auto& source : {path, binary_path, rebuild_path}) {
        options.warm_start = source;
        const auto before = observations.snapshot();
        rejected([&] { run(study(changed_catalogue, plugin, options)); });
        const auto after = observations.snapshot();
        require(integer(before, "created") == integer(after, "created") &&
                    integer(before, "freed") == integer(after, "freed"),
                "mismatching context reached executor validation or trial handles");
    }
    // Internally supplied history also takes the guard; it cannot bypass file admission.
    options.warm_start.clear();
    options.warm_history = std::make_shared<const detail::WarmHistory>(history);
    rejected([&] { run(study(changed_catalogue, plugin, options)); });
    clean(observations);
}

void repeated_trial_resources(const Path& directory,
                              const std::shared_ptr<const pfh::StrategyPlugin>& plugin,
                              const Observations& observations) {
    std::shared_ptr<Counters> counts;
    {
        Fixture fixture(directory);
        counts = fixture.counters;
        cli::Options options;
        options.sampler = "random";
        options.workers = 2;
        options.trials_out = "none";
        const auto base = study(fixture.catalogue, plugin, options);
        const detail::ContinuationDataContext context(fixture.catalogue->canonical_descriptor());
        const auto recorded = detail::parse_json(*base.options.space_json);
        options.warm_history = std::make_shared<const detail::WarmHistory>(
            detail::load_warm_history(
                directory.parent_path() / "parent.json", base.space, recorded, &context));
        // A transient validation handle exists only when request feeds are configured (or
        // the jailed profile is requested), so the study carries one feed set: every run
        // then validates each context's executor exactly once, and nothing validates inside
        // the trial loop.
        pfh::SymbolData reference;
        reference.symbol = "reference";
        reference.facts = {{"currency", std::string("units")}};
        reference.feeds.push_back(detail::load_symbol_feed(
            fixture.declarations.back().path, reference.symbol, "60"));
        const auto feeds = std::make_shared<const pfh::SymbolFeeds>(
            pfh::SymbolFeeds{std::move(reference)});
        auto prepared = study(fixture.catalogue, plugin, options, feeds);
        std::vector<long> owner_references;
        for (const auto& context : fixture.catalogue->contexts())
            owner_references.push_back(context.view.owner.use_count());
        const auto admissions = integer(observations.snapshot(), "admissions");
        for (const auto trials : {3u, 19u, 83u}) {
            observations.clear();
            const auto before = observations.snapshot();
            prepared.options.max_trials = trials;
            const auto result = run(prepared);
            require(integer(result, "trials_completed") == trials &&
                        detail::field(result, "trials").items.empty(),
                    "fixed output policy retained per-trial rows");
            const auto after = observations.snapshot();
            require(integer(after, "created") - integer(before, "created") == trials + 2 &&
                        integer(after, "freed") - integer(before, "freed") == trials + 2 &&
                        integer(after, "validations") - integer(before, "validations") == 2 &&
                        integer(after, "admissions") == admissions,
                    "per-handle resources accumulated or plugin validation repeated "
                    "per trial: trials " +
                        std::to_string(trials) + ", created " +
                        std::to_string(integer(after, "created") - integer(before, "created")) +
                        ", freed " +
                        std::to_string(integer(after, "freed") - integer(before, "freed")) +
                        ", validations " +
                        std::to_string(integer(after, "validations") -
                                       integer(before, "validations")) +
                        ", admissions " + std::to_string(integer(after, "admissions")) +
                        " (expected " + std::to_string(admissions) + ")");
            require(counts->loads == 2 && counts->owners == 2,
                    "repeated trials loaded or retained another catalogue owner");
            std::size_t index = 0;
            for (const auto& context : fixture.catalogue->contexts())
                require(context.view.owner.use_count() == owner_references[index++],
                        "completed trials retained borrowed dataset owners");
            clean(observations);
        }
    }
    require(counts->owners == 0 && counts->observed.size() == 2 &&
                std::all_of(counts->observed.begin(), counts->observed.end(),
                            [](const auto& owner) { return owner.expired(); }),
            "catalogue owners survived the last prepared study");
}

}  // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 3, "usage: symbol continuation test <routing-plugin> <fixtures>");
        const Path directory(argv[2]);
        const ScratchEnvironment scratch;
        Observations observations(argv[1]);
        const auto plugin = std::make_shared<const pfh::StrategyPlugin>(argv[1]);
        const Fixture fixture(directory / "charts");
        const auto prepared = study(fixture.catalogue, plugin);
        const auto recorded = detail::parse_json(*prepared.options.space_json);
        const auto parent = detail::parse_json(read(directory / "parent.json"));
        json_contracts(directory, fixture, parent, prepared.space, recorded);
        mapping_and_role_changes(directory, fixture, parent, prepared.space, recorded);
        binary_contracts(directory, fixture, prepared.space, recorded);
        grid_and_random(directory, fixture, plugin);
        tpe_contracts(directory, fixture, plugin, observations);
        repeated_trial_resources(directory / "repeated", plugin, observations);
        clean(observations);
        require(integer(observations.snapshot(), "admissions") == 1,
                "continuation loaded the native plugin more than once");
        std::cout << "PASS context continuation, sampler contracts and component lifetimes\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
