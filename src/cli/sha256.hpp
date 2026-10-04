#pragma once

#include <array>
#include <cstdint>
#include <iomanip>
#include <sstream>
#include <string>
#include <string_view>
#include <vector>

namespace pineforge::hpo::detail {

inline std::string sha256(std::string_view input) {
    constexpr std::array<std::uint32_t, 64> constants{
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
        0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
        0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
        0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
        0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
        0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
        0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
        0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
    std::array<std::uint32_t, 8> state{
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    std::vector<unsigned char> bytes(input.begin(), input.end());
    const auto bit_length = static_cast<std::uint64_t>(bytes.size()) * 8;
    bytes.push_back(0x80);
    while (bytes.size() % 64 != 56)
        bytes.push_back(0);
    for (int shift = 56; shift >= 0; shift -= 8)
        bytes.push_back(static_cast<unsigned char>(bit_length >> shift));
    const auto rotate = [](std::uint32_t value, unsigned shift) {
        return (value >> shift) | (value << (32 - shift));
    };
    for (std::size_t offset = 0; offset < bytes.size(); offset += 64) {
        std::array<std::uint32_t, 64> words{};
        for (std::size_t index = 0; index < 16; ++index) {
            for (std::size_t byte = 0; byte < 4; ++byte)
                words[index] = (words[index] << 8) | bytes[offset + index * 4 + byte];
        }
        for (std::size_t index = 16; index < words.size(); ++index) {
            const auto first = words[index - 15];
            const auto second = words[index - 2];
            words[index] = words[index - 16] +
                (rotate(first, 7) ^ rotate(first, 18) ^ (first >> 3)) + words[index - 7] +
                (rotate(second, 17) ^ rotate(second, 19) ^ (second >> 10));
        }
        auto working = state;
        for (std::size_t index = 0; index < words.size(); ++index) {
            const auto choose = (working[4] & working[5]) ^ (~working[4] & working[6]);
            const auto majority = (working[0] & working[1]) ^ (working[0] & working[2]) ^
                (working[1] & working[2]);
            const auto first = working[7] + choose + constants[index] + words[index] +
                (rotate(working[4], 6) ^ rotate(working[4], 11) ^ rotate(working[4], 25));
            const auto second = majority +
                (rotate(working[0], 2) ^ rotate(working[0], 13) ^ rotate(working[0], 22));
            for (std::size_t slot = 7; slot > 0; --slot)
                working[slot] = working[slot - 1];
            working[4] += first;
            working[0] = first + second;
        }
        for (std::size_t index = 0; index < state.size(); ++index)
            state[index] += working[index];
    }
    std::ostringstream output;
    output << std::hex << std::setfill('0');
    for (const auto word : state)
        output << std::setw(8) << word;
    return output.str();
}

}
