#include "symbol_bindings.hpp"

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <unistd.h>

namespace pfh = pineforge::hpo;
namespace detail = pineforge::hpo::detail;

namespace {

void require(bool condition, const std::string& message) {
    if (!condition)
        throw std::runtime_error(message);
}

template <typename Function>
void require_refusal(Function&& function, const std::string& code,
                     const std::string& reason, const std::string& cause) {
    try {
        function();
    } catch (const pfh::HpoError& error) {
        require(error.code() == code, "wrong admission failure code");
        if (reason.empty()) {
            require(error.args().empty(), "unexpected failure arguments");
        } else {
            require(error.args().size() == 1 &&
                        std::get<std::string>(error.args().at("reason").value()) == reason,
                    "wrong admission failure reason");
        }
        require(std::string(error.what()).find(cause) != std::string::npos,
                "admission failure did not name " + cause);
        return;
    }
    throw std::runtime_error("expected admission refusal: " + cause);
}

detail::ChartDataDeclaration declaration(const std::filesystem::path& path,
                                        const std::string& id, const std::string& symbol) {
    return {id, symbol, "60", "60", "UTC",
            {symbol, "synthetic", "units", 1.0, 1.0, 1.0, "UTC", "24x7"},
            path, detail::ChartDataFormat::Csv, pfh::BinaryVerification::Full};
}

detail::SymbolBindingDeclaration binding() {
    // Choices and dataset declarations are opposite; the map is independently written.
    return {"Market", detail::SymbolBindingRole::Chart, {"asset-a", "asset-b"},
            {{"asset-b", "second"}, {"asset-a", "first"}}};
}

detail::DataContextSettings settings() {
    return {"artifact", std::string(64, 'a'),
            {{"Count", std::int64_t(2)}, {"Enabled", true}, {"Label", std::string("α")},
             {"Threshold", 0.5}},
            {{"slippage", std::int64_t(0)}},
            "exact_timestamps", "full_input", "none", nullptr};
}

void csv(const std::filesystem::path& path, int price) {
    std::ofstream output(path);
    output << "timestamp,open,high,low,close,volume\n0,"
           << price << ',' << price << ',' << price << ',' << price << ',' << price << '\n';
    require(bool(output), "cannot write chart fixture");
}

// Independent byte fixture: five little-endian binary64 ones, then a signed zero timestamp.
std::string binary_record() {
    std::string bytes;
    for (int index = 0; index < 5; ++index)
        bytes.append("\x00\x00\x00\x00\x00\x00\xf0\x3f", 8);
    bytes.append(8, '\0');
    return bytes;
}

void binary(const std::filesystem::path& path, const std::string& symbol,
            const std::string& timeframe = "60", const std::string& bytes = binary_record()) {
    std::ofstream output(path, std::ios::binary);
    output.write(bytes.data(), bytes.size());
    require(bool(output), "cannot write binary fixture");
    output.close();
    std::ofstream sidecar(path.string() + ".json");
    sidecar
        << "{\"format\":\"pf_bar_t/v1\",\"record_size\":48,\"symbol\":\"" << symbol
        << "\",\"tf\":\"" << timeframe
        << "\",\"count\":1,\"first_ts\":0,\"last_ts\":0,\"sha256\":\""
        << detail::sha256(bytes) << "\"}";
    require(bool(sidecar), "cannot write binary sidecar fixture");
}

void routing_and_ownership(const std::filesystem::path& directory) {
    const auto first = declaration(directory / "first.csv", "first", "asset-a");
    const auto second = declaration(directory / "second.csv", "second", "asset-b");
    auto request = binding();
    request.input_title = "Reference";
    request.role = detail::SymbolBindingRole::Request;
    std::reverse(request.choices.begin(), request.choices.end());
    const auto unused = declaration(directory / "not-loaded.csv", "unused", "asset-c");
    const detail::AdmittedDataCatalogue catalogue(
        {second, unused, first}, {"second", "first"}, {binding(), request}, settings(),
        {"exact_timestamps"});
    const auto first_id = catalogue.resolve("Market", detail::SymbolChoiceOrdinal{0});
    const auto second_id = catalogue.resolve("Market", detail::SymbolChoiceOrdinal{1});
    require(first_id != second_id, "choice ordinals collapsed to one context");
    const auto& a = catalogue.context(first_id);
    const auto& b = catalogue.context(second_id);
    require(a.identity.id == "first" && a.identity.symbol == "asset-a" &&
                b.identity.id == "second" && b.identity.symbol == "asset-b",
            "choice ordinals were zipped with dataset or map order");
    require(a.view.data() == a.view.owner->data() && b.view.data() == b.view.owner->data() &&
                a.view.data() != b.view.data(),
            "chart views do not borrow distinct admitted owners");
    require(a.view.data()[0].close == 1 && b.view.data()[0].close == 2,
            "resolved context selected the wrong physical chart");
    require(a.identity.syminfo.tickerid == "asset-a" &&
                b.identity.syminfo.tickerid == "asset-b",
            "resolved context lost its declared facts");
    const auto& requested = catalogue.context(
        catalogue.resolve("Reference", detail::SymbolChoiceOrdinal{0}));
    require(requested.view.owner == b.view.owner && requested.view.data() == b.view.data(),
            "another binding to the same dataset loaded or copied its tape");
    for (const auto ordinal : {0u, 1u, 0u, 1u}) {
        const auto& selected = catalogue.context(
            catalogue.resolve("Market", detail::SymbolChoiceOrdinal{ordinal}));
        const auto& expected = ordinal == 0 ? a : b;
        require(selected.view.owner == expected.view.owner &&
                    selected.view.data() == expected.view.data(),
                "repeated selection loaded or copied a chart");
        const pfh::DatasetView prefix(selected.view.owner, 0, 1);
        require(prefix.data() == expected.view.data(), "prefix copied chart bars");
    }
    auto opposite = binding();
    std::reverse(opposite.choices.begin(), opposite.choices.end());
    const detail::AdmittedDataCatalogue reversed(
        {first, second}, {"first", "second"}, {opposite}, settings(), {"exact_timestamps"});
    require(reversed.context(reversed.resolve("Market", detail::SymbolChoiceOrdinal{0}))
                .identity.id == "second" &&
                reversed.context(reversed.resolve("Market", detail::SymbolChoiceOrdinal{1}))
                .identity.id == "first",
            "chart selection followed sorted dataset IDs instead of declared choice order");
    require_refusal([&] { catalogue.resolve_choice("Market", "missing"); },
                    "hpo_study_spec_invalid", "input_symbol", "missing");
    require_refusal([&] { catalogue.resolve("Market", detail::SymbolChoiceOrdinal{2}); },
                    "hpo_study_spec_invalid", "input_symbol", "ordinal");
    require_refusal([&] { catalogue.resolve("Missing", detail::SymbolChoiceOrdinal{0}); },
                    "hpo_study_spec_invalid", "input_symbol", "Missing");
}

void identity_changes(const std::filesystem::path& directory) {
    const auto first = declaration(directory / "first.csv", "first", "asset-a");
    const auto second = declaration(directory / "second.csv", "second", "asset-b");
    const auto digest = [&](const std::vector<detail::ChartDataDeclaration>& datasets,
                            const detail::SymbolBindingDeclaration& map,
                            const detail::DataContextSettings& config) {
        return detail::AdmittedDataCatalogue(datasets, {"first", "second"}, {map}, config,
                                            {"exact_timestamps"}).data_context_sha256();
    };
    const auto expected = digest({second, first}, binding(), settings());
    csv(directory / "relocated.csv", 1);
    auto relocated = first;
    relocated.path = directory / "relocated.csv";
    auto reordered = binding();
    std::reverse(reordered.datasets.begin(), reordered.datasets.end());
    require(digest({relocated, second}, reordered, settings()) == expected,
            "path relocation or declaration/map order changed context identity");
    std::ofstream(directory / "changed.csv")
        << "timestamp,open,high,low,close,volume\n0,1,1,1,2,1\n";
    auto changed = first;
    changed.path = directory / "changed.csv";
    require(digest({changed, second}, binding(), settings()) != expected,
            "changed bars were absent from context identity");
    changed = first;
    changed.syminfo.mintick = 0.5;
    require(digest({changed, second}, binding(), settings()) != expected,
            "changed facts were absent from context identity");
    auto config = settings();
    config.fixed_inputs["Count"] = std::int64_t(3);
    require(digest({first, second}, binding(), config) != expected,
            "changed fixed input was absent from context identity");
    config = settings();
    config.fixed_inputs["Count"] = 2.0;
    require(digest({first, second}, binding(), config) != expected,
            "integer and binary64 settings lost their types");
    config.fixed_inputs["Count"] = std::string("int:2");
    require(digest({first, second}, binding(), config) != expected,
            "integer and text settings lost their types");
    config = settings();
    config.overrides["slippage"] = std::int64_t(1);
    require(digest({first, second}, binding(), config) != expected,
            "changed override was absent from context identity");
    config = settings();
    config.plugin_sha256 = std::string(64, 'b');
    require(digest({first, second}, binding(), config) != expected,
            "changed artifact identity was absent from context identity");
    auto alternate = first;
    alternate.id = "alternate";
    alternate.path = directory / "relocated.csv";
    const auto map_digest = [&](const detail::SymbolBindingDeclaration& map) {
        return detail::AdmittedDataCatalogue(
            {first, second, alternate}, {"first", "second", "alternate"}, {map},
            settings(), {"exact_timestamps"}).data_context_sha256();
    };
    auto remapped = binding();
    remapped.datasets = {{"asset-a", "alternate"}, {"asset-b", "second"}};
    require(map_digest(remapped) != map_digest(binding()),
            "changed mapping alone was absent from context identity");
    auto reordered_choices = binding();
    std::reverse(reordered_choices.choices.begin(), reordered_choices.choices.end());
    require(digest({first, second}, reordered_choices, settings()) != expected,
            "choice declaration order was absent from context identity");
}

void integrity_of_all_inputs(const std::filesystem::path& directory) {
    const auto first_path = directory / "first.bin";
    const auto second_path = directory / "second.bin";
    binary(first_path, "asset-a");
    binary(second_path, "asset-b");
    auto first = declaration(first_path, "first", "asset-a");
    auto second = declaration(second_path, "second", "asset-b");
    first.format = second.format = detail::ChartDataFormat::Binary;
    const detail::AdmittedDataCatalogue catalogue(
        {first, second}, {"first", "second"}, {binding()}, settings(), {"exact_timestamps"});
    catalogue.verify_unchanged();
    const auto& a = catalogue.context(catalogue.resolve("Market", detail::SymbolChoiceOrdinal{0}));
    const auto& b = catalogue.context(catalogue.resolve("Market", detail::SymbolChoiceOrdinal{1}));
    // Keep both real mappings held. Change the non-last input and its mtime without truncation.
    std::fstream changed(first_path, std::ios::in | std::ios::out | std::ios::binary);
    changed.put('\x01');
    require(bool(changed), "cannot mutate held binary input");
    changed.close();
    require(bool(changed), "cannot flush mutation of held binary input");
    std::filesystem::last_write_time(first_path,
        std::filesystem::last_write_time(first_path) + std::chrono::seconds(2));
    b.view.owner->verify_unchanged();
    require_refusal([&] { catalogue.verify_unchanged(); },
                    "hpo_dataset_invalid", "", "changed while mapped");
    require_refusal([&] { a.view.owner->verify_unchanged(); },
                    "hpo_dataset_invalid", "", "changed while mapped");
}

void request_feed_identity(const std::filesystem::path& directory) {
    const auto first = declaration(directory / "first.csv", "first", "asset-a");
    const auto second = declaration(directory / "second.csv", "second", "asset-b");
    const auto make_feeds = [&](const std::filesystem::path& path, std::int64_t close) {
        std::ofstream(path) << "timestamp,open,high,low,close,volume,time_close\n"
                           << "0,1,1,1,1,1," << close << '\n';
        pfh::SymbolData data;
        data.symbol = "reference";
        data.facts = {{"currency", std::string("units")},
                      {"mintick", std::numeric_limits<double>::denorm_min()}};
        data.feeds.push_back(detail::load_symbol_feed(path, data.symbol, "60"));
        pfh::SymbolFeeds feeds;
        feeds.push_back(std::move(data));
        return std::make_shared<const pfh::SymbolFeeds>(std::move(feeds));
    };
    auto config = settings();
    config.symbol_feeds = make_feeds(directory / "request.csv", 100);
    const detail::AdmittedDataCatalogue original(
        {first, second}, {"first", "second"}, {binding()}, config, {"exact_timestamps"});
    config.symbol_feeds = make_feeds(directory / "request-relocated.csv", 100);
    const detail::AdmittedDataCatalogue relocated(
        {first, second}, {"first", "second"}, {binding()}, config, {"exact_timestamps"});
    require(original.data_context_sha256() == relocated.data_context_sha256(),
            "request-feed path leaked into context identity");
    require(original.canonical_descriptor().find(
                R"("mintick":{"binary64":"0000000000000001"})") != std::string::npos,
            "request-feed subnormal fact lost its binary64 bits");
    config.symbol_feeds = make_feeds(directory / "request-changed.csv", 200);
    const detail::AdmittedDataCatalogue changed(
        {first, second}, {"first", "second"}, {binding()}, config, {"exact_timestamps"});
    require(original.data_context_sha256() != changed.data_context_sha256(),
            "changed explicit request close was absent from context identity");
}

void binary_values_and_loader_policies(const std::filesystem::path& directory) {
    const auto path = directory / "bits.bin";
    auto source = declaration(path, "first", "asset-a");
    source.format = detail::ChartDataFormat::Binary;
    const detail::SymbolBindingDeclaration single{
        "Market", detail::SymbolBindingRole::Chart, {"asset-a"}, {{"asset-a", "first"}}};
    const auto values = [&](const std::string& bytes) {
        binary(path, "asset-a", "60", bytes);
        const detail::AdmittedDataCatalogue catalogue(
            {source}, {"first"}, {single}, settings(), {"exact_timestamps"});
        return catalogue.context(catalogue.resolve("Market", detail::SymbolChoiceOrdinal{0}))
            .values_sha256;
    };
    auto positive_zero = binary_record();
    positive_zero.replace(0, 8, std::string(8, '\0'));
    auto negative_zero = positive_zero;
    negative_zero[7] = '\x80';
    require(values(positive_zero) != values(negative_zero),
            "chart hashing normalized the sign of zero");
    auto first_nan = binary_record();
    first_nan.replace(32, 8, "\x01\x00\x00\x00\x00\x00\xf8\x7f", 8);
    auto second_nan = first_nan;
    second_nan[32] = '\x02';
    require(values(first_nan) != values(second_nan), "chart hashing normalized NaN payloads");

    const auto original_values = values(binary_record());
    {
        std::fstream changed(path, std::ios::in | std::ios::out | std::ios::binary);
        changed.put('\x01');
        require(bool(changed), "cannot change binary bytes before admission");
        changed.close();
        require(bool(changed), "cannot flush changed binary bytes before admission");
    }
    require_refusal([&] {
        detail::AdmittedDataCatalogue({source}, {"first"}, {single}, settings(),
                                     {"exact_timestamps"});
    }, "hpo_dataset_invalid", "", "sidecar does not match");
    source.verify_bin = pfh::BinaryVerification::Sidecar;
    const detail::AdmittedDataCatalogue sidecar(
        {source}, {"first"}, {single}, settings(), {"exact_timestamps"});
    require(sidecar.context(sidecar.resolve("Market", detail::SymbolChoiceOrdinal{0}))
                .values_sha256 != original_values,
            "sidecar verification hashed the declared digest instead of executed values");
    require(sidecar.canonical_descriptor().find(R"("verification":"sidecar")") !=
                std::string::npos,
            "weaker verification policy was omitted from provenance");
    auto csv_source = declaration(directory / "second.csv", "second", "asset-b");
    const detail::AdmittedDataCatalogue mixed(
        {csv_source, source}, {"first", "second"}, {binding()}, settings(), {"exact_timestamps"});
    require(mixed.context(mixed.resolve("Market", detail::SymbolChoiceOrdinal{1}))
                .view.data()[0].close == 2,
            "mixed binary and CSV admission selected the wrong tape");
    const auto missing_volume = directory / "missing-volume.csv";
    std::ofstream(missing_volume) << "timestamp,open,high,low,close\n0,1,1,1,1\n";
    auto invalid = declaration(missing_volume, "first", "asset-a");
    require_refusal([&] {
        detail::AdmittedDataCatalogue({invalid}, {"first"}, {single}, settings(),
                                     {"exact_timestamps"});
    }, "hpo_dataset_invalid", "", "volume");
}

void admission_refusals(const std::filesystem::path& directory) {
    const auto first = declaration(directory / "first.csv", "first", "asset-a");
    const auto second = declaration(directory / "second.csv", "second", "asset-b");
    auto config = settings();
    config.pruner = "successive_halving";
    require_refusal([&] {
        detail::AdmittedDataCatalogue({first, second}, {"first", "second"}, {binding()},
                                     config, {"exact_timestamps"});
    }, "hpo_study_spec_invalid", "pruner", "execution.pruner");
    config = settings();
    auto other = second;
    other.syminfo.currency = "other-units";
    require_refusal([&] {
        detail::AdmittedDataCatalogue({first, other}, {"first", "second"}, {binding()},
                                     config, {"exact_timestamps"});
    }, "hpo_study_spec_invalid", "objective", "other-units");
    require_refusal([&] {
        detail::AdmittedDataCatalogue({first, second}, {"first", "second"}, {binding()},
                                     config, {});
    }, "hpo_study_spec_invalid", "backtest", "data_comparison.alignment");
    auto partial = binding();
    partial.datasets.pop_back();
    require_refusal([&] {
        detail::AdmittedDataCatalogue({first, second}, {"first", "second"}, {partial},
                                     config, {"exact_timestamps"});
    }, "hpo_study_spec_invalid", "input_symbol", "datasets");
    binary(directory / "wrong-symbol.bin", "asset-b");
    auto wrong = first;
    wrong.path = directory / "wrong-symbol.bin";
    wrong.format = detail::ChartDataFormat::Binary;
    require_refusal([&] {
        detail::AdmittedDataCatalogue({wrong, second}, {"first", "second"}, {binding()},
                                     config, {"exact_timestamps"});
    }, "hpo_dataset_invalid", "", "symbol");
    binary(directory / "wrong-timeframe.bin", "asset-a", "1");
    wrong.path = directory / "wrong-timeframe.bin";
    require_refusal([&] {
        detail::AdmittedDataCatalogue({wrong, second}, {"first", "second"}, {binding()},
                                     config, {"exact_timestamps"});
    }, "hpo_dataset_invalid", "", "timeframe");
}

void canonical_fixture(const std::filesystem::path& directory) {
    const detail::AdmittedDataCatalogue catalogue(
        {declaration(directory / "second.csv", "second", "asset-b"),
         declaration(directory / "first.csv", "first", "asset-a")},
        {"second", "first"}, {binding()}, settings(), {"exact_timestamps"});
    // Hand-written descriptor: one bar of five ones or five twos at timestamp zero.
    // binary64(1) = 3ff0000000000000; binary64(0.5) = 3fe0000000000000.
    // The expected hashes were computed independently from these literal bytes.
    const std::string expected =
        R"({"artifact_key":"artifact","bindings":{"Market":{)"
        R"("choices":["asset-a","asset-b"],)"
        R"("datasets":{"asset-a":"first","asset-b":"second"},"role":"chart"}},)"
        R"("comparison":{"alignment":"exact_timestamps","pruner":"none","warmup":"full_input"},)"
        R"("datasets":{"first":{"chart_timezone":"UTC","count":{"int":"1"},)"
        R"("facts":{"currency":"units","mincontract":{"binary64":"3ff0000000000000"},)"
        R"("mintick":{"binary64":"3ff0000000000000"},)"
        R"("pointvalue":{"binary64":"3ff0000000000000"},)"
        R"("session":"24x7","tickerid":"asset-a","timezone":"UTC","type":"synthetic"},)"
        R"("first_ts":{"int":"0"},"identity_revision":{"int":"1"},"input_tf":"60",)"
        R"("last_ts":{"int":"0"},"parser":"chart-csv/v1","script_tf":"60","symbol":"asset-a",)"
        R"("values_sha256":"5e22472d6c13a9700d6b3a794e27779e938c43125b09be977f69218e72239ed4",)"
        R"("verification":"parsed"},"second":{"chart_timezone":"UTC","count":{"int":"1"},)"
        R"("facts":{"currency":"units","mincontract":{"binary64":"3ff0000000000000"},)"
        R"("mintick":{"binary64":"3ff0000000000000"},)"
        R"("pointvalue":{"binary64":"3ff0000000000000"},)"
        R"("session":"24x7","tickerid":"asset-b","timezone":"UTC","type":"synthetic"},)"
        R"("first_ts":{"int":"0"},"identity_revision":{"int":"1"},"input_tf":"60",)"
        R"("last_ts":{"int":"0"},"parser":"chart-csv/v1","script_tf":"60","symbol":"asset-b",)"
        R"("values_sha256":"76259225441bd1c4f6ff0d7a55c86ea525844129d5c7ebbbdb9931deea073d3c",)"
        R"("verification":"parsed"}},"fixed_inputs":{"Count":{"int":"2"},"Enabled":true,)"
        R"("Label":"α","Threshold":{"binary64":"3fe0000000000000"}},)"
        R"("overrides":{"slippage":{"int":"0"}},)"
        R"("plugin_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",)"
        R"("schema":"pineforge-hpo-data-context/v1","symbol_feeds":{}})";
    require(catalogue.canonical_descriptor() == expected,
            "canonical descriptor differs from hand-written bytes");
    require(catalogue.data_context_sha256() ==
                "01f34c8524969d11ffeb178098f0240fef6ec0c7b0d1bbe9978a27e92eaf2e9d",
            "context digest differs from independent literal-byte digest");
}

}  // namespace

int main() {
    const auto directory = std::filesystem::temp_directory_path() /
                           ("pfh-bindings-" + std::to_string(::getpid()));
    try {
        std::filesystem::create_directories(directory);
        csv(directory / "first.csv", 1);
        csv(directory / "second.csv", 2);
        routing_and_ownership(directory);
        identity_changes(directory);
        canonical_fixture(directory);
        admission_refusals(directory);
        request_feed_identity(directory);
        binary_values_and_loader_policies(directory);
        integrity_of_all_inputs(directory);
        std::cout << "PASS symbol admission, immutable ownership, identity and integrity\n";
        std::filesystem::remove_all(directory);
        return 0;
    } catch (const std::exception& error) {
        std::filesystem::remove_all(directory);
        std::cerr << error.what() << '\n';
        return 1;
    }
}
