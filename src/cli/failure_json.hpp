#pragma once

#include <pineforge/hpo/error.hpp>

#include "continuation.hpp"

#include <iomanip>
#include <optional>
#include <type_traits>

namespace pineforge::hpo::detail {

inline std::string capped_error(const std::string& text) {
    constexpr std::size_t limit = 4096;
    if (text.size() <= limit)
        return text;
    std::size_t end = limit;
    while (end && (static_cast<unsigned char>(text[end]) & 0xc0) == 0x80)
        --end;
    return text.substr(0, end);
}

inline std::optional<std::string> canonical_failure_args(const std::optional<std::string>& text) {
    if (!text || text->empty() || text->size() > 16384)
        return std::nullopt;
    try {
        auto document = parse_json(*text, 16384);
        if (document.kind != Json::Kind::Object)
            return std::nullopt;
        for (auto& member : document.members) {
            auto& value = member.second;
            if (value.kind == Json::Kind::Object || value.kind == Json::Kind::Array ||
                (value.kind == Json::Kind::String && value.value.size() > 1024))
                return std::nullopt;
            if (value.kind == Json::Kind::Number &&
                value.value.find_first_of(".eE") != std::string::npos) {
                std::istringstream input(value.value);
                input.imbue(std::locale::classic());
                double number = 0;
                input >> number;
                if (!input || !input.eof() || !std::isfinite(number))
                    return std::nullopt;
                std::ostringstream output;
                output.imbue(std::locale::classic());
                output << std::setprecision(17) << number;
                value.value = output.str();
            }
            if (value.kind == Json::Kind::Number && value.value == "-0")
                value.value = "0";
        }
        return dump_json(document);
    } catch (const std::exception&) {
        return std::nullopt;
    }
}

inline std::string failure_args_json(const ErrorArguments& args) {
    auto document = object_json();
    for (const auto& member : args) {
        document.members.emplace(member.first,
                                 std::visit(
                                     [](const auto& value) -> Json {
                                         using Type = std::decay_t<decltype(value)>;
                                         if constexpr (std::is_same_v<Type, std::nullptr_t>) {
                                             return {};
                                         } else if constexpr (std::is_same_v<Type, std::string>) {
                                             return Json::string(value);
                                         } else if constexpr (std::is_same_v<Type, bool>) {
                                             return Json::boolean(value);
                                         } else if constexpr (std::is_integral_v<Type>) {
                                             return Json::number(std::to_string(value));
                                         } else {
                                             std::ostringstream output;
                                             output.imbue(std::locale::classic());
                                             output << std::setprecision(17) << value;
                                             return Json::number(output.str());
                                         }
                                     },
                                     member.second.value()));
    }
    return dump_json(document);
}

struct FailureDetails {
    std::optional<std::string> code;
    std::optional<std::string> args;
    std::string origin;
};

inline FailureDetails exception_failure(const std::exception& error) {
    if (const auto* engine = dynamic_cast<const EngineError*>(&error))
        return {engine->code().empty() ? std::nullopt : std::optional<std::string>(engine->code()),
                canonical_failure_args(engine->raw_args()), "engine"};
    if (const auto* typed = dynamic_cast<const HpoError*>(&error))
        return {typed->code(), failure_args_json(typed->args()),
                typed->origin() == FailureOrigin::Engine ? "engine" : "hpo"};
    return {"hpo_unclassified_error", "{}", "hpo"};
}

inline std::string failure_json(const std::exception& error, std::int32_t exit_code) {
    const auto failure = exception_failure(error);
    return "{\"origin\":" +
           dump_json(Json::string(failure.origin)) +
           ",\"code\":" + (failure.code ? dump_json(Json::string(*failure.code)) : "null") +
           ",\"args\":" + failure.args.value_or("null") +
           ",\"exit_code\":" + std::to_string(exit_code) + "}";
}

inline std::string failure_document(const std::exception& error, std::int32_t exit_code) {
    return "{\"schema_version\":1,\"ok\":false,\"failure\":" + failure_json(error, exit_code) +
           "}\n";
}

}  // namespace pineforge::hpo::detail
