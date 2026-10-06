#include "pineforge/hpo/types.hpp"
#include <pineforge/hpo/error.hpp>

#include <cmath>
#include <iomanip>
#include <limits>
#include <locale>
#include <sstream>
#include <stdexcept>
#include <type_traits>

namespace pineforge::hpo {

ParameterType parameter_type(const ParameterValue& value) noexcept {
    switch (value.index()) {
    case 0:
        return ParameterType::Integer;
    case 1:
        return ParameterType::Real;
    case 2:
        return ParameterType::Boolean;
    default:
        return ParameterType::String;
    }
}

const char* parameter_type_name(ParameterType type) noexcept {
    switch (type) {
    case ParameterType::Integer:
        return "integer";
    case ParameterType::Real:
        return "real";
    case ParameterType::Boolean:
        return "boolean";
    case ParameterType::String:
        return "string";
    }
    return "unknown";
}

std::string serialize_parameter_value(const ParameterValue& value) {
    return std::visit(
        [](const auto& item) -> std::string {
            using T = std::decay_t<decltype(item)>;
            if constexpr (std::is_same_v<T, std::int64_t>) {
                return std::to_string(item);
            } else if constexpr (std::is_same_v<T, double>) {
                if (!std::isfinite(item)) {
                    throw TypedHpoError<std::invalid_argument>(
                        "hpo_study_spec_invalid", {{"reason", "input"}},
                        "cannot serialize a non-finite real parameter");
                }
                std::ostringstream out;
                out.imbue(std::locale::classic());
                out << std::setprecision(std::numeric_limits<double>::max_digits10) << item;
                return out.str();
            } else if constexpr (std::is_same_v<T, bool>) {
                return item ? "true" : "false";
            } else {
                return item;
            }
        },
        value);
}

const ParameterValue* Candidate::find(const std::string& name) const noexcept {
    const auto it = values.find(name);
    return it == values.end() ? nullptr : &it->second;
}

}  // namespace pineforge::hpo
