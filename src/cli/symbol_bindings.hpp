#pragma once

#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/error.hpp>

#include "continuation.hpp"
#include "sha256.hpp"
#include "symbol_feeds.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iterator>
#include <limits>
#include <map>
#include <memory>
#include <regex>
#include <set>
#include <string>
#include <type_traits>
#include <utility>
#include <variant>
#include <vector>

namespace pineforge::hpo::detail {

enum class SymbolBindingRole : std::uint8_t { Chart, Request };
enum class ChartDataFormat : std::uint8_t { Csv, Binary };

struct SymbolChoiceOrdinal final {
    std::uint64_t value;
};

struct DataContextId final {
    std::uint64_t value;
    bool operator==(DataContextId other) const noexcept { return value == other.value; }
    bool operator!=(DataContextId other) const noexcept { return !(*this == other); }
};

struct ChartSymbolFacts final {
    std::string tickerid;
    std::string type;
    std::string currency;
    double mintick;
    double pointvalue;
    double mincontract;
    std::string timezone;
    std::string session;
};

struct ChartDataDeclaration final {
    std::string id;
    std::string symbol;
    std::string input_tf;
    std::string script_tf;
    std::string chart_timezone;
    ChartSymbolFacts syminfo;
    std::filesystem::path path;
    ChartDataFormat format;
    BinaryVerification verify_bin;
};

struct SymbolBindingDeclaration final {
    std::string input_title;
    SymbolBindingRole role;
    std::vector<std::string> choices;
    // Retain pairs until duplicate-key validation, rather than silently overwriting a map.
    std::vector<std::pair<std::string, std::string>> datasets;
};

struct DataContextSettings final {
    std::string artifact_key;
    std::string plugin_sha256;
    std::map<std::string, ParameterValue> fixed_inputs;
    std::map<std::string, ParameterValue> overrides;
    std::string alignment;
    std::string warmup;
    std::string pruner;
    std::shared_ptr<const SymbolFeeds> symbol_feeds;
};

struct BoundTrialContext final {
    const DataContextId id;
    const ChartDataDeclaration identity;
    const DatasetView view;
    const std::string values_sha256;
};

[[noreturn]] inline void binding_error(const std::string& reason, const std::string& message) {
    throw TypedHpoError<std::invalid_argument>(
        "hpo_study_spec_invalid", {{"reason", reason}}, message);
}

[[noreturn]] inline void binding_dataset_error(const std::string& message) {
    throw TypedHpoError<std::runtime_error>("hpo_dataset_invalid", {}, message);
}

inline void context_text(const std::string& value, const std::string& where,
                         const std::string& reason) {
    if (value.empty() ||
        std::any_of(value.begin(), value.end(), [](unsigned char byte) { return byte < 32; }))
        binding_error(reason, where + ": must be nonempty text without control characters");
    try {
        const auto encoded = dump_json(Json::string(value));
        (void)parse_json(encoded, encoded.size());
    } catch (const HpoError&) {
        binding_error(reason, where + ": must be valid UTF-8");
    }
}

inline std::string context_timeframe(std::string value, const std::string& where) {
    if (value == "D" || value == "W" || value == "M" || value == "S")
        value = "1" + value;
    static const std::regex spelling("[1-9][0-9]*[DWMS]?");
    if (!std::regex_match(value, spelling))
        binding_error("backtest", where + ": invalid timeframe");
    return value;
}

inline ChartDataDeclaration normalized_chart(ChartDataDeclaration value) {
    const auto where = "datasets." + value.id;
    context_text(value.id, where + ".id", "input_symbol");
    context_text(value.symbol, where + ".symbol", "input_symbol");
    const auto& facts = value.syminfo;
    context_text(facts.tickerid, where + ".syminfo.tickerid", "input_symbol");
    if (facts.tickerid != value.symbol)
        binding_error("input_symbol", where + ".syminfo.tickerid differs from declared symbol");
    for (const auto& item : std::vector<std::pair<std::string, std::string>>{
             {"type", facts.type}, {"currency", facts.currency},
             {"timezone", facts.timezone}, {"session", facts.session}})
        context_text(item.second, where + ".syminfo." + item.first, "backtest");
    for (const auto& item : std::vector<std::pair<std::string, double>>{
             {"mintick", facts.mintick}, {"pointvalue", facts.pointvalue},
             {"mincontract", facts.mincontract}}) {
        if (!std::isfinite(item.second) || item.second <= 0)
            binding_error("backtest", where + ".syminfo." + item.first +
                                      ": must be finite and positive");
    }
    context_text(value.chart_timezone, where + ".chart_timezone", "backtest");
    if (value.chart_timezone != facts.timezone)
        binding_error("backtest", where + ".chart_timezone differs from syminfo.timezone");
    value.input_tf = context_timeframe(value.input_tf, where + ".input_tf");
    value.script_tf = context_timeframe(value.script_tf, where + ".script_tf");
    if (value.format != ChartDataFormat::Csv && value.format != ChartDataFormat::Binary)
        binding_error("backtest", where + ": unknown chart format");
    if (value.verify_bin != BinaryVerification::Full &&
        value.verify_bin != BinaryVerification::Sidecar)
        binding_error("backtest", where + ": unknown binary verification policy");
    if (value.format == ChartDataFormat::Csv && value.verify_bin != BinaryVerification::Full)
        binding_error("backtest", where + ".verify_bin: requires binary input");
    return value;
}

inline std::uint64_t context_double_bits(const double& value) {
    static_assert(sizeof(double) == 8 && std::numeric_limits<double>::is_iec559,
                  "context identity requires IEEE-754 binary64");
    std::uint64_t bits;
    std::memcpy(&bits, &value, sizeof(bits));
    return bits;
}

inline void context_append_le(unsigned char*& output, std::uint64_t bits) {
    for (unsigned shift = 0; shift < 64; shift += 8)
        *output++ = static_cast<unsigned char>((bits >> shift) & 255);
}

inline std::string chart_values_sha256(const Dataset& dataset) {
    constexpr char domain[] = "pineforge:hpo:chart-bars:le:v1";
    Sha256 hasher;
    hasher.update(domain, sizeof(domain));  // Includes the terminating NUL.
    std::array<unsigned char, 48> bytes{};
    for (std::size_t index = 0; index < dataset.size(); ++index) {
        const auto& bar = dataset.data()[index];
        auto* output = bytes.data();
        for (const auto member : {&pf_bar_t::open, &pf_bar_t::high, &pf_bar_t::low,
                                  &pf_bar_t::close, &pf_bar_t::volume})
            context_append_le(output, context_double_bits(bar.*member));
        context_append_le(output, static_cast<std::uint64_t>(bar.timestamp));
        hasher.update(bytes.data(), bytes.size());
    }
    return hasher.hex_digest();
}

// Numbers use tagged string payloads, so text such as "int:1" cannot collide with an integer.
// Integers are decimal; binary64 payloads are exactly sixteen lowercase, most-significant-first
// hexadecimal digits. Booleans and exact UTF-8 strings retain their JSON types.
inline Json context_integer(std::int64_t value) {
    auto encoded = object_json();
    encoded.members["int"] = Json::string(std::to_string(value));
    return encoded;
}

inline Json context_real(double value) {
    if (!std::isfinite(value))
        binding_error("backtest", "data context metadata must be finite");
    constexpr char digits[] = "0123456789abcdef";
    auto bits = context_double_bits(value);
    std::string payload(16, '0');
    for (std::size_t index = payload.size(); index > 0; --index) {
        payload[index - 1] = digits[bits & 15];
        bits >>= 4;
    }
    auto encoded = object_json();
    encoded.members["binary64"] = Json::string(std::move(payload));
    return encoded;
}

inline Json context_scalar(const ParameterValue& value) {
    return std::visit([](const auto& scalar) -> Json {
        using Scalar = std::decay_t<decltype(scalar)>;
        if constexpr (std::is_same_v<Scalar, std::string>) {
            // Validate UTF-8 while allowing ordinary JSON escaping of control characters.
            const auto encoded = dump_json(Json::string(scalar));
            return parse_json(encoded, encoded.size());
        } else if constexpr (std::is_same_v<Scalar, bool>) {
            return Json::boolean(scalar);
        } else if constexpr (std::is_same_v<Scalar, double>) {
            return context_real(scalar);
        } else {
            return context_integer(scalar);
        }
    }, value);
}

inline Json context_settings(const std::map<std::string, ParameterValue>& settings) {
    auto result = object_json();
    for (const auto& item : settings)
        result.members[item.first] = context_scalar(item.second);
    return result;
}

inline Json context_typed_json(const Json& value) {
    if (value.kind == Json::Kind::Number) {
        if (value.value.find_first_of(".eE") == std::string::npos)
            return context_integer(json_integer(value));
        // Like admitted feed facts, finite subnormals remain valid even when strtod sets ERANGE.
        char* end = nullptr;
        const auto number = std::strtod(value.value.c_str(), &end);
        if (value.value.empty() || end != value.value.c_str() + value.value.size() ||
            !std::isfinite(number))
            binding_error("symbol_feeds", "data context feed metadata must be a finite number");
        return context_real(number);
    }
    auto result = value;
    for (auto& item : result.members)
        item.second = context_typed_json(item.second);
    for (auto& item : result.items)
        item = context_typed_json(item);
    return result;
}

inline Json context_facts(const ChartSymbolFacts& facts) {
    auto result = object_json();
    result.members = {
        {"currency", Json::string(facts.currency)},
        {"mincontract", context_real(facts.mincontract)},
        {"mintick", context_real(facts.mintick)}, {"pointvalue", context_real(facts.pointvalue)},
        {"session", Json::string(facts.session)}, {"tickerid", Json::string(facts.tickerid)},
        {"timezone", Json::string(facts.timezone)}, {"type", Json::string(facts.type)},
    };
    return result;
}

inline std::shared_ptr<const Dataset> load_bound_chart(const ChartDataDeclaration& declaration) {
    std::shared_ptr<const Dataset> owner;
    try {
        owner = std::make_shared<const Dataset>(
            declaration.format == ChartDataFormat::Csv
                ? Dataset::load_csv(declaration.path)
                : Dataset::load_bin(declaration.path, declaration.verify_bin));
    } catch (const HpoError&) {
        throw;
    } catch (const std::runtime_error& error) {
        binding_dataset_error("dataset " + declaration.id + ": " + error.what());
    } catch (const std::invalid_argument& error) {
        binding_dataset_error("dataset " + declaration.id + ": " + error.what());
    }
    if (declaration.format == ChartDataFormat::Binary) {
        // The existing loader validates records/sidecar first. Bound identity additionally
        // checks the descriptive labels using that same JSON parser.
        std::ifstream source(declaration.path.string() + ".json", std::ios::binary);
        if (!source)
            binding_dataset_error("dataset " + declaration.id + ": cannot read binary sidecar");
        const std::string bytes((std::istreambuf_iterator<char>(source)), {});
        if (source.bad())
            binding_dataset_error("dataset " + declaration.id + ": cannot read binary sidecar");
        std::string symbol;
        std::string timeframe;
        try {
            const auto metadata = parse_json(bytes, bytes.size());
            symbol = field(metadata, "symbol").text();
            timeframe = field(metadata, "tf").text();
        } catch (const HpoError& error) {
            binding_dataset_error("dataset " + declaration.id + ": " + error.what());
        }
        if (symbol != declaration.symbol)
            binding_dataset_error("dataset " + declaration.id + ": binary symbol " + symbol +
                " differs from " + declaration.symbol + " (" + declaration.path.string() + ")");
        if (timeframe != declaration.input_tf)
            binding_dataset_error("dataset " + declaration.id + ": binary timeframe " + timeframe +
                " differs from " + declaration.input_tf + " (" + declaration.path.string() + ")");
    }
    if (owner->empty())
        binding_dataset_error("dataset " + declaration.id + ": chart must not be empty");
    return owner;
}

// Callers invoke this catalogue and Python's validate_symbol_bindings only when bindings exist.
// An empty list is refused here (exactly one chart role); Python returns an empty tuple for it.
class AdmittedDataCatalogue final {
public:
    using ChartLoader =
        std::function<std::shared_ptr<const Dataset>(const ChartDataDeclaration&)>;

    AdmittedDataCatalogue(const std::vector<ChartDataDeclaration>& declarations,
                          const std::vector<std::string>& referenced_dataset_ids,
                          const std::vector<SymbolBindingDeclaration>& bindings,
                          const DataContextSettings& settings,
                          const std::set<std::string>& approved_alignment_modes,
                          ChartLoader load_chart = load_bound_chart) {
        std::map<std::string, ChartDataDeclaration> datasets;
        for (const auto& declaration : declarations) {
            auto normalized = normalized_chart(declaration);
            if (!datasets.emplace(normalized.id, normalized).second)
                binding_error("input_symbol", "datasets: duplicate id " + normalized.id);
        }
        std::set<std::string> references;
        for (const auto& id : referenced_dataset_ids) {
            if (!references.insert(id).second)
                binding_error("input_symbol", "strategy.datasets: duplicate id " + id);
            if (!datasets.count(id))
                binding_error("input_symbol", "strategy.datasets: unknown dataset " + id);
        }
        std::map<std::string, std::map<std::string, std::string>> mappings;
        std::size_t charts = 0;
        std::set<std::string> bound_datasets;
        auto binding_records = object_json();
        for (const auto& binding : bindings) {
            const auto where = "symbol_bindings." + binding.input_title;
            context_text(binding.input_title, where, "input_symbol");
            if (mappings.count(binding.input_title))
                binding_error("input_symbol", where + ": duplicate binding input");
            if (binding.role != SymbolBindingRole::Chart &&
                binding.role != SymbolBindingRole::Request)
                binding_error("input_symbol", where + ": unknown role");
            charts += binding.role == SymbolBindingRole::Chart ? 1 : 0;
            if (binding.role == SymbolBindingRole::Chart)
                chart_input_title_ = binding.input_title;
            std::set<std::string> domain;
            auto choices = array_json();
            for (const auto& choice : binding.choices) {
                context_text(choice, where + ".choices", "input_symbol");
                if (!domain.insert(choice).second)
                    binding_error("input_symbol", where + ".choices: duplicate " + choice);
                choices.items.push_back(Json::string(choice));
            }
            if (domain.empty())
                binding_error("input_symbol", where + ".choices: empty domain");
            auto& mapping = mappings[binding.input_title];
            for (const auto& entry : binding.datasets) {
                if (!mapping.emplace(entry).second)
                    binding_error("input_symbol",
                                  where + ".datasets: duplicate key " + entry.first);
            }
            if (mapping.size() != domain.size())
                binding_error("input_symbol", where + ".datasets: keys must equal choices exactly");
            auto recorded_map = object_json();
            std::set<std::string> targets;
            for (const auto& choice : binding.choices) {
                const auto target = mapping.find(choice);
                if (target == mapping.end())
                    binding_error("input_symbol", where + ".datasets: missing choice " + choice);
                const auto& id = target->second;
                if (!targets.insert(id).second)
                    binding_error("input_symbol", where + ".datasets: duplicate target " + id);
                if (!datasets.count(id))
                    binding_error("input_symbol", where + ".datasets: unknown dataset " + id);
                if (!references.count(id))
                    binding_error("input_symbol", where + ".datasets: unreferenced dataset " + id);
                if (datasets.at(id).symbol != choice)
                    binding_error("input_symbol", where + ".datasets[" + choice +
                                                  "]: declared symbol differs for " + id);
                bound_datasets.insert(id);
                recorded_map.members[choice] = Json::string(id);
            }
            auto record = object_json();
            record.members = {
                {"choices", choices}, {"datasets", recorded_map},
                {"role", Json::string(
                    binding.role == SymbolBindingRole::Chart ? "chart" : "request")},
            };
            binding_records.members[binding.input_title] = std::move(record);
        }
        if (charts != 1)
            binding_error("input_symbol", "symbol_bindings: exactly one chart role is required");
        if (settings.pruner != "none")
            binding_error("pruner", "execution.pruner: undecided policy for bound data");
        std::set<std::string> currencies;
        std::string declared_currencies;
        for (const auto& id : bound_datasets) {
            const auto& currency = datasets.at(id).syminfo.currency;
            currencies.insert(currency);
            declared_currencies += id + "=" + currency + " ";
        }
        if (currencies.size() > 1)
            binding_error("objective", "objective: undecided quote currency policy: " +
                                       declared_currencies);
        if (!approved_alignment_modes.count(settings.alignment))
            binding_error("backtest", "data_comparison.alignment: missing approval for " +
                                      settings.alignment);
        context_text(settings.warmup, "data_comparison.warmup", "backtest");
        if (settings.plugin_sha256.size() != 64 ||
            settings.plugin_sha256.find_first_not_of("0123456789abcdef") != std::string::npos)
            binding_error("artifact_metadata", "data context requires the verified plugin sha256");
        auto dataset_records = object_json();
        std::map<std::string, DataContextId> ids;
        // IDs and declared facts identify owners: shared paths still load separate tapes.
        // Load all references, including binding targets; only validate other declarations.
        contexts_.reserve(references.size());
        for (const auto& id : references) {
            const auto& declaration = datasets.at(id);
            const auto owner = load_chart(declaration);
            if (!owner || owner->empty())
                binding_dataset_error("dataset " + id + ": chart must not be empty");
            const auto values = chart_values_sha256(*owner);
            const DataContextId context_id{static_cast<std::uint64_t>(contexts_.size())};
            ids.emplace(id, context_id);
            contexts_.push_back({context_id, declaration, DatasetView(owner), values});
            if (owner->size() >
                static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
                binding_dataset_error("dataset count exceeds context integer encoding");
            auto record = object_json();
            record.members = {
                {"chart_timezone", Json::string(declaration.chart_timezone)},
                {"count", context_integer(static_cast<std::int64_t>(owner->size()))},
                {"facts", context_facts(declaration.syminfo)},
                {"first_ts", context_integer(owner->data()[0].timestamp)},
                {"identity_revision", context_integer(1)},
                {"input_tf", Json::string(declaration.input_tf)},
                {"last_ts", context_integer(owner->data()[owner->size() - 1].timestamp)},
                {"parser", Json::string(declaration.format == ChartDataFormat::Csv
                                           ? "chart-csv/v1" : "pf_bar_t/v1")},
                {"script_tf", Json::string(declaration.script_tf)},
                {"symbol", Json::string(declaration.symbol)},
                {"values_sha256", Json::string(values)},
                {"verification", Json::string(declaration.format == ChartDataFormat::Csv
                    ? "parsed" : declaration.verify_bin == BinaryVerification::Full
                    ? "full" : "sidecar")},
            };
            dataset_records.members[id] = std::move(record);
        }
        for (const auto& binding : bindings) {
            choices_.emplace(binding.input_title, binding.choices);
            auto& selector = selectors_[binding.input_title];
            selector.reserve(binding.choices.size());
            for (const auto& choice : binding.choices)
                selector.push_back(ids.at(mappings.at(binding.input_title).at(choice)));
        }
        auto comparison = object_json();
        comparison.members = {{"alignment", Json::string(settings.alignment)},
                              {"pruner", Json::string(settings.pruner)},
                              {"warmup", Json::string(settings.warmup)}};
        auto descriptor = object_json();
        descriptor.members = {
            {"artifact_key", Json::string(settings.artifact_key)}, {"bindings", binding_records},
            {"comparison", comparison}, {"datasets", dataset_records},
            {"fixed_inputs", context_settings(settings.fixed_inputs)},
            {"overrides", context_settings(settings.overrides)},
            {"plugin_sha256", Json::string(settings.plugin_sha256)},
            {"schema", Json::string("pineforge-hpo-data-context/v1")},
            {"symbol_feeds", settings.symbol_feeds && !settings.symbol_feeds->empty()
                ? context_typed_json(symbol_feeds_record(*settings.symbol_feeds)) : object_json()},
        };
        descriptor_ = dump_json(descriptor);
        // The parser validates exact UTF-8, including object keys supplied by internal callers.
        (void)parse_json(descriptor_, descriptor_.size());
        constexpr char domain[] = "pineforge:hpo:data-context:v1";
        digest_ = sha256(std::string(domain, sizeof(domain)) + descriptor_);
        verify_unchanged();
    }

    DataContextId resolve(const std::string& input_title, SymbolChoiceOrdinal ordinal) const {
        const auto found = selectors_.find(input_title);
        if (found == selectors_.end())
            binding_error("input_symbol", "unknown binding input " + input_title);
        if (ordinal.value >= found->second.size())
            binding_error("input_symbol", input_title + ": unknown symbol choice ordinal " +
                                          std::to_string(ordinal.value));
        return found->second[static_cast<std::size_t>(ordinal.value)];
    }

    DataContextId resolve_choice(const std::string& input_title, const std::string& choice) const {
        const auto found = choices_.find(input_title);
        if (found != choices_.end()) {
            const auto selected = std::find(found->second.begin(), found->second.end(), choice);
            if (selected != found->second.end())
                return resolve(input_title, SymbolChoiceOrdinal{
                    static_cast<std::uint64_t>(selected - found->second.begin())});
        }
        binding_error("input_symbol", input_title + ": unknown symbol choice " + choice);
    }

    const BoundTrialContext& context(DataContextId id) const {
        if (id.value >= contexts_.size())
            binding_error("input_symbol", "unknown data context id " + std::to_string(id.value));
        return contexts_[static_cast<std::size_t>(id.value)];
    }

    const std::vector<BoundTrialContext>& contexts() const noexcept { return contexts_; }
    const std::string& chart_input_title() const noexcept { return chart_input_title_; }

    const std::vector<std::string>& choices(const std::string& input_title) const {
        const auto found = choices_.find(input_title);
        if (found == choices_.end())
            binding_error("input_symbol", "unknown binding input " + input_title);
        return found->second;
    }

    std::vector<std::filesystem::path> binary_inputs() const {
        std::vector<std::filesystem::path> inputs;
        for (const auto& context : contexts_) {
            if (context.identity.format == ChartDataFormat::Binary) {
                inputs.push_back(context.identity.path);
                inputs.emplace_back(context.identity.path.string() + ".json");
            }
        }
        return inputs;
    }

    const std::string& canonical_descriptor() const noexcept { return descriptor_; }
    const std::string& data_context_sha256() const noexcept { return digest_; }

    void verify_unchanged() const {
        for (const auto& context : contexts_)
            context.view.owner->verify_unchanged();
    }

private:
    std::vector<BoundTrialContext> contexts_;
    std::map<std::string, std::vector<DataContextId>> selectors_;
    std::map<std::string, std::vector<std::string>> choices_;
    std::string chart_input_title_;
    std::string descriptor_;
    std::string digest_;
};

}  // namespace pineforge::hpo::detail
