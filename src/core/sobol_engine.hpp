#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace pineforge::hpo::detail {

// Pure, indexed 64-bit Sobol generator over the vendored Joe-Kuo direction numbers. A point is a
// function of (index, column, seed, scramble) alone: no running state, no feedback.

enum class SobolScramble : std::uint8_t { None = 0, DigitalShift = 1 };

// Rows d = 2..1024 of Joe and Kuo's new-joe-kuo-6.21201, vendored verbatim under
// third_party/sobol_joe_kuo; dimension 1 has every m_k = 1 and no row.
inline constexpr char kSobolTableName[] = "new-joe-kuo-6.21201";
inline constexpr char kSobolTableSubsetSha256[] =
    "52eeb57738dd69f5e1f593f2085c4efb3fb410c3af241d5396d7a2bef60b9257";
inline constexpr char kSobolTableUpstreamSha256[] =
    "68eedd2a4e3b659b9695e7aff0f8ac68718bcf620730fc3d3a8c65df2a067441";
inline constexpr char kSobolLicenceSha256[] =
    "9d10226b50eeb34be0ab06bfa3392c7bd1f04bf602f9af4343295d1fd003d0e3";
inline constexpr unsigned kSobolWordBits = 64;
inline constexpr std::size_t kSobolMaxColumns = 1024;  // columns 0..1023 are dimensions 1..1024
inline constexpr std::size_t kSobolTableRows = 1023;   // dimensions 2..1024
inline constexpr unsigned kSobolMaxDegree = 13;        // largest s among those rows

// One table line: degree s, polynomial a (a_1 is the most significant of its s - 1 bits) and the
// initial direction numbers m_1..m_s, zero padded.
struct SobolTableRow {
    std::uint8_t degree;
    std::uint16_t polynomial;
    std::uint16_t initial[kSobolMaxDegree];
};

// Row of Sobol dimension 2..1024. Throws hpo_invariant outside that range.
const SobolTableRow& sobol_table_row(std::size_t dimension);

// V_1..V_64 of a column (V_k at index k - 1); column c is Sobol dimension c + 1. V_k is
// m_k * 2^(64-k) with m_k odd and below 2^k. Throws hpo_invariant if column >= kSobolMaxColumns.
using SobolDirections64 = std::array<std::uint64_t, 64>;
SobolDirections64 sobol_directions64(std::size_t column);

// Digital shift of a column: output number column + 1 of SplitMix64 started from seed (64 bits).
std::uint64_t sobol_shift64(std::uint64_t seed, std::size_t column);

// Coordinate `index` of a column as a 64-bit fixed-point fraction (the value is X / 2^64). The
// sequence is in Gray order and includes index 0; every uint64 index is valid. For index < 2^32
// without a shift, the word is the published 32-bit word shifted left by 32.
std::uint64_t sobol_coordinate64(std::uint64_t index,
                                 std::size_t column,
                                 std::uint64_t seed,
                                 SobolScramble scramble);

}  // namespace pineforge::hpo::detail
