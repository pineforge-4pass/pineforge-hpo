// UNEXECUTED: written on the Mac without a compiler or test run; first execution is on a spot box.
//
// Checks of the 64-bit Sobol engine (src/core/sobol_engine.hpp):
//   1. the vendored Joe-Kuo bytes, and the compiled and generated tables against them;
//   2. the published 32-bit vectors: for n < 2^32 the 64-bit word is the 32-bit word << 32;
//   3. hand-derived 64-bit anchors and structural identities up to index 2^64 - 1;
//   4. digital-shift, column and index bounds, and purity;
//   5. an independent big-integer oracle (tests/fixtures/sobol/engine/oracle64.py), given with
//      --oracle <file>. Without it the run fails unless --no-oracle is passed.
#include "../src/core/sha256.hpp"
#include "../src/core/sobol_engine.hpp"

#include <pineforge/hpo/error.hpp>

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <functional>
#include <iostream>
#include <iterator>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#ifndef PFH_SOBOL_TABLE_FILE
#error "PFH_SOBOL_TABLE_FILE must name third_party/sobol_joe_kuo/new-joe-kuo-6.21201.first1024"
#endif
#ifndef PFH_SOBOL_LICENCE_FILE
#error "PFH_SOBOL_LICENCE_FILE must name third_party/sobol_joe_kuo/LICENSE"
#endif
#ifndef PFH_SOBOL_INC_FILE
#error "PFH_SOBOL_INC_FILE must name src/core/sobol_table_joe_kuo_d6_1024.inc"
#endif
#ifndef PFH_SOBOL_FIXTURE_DIR
#error "PFH_SOBOL_FIXTURE_DIR must name tests/fixtures/sobol/engine"
#endif

namespace detail = pineforge::hpo::detail;

namespace {

using u64 = std::uint64_t;
constexpr u64 kMax = std::numeric_limits<u64>::max();
const detail::SobolScramble kNone = detail::SobolScramble::None;
const detail::SobolScramble kShift = detail::SobolScramble::DigitalShift;

const std::string kSubsetHash = "52eeb57738dd69f5e1f593f2085c4efb3fb410c3af241d5396d7a2bef60b9257";
const std::string kUpstreamHash =
    "68eedd2a4e3b659b9695e7aff0f8ac68718bcf620730fc3d3a8c65df2a067441";
const std::string kLicenceHash = "9d10226b50eeb34be0ab06bfa3392c7bd1f04bf602f9af4343295d1fd003d0e3";

void require(bool condition, const char* message) {
    if (!condition)
        throw std::runtime_error(message);
}

void require(bool condition, const std::string& message) {
    if (!condition)
        throw std::runtime_error(message);
}

template <typename Function>
void require_invariant(Function&& function, const std::string& label) {
    try {
        function();
    } catch (const pineforge::hpo::HpoError& error) {
        require(error.code() == "hpo_invariant", label + ": wrong failure code " + error.code());
        return;
    }
    throw std::runtime_error(label + ": expected an hpo_invariant failure, none was raised");
}

std::string read_file(const std::string& path) {
    std::ifstream input(path, std::ios::binary);
    require(input.good(), "cannot open " + path);
    return std::string(std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>());
}

std::vector<std::string> split(const std::string& text, char separator) {
    std::vector<std::string> parts;
    std::string::size_type start = 0;
    for (;;) {
        const auto end = text.find(separator, start);
        if (end == std::string::npos) {
            parts.push_back(text.substr(start));
            return parts;
        }
        parts.push_back(text.substr(start, end - start));
        start = end + 1;
    }
}

std::vector<std::string> words_of(const std::string& text) {
    std::istringstream input(text);
    std::vector<std::string> words;
    for (std::string word; input >> word;)
        words.push_back(word);
    return words;
}

u64 parse_number(const std::string& word, int base) {
    require(!word.empty(), "empty number");
    std::size_t used = 0;
    const auto value = std::stoull(word, &used, base);
    require(used == word.size(), "trailing characters in number " + word);
    return static_cast<u64>(value);
}

u64 parse_dec(const std::string& word) { return parse_number(word, 10); }
u64 parse_hex(const std::string& word) { return parse_number(word, 16); }

std::string fixture(const char* name) { return std::string(PFH_SOBOL_FIXTURE_DIR) + "/" + name; }

// Non-comment, non-empty lines of a fixture split on tabs.
std::vector<std::vector<std::string>> tsv_rows(const std::string& path) {
    std::vector<std::vector<std::string>> rows;
    for (const auto& line : split(read_file(path), '\n')) {
        if (line.empty() || line[0] == '#')
            continue;
        rows.push_back(split(line, '\t'));
    }
    return rows;
}

unsigned trailing_zeros(u64 value) {
    unsigned count = 0;
    while ((value & 1) == 0) {
        value >>= 1;
        ++count;
    }
    return count;
}

// Deterministic test inputs only; not part of the engine.
u64 xorshift(u64& state) {
    state ^= state << 13;
    state ^= state >> 7;
    state ^= state << 17;
    return state;
}

std::vector<u64> boundary_indices() {
    std::vector<u64> indices;
    for (u64 value = 0; value <= 70; ++value)
        indices.push_back(value);
    for (unsigned bit = 1; bit < 64; ++bit) {
        const u64 power = u64{1} << bit;
        indices.push_back(power - 1);
        indices.push_back(power);
        indices.push_back(power + 1);
    }
    indices.push_back(kMax - 2);
    indices.push_back(kMax - 1);
    indices.push_back(kMax);
    u64 state = 0x9e3779b97f4a7c15ULL;
    for (unsigned count = 0; count < 200; ++count)
        indices.push_back(xorshift(state));
    return indices;
}

const std::vector<std::size_t>& sample_columns() {
    static const std::vector<std::size_t> columns = {0,   1,   2,   3,   4,   5,   6,   7,
                                                     8,   15,  16,  31,  32,  63,  64,  127,
                                                     128, 255, 256, 511, 512, 1000, 1022, 1023};
    return columns;
}

// ---------------------------------------------------------------- table integrity

void test_constants() {
    static_assert(detail::kSobolMaxColumns == 1024, "1024 varying columns");
    static_assert(detail::kSobolTableRows == 1023, "rows for dimensions 2..1024");
    static_assert(detail::kSobolWordBits == 64, "64-bit words");
    static_assert(detail::kSobolMaxDegree == 13, "degree of the first 1024 dimensions");
    require(std::string(detail::kSobolTableName) == "new-joe-kuo-6.21201", "table name");
    require(std::string(detail::kSobolTableSubsetSha256) == kSubsetHash, "subset hash constant");
    require(std::string(detail::kSobolTableUpstreamSha256) == kUpstreamHash,
            "upstream hash constant");
    require(std::string(detail::kSobolLicenceSha256) == kLicenceHash, "licence hash constant");
}

void test_vendored_bytes() {
    const auto table = read_file(PFH_SOBOL_TABLE_FILE);
    require(table.size() == 63469, "vendored table is not the 63,469-byte upstream prefix");
    require(detail::sha256(table) == kSubsetHash, "vendored table hash differs from the pin");
    require(std::count(table.begin(), table.end(), '\n') == 1024, "vendored table line count");
    require(table.find('\r') == std::string::npos, "vendored table gained a carriage return");
    require(!table.empty() && table.back() == '\n', "vendored table lost its final newline");
    const auto licence = read_file(PFH_SOBOL_LICENCE_FILE);
    require(licence.size() == 1821, "vendored licence is not the 1,821-byte upstream file");
    require(detail::sha256(licence) == kLicenceHash, "vendored licence hash differs from the pin");
}

void test_compiled_table_matches_vendored_text() {
    const auto lines = split(read_file(PFH_SOBOL_TABLE_FILE), '\n');
    require(lines.size() == 1025 && lines.back().empty(), "vendored table line split");
    require(words_of(lines[0]) == std::vector<std::string>({"d", "s", "a", "m_i"}),
            "vendored table header row");
    for (std::size_t line = 1; line < 1024; ++line) {
        const std::string where = " (dimension " + std::to_string(line + 1) + ")";
        const auto fields = words_of(lines[line]);
        require(fields.size() >= 4, "short table row" + where);
        const u64 dimension = line + 1;
        require(parse_dec(fields[0]) == dimension, "table rows are not consecutive" + where);
        const u64 degree = parse_dec(fields[1]);
        const u64 polynomial = parse_dec(fields[2]);
        require(degree >= 1 && degree <= detail::kSobolMaxDegree, "table degree range" + where);
        require(fields.size() == 3 + degree, "table row width" + where);
        require(polynomial < (u64{1} << (degree - 1)), "table polynomial range" + where);
        const auto& row = detail::sobol_table_row(dimension);
        require(static_cast<u64>(row.degree) == degree, "compiled degree" + where);
        require(static_cast<u64>(row.polynomial) == polynomial, "compiled polynomial" + where);
        for (std::size_t k = 0; k < detail::kSobolMaxDegree; ++k) {
            if (k < degree) {
                const u64 m = parse_dec(fields[3 + k]);
                require(m % 2 == 1 && m < (u64{1} << (k + 1)), "initial m_k odd and < 2^k" + where);
                require(static_cast<u64>(row.initial[k]) == m, "compiled initial value" + where);
            } else {
                require(row.initial[k] == 0, "compiled padding is not zero" + where);
            }
        }
    }
}

void test_generated_include_matches_vendored_text() {
    const auto include = read_file(PFH_SOBOL_INC_FILE);
    for (const auto* hash : {&kSubsetHash, &kUpstreamHash, &kLicenceHash})
        require(include.find(*hash) != std::string::npos, "generated table omits hash " + *hash);

    // The BSD notice is reproduced verbatim as `// ` comment lines.
    auto licence_lines = split(read_file(PFH_SOBOL_LICENCE_FILE), '\n');
    require(!licence_lines.empty() && licence_lines.back().empty(), "licence split");
    licence_lines.pop_back();
    std::string notice;
    for (const auto& line : licence_lines)
        notice += (line.empty() ? std::string("//") : "// " + line) + "\n";
    require(include.find(notice) != std::string::npos,
            "generated table does not carry the licence notice verbatim");

    std::vector<std::string> rows;
    for (const auto& line : split(include, '\n'))
        if (!line.empty() && line[0] == '{')
            rows.push_back(line);
    require(rows.size() == detail::kSobolTableRows, "generated table row count");
    require(!include.empty() && include.back() == '\n', "generated table final newline");

    const auto lines = split(read_file(PFH_SOBOL_TABLE_FILE), '\n');
    for (std::size_t line = 1; line < 1024; ++line) {
        const auto fields = words_of(lines[line]);
        const u64 degree = parse_dec(fields[1]);
        std::string expected = "{" + fields[1] + ", " + fields[2] + ", {";
        for (std::size_t k = 0; k < detail::kSobolMaxDegree; ++k)
            expected += (k == 0 ? "" : ", ") + (k < degree ? fields[3 + k] : std::string("0"));
        expected += "}},";
        require(rows[line - 1] == expected,
                "generated row differs from the vendored text (dimension " +
                    std::to_string(line + 1) + ")");
    }
}

void test_table_accessor_bounds() {
    for (const std::size_t bad : {std::size_t{0}, std::size_t{1}, std::size_t{1025},
                                  std::numeric_limits<std::size_t>::max()})
        require_invariant([&] { detail::sobol_table_row(bad); },
                          "table row " + std::to_string(bad));
    require(detail::sobol_table_row(2).degree == 1, "dimension 2 degree");
    require(detail::sobol_table_row(2).initial[0] == 1, "dimension 2 m_1");
    require(detail::sobol_table_row(1024).degree == 13, "dimension 1024 degree");
}

// ---------------------------------------------------------------- published 32-bit prefix

void check_published_rows(const char* name, std::size_t columns, std::size_t minimum_rows) {
    const auto rows = tsv_rows(fixture(name));
    require(rows.size() >= minimum_rows, std::string("too few rows in ") + name);
    bool saw_last_32_bit_index = false;
    for (const auto& row : rows) {
        require(row.size() == 2, std::string("row shape in ") + name);
        const u64 index = parse_dec(row[0]);
        const auto words = words_of(row[1]);
        require(words.size() == columns, std::string("column count in ") + name);
        saw_last_32_bit_index = saw_last_32_bit_index || index == 0xffffffffULL;
        for (std::size_t column = 0; column < columns; ++column) {
            const u64 expected = parse_hex(words[column]) << 32;
            if (detail::sobol_coordinate64(index, column, 0, kNone) != expected ||
                detail::sobol_coordinate64(index, column, kMax, kNone) != expected)
                throw std::runtime_error(std::string("published prefix differs in ") + name +
                                         " at n=" + std::to_string(index) +
                                         " column=" + std::to_string(column));
        }
    }
    if (minimum_rows > 32)
        require(saw_last_32_bit_index, std::string(name) + " lacks n = 2^32 - 1");
}

void test_published_32_bit_prefix() {
    check_published_rows("vectors-gray-n0-31-d1-8.tsv", 8, 32);
    check_published_rows("vectors-powers-of-two-d1-6.tsv", 6, 90);
}

// The page https://web.maths.unsw.edu.au/~fkuo/sobol/ prints `./sobol 10 3 new-joe-kuo-6.21201`.
void test_joe_kuo_page_vector() {
    const u64 page[10][3] = {
        {0x0000000000000000ULL, 0x0000000000000000ULL, 0x0000000000000000ULL},  // 0 0 0
        {0x8000000000000000ULL, 0x8000000000000000ULL, 0x8000000000000000ULL},  // .5 .5 .5
        {0xc000000000000000ULL, 0x4000000000000000ULL, 0x4000000000000000ULL},  // .75 .25 .25
        {0x4000000000000000ULL, 0xc000000000000000ULL, 0xc000000000000000ULL},  // .25 .75 .75
        {0x6000000000000000ULL, 0x6000000000000000ULL, 0xa000000000000000ULL},  // .375 .375 .625
        {0xe000000000000000ULL, 0xe000000000000000ULL, 0x2000000000000000ULL},  // .875 .875 .125
        {0xa000000000000000ULL, 0x2000000000000000ULL, 0xe000000000000000ULL},  // .625 .125 .875
        {0x2000000000000000ULL, 0xa000000000000000ULL, 0x6000000000000000ULL},  // .125 .625 .375
        {0x3000000000000000ULL, 0x5000000000000000ULL, 0xf000000000000000ULL},  // .1875 .3125 .9375
        {0xb000000000000000ULL, 0xd000000000000000ULL, 0x7000000000000000ULL},  // .6875 .8125 .4375
    };
    for (u64 index = 0; index < 10; ++index)
        for (std::size_t column = 0; column < 3; ++column)
            require(detail::sobol_coordinate64(index, column, 0, kNone) == page[index][column],
                    "Joe-Kuo page vector differs at n=" + std::to_string(index) +
                        " column=" + std::to_string(column));
}

void test_direction_numbers_prefix_relation() {
    const auto rows = tsv_rows(fixture("direction-numbers.tsv"));
    require(rows.size() == 10, "direction-number fixture has dimensions 1..8, 64 and 1024");
    for (const auto& row : rows) {
        require(row.size() == 2, "direction-number row shape");
        const u64 dimension = parse_dec(row[0]);
        const auto words = words_of(row[1]);
        require(words.size() == 32, "direction-number row width");
        const auto directions = detail::sobol_directions64(dimension - 1);
        const std::string where = " (dimension " + std::to_string(dimension) + ")";
        for (std::size_t k = 0; k < 32; ++k)
            require(directions[k] == (parse_hex(words[k]) << 32),
                    "V_k is not the 32-bit word shifted left by 32, k=" + std::to_string(k + 1) +
                        where);
        for (std::size_t k = 0; k < 64; ++k) {
            const u64 lowest = directions[k] & (~directions[k] + 1);
            require(lowest == (u64{1} << (63 - k)),
                    "m_k must be odd and below 2^k, k=" + std::to_string(k + 1) + where);
        }
    }
}

// ---------------------------------------------------------------- 64-bit anchors and identities

void test_hand_derived_anchors() {
    struct Anchor {
        u64 index;
        std::size_t column;
        u64 expected;
    };
    const u64 two32 = u64{1} << 32;
    const u64 two53 = u64{1} << 53;
    const u64 two63 = u64{1} << 63;
    // Column 0 is dimension 1, every m_k = 1, so X(n) is the bit reversal of n ^ (n >> 1).
    // Column 1 is dimension 2 (s = 1, a = 0, m_1 = 1), so V_k = V_{k-1} ^ (V_{k-1} >> 1).
    const std::vector<Anchor> anchors = {
        {0, 0, 0x0000000000000000ULL},
        {1, 0, 0x8000000000000000ULL},
        {2, 0, 0xc000000000000000ULL},
        {3, 0, 0x4000000000000000ULL},
        {two32 - 1, 0, 0x0000000100000000ULL},
        {two32, 0, 0x0000000180000000ULL},
        {two53 - 1, 0, 0x0000000000000800ULL},
        {two53, 0, 0x0000000000000c00ULL},
        {two63, 0, 0x0000000000000003ULL},
        {kMax - 1, 0, 0x8000000000000001ULL},
        {kMax, 0, 0x0000000000000001ULL},
        {1, 1, 0x8000000000000000ULL},
        {2, 1, 0x4000000000000000ULL},
        {two32 - 1, 1, 0xffffffff00000000ULL},
        {two32, 1, 0x7fffffff80000000ULL},
        {two63, 1, 0x5555555555555555ULL},
        {kMax - 1, 1, 0x7fffffffffffffffULL},
        {kMax, 1, 0xffffffffffffffffULL},
    };
    for (const auto& anchor : anchors)
        require(detail::sobol_coordinate64(anchor.index, anchor.column, 0, kNone) ==
                    anchor.expected,
                "anchor differs at n=" + std::to_string(anchor.index) +
                    " column=" + std::to_string(anchor.column));
    const auto first = detail::sobol_directions64(0);
    const auto second = detail::sobol_directions64(1);
    for (std::size_t k = 0; k < 64; ++k) {
        require(first[k] == (u64{1} << (63 - k)), "dimension 1 V_k = 2^(64-k)");
        if (k > 0)
            require(second[k] == (second[k - 1] ^ (second[k - 1] >> 1)), "dimension 2 recurrence");
    }
    require(second[63] == kMax, "dimension 2 V_64 is all ones");
}

// Consecutive Gray points differ by exactly one direction number, V_{ctz(n+1)+1}.
void test_gray_successor_identity() {
    const auto indices = boundary_indices();
    for (const auto column : sample_columns()) {
        const auto directions = detail::sobol_directions64(column);
        for (const auto index : indices) {
            if (index == kMax)
                continue;
            const u64 difference = detail::sobol_coordinate64(index, column, 0, kNone) ^
                                   detail::sobol_coordinate64(index + 1, column, 0, kNone);
            if (difference != directions[trailing_zeros(index + 1)])
                throw std::runtime_error("Gray successor identity fails at n=" +
                                         std::to_string(index) +
                                         " column=" + std::to_string(column));
        }
    }
}

// Every aligned block of 2^m consecutive indices is a perfect one-dimensional stratification of
// each column, with or without a digital shift, including the very last block of the sequence.
void test_aligned_block_stratification() {
    constexpr unsigned kBits = 10;
    constexpr u64 kBlock = u64{1} << kBits;
    const std::vector<u64> starts = {0,
                                     kBlock,
                                     5 * kBlock,
                                     u64{1} << 40,
                                     (u64{1} << 52) + 3 * kBlock,
                                     u64{1} << 63,
                                     kMax - kBlock + 1};
    const std::vector<std::pair<detail::SobolScramble, u64>> modes = {
        {kNone, 0}, {kShift, 42}, {kShift, kMax}};
    std::vector<unsigned char> seen(kBlock);
    for (const auto start : starts)
        for (const auto column : sample_columns())
            for (const auto& mode : modes) {
                std::fill(seen.begin(), seen.end(), 0);
                for (u64 offset = 0; offset < kBlock; ++offset) {
                    const u64 point =
                        detail::sobol_coordinate64(start + offset, column, mode.second, mode.first);
                    auto& cell = seen[point >> (64 - kBits)];
                    if (cell != 0)
                        throw std::runtime_error("stratum hit twice in block " +
                                                 std::to_string(start) +
                                                 " column=" + std::to_string(column));
                    cell = 1;
                }
            }
}

// ---------------------------------------------------------------- shift, bounds, purity

void test_digital_shift_against_published_halves() {
    const auto rows = tsv_rows(fixture("digital-shift-vectors.tsv"));
    std::size_t shift_rows = 0, point_rows = 0;
    for (const auto& row : rows) {
        const u64 seed = parse_dec(row[0]);
        if (row.size() == 2) {
            ++shift_rows;
            const auto words = words_of(row[1]);
            require(words.size() == 6, "shift row width");
            for (std::size_t column = 0; column < 6; ++column)
                require((detail::sobol_shift64(seed, column) >> 32) == parse_hex(words[column]),
                        "SplitMix64 shift high half differs, seed=" + row[0] +
                            " column=" + std::to_string(column));
        } else {
            require(row.size() == 3, "shifted point row shape");
            ++point_rows;
            const u64 index = parse_dec(row[1]);
            const auto words = words_of(row[2]);
            require(words.size() == 3, "shifted point row width");
            for (std::size_t column = 0; column < 3; ++column) {
                const u64 shifted = detail::sobol_coordinate64(index, column, seed, kShift);
                require((shifted >> 32) == parse_hex(words[column]),
                        "shifted point high half differs, seed=" + row[0] + " n=" + row[1] +
                            " column=" + std::to_string(column));
                require(shifted == (detail::sobol_coordinate64(index, column, 0, kNone) ^
                                    detail::sobol_shift64(seed, column)),
                        "digital shift is not a full 64-bit XOR");
            }
        }
    }
    require(shift_rows == 6 && point_rows == 16, "digital-shift fixture row counts");
}

void test_digital_shift_properties() {
    for (const u64 seed : {u64{0}, u64{1}, u64{42}, u64{1} << 63, kMax}) {
        std::vector<u64> shifts;
        for (std::size_t column = 0; column < detail::kSobolMaxColumns; ++column)
            shifts.push_back(detail::sobol_shift64(seed, column));
        std::sort(shifts.begin(), shifts.end());
        // SplitMix64 outputs from distinct states are distinct: no two columns share a shift.
        require(std::adjacent_find(shifts.begin(), shifts.end()) == shifts.end(),
                "two columns share a digital shift, seed=" + std::to_string(seed));
        // Wrapping state arithmetic: a seed differing by one step shifts the column index.
        require(detail::sobol_shift64(seed, 1) ==
                    detail::sobol_shift64(seed + 0x9e3779b97f4a7c15ULL, 0),
                "shift is not the output at state seed + (column + 1) * gamma");
    }
    require(detail::sobol_shift64(0, 0) != detail::sobol_shift64(1, 0), "seed has no effect");
    // The shift cancels in differences, so it never changes which points are distinct.
    for (const auto column : sample_columns())
        for (const u64 a : {u64{0}, u64{7}, u64{1} << 40, kMax})
            for (const u64 b : {u64{1}, u64{1} << 63, kMax - 1})
                require((detail::sobol_coordinate64(a, column, 99, kShift) ^
                         detail::sobol_coordinate64(b, column, 99, kShift)) ==
                            (detail::sobol_coordinate64(a, column, 0, kNone) ^
                             detail::sobol_coordinate64(b, column, 0, kNone)),
                        "shift does not cancel");
}

void test_column_and_index_bounds() {
    for (const std::size_t bad : {std::size_t{1024}, std::size_t{1025},
                                  std::numeric_limits<std::size_t>::max()}) {
        const std::string label = "column " + std::to_string(bad);
        require_invariant([&] { detail::sobol_coordinate64(0, bad, 0, kNone); }, label);
        require_invariant([&] { detail::sobol_coordinate64(kMax, bad, 7, kShift); }, label);
        require_invariant([&] { detail::sobol_directions64(bad); }, label + " directions");
        require_invariant([&] { detail::sobol_shift64(7, bad); }, label + " shift");
    }
    require_invariant(
        [] { detail::sobol_coordinate64(1, 0, 0, static_cast<detail::SobolScramble>(7)); },
        "invalid scramble value");
    for (std::size_t column = 0; column < detail::kSobolMaxColumns; ++column) {
        const auto directions = detail::sobol_directions64(column);
        require(detail::sobol_coordinate64(0, column, 0, kNone) == 0, "index 0 is the origin");
        require(detail::sobol_coordinate64(kMax, column, 0, kNone) == directions[63],
                "index 2^64 - 1 is V_64");
        require(detail::sobol_coordinate64(u64{1} << 63, column, 0, kNone) ==
                    (directions[63] ^ directions[62]),
                "index 2^63 is V_64 ^ V_63");
        // The largest index and column evaluate with and without a shift; nothing wraps or throws.
        detail::sobol_coordinate64(kMax, column, kMax, kShift);
    }
}

void test_pure_function_order_and_threads() {
    std::vector<std::pair<u64, std::size_t>> inputs;
    u64 state = 0x2545f4914f6cdd1dULL;
    for (unsigned count = 0; count < 600; ++count) {
        const u64 index = xorshift(state);
        inputs.emplace_back(count % 3 == 0 ? index >> (index % 64) : index,
                            static_cast<std::size_t>(xorshift(state) % detail::kSobolMaxColumns));
    }
    std::vector<u64> forward(inputs.size());
    for (std::size_t i = 0; i < inputs.size(); ++i)
        forward[i] = detail::sobol_coordinate64(inputs[i].first, inputs[i].second, 42, kShift);
    for (std::size_t i = inputs.size(); i-- > 0;)
        require(detail::sobol_coordinate64(inputs[i].first, inputs[i].second, 42, kShift) ==
                    forward[i],
                "a point depends on call order");
    std::vector<u64> threaded(inputs.size());
    std::vector<std::thread> workers;
    for (std::size_t worker = 0; worker < 4; ++worker)
        workers.emplace_back([&, worker] {
            for (std::size_t i = worker; i < inputs.size(); i += 4)
                threaded[i] =
                    detail::sobol_coordinate64(inputs[i].first, inputs[i].second, 42, kShift);
        });
    for (auto& worker : workers)
        worker.join();
    require(threaded == forward, "a point depends on the calling thread");
}

// ---------------------------------------------------------------- independent oracle

void test_against_independent_oracle(const std::string& path) {
    std::size_t declared[4] = {0, 0, 0, 0};
    std::size_t actual[4] = {0, 0, 0, 0};
    bool saw_table_hash = false;
    for (const auto& line : split(read_file(path), '\n')) {
        if (line.empty())
            continue;
        if (line[0] == '#') {
            if (line.rfind("# table_sha256=", 0) == 0) {
                require(line == "# table_sha256=" + kSubsetHash, "oracle used another table");
                saw_table_hash = true;
            }
            unsigned long long d = 0, p = 0, s = 0, q = 0;
            if (std::sscanf(line.c_str(), "# counts D=%llu P=%llu S=%llu Q=%llu", &d, &p, &s,
                            &q) == 4) {
                declared[0] = d;
                declared[1] = p;
                declared[2] = s;
                declared[3] = q;
            }
            continue;
        }
        const auto fields = split(line, '\t');
        const std::string& kind = fields[0];
        if (kind == "D") {  // D column k V_k
            require(fields.size() == 4, "oracle D row shape");
            const u64 k = parse_dec(fields[2]);
            require(k >= 1 && k <= 64, "oracle D row k");
            require(detail::sobol_directions64(parse_dec(fields[1]))[k - 1] == parse_hex(fields[3]),
                    "direction number differs from the oracle: " + line);
            ++actual[0];
        } else if (kind == "P") {  // P column index X
            require(fields.size() == 4, "oracle P row shape");
            require(detail::sobol_coordinate64(parse_dec(fields[2]), parse_dec(fields[1]), 0,
                                               kNone) == parse_hex(fields[3]),
                    "point differs from the oracle: " + line);
            ++actual[1];
        } else if (kind == "S") {  // S seed column shift
            require(fields.size() == 4, "oracle S row shape");
            require(detail::sobol_shift64(parse_dec(fields[1]), parse_dec(fields[2])) ==
                        parse_hex(fields[3]),
                    "shift differs from the oracle: " + line);
            ++actual[2];
        } else if (kind == "Q") {  // Q seed column index X'
            require(fields.size() == 5, "oracle Q row shape");
            require(detail::sobol_coordinate64(parse_dec(fields[3]), parse_dec(fields[2]),
                                               parse_dec(fields[1]), kShift) ==
                        parse_hex(fields[4]),
                    "shifted point differs from the oracle: " + line);
            ++actual[3];
        } else {
            throw std::runtime_error("unknown oracle row: " + line);
        }
    }
    require(saw_table_hash, "oracle file lacks its table hash line");
    for (std::size_t kind = 0; kind < 4; ++kind)
        require(declared[kind] == actual[kind], "oracle row count differs from its header");
    require(actual[0] >= 64 * 24 && actual[1] >= 2000 && actual[2] >= 1024 * 4 && actual[3] >= 2000,
            "oracle file is too small to count as a proof");
}

}  // namespace

int main(int argc, char** argv) {
    std::string oracle;
    bool skip_oracle = false;
    for (int i = 1; i < argc; ++i) {
        const std::string argument = argv[i];
        if (argument == "--oracle" && i + 1 < argc) {
            oracle = argv[++i];
        } else if (argument == "--no-oracle") {
            skip_oracle = true;
        } else {
            std::cerr << "usage: test_sobol_engine (--oracle <oracle64.tsv> | --no-oracle)\n";
            return 2;
        }
    }
    std::vector<std::pair<std::string, std::function<void()>>> tests = {
        {"constants", test_constants},
        {"vendored_bytes", test_vendored_bytes},
        {"compiled_table_matches_vendored_text", test_compiled_table_matches_vendored_text},
        {"generated_include_matches_vendored_text", test_generated_include_matches_vendored_text},
        {"table_accessor_bounds", test_table_accessor_bounds},
        {"published_32_bit_prefix", test_published_32_bit_prefix},
        {"joe_kuo_page_vector", test_joe_kuo_page_vector},
        {"direction_numbers_prefix_relation", test_direction_numbers_prefix_relation},
        {"hand_derived_anchors", test_hand_derived_anchors},
        {"gray_successor_identity", test_gray_successor_identity},
        {"aligned_block_stratification", test_aligned_block_stratification},
        {"digital_shift_against_published_halves", test_digital_shift_against_published_halves},
        {"digital_shift_properties", test_digital_shift_properties},
        {"column_and_index_bounds", test_column_and_index_bounds},
        {"pure_function_order_and_threads", test_pure_function_order_and_threads},
    };
    if (!oracle.empty())
        tests.emplace_back("independent_oracle",
                           [oracle] { test_against_independent_oracle(oracle); });
    int failures = 0;
    for (const auto& test : tests) {
        try {
            test.second();
            std::cout << "PASS " << test.first << '\n';
        } catch (const std::exception& error) {
            ++failures;
            std::cout << "FAIL " << test.first << ": " << error.what() << '\n';
        }
    }
    if (oracle.empty()) {
        if (skip_oracle) {
            std::cout << "NOT RUN independent_oracle (--no-oracle): 64-bit proof is incomplete\n";
        } else {
            ++failures;
            std::cout << "FAIL independent_oracle: pass --oracle <file> generated by oracle64.py "
                         "(or --no-oracle to run without it)\n";
        }
    }
    return failures == 0 ? 0 : 1;
}
