#pragma once

#include "sha256.hpp"

#include <algorithm>
#include <iomanip>
#include <locale>
#include <sstream>
#include <stdexcept>
#include <string_view>

namespace pineforge::hpo::detail {

inline std::string_view sampler_checkpoint_payload(std::string_view state) {
    const auto newline = state.find('\n');
    if (state.size() > 16 * 1024 * 1024 || newline < 7 || newline > 16 ||
        state.substr(0, 6) != "PFHTPE" || state[6] < '1' || state[6] > '9' ||
        !std::all_of(state.begin() + 7, state.begin() + newline,
                     [](char value) { return value >= '0' && value <= '9'; }) ||
        state.size() < newline + 66 || state[newline + 65] != '\n')
        throw std::invalid_argument("invalid sampler-state checksum/version");
    const auto payload = state.substr(newline + 66);
    if (sha256(payload) != state.substr(newline + 1, 64))
        throw std::invalid_argument("invalid sampler-state checksum/version");
    return payload;
}

inline bool current_sampler_checkpoint(std::string_view state) {
    sampler_checkpoint_payload(state);
    return state.substr(0, 8) == "PFHTPE2\n";
}

inline std::string sampler_checkpoint_numeric_identity(const std::string& state) {
    if (state.empty() || !current_sampler_checkpoint(state))
        return {};
    std::istringstream input(std::string(sampler_checkpoint_payload(state)));
    input.imbue(std::locale::classic());
    std::string signature, identity;
    if (!(input >> std::quoted(signature) >> std::quoted(identity)))
        throw std::invalid_argument("invalid TPE sampler-state build identity");
    return identity;
}

}
