#pragma once

#include <pineforge/hpo/error.hpp>

#include "json.hpp"
#include "sha256.hpp"
#include "../core/sampler_checkpoint.hpp"

#include <pineforge/hpo/sampler.hpp>
#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <set>
#include <sstream>
#include <unordered_set>

namespace pineforge::hpo::detail {

inline constexpr int space_hash_version = 1;

class WarmStartError : public TypedHpoError<> {
public:
    explicit WarmStartError(const std::string& message)
        : TypedHpoError<>("hpo_warm_start_rejected", {}, "warm-start incompatible: " + message) {}
};

class SpaceExhausted : public TypedHpoError<> {
public:
    SpaceExhausted()
        : TypedHpoError<>(
              "hpo_space_exhausted", {}, "space exhausted: every parameter vector was tried") {}
};

inline const Json& field(const Json& document, const std::string& name) {
    const auto* value = document.find(name);
    if (!value)
        throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                "missing " + name);
    return *value;
}

inline Json object_json() {
    Json result;
    result.kind = Json::Kind::Object;
    return result;
}

inline Json array_json() {
    Json result;
    result.kind = Json::Kind::Array;
    return result;
}

inline std::string dump_json(const Json& document) {
    switch (document.kind) {
    case Json::Kind::Null: return "null";
    case Json::Kind::Bool:
    case Json::Kind::Number: return document.value;
    case Json::Kind::String: {
        std::ostringstream output;
        output << '"';
        for (const auto character : document.value) {
            const auto byte = static_cast<unsigned char>(character);
            if (character == '"' || character == '\\')
                output << '\\' << character;
            else if (character == '\b')
                output << "\\b";
            else if (character == '\f')
                output << "\\f";
            else if (character == '\n')
                output << "\\n";
            else if (character == '\r')
                output << "\\r";
            else if (character == '\t')
                output << "\\t";
            else if (byte < 0x20)
                output << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                       << static_cast<unsigned>(byte) << std::dec;
            else
                output << character;
        }
        output << '"';
        return output.str();
    }
    case Json::Kind::Array:
    case Json::Kind::Object: {
        const bool array = document.kind == Json::Kind::Array;
        std::string output = array ? "[" : "{";
        std::size_t index = 0;
        if (array) {
            for (const auto& item : document.items) {
                if (index++)
                    output += ',';
                output += dump_json(item);
            }
        } else {
            for (const auto& entry : document.members) {
                if (index++)
                    output += ',';
                output += dump_json(Json::string(entry.first)) + ':' + dump_json(entry.second);
            }
        }
        return output + (array ? "]" : "}");
    }
    }
    throw TypedHpoError<std::logic_error>("hpo_invariant", {}, "invalid JSON kind");
}

inline Json scalar_json(const ParameterValue& value) {
    return std::visit([](const auto& scalar) {
        using Scalar = std::decay_t<decltype(scalar)>;
        if constexpr (std::is_same_v<Scalar, std::string>)
            return Json::string(scalar);
        else if constexpr (std::is_same_v<Scalar, bool>)
            return Json::boolean(scalar);
        else {
            auto text = serialize_parameter_value(ParameterValue(scalar));
            if constexpr (std::is_same_v<Scalar, double>) {
                if (text.find_first_of(".eE") == std::string::npos)
                    text += ".0";
            }
            return Json::number(std::move(text));
        }
    }, value);
}

inline std::int64_t json_integer(const Json& value) {
    if (value.kind != Json::Kind::Number || value.value.find_first_of(".eE") != std::string::npos)
        throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                "expected int64 JSON integer");
    std::size_t consumed = 0;
    std::int64_t parsed;
    try {
        parsed = std::stoll(value.value, &consumed);
    } catch (const std::out_of_range& error) {
        throw TypedHpoError<std::out_of_range>(
            "hpo_study_spec_invalid", {{"reason", "study"}}, error.what());
    } catch (const std::invalid_argument& error) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "study"}}, error.what());
    }
    if (consumed != value.value.size())
        throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                "invalid integer");
    return parsed;
}

inline std::uint64_t json_id(const Json& value) {
    if (value.kind != Json::Kind::Number || value.value.empty() || value.value.front() == '-' ||
        value.value.find_first_of(".eE") != std::string::npos)
        throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                "expected uint64 trial_id");
    std::size_t consumed = 0;
    std::uint64_t parsed;
    try {
        parsed = std::stoull(value.value, &consumed);
    } catch (const std::out_of_range& error) {
        throw TypedHpoError<std::out_of_range>(
            "hpo_study_spec_invalid", {{"reason", "study"}}, error.what());
    } catch (const std::invalid_argument& error) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "study"}}, error.what());
    }
    if (consumed != value.value.size() || parsed == std::numeric_limits<std::uint64_t>::max())
        throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                "trial_id leaves no continuation ID");
    return parsed;
}

inline ParameterValue scalar_value(const Json& value) {
    if (value.kind == Json::Kind::String)
        return value.text();
    if (value.kind == Json::Kind::Bool)
        return value.value == "true";
    if (value.kind == Json::Kind::Number) {
        if (value.value.find_first_of(".eE") == std::string::npos)
            return json_integer(value);
        return value.real();
    }
    throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                            "expected scalar parameter");
}

inline Json recorded_space(const SearchSpace& space, const std::string& expression,
                           const std::string& direction,
                           const std::vector<std::string>& constraints) {
    auto result = object_json();
    auto parameters = object_json();
    for (const auto& dimension : space.dimensions()) {
        auto parameter = object_json();
        std::visit([&](const auto& item) {
            using Item = std::decay_t<decltype(item)>;
            if constexpr (std::is_same_v<Item, IntegerDimension> ||
                          std::is_same_v<Item, RealDimension>) {
                parameter.members["kind"] = Json::string(
                    std::is_same_v<Item, IntegerDimension> ? "integer" : "real");
                parameter.members["low"] = scalar_json(item.low());
                parameter.members["high"] = scalar_json(item.high());
                parameter.members["log"] = Json::boolean(item.log());
                if constexpr (std::is_same_v<Item, IntegerDimension>)
                    parameter.members["step"] = scalar_json(item.step());
                else
                    parameter.members["step"] = item.step() ? scalar_json(*item.step()) : Json{};
            } else if constexpr (std::is_same_v<Item, BooleanDimension>) {
                parameter.members["kind"] = Json::string("boolean");
            } else {
                parameter.members["kind"] = Json::string("categorical");
                auto choices = array_json();
                for (const auto& choice : item.choices())
                    choices.items.push_back(scalar_json(choice));
                parameter.members["choices"] = std::move(choices);
            }
        }, dimension);
        parameters.members[std::string(dimension_name(dimension))] = std::move(parameter);
    }
    result.members["parameters"] = std::move(parameters);
    auto objective = object_json();
    objective.members["expression"] = Json::string(expression);
    objective.members["direction"] = Json::string(direction);
    auto constraint_array = array_json();
    for (const auto& constraint : constraints)
        constraint_array.items.push_back(Json::string(constraint));
    objective.members["constraints"] = std::move(constraint_array);
    result.members["objective"] = std::move(objective);
    return result;
}

inline std::string real_bits(double value) {
    if (value == 0.0)
        value = 0.0;
    std::uint64_t bits = 0;
    static_assert(sizeof(bits) == sizeof(value));
    std::memcpy(&bits, &value, sizeof(bits));
    std::ostringstream output;
    output << std::hex << std::setw(16) << std::setfill('0') << bits;
    return output.str();
}

inline Json canonical_scalar(const Json& value) {
    auto result = array_json();
    const auto scalar = scalar_value(value);
    result.items.push_back(Json::string(std::holds_alternative<std::int64_t>(scalar)
        ? "integer" : std::holds_alternative<double>(scalar) ? "real" :
          std::holds_alternative<bool>(scalar) ? "boolean" : "string"));
    result.items.push_back(std::holds_alternative<double>(scalar)
        ? Json::string(real_bits(std::get<double>(scalar))) : scalar_json(scalar));
    if (std::holds_alternative<std::int64_t>(scalar))
        result.items.back() = Json::string(std::to_string(std::get<std::int64_t>(scalar)));
    return result;
}

inline std::string canonical_space(const Json& space) {
    auto result = object_json();
    result.members["space_hash_version"] = Json::number(std::to_string(space_hash_version));
    auto parameters = object_json();
    if (field(space, "parameters").kind != Json::Kind::Object)
        throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                "parameters must be an object");
    for (const auto& entry : field(space, "parameters").members) {
        const auto& source = entry.second;
        auto parameter = object_json();
        const auto kind = field(source, "kind").text();
        parameter.members["kind"] = Json::string(kind);
        if (kind == "integer" || kind == "real") {
            for (const auto* name : {"low", "high", "step"}) {
                const auto& number = field(source, name);
                parameter.members[name] = number.kind == Json::Kind::Null ? Json{} :
                    Json::string(kind == "integer" ? std::to_string(json_integer(number)) :
                                                 real_bits(number.real()));
            }
            parameter.members["log"] = field(source, "log");
            if (parameter.members["log"].kind != Json::Kind::Bool)
                throw TypedHpoError<std::runtime_error>(
                    "hpo_study_spec_invalid", {{"reason", "study"}}, "log must be boolean");
        } else if (kind == "categorical") {
            auto choices = array_json();
            if (field(source, "choices").kind != Json::Kind::Array)
                throw TypedHpoError<std::runtime_error>(
                    "hpo_study_spec_invalid", {{"reason", "study"}}, "choices must be an array");
            for (const auto& choice : field(source, "choices").items)
                choices.items.push_back(canonical_scalar(choice));
            parameter.members["choices"] = std::move(choices);
        } else if (kind != "boolean") {
            throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                    "unknown parameter kind");
        }
        parameters.members[entry.first] = std::move(parameter);
    }
    result.members["parameters"] = std::move(parameters);
    const auto& source = field(space, "objective");
    auto objective = object_json();
    objective.members["expression"] = field(source, "expression");
    objective.members["direction"] = field(source, "direction");
    field(source, "expression").text();
    field(source, "direction").text();
    auto constraints = field(source, "constraints");
    if (constraints.kind != Json::Kind::Array)
        throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                "constraints must be an array");
    for (const auto& constraint : constraints.items)
        constraint.text();
    std::sort(constraints.items.begin(), constraints.items.end(), [](const auto& left,
                                                                  const auto& right) {
        return left.text() < right.text();
    });
    objective.members["constraints"] = std::move(constraints);
    result.members["objective"] = std::move(objective);
    return dump_json(result);
}

inline std::string space_hash(const Json& space) { return sha256(canonical_space(space)); }

inline double symbol_mintick(const Json& value) {
    char* end = nullptr;
    const auto number = std::strtod(value.value.c_str(), &end);
    if (value.kind != Json::Kind::Number || end != value.value.c_str() + value.value.size() ||
        !(number > 0) || !std::isfinite(number))
        throw TypedHpoError<std::runtime_error>(
            "hpo_study_spec_invalid", {{"reason", "study"}},
            "symbol feed mintick must be a positive finite number");
    return number;
}

inline std::string symbol_feeds_identity(const Json* record) {
    if (!record)
        return {};
    auto normalized = *record;
    const auto* symbols = normalized.kind == Json::Kind::Object ? normalized.find("symbols") : nullptr;
    if (!symbols || symbols->kind != Json::Kind::Object)
        throw TypedHpoError<std::runtime_error>(
            "hpo_study_spec_invalid", {{"reason", "study"}},
            "invalid symbol feeds header record: symbols must be an object");
    for (auto& symbol : normalized.members.find("symbols")->second.members) {
        const auto* facts_record = symbol.second.kind == Json::Kind::Object ?
            symbol.second.find("facts") : nullptr;
        const auto* feeds_record = symbol.second.kind == Json::Kind::Object ?
            symbol.second.find("feeds") : nullptr;
        if (!facts_record || facts_record->kind != Json::Kind::Object ||
            !feeds_record || feeds_record->kind != Json::Kind::Object)
            throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                    "invalid symbol feeds header record for " +
                                                        symbol.first +
                                                        ": facts and feeds must be objects");
        auto& facts = symbol.second.members.find("facts")->second;
        const auto mintick = facts.members.find("mintick");
        if (mintick != facts.members.end())
            mintick->second = Json::string(real_bits(symbol_mintick(mintick->second)));
    }
    return sha256(dump_json(normalized));
}

inline void validate_symbol_feeds_identity(const Json* parent, const Json& current) {
    if (const auto* deferred = current.find("_defer_symbol_feeds");
        deferred && deferred->kind == Json::Kind::Bool && deferred->value == "true")
        return;
    if (symbol_feeds_identity(parent) != symbol_feeds_identity(current.find("symbol_feeds")))
        throw TypedHpoError<std::runtime_error>(
            "hpo_study_spec_invalid", {{"reason", "study"}},
            "symbol feeds differ from parent (facts or feed values)");
}

inline Json space_from_spec(const Json& spec) {
    const auto& strategies = field(spec, "strategies");
    if (strategies.kind != Json::Kind::Array || strategies.items.size() != 1)
        throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                "space-info requires a single-strategy StudySpec");
    const auto& parameters = field(strategies.items.front(), "search_space");
    if (parameters.kind != Json::Kind::Object)
        throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                "search_space must be an object");
    std::vector<Dimension> dimensions;
    for (const auto& entry : parameters.members) {
        const auto& parameter = entry.second;
        const auto kind = field(parameter, "kind").text();
        const auto* step = parameter.find("step");
        const auto* log = parameter.find("log");
        if (log && log->kind != Json::Kind::Bool)
            throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                    "log must be boolean");
        const bool logarithmic = log && log->value == "true";
        if (kind == "integer")
            dimensions.emplace_back(IntegerDimension(entry.first,
                json_integer(field(parameter, "low")), json_integer(field(parameter, "high")),
                step ? json_integer(*step) : 1, logarithmic));
        else if (kind == "real")
            dimensions.emplace_back(RealDimension(entry.first, field(parameter, "low").real(),
                field(parameter, "high").real(), step && step->kind != Json::Kind::Null ?
                std::optional<double>(step->real()) : std::nullopt, logarithmic));
        else if (kind == "boolean")
            dimensions.emplace_back(BooleanDimension(entry.first));
        else if (kind == "categorical") {
            std::vector<ParameterValue> choices;
            const auto& values = field(parameter, "choices");
            if (values.kind != Json::Kind::Array)
                throw TypedHpoError<std::runtime_error>(
                    "hpo_study_spec_invalid", {{"reason", "study"}}, "choices must be an array");
            for (const auto& value : values.items)
                choices.push_back(scalar_value(value));
            dimensions.emplace_back(CategoricalDimension(entry.first, std::move(choices)));
        } else
            throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                    "unknown parameter kind");
    }
    const auto& objective = field(spec, "objective");
    if (field(objective, "kind").text() != "expression")
        throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                "space-info requires an expression objective");
    std::vector<std::string> constraints;
    if (const auto* values = objective.find("constraints")) {
        if (values->kind != Json::Kind::Array)
            throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                    "constraints must be an array");
        for (const auto& value : values->items)
            constraints.push_back(value.text());
    }
    const auto direction = field(objective, "direction").text();
    if (direction != "maximize" && direction != "minimize")
        throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                "unknown objective direction");
    return recorded_space(SearchSpace(std::move(dimensions)),
        field(objective, "expression").text(), direction, constraints);
}

inline SearchSpace search_space_from_recorded(const Json& space) {
    auto strategy = object_json();
    strategy.members["search_space"] = field(space, "parameters");
    auto spec = object_json();
    spec.members["strategies"] = array_json();
    spec.members["strategies"].items.push_back(std::move(strategy));
    spec.members["objective"] = field(space, "objective");
    spec.members["objective"].members["kind"] = Json::string("expression");
    const auto normalized = space_from_spec(spec);
    std::vector<Dimension> dimensions;
    for (const auto& entry : field(normalized, "parameters").members) {
        const auto& parameter = entry.second;
        const auto kind = field(parameter, "kind").text();
        if (kind == "integer")
            dimensions.emplace_back(IntegerDimension(entry.first,
                json_integer(field(parameter, "low")), json_integer(field(parameter, "high")),
                json_integer(field(parameter, "step")), field(parameter, "log").value == "true"));
        else if (kind == "real") {
            const auto& step = field(parameter, "step");
            dimensions.emplace_back(RealDimension(entry.first, field(parameter, "low").real(),
                field(parameter, "high").real(), step.kind == Json::Kind::Null ? std::nullopt :
                std::optional<double>(step.real()), field(parameter, "log").value == "true"));
        } else if (kind == "boolean")
            dimensions.emplace_back(BooleanDimension(entry.first));
        else {
            std::vector<ParameterValue> choices;
            for (const auto& value : field(parameter, "choices").items)
                choices.push_back(scalar_value(value));
            dimensions.emplace_back(CategoricalDimension(entry.first, std::move(choices)));
        }
    }
    return SearchSpace(std::move(dimensions));
}

inline std::string candidate_key(const Candidate& candidate) {
    auto result = object_json();
    for (const auto& entry : candidate.values)
        result.members[entry.first] = canonical_scalar(scalar_json(entry.second));
    return dump_json(result);
}

inline std::string read_document(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input)
        throw TypedHpoError<std::runtime_error>("hpo_input_file_invalid", {},
                                                "cannot read " + path.string());
    std::string result;
    char buffer[8192];
    while (input.read(buffer, sizeof(buffer)) || input.gcount() != 0) {
        result.append(buffer, static_cast<std::size_t>(input.gcount()));
        if (result.size() > 256 * 1024 * 1024)
            throw TypedHpoError<std::runtime_error>(
                "hpo_input_file_invalid", {},
                "warm-start document exceeds 256 MiB; use JSONL shards");
    }
    if (input.bad())
        throw TypedHpoError<std::runtime_error>("hpo_input_file_invalid", {},
                                                "failed reading " + path.string());
    return result;
}

class BinaryWarmSource;

struct WarmHistory {
    std::string source_sha256;
    std::string sampler_state;
    std::vector<WarmStartObservation> observations;
    std::vector<std::vector<std::optional<double>>> rung_scores;
    std::vector<Json> records;
    std::unordered_set<std::string> tried;
    std::uint64_t completed = 0;
    std::uint64_t feasible = 0;
    std::uint64_t next_id = 0;
    std::shared_ptr<const BinaryWarmSource> binary;

    std::uint64_t size() const;
    std::uint64_t tried_count() const;
    bool contains(const Candidate& candidate) const;
    std::uint64_t ordinal(const SearchSpace& space, std::uint64_t row) const;
    Json record(const SearchSpace& space, std::uint64_t row) const;
    std::string source_digest() const;
};

inline WarmHistory load_json_warm_history(const std::filesystem::path& path,
                                     const SearchSpace& space, const Json& current_space) {
    WarmHistory history;
    if (path.empty())
        return history;
    try {
        const auto content = read_document(path);
        history.source_sha256 = sha256(content);
        std::optional<Json> document;
        try {
            document = parse_json(content, 256 * 1024 * 1024);
        } catch (const std::runtime_error&) {
        }
        const Json* parent_space = nullptr;
        std::optional<Json> legacy_space;
        if (document && document->kind == Json::Kind::Object && document->find("trials")) {
            if (const auto* state = document->find("tpe_sampler_state")) {
                history.sampler_state = state->text();
                const auto& checkpoint = history.sampler_state;
                sampler_checkpoint_payload(checkpoint);
            }
            if (const auto* mode = document->find("trials_out"); mode && mode->text() != "all")
                throw TypedHpoError<std::runtime_error>(
                    "hpo_study_spec_invalid", {{"reason", "study"}},
                    "summary/none result is not a complete trial history");
            const auto& records = field(*document, "trials");
            if (records.kind != Json::Kind::Array)
                throw TypedHpoError<std::runtime_error>(
                    "hpo_study_spec_invalid", {{"reason", "study"}}, "trials must be an array");
            if (const auto* count = document->find("trials_completed"); count &&
                json_integer(*count) != static_cast<std::int64_t>(records.items.size()))
                throw TypedHpoError<std::runtime_error>(
                    "hpo_study_spec_invalid", {{"reason", "study"}},
                    "result does not contain all completed trials");
            if (const auto* inherited = document->find("warm_start_trials")) {
                if (inherited->kind != Json::Kind::Array)
                    throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid",
                                                            {{"reason", "study"}},
                                                            "warm_start_trials must be an array");
                history.records = inherited->items;
            }
            if (const auto* warm = document->find("warm_start"); warm &&
                json_integer(field(*warm, "trials")) !=
                    static_cast<std::int64_t>(history.records.size()))
                throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid",
                                                        {{"reason", "study"}},
                                                        "result is missing ancestor trials");
            history.records.insert(history.records.end(), records.items.begin(),
                                   records.items.end());
            parent_space = document->find("space");
            if (!parent_space) {
                if (const auto* spec = document->find("study_spec"))
                    legacy_space = space_from_spec(*spec);
                else if (const auto* source = document->find("study")) {
                    auto spec_path = std::filesystem::path(source->text());
                    if (spec_path.is_relative())
                        spec_path = path.parent_path() / spec_path;
                    legacy_space = space_from_spec(parse_json(read_document(spec_path)));
                }
                if (legacy_space)
                    parent_space = &*legacy_space;
            }
        } else if (document && document->kind == Json::Kind::Array) {
            history.records = document->items;
        } else {
            std::istringstream lines(content);
            std::string line;
            while (std::getline(lines, line)) {
                if (line.find_first_not_of(" \r\t") != std::string::npos)
                    history.records.push_back(parse_json(line, 256 * 1024 * 1024));
            }
        }
        if (history.records.empty())
            throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid", {{"reason", "study"}},
                                                    "parent has no trials");
        const auto expected = canonical_space(current_space);
        const auto expected_hash = sha256(expected);
        const auto verify_hash = [&](const Json& owner) {
            const auto* version = owner.find("space_hash_version");
            const auto* hash = owner.find("space_hash");
            if (version && json_integer(*version) == space_hash_version && hash &&
                hash->text() != expected_hash)
                throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid",
                                                        {{"reason", "study"}},
                                                        "recorded space_hash does not match space");
        };
        if (parent_space && canonical_space(*parent_space) != expected)
            throw TypedHpoError<std::runtime_error>(
                "hpo_study_spec_invalid", {{"reason", "study"}},
                "search space or objective differs (space_hash)");
        if (parent_space)
            validate_symbol_feeds_identity(parent_space->find("symbol_feeds"), current_space);
        if (document && document->kind == Json::Kind::Object) {
            if (const auto* runtime = document->find("applied_runtime")) {
                validate_symbol_feeds_identity(runtime->find("symbol_feeds"), current_space);
            }
            if (const auto* digest = document->find("runtime_sha256")) {
                if (digest->text() != symbol_feeds_identity(current_space.find("symbol_feeds")))
                    throw TypedHpoError<std::runtime_error>(
                        "hpo_study_spec_invalid", {{"reason", "study"}},
                        "symbol feeds differ from parent (runtime_sha256)");
            }
        }
        if (parent_space && document)
            verify_hash(*document);
        std::sort(history.records.begin(), history.records.end(), [](const auto& left,
                                                                    const auto& right) {
            return json_id(field(left, "trial_id")) < json_id(field(right, "trial_id"));
        });
        std::set<std::uint64_t> ids;
        for (const auto& record : history.records) {
            const auto* recorded = record.find("space");
            if (!recorded && !parent_space)
                throw TypedHpoError<std::runtime_error>(
                    "hpo_study_spec_invalid", {{"reason", "study"}},
                    "missing recorded space; compatibility cannot be proven");
            if (recorded && canonical_space(*recorded) != expected)
                throw TypedHpoError<std::runtime_error>(
                    "hpo_study_spec_invalid", {{"reason", "study"}},
                    "search space or objective differs (space_hash)");
            if (recorded)
                validate_symbol_feeds_identity(recorded->find("symbol_feeds"), current_space);
            verify_hash(record);
            const auto status = field(record, "status").text();
            const std::set<std::string> statuses{"ok", "constraint_violation", "engine_error",
                "objective_error", "constraint_error", "trial_error", "trial_timeout",
                "pruned", "partial"};
            if (!statuses.count(status))
                throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid",
                                                        {{"reason", "study"}},
                                                        "unknown trial status: " + status);
            Candidate candidate;
            candidate.id = json_id(field(record, "trial_id"));
            if (!ids.insert(candidate.id).second)
                throw TypedHpoError<std::runtime_error>(
                    "hpo_study_spec_invalid", {{"reason", "study"}}, "duplicate trial_id");
            history.next_id = std::max(history.next_id, candidate.id + 1);
            const auto& values = field(record, "parameters");
            if (values.kind != Json::Kind::Object)
                throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid",
                                                        {{"reason", "study"}},
                                                        "parameters must be an object");
            for (const auto& dimension : space.dimensions()) {
                const auto name = std::string(dimension_name(dimension));
                const auto& value = field(values, name);
                ParameterValue scalar = dimension_kind(dimension) == DimensionKind::Real
                    ? ParameterValue(value.real()) : scalar_value(value);
                if (const auto* categorical = std::get_if<CategoricalDimension>(&dimension)) {
                    if (value.kind == Json::Kind::Number && !categorical->contains(scalar)) {
                        const ParameterValue real = value.real();
                        if (categorical->contains(real))
                            scalar = real;
                    }
                }
                candidate.values.emplace(name, std::move(scalar));
            }
            if (values.members.size() != candidate.values.size() || !space.is_valid(candidate))
                throw TypedHpoError<std::runtime_error>(
                    "hpo_study_spec_invalid", {{"reason", "study"}}, "invalid trial parameters");
            try {
                if (space.finite_cardinality())
                    candidate = space.candidate_at(space.candidate_ordinal(candidate),
                                                   candidate.id);
            } catch (const std::overflow_error&) {
            }
            const auto& feasible = field(record, "feasible");
            if (feasible.kind != Json::Kind::Bool)
                throw TypedHpoError<std::runtime_error>(
                    "hpo_study_spec_invalid", {{"reason", "study"}}, "feasible must be boolean");
            const auto& objective = field(record, "objective");
            const bool completed = status == "ok" || status == "constraint_violation";
            const bool is_feasible = feasible.value == "true";
            if ((status == "ok" && !is_feasible) || (status != "ok" && is_feasible))
                throw TypedHpoError<std::runtime_error>(
                    "hpo_study_spec_invalid", {{"reason", "study"}},
                    "inconsistent status/feasibility/objective");
            std::optional<double> score;
            if (objective.kind != Json::Kind::Null) {
                const auto parsed = objective.real();
                if (completed && is_feasible)
                    score = parsed;
            }
            history.completed += completed;
            history.feasible += is_feasible;
            history.tried.insert(candidate_key(candidate));
            history.observations.push_back({std::move(candidate), score});
            std::vector<std::optional<double>> scores;
            if (const auto* pruning = record.find("pruning")) {
                if (pruning->kind != Json::Kind::Object)
                    throw TypedHpoError<std::runtime_error>("hpo_study_spec_invalid",
                                                            {{"reason", "study"}},
                                                            "pruning must be an object");
                if (const auto* rungs = pruning->find("rung_scores")) {
                    if (rungs->kind != Json::Kind::Array)
                        throw TypedHpoError<std::runtime_error>(
                            "hpo_study_spec_invalid", {{"reason", "study"}},
                            "pruning.rung_scores must be an array");
                    for (const auto& rung : rungs->items)
                        scores.push_back(rung.kind == Json::Kind::Null ? std::nullopt :
                            std::optional<double>(rung.real()));
                }
            }
            history.rung_scores.push_back(std::move(scores));
        }
        return history;
    } catch (const std::exception& error) {
        throw WarmStartError(error.what());
    }
}

}

#include "warm_binary.hpp"
