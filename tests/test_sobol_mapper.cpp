// Sobol coordinate mapper and indexed sampler: unit tests.
//
// STATUS: UNEXECUTED. Written test-first by the methods-sobol-mapper leaf (base d2f83326) under a
// rule that forbids compiling or running anything on the preparing machine. The first compile and
// run belong to the spot proof; until then nothing here is evidence of behaviour.
//
// Build notes for the integrator (this file is not registered in tests/CMakeLists.txt):
//   - include directories: <repo>/src/core and <repo>/include;
//   - link PineForgeHPO::core (which must contain sobol_mapper.cpp, sobol_sampler.cpp and the
//     engine's sobol_engine.cpp);
//   - compile options -ffp-contract=off -fno-fast-math, like test_portable_math.cpp;
//   - compile definition PFH_SOBOL_MAPPER_FIXTURES="<repo>/tests/fixtures/sobol/mapper".
//
// Coverage: the high word of the exact 128-bit product, the 53-bit unit, Boolean/categorical splits,
// signed and extreme integer lattices, stepped reals, linear/log real and log-integer mapping with
// the revised representability limits, per-dimension authority differential against
// SearchSpace::candidate_at (not a whole-space query), name-sorted columns, constants, the
// first_index/budget/cursor limits, pure repeated indexed points, duplicates, the 1024-column limit,
// the published unscrambled prefix through the sampler, and independent float reference fixtures.
#include "sobol_engine.hpp"
#include "portable_math.hpp"
#include "sobol_mapper.hpp"
#include "sobol_sampler.hpp"

#include <pineforge/hpo/error.hpp>
#include <pineforge/hpo/sampler.hpp>
#include <pineforge/hpo/search_space.hpp>

#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <initializer_list>
#include <iostream>
#include <limits>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#ifndef PFH_SOBOL_MAPPER_FIXTURES
#error "PFH_SOBOL_MAPPER_FIXTURES must name tests/fixtures/sobol/mapper"
#endif
#if !defined(__SIZEOF_INT128__)
#error "the reference arithmetic in this test needs __int128 (GCC or Clang)"
#endif

namespace {

using namespace pineforge::hpo;
using pineforge::hpo::detail::SobolDimensionMap;
using pineforge::hpo::detail::SobolMapKind;
using pineforge::hpo::detail::SobolScramble;

__extension__ typedef unsigned __int128 Wide;
__extension__ typedef __int128 SignedWide;

constexpr std::uint64_t kMax = std::numeric_limits<std::uint64_t>::max();
constexpr std::int64_t kI64Min = std::numeric_limits<std::int64_t>::min();
constexpr std::int64_t kI64Max = std::numeric_limits<std::int64_t>::max();

void require(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

template <typename Exception, typename Function>
void require_throws(Function&& function, const std::string& message) {
    try {
        function();
    } catch (const Exception&) {
        return;
    } catch (const std::exception& error) {
        throw std::runtime_error(message + "; unexpected exception: " + error.what());
    }
    throw std::runtime_error(message + "; no exception was thrown");
}

// A refusal before any trial work: stable code hpo_study_spec_invalid, reason "sampler" (N13).
template <typename Function>
void require_refused(Function&& function, const std::string& message) {
    try {
        function();
    } catch (const HpoError& error) {
        require(error.code() == "hpo_study_spec_invalid",
                message + ": wrong failure code " + error.code());
        const auto reason = error.args().find("reason");
        require(reason != error.args().end() &&
                    std::holds_alternative<std::string>(reason->second.value()) &&
                    std::get<std::string>(reason->second.value()) == "sampler",
                message + ": reason argument is not \"sampler\"");
        return;
    }
    throw std::runtime_error(message + ": no refusal was thrown");
}

std::string hex64(std::uint64_t value) {
    std::ostringstream out;
    out << "0x" << std::hex << value;
    return out.str();
}

std::uint64_t bits_of(double value) {
    std::uint64_t bits = 0;
    std::memcpy(&bits, &value, sizeof(bits));
    return bits;
}

double double_of(std::uint64_t bits) {
    double value = 0.0;
    std::memcpy(&value, &bits, sizeof(value));
    return value;
}

std::int64_t as_int(const ParameterValue& value) { return std::get<std::int64_t>(value); }
double as_real(const ParameterValue& value) { return std::get<double>(value); }
bool as_bool(const ParameterValue& value) { return std::get<bool>(value); }

SobolDimensionMap make_map(Dimension dimension) { return SobolDimensionMap(dimension); }

// std::uint64_t is `unsigned long` on LP64 Linux but `unsigned long long` on macOS, so a braced list
// mixing ULL literals with std::uint64_t values cannot deduce a range-for initializer_list. Loops
// over literal lists go through this typed helper instead.
std::vector<std::uint64_t> u64s(std::initializer_list<std::uint64_t> values) {
    return std::vector<std::uint64_t>(values);
}

// Reference arithmetic, written against __int128 so it shares nothing with the limb code under test.
std::uint64_t reference_high64(std::uint64_t x, std::uint64_t count) {
    return static_cast<std::uint64_t>((static_cast<Wide>(x) * count) >> 64);
}

// Smallest x whose lattice index is `index`: ceil(index * 2^64 / count), for index < count.
std::uint64_t smallest_coordinate(std::uint64_t index, std::uint64_t count) {
    return static_cast<std::uint64_t>(((static_cast<Wide>(index) << 64) + (count - 1)) / count);
}

// Largest x whose lattice index is `index`.
std::uint64_t largest_coordinate(std::uint64_t index, std::uint64_t count) {
    return index + 1 == count ? kMax : smallest_coordinate(index + 1, count) - 1;
}

// A coordinate whose 53-bit unit is round(u * 2^53) / 2^53.
std::uint64_t coordinate_for_unit(double unit) {
    return static_cast<std::uint64_t>(std::llround(unit * 9007199254740992.0)) << 11U;
}

struct TestRng {
    std::uint64_t state;
    std::uint64_t next() {
        state += 0x9E3779B97F4A7C15ULL;
        std::uint64_t z = state;
        z = (z ^ (z >> 30U)) * 0xBF58476D1CE4E5B9ULL;
        z = (z ^ (z >> 27U)) * 0x94D049BB133111EBULL;
        return z ^ (z >> 31U);
    }
};

std::vector<std::vector<std::string>> read_tsv(const std::string& path) {
    std::ifstream input(path);
    require(input.good(), "cannot open fixture " + path);
    std::vector<std::vector<std::string>> rows;
    std::string line;
    while (std::getline(input, line)) {
        if (line.empty() || line[0] == '#') {
            continue;
        }
        std::vector<std::string> fields;
        std::string field;
        std::istringstream splitter(line);
        while (std::getline(splitter, field, '\t')) {
            fields.push_back(field);
        }
        rows.push_back(std::move(fields));
    }
    return rows;
}

// ---------------------------------------------------------------------------------------------
// Integer-exact product and 53-bit unit
// ---------------------------------------------------------------------------------------------

void test_contract_constants() {
    require(detail::kSobolMapperContract == "pineforge_sobol_mapper_v1",
            "mapper contract string changed; identity binders must be told");
    require(detail::kSobolMapperRevision == 2,
            "mapper revision changed without a test update (2 = four-factor log-real overflow rule)");
    require(detail::kSobolMaxColumns == 1024, "column ceiling is part of the pinned contract");
    require(detail::kSobolLogIntegerMaxLow == (std::int64_t{1} << 52), "log-integer low bound");
    require(detail::kSobolLogIntegerMaxSpan == (std::uint64_t{1} << 53), "log-integer span bound");
}

void test_high64_hand_derived_table() {
    const auto rows = read_tsv(std::string(PFH_SOBOL_MAPPER_FIXTURES) + "/high64-boundaries.tsv");
    require(rows.size() >= 30, "high64-boundaries.tsv lost rows");
    for (const auto& row : rows) {
        require(row.size() >= 3, "malformed high64 row");
        const std::uint64_t x = std::stoull(row[0], nullptr, 16);
        const std::uint64_t count = std::stoull(row[1]);
        const std::uint64_t expected = std::stoull(row[2]);
        const std::string where = " x=" + row[0] + " count=" + row[1];
        require(detail::sobol_mul_high64(x, count) == expected, "mul_high64 differs from fixture:" + where);
        require(reference_high64(x, count) == expected, "fixture differs from __int128:" + where);
    }
    // Anchors written out in the revised contract (Appendix B, "Mapping anchors").
    require(detail::sobol_mul_high64(0x5555555555555556ULL, 3) == 1, "count 3 anchor");
    require(detail::sobol_mul_high64(0x5555555555555555ULL, 3) == 0, "count 3 anchor, one below");
    require(detail::sobol_mul_high64(0x199999999999999AULL, 10) == 1, "count 10 anchor");
    require(detail::sobol_mul_high64(0x1999999999999999ULL, 10) == 0, "count 10 anchor, one below");
    require(detail::sobol_mul_high64(kMax, kMax) == kMax - 1, "count 2^64-1 with x = 2^64-1");
}

void test_high64_matches_wide_arithmetic() {
    TestRng rng{0x5eed};
    for (int round = 0; round < 40000; ++round) {
        const std::uint64_t x = rng.next();
        const std::uint64_t shift = rng.next() % 64U;
        std::uint64_t count = rng.next() >> shift;
        if (count == 0) {
            count = 1;
        }
        require(detail::sobol_mul_high64(x, count) == reference_high64(x, count),
                "mul_high64 differs from __int128 at x=" + hex64(x) + " count=" + hex64(count));
    }
    // Every cardinality boundary: x just below and at the smallest coordinate of each index.
    for (const std::uint64_t count : u64s({2, 3, 7, 10, 255, 4294967295ULL, 4294967296ULL, 4294967297ULL,
                                           9223372036854775809ULL, kMax})) {
        for (const std::uint64_t index : u64s({0, 1, 2, count / 2, count - 2, count - 1})) {
            if (index >= count) {
                continue;
            }
            const std::uint64_t first = smallest_coordinate(index, count);
            require(detail::sobol_mul_high64(first, count) == index,
                    "smallest coordinate must map to its index; count=" + hex64(count));
            if (index > 0) {
                require(detail::sobol_mul_high64(first - 1, count) == index - 1,
                        "coordinate below the boundary must map to the previous index; count=" +
                            hex64(count));
            }
            require(detail::sobol_mul_high64(largest_coordinate(index, count), count) == index,
                    "largest coordinate must map to its index; count=" + hex64(count));
        }
    }
}

void test_unit53() {
    require(detail::sobol_unit53(0) == 0.0, "unit of 0");
    require(detail::sobol_unit53(0x7FFULL) == 0.0, "the low 11 bits must be dropped");
    require(detail::sobol_unit53(0x800ULL) == 0x1p-53, "smallest positive unit");
    require(detail::sobol_unit53(0x8000000000000000ULL) == 0.5, "unit of 2^63");
    require(detail::sobol_unit53(kMax) == 1.0 - 0x1p-53, "largest unit must stay below 1");
    // Appendix B table: X for n = 2^32-1, 2^32, 2^53-1, 2^53, 2^63 and 2^64-2 (unscrambled, column 0).
    require(detail::sobol_unit53(0x0000000100000000ULL) == 0x1p-32, "u(2^32-1)");
    require(detail::sobol_unit53(0x0000000180000000ULL) == 3.0 * 0x1p-33, "u(2^32)");
    require(detail::sobol_unit53(0x0000000000000800ULL) == 0x1p-53, "u(2^53-1)");
    require(detail::sobol_unit53(0x0000000000000C00ULL) == 0x1p-53, "u(2^53)");
    require(detail::sobol_unit53(0x0000000000000003ULL) == 0.0, "u(2^63)");
    require(detail::sobol_unit53(0x8000000000000001ULL) == 0.5, "u(2^64-2)");
    double previous = -1.0;
    TestRng rng{7};
    std::vector<std::uint64_t> grid;
    for (int i = 0; i < 2000; ++i) {
        grid.push_back(rng.next());
    }
    std::sort(grid.begin(), grid.end());
    for (const std::uint64_t x : grid) {
        const double unit = detail::sobol_unit53(x);
        require(unit >= previous && unit >= 0.0 && unit < 1.0, "unit must be monotone and in [0, 1)");
        previous = unit;
    }
}

// ---------------------------------------------------------------------------------------------
// Discrete kinds
// ---------------------------------------------------------------------------------------------

void test_boolean_split() {
    const SobolDimensionMap flag = make_map(BooleanDimension("flag"));
    require(flag.kind() == SobolMapKind::Boolean && flag.varying() && flag.count() == 2,
            "Boolean is a two-point varying lattice");
    require(!as_bool(flag.value_at(0)), "x = 0");
    require(!as_bool(flag.value_at(0x7FFFFFFFFFFFFFFFULL)), "x = 2^63-1");
    require(as_bool(flag.value_at(0x8000000000000000ULL)), "x = 2^63");
    require(as_bool(flag.value_at(kMax)), "x = 2^64-1");
}

void test_categorical_split_and_constant() {
    const SobolDimensionMap three =
        make_map(CategoricalDimension("c", {std::string("a"), std::string("b"), std::string("c")}));
    require(three.kind() == SobolMapKind::Categorical && three.count() == 3, "three choices");
    require(std::get<std::string>(three.value_at(0x5555555555555555ULL)) == "a", "below first split");
    require(std::get<std::string>(three.value_at(0x5555555555555556ULL)) == "b", "first split");
    require(std::get<std::string>(three.value_at(0xAAAAAAAAAAAAAAAAULL)) == "b", "below second split");
    require(std::get<std::string>(three.value_at(0xAAAAAAAAAAAAAAABULL)) == "c", "second split");
    require(std::get<std::string>(three.value_at(kMax)) == "c", "top");

    const std::vector<ParameterValue> mixed = {std::int64_t{14}, 2.5, true, std::string("z")};
    const SobolDimensionMap four = make_map(CategoricalDimension("m", mixed));
    for (std::uint64_t index = 0; index < 4; ++index) {
        require(four.value_at(smallest_coordinate(index, 4)) == mixed[index], "mixed-type choice");
        require(four.value_at(largest_coordinate(index, 4)) == mixed[index], "mixed-type choice, top");
    }

    const SobolDimensionMap single = make_map(CategoricalDimension("only", {std::string("solo")}));
    require(single.kind() == SobolMapKind::Constant && !single.varying(),
            "a one-choice categorical takes no column");
    require(std::get<std::string>(single.value_at(kMax)) == "solo", "constant value");
}

struct IntegerCase {
    std::int64_t low;
    std::int64_t high;
    std::int64_t step;
};

std::int64_t lattice_value(std::int64_t low, std::int64_t step, std::uint64_t index) {
    return static_cast<std::int64_t>(static_cast<SignedWide>(low) +
                                     static_cast<SignedWide>(index) * static_cast<SignedWide>(step));
}

void test_integer_lattice_signed_and_extreme_ranges() {
    const std::vector<IntegerCase> cases = {
        {-5, 5, 1},
        {-3, 3, 2},
        {-1, 1, 1},
        {0, 5, 10},                       // step wider than the span: one value, still a column
        {-1000, 1000, 7},
        {kI64Min, kI64Min + 10, 1},       // negative extreme
        {kI64Max - 10, kI64Max, 1},       // positive extreme
        {kI64Min + 1, kI64Max, 1},        // 2^64-1 values: the largest representable count
        {kI64Min, kI64Max, 2},            // 2^63 values, last value INT64_MAX - 1
        {kI64Min, kI64Max, 3},            // last value INT64_MAX exactly
        {kI64Min, kI64Max, kI64Max},
        {-9, kI64Max, 1000000007},
    };
    for (const IntegerCase& item : cases) {
        const SignedWide span = static_cast<SignedWide>(item.high) - static_cast<SignedWide>(item.low);
        const SignedWide count_wide = span / item.step + 1;
        require(count_wide <= static_cast<SignedWide>(kMax), "test table holds only representable counts");
        const auto count = static_cast<std::uint64_t>(count_wide);
        const std::string where = " [" + std::to_string(item.low) + ", " + std::to_string(item.high) +
                                  "] step " + std::to_string(item.step);
        const SobolDimensionMap map = make_map(IntegerDimension("v", item.low, item.high, item.step));
        require(map.kind() == SobolMapKind::Integer && map.varying(), "integer kind" + where);
        require(map.count() == count, "lattice size" + where);
        for (const std::uint64_t index :
             u64s({0, 1, 2, count / 3, count / 2, count >= 2 ? count - 2 : 0, count - 1})) {
            if (index >= count) {
                continue;
            }
            const std::int64_t expected = lattice_value(item.low, item.step, index);
            require(as_int(map.value_at(smallest_coordinate(index, count))) == expected,
                    "first coordinate of index " + std::to_string(index) + where);
            require(as_int(map.value_at(largest_coordinate(index, count))) == expected,
                    "last coordinate of index " + std::to_string(index) + where);
        }
        require(as_int(map.value_at(0)) == item.low, "x = 0 is the lower bound" + where);
    }
    // Spot values named in the analysis, independent of the table above.
    require(as_int(make_map(IntegerDimension("v", kI64Min, kI64Max, 3)).value_at(kMax)) == kI64Max,
            "step 3 over the full range reaches INT64_MAX");
    require(as_int(make_map(IntegerDimension("v", kI64Min, kI64Max, 2)).value_at(kMax)) == kI64Max - 1,
            "step 2 over the full range ends at INT64_MAX - 1");
    require(as_int(make_map(IntegerDimension("v", kI64Max - 10, kI64Max)).value_at(kMax)) == kI64Max,
            "top of the positive extreme");

    const SobolDimensionMap fixed = make_map(IntegerDimension("v", 7, 7));
    require(fixed.kind() == SobolMapKind::Constant && !fixed.varying() && as_int(fixed.value_at(kMax)) == 7,
            "low == high is a constant without a column");
}

void test_unrepresentable_integer_count_is_refused() {
    require_refused([] { make_map(IntegerDimension("v", kI64Min, kI64Max, 1)); },
                    "2^64 integer values cannot be indexed");
    require_throws<std::overflow_error>([] { make_map(IntegerDimension("v", kI64Min, kI64Max, 1)); },
                                        "the refusal must be an overflow_error like the authority");
}

// ---------------------------------------------------------------------------------------------
// Per-dimension authority differential (not a whole-space query)
// ---------------------------------------------------------------------------------------------

void check_against_authority(const SearchSpace& space, const std::string& name) {
    const auto total = space.finite_cardinality();
    require(total.has_value(), name + ": authority space must be finite");
    std::vector<SobolDimensionMap> maps;
    for (const Dimension& dimension : space.dimensions()) {
        maps.emplace_back(dimension);
    }
    for (std::uint64_t ordinal = 0; ordinal < *total; ++ordinal) {
        const Candidate authority = space.candidate_at(ordinal);
        std::uint64_t remaining = ordinal;
        for (std::size_t position = maps.size(); position > 0; --position) {
            const SobolDimensionMap& map = maps[position - 1];
            const std::uint64_t count = map.varying() ? map.count() : 1;
            const std::uint64_t index = remaining % count;
            remaining /= count;
            const ParameterValue* expected = authority.find(map.name());
            require(expected != nullptr, name + ": authority lost a dimension");
            for (const std::uint64_t x : {smallest_coordinate(index, count), largest_coordinate(index, count)}) {
                require(map.value_at(x) == *expected,
                        name + ": " + map.name() + " differs from SearchSpace::candidate_at at ordinal " +
                            std::to_string(ordinal) + ", x=" + hex64(x));
            }
        }
    }
}

void test_authority_differential_finite_spaces() {
    check_against_authority(
        SearchSpace({IntegerDimension("a", -5, 5), IntegerDimension("b", -3, 3, 2), BooleanDimension("c"),
                     CategoricalDimension("d", {std::string("ema"), std::string("sma"), std::int64_t{14}})}),
        "ints-bool-cat");
    check_against_authority(
        SearchSpace({RealDimension("r", 0.0, 1.0, 0.25), IntegerDimension("i", 10, 40, 5),
                     BooleanDimension("b")}),
        "stepped-real");
    check_against_authority(SearchSpace({RealDimension("r", 0.0, 0.3, 0.1)}), "stepped-real-last-point-clamp");
    check_against_authority(SearchSpace({RealDimension("r", -1.0, 1.0, 0.1), RealDimension("s", 0.0, 0.7, 0.1)}),
                            "stepped-real-negative-and-decimal-steps");
    check_against_authority(
        SearchSpace({RealDimension("fixed", 2.5, 2.5), IntegerDimension("k", 7, 7),
                     CategoricalDimension("one", {std::string("only")}), IntegerDimension("v", 1, 3)}),
        "constants");
    check_against_authority(SearchSpace({IntegerDimension("m", kI64Min, kI64Min + 6),
                                         IntegerDimension("n", kI64Max - 6, kI64Max)}),
                            "signed-extremes");
    check_against_authority(SearchSpace({IntegerDimension("w", -1000, 1000, 7)}), "negative-low-step");
}

void test_whole_space_overflow_does_not_matter() {
    // The authority's product query refuses this space; the sampler must still index each
    // dimension by its own count.
    const SearchSpace huge({IntegerDimension("a", 0, 1099511627776), IntegerDimension("b", 0, 1099511627776),
                            IntegerDimension("c", 0, 1099511627776)});
    require_throws<std::overflow_error>([&] { (void)huge.finite_cardinality(); },
                                        "the authority must refuse the 2^123 product");
    SobolSampler sampler(huge, 5, SobolScramble::DigitalShift);
    for (std::uint64_t n = 0; n < 64; ++n) {
        const Candidate candidate = sampler.at(n);
        for (const char* name : {"a", "b", "c"}) {
            const std::int64_t value = as_int(*candidate.find(name));
            require(value >= 0 && value <= 1099511627776, "huge-count value left its range");
        }
    }
    // A continuous real makes finite_cardinality() empty; the sampler does not care.
    const SearchSpace continuous({RealDimension("x", 0.0, 1.0), BooleanDimension("b")});
    require(!continuous.finite_cardinality().has_value(), "continuous space has no cardinality");
    SobolSampler mixed(continuous, 1, SobolScramble::None);
    require(mixed.next().has_value(), "continuous plus discrete must be samplable");
}

// ---------------------------------------------------------------------------------------------
// Real mappings
// ---------------------------------------------------------------------------------------------

void test_stepped_real_matches_lattice() {
    const SobolDimensionMap map = make_map(RealDimension("r", 0.0, 1.0, 0.25));
    require(map.kind() == SobolMapKind::SteppedReal && map.count() == 5, "five grid points");
    const double expected[5] = {0.0, 0.25, 0.5, 0.75, 1.0};
    for (std::uint64_t index = 0; index < 5; ++index) {
        require(as_real(map.value_at(smallest_coordinate(index, 5))) == expected[index], "stepped grid value");
        require(as_real(map.value_at(largest_coordinate(index, 5))) == expected[index], "stepped grid value, top");
    }
    const SobolDimensionMap clamped = make_map(RealDimension("r", 0.0, 0.3, 0.1));
    require(as_real(clamped.value_at(kMax)) == 0.3, "last grid point is clamped to the upper bound");
}

void test_linear_real_exact_cases() {
    const SobolDimensionMap unit = make_map(RealDimension("r", 0.0, 1.0));
    require(unit.kind() == SobolMapKind::LinearReal && unit.count() == 0, "continuous kind has no count");
    for (const std::uint64_t x : u64s({0, 0x800, 0x1234567890ABCDEFULL, 0x8000000000000000ULL, kMax})) {
        require(as_real(unit.value_at(x)) == detail::sobol_unit53(x), "[0, 1] returns the unit itself");
    }
    const SobolDimensionMap symmetric = make_map(RealDimension("r", -1.0, 1.0));
    require(as_real(symmetric.value_at(0)) == -1.0, "[-1, 1] at x = 0");
    require(as_real(symmetric.value_at(0x8000000000000000ULL)) == 0.0, "[-1, 1] at x = 2^63");
    require(as_real(symmetric.value_at(kMax)) == 1.0 - 0x1p-52, "[-1, 1] at x = 2^64-1");

    const SobolDimensionMap wide = make_map(RealDimension("r", 0.0, 9007199254740992.0));
    for (const std::uint64_t x : u64s({0, 0x800, 0xFFFFFFFFFFFFF800ULL, kMax, 0x123456789ABCDEF0ULL})) {
        require(as_real(wide.value_at(x)) == static_cast<double>(x >> 11U), "[0, 2^53] is exact integer steps");
    }

    // high - low overflows: the finite fallback of sampler.cpp (low*(1-u) + high*u) applies.
    const SobolDimensionMap full = make_map(RealDimension("r", -DBL_MAX, DBL_MAX));
    require(as_real(full.value_at(0)) == -DBL_MAX, "full range at x = 0");
    require(as_real(full.value_at(0x8000000000000000ULL)) == 0.0, "full range at x = 2^63");
    const double top = as_real(full.value_at(kMax));
    require(std::isfinite(top) && top <= DBL_MAX && top > 0.0, "full range at the top is finite");

    const SobolDimensionMap tiny = make_map(RealDimension("r", 1.0, 1.0000000000000002));
    require(as_real(tiny.value_at(0)) == 1.0, "one-ulp range at x = 0");
    require(as_real(tiny.value_at(kMax)) == 1.0000000000000002, "one-ulp range at the top");

    const SobolDimensionMap fixed = make_map(RealDimension("r", 3.25, 3.25));
    require(fixed.kind() == SobolMapKind::Constant && !fixed.varying() && as_real(fixed.value_at(kMax)) == 3.25,
            "low == high real is a constant without a column");
}

void test_real_mappings_stay_in_range_and_monotone() {
    struct Range {
        double low;
        double high;
        bool log;
    };
    const std::vector<Range> ranges = {
        {0.0, 1.0, false},        {-1.0, 1.0, false},       {1e-5, 3.5, false},        {-250.5, -3.25, false},
        {-1e300, 1e300, false},   {-DBL_MAX, DBL_MAX, false}, {1.0, 100.0, true},       {1e-5, 1e5, true},
        {0.5, 2.5, true},         {1e-300, 1e300, true},    {DBL_MIN, DBL_MAX, true},  {1.0, 1.0000000000000002, true},
    };
    TestRng rng{99};
    std::vector<std::uint64_t> grid = {0, 1, 0x7FF, 0x800, 0x8000000000000000ULL, kMax - 1, kMax};
    for (int i = 0; i < 3000; ++i) {
        grid.push_back(rng.next());
    }
    std::sort(grid.begin(), grid.end());
    for (const Range& range : ranges) {
        const SobolDimensionMap map = make_map(RealDimension("r", range.low, range.high, std::nullopt, range.log));
        require(map.kind() == (range.log ? SobolMapKind::LogReal : SobolMapKind::LinearReal), "real kind");
        double previous = -DBL_MAX;
        for (const std::uint64_t x : grid) {
            const double value = as_real(map.value_at(x));
            require(std::isfinite(value) && value >= range.low && value <= range.high,
                    "value left [low, high] at x=" + hex64(x));
            require(value >= previous, "mapping must be monotone in the coordinate; x=" + hex64(x));
            previous = value;
        }
        require(as_real(map.value_at(0)) == range.low, "x = 0 must give exactly the lower bound");
    }
}

// ---- log-real, mapper revision 2 (AR amendment 2026-10-09 14:10) --------------------------------
//
// Test-local statement of the pinned rule, restated with plain statements and the same CORE-MATH
// wrappers the product uses. mapper_reference.py (Python floats plus a 900-digit decimal exp, run on
// spot) stays the arithmetic authority; these helpers pin the operation ORDER and the branch choice.
struct LogRealRule {
    double low;
    double high;
    double log_ratio;
};

LogRealRule log_real_rule(double low, double high) {
    const double relative_span = (high - low) / low;
    const double log_ratio = std::isfinite(relative_span)
                                 ? detail::math::log1p(relative_span)
                                 : detail::math::log(high) - detail::math::log(low);
    return {low, high, log_ratio};
}

// The revision 1 formula for the case where exp is finite: low * exp(z), clamped.
double rule_literal(const LogRealRule& rule, double unit) {
    return std::clamp(rule.low * detail::math::exp(rule.log_ratio * unit), rule.low, rule.high);
}

// q = exp(z * 0.25), then ((((low * q) * q) * q) * q), clamped. Left-to-right, no regrouping.
double rule_quartered(const LogRealRule& rule, double unit) {
    const double exponent = rule.log_ratio * unit;
    const double quarter = detail::math::exp(exponent * 0.25);
    volatile double first = rule.low * quarter;
    volatile double second = first * quarter;
    volatile double third = second * quarter;
    volatile double fourth = third * quarter;
    return std::clamp(static_cast<double>(fourth), rule.low, rule.high);
}

bool exp_is_finite(const LogRealRule& rule, double unit) {
    return std::isfinite(detail::math::exp(rule.log_ratio * unit));
}

struct LogRealPair {
    double low;
    double high;
};

// Pairs whose high / low is not finite, so exp(L * u) overflows for large u.
std::vector<LogRealPair> overflowing_pairs() {
    constexpr double kDenormMin = std::numeric_limits<double>::denorm_min();
    constexpr double kNormMin = std::numeric_limits<double>::min();
    constexpr double kMaxFinite = std::numeric_limits<double>::max();
    return {{kDenormMin, kMaxFinite}, {kNormMin, kMaxFinite}, {kDenormMin, 1e300},
            {kNormMin, 1e300},        {1e-300, 1e300},        {kDenormMin, 1.0}};
}

void test_log_real_literal_path_is_unchanged() {
    // Where exp(z) is finite the product must be the revision 1 value, bit for bit.
    TestRng rng{2026};
    std::vector<std::uint64_t> grid = {0, 0x800, 0x4000000000000000ULL, 0x8000000000000000ULL, kMax};
    for (int i = 0; i < 400; ++i) {
        grid.push_back(rng.next());
    }
    std::vector<LogRealPair> pairs = overflowing_pairs();
    pairs.push_back({1.0, 100.0});
    pairs.push_back({1e-5, 1e5});
    pairs.push_back({1e-300, 1e8});  // finite high / low: the log1p form, never overflows
    std::size_t literal_rows = 0;
    for (const LogRealPair& pair : pairs) {
        const SobolDimensionMap map = make_map(RealDimension("r", pair.low, pair.high, std::nullopt, true));
        const LogRealRule rule = log_real_rule(pair.low, pair.high);
        for (const std::uint64_t x : grid) {
            const double unit = detail::sobol_unit53(x);
            if (!exp_is_finite(rule, unit)) {
                continue;
            }
            require(bits_of(as_real(map.value_at(x))) == bits_of(rule_literal(rule, unit)),
                    "literal-finite path changed; low=" + hex64(bits_of(pair.low)) + " x=" + hex64(x));
            ++literal_rows;
        }
    }
    require(literal_rows > 500, "too few literal-path rows were exercised");
}

void test_log_real_quartered_witnesses() {
    // The amendment's witness: the smallest positive subnormal .. the largest finite double, unit 63/64.
    // Half of L * 63/64 (about 715.7) already exceeds the largest finite exp argument (709.78), so the
    // revision 1 half-exponent fallback would itself overflow and the clamp would fabricate `high`.
    const std::vector<std::uint64_t> coordinates = {
        0x8000000000000000ULL,  // u = 1/2
        0xC000000000000000ULL,  // u = 3/4
        0xE000000000000000ULL,  // u = 7/8
        0xFC00000000000000ULL,  // u = 63/64
        0xFFFFFFFF00000000ULL,  // high-index coordinate of column 1, n = 2^32-1
        0xFFFFFFFFFFFFF800ULL,  // maximal 53-bit unit
        0xFFFFFFFFFFFFFFFFULL};
    constexpr double kLn10 = 2.302585092994045684;
    std::size_t quartered_rows = 0;
    for (const LogRealPair& pair : overflowing_pairs()) {
        const SobolDimensionMap map = make_map(RealDimension("r", pair.low, pair.high, std::nullopt, true));
        const LogRealRule rule = log_real_rule(pair.low, pair.high);
        const long double ln_low = std::log(static_cast<long double>(pair.low));
        const long double ln_ratio = std::log(static_cast<long double>(pair.high)) - ln_low;
        for (const std::uint64_t x : coordinates) {
            const double unit = detail::sobol_unit53(x);
            const double value = as_real(map.value_at(x));
            const std::string where = " low=" + hex64(bits_of(pair.low)) + " high=" + hex64(bits_of(pair.high)) +
                                      " x=" + hex64(x);
            require(std::isfinite(value) && value >= pair.low && value <= pair.high, "value left its range;" + where);
            require(value > 0.0, "value must be positive;" + where);
            const bool overflow = !exp_is_finite(rule, unit);
            require(bits_of(value) == bits_of(overflow ? rule_quartered(rule, unit) : rule_literal(rule, unit)),
                    "value differs from the pinned operation order;" + where);
            quartered_rows += overflow ? 1U : 0U;
            if (unit <= 0.984375) {
                // No premature high-end saturation: the interior point stays far below `high`, and its
                // decimal order of magnitude is the log-uniform one (host long double only as a sanity
                // bound; the bit-exact authority is float-reference.tsv).
                require(value < pair.high, "interior point saturated to `high`;" + where);
                const long double expected_log10 =
                    (ln_low + ln_ratio * static_cast<long double>(unit)) / static_cast<long double>(kLn10);
                require(std::fabs(static_cast<long double>(std::log10(value)) - expected_log10) < 1e-9L,
                        "log-uniform position is wrong;" + where);
            }
        }
    }
    require(quartered_rows >= 25, "the quartered branch was not exercised enough");
}

void test_log_real_branch_boundary_both_sides() {
    // For each overflowing pair, bisect the 53-bit unit grid for the last k whose exp is finite and
    // check the grid points around it: literal at or below k, quartered above, and a monotone, in-range
    // result across the switch (one grid step changes the value by about L * 2^-53, far above the
    // few-ulp difference between the two constructions).
    constexpr std::uint64_t kGridTop = (std::uint64_t{1} << 53) - 1;
    for (const LogRealPair& pair : overflowing_pairs()) {
        const SobolDimensionMap map = make_map(RealDimension("r", pair.low, pair.high, std::nullopt, true));
        const LogRealRule rule = log_real_rule(pair.low, pair.high);
        const auto finite_at = [&](std::uint64_t k) {
            return exp_is_finite(rule, static_cast<double>(k) * 0x1p-53);
        };
        require(finite_at(0) && !finite_at(kGridTop), "pair must overflow somewhere on the unit grid");
        std::uint64_t good = 0;
        std::uint64_t bad = kGridTop;
        while (bad - good > 1) {
            const std::uint64_t middle = good + (bad - good) / 2;
            (finite_at(middle) ? good : bad) = middle;
        }
        double previous = 0.0;
        for (const std::uint64_t k : {good - 1, good, good + 1, good + 2}) {
            const std::uint64_t x = k << 11U;
            const double unit = detail::sobol_unit53(x);
            const double value = as_real(map.value_at(x));
            const std::string where = " low=" + hex64(bits_of(pair.low)) + " k=" + std::to_string(k);
            const bool literal = k <= good;
            require(bits_of(value) == bits_of(literal ? rule_literal(rule, unit) : rule_quartered(rule, unit)),
                    "wrong branch at the overflow boundary;" + where);
            require(std::isfinite(value) && value >= pair.low && value <= pair.high && value > 0.0,
                    "boundary value left its range;" + where);
            require(value >= previous, "mapping lost monotonicity across the branch switch;" + where);
            previous = value;
            // The low 11 bits of the coordinate are ignored, on both sides of the switch.
            require(bits_of(as_real(map.value_at(x | 0x7FFULL))) == bits_of(value),
                    "the low 11 bits must not matter;" + where);
        }
    }
}

void test_log_real_geometric_mean_and_finite_span() {
    // [1e-300, 1e300]: u = 0.5 gives z = 690.78 < 709.78, so the literal path returns about 1.
    const SobolDimensionMap map = make_map(RealDimension("r", 1e-300, 1e300, std::nullopt, true));
    require(std::fabs(as_real(map.value_at(0x8000000000000000ULL)) - 1.0) < 1e-9, "u = 0.5 is the geometric mean");
    // u = 0.875 overflows exp (z = 1208.9): four quarter factors must land near 1e225, not at 1e300.
    const double upper = as_real(map.value_at(0xE000000000000000ULL));
    require(std::fabs(std::log10(upper) - 225.0) < 1e-6, "overflow branch keeps the log-uniform position");
    require(as_real(map.value_at(kMax)) <= 1e300, "top stays within the upper bound");
    // A finite high / low takes the log1p form and can never overflow exp.
    const SobolDimensionMap finite_span = make_map(RealDimension("r", 1e-300, 1e8, std::nullopt, true));
    require(as_real(finite_span.value_at(kMax)) <= 1e8, "finite-span top stays within the upper bound");
}

void test_log_integer_cases_and_representability_limits() {
    const SobolDimensionMap four = make_map(IntegerDimension("p", 1, 4, 1, true));
    require(four.kind() == SobolMapKind::LogInteger && four.varying(), "log integer kind");
    // low = 1, high = 4: displacement = 0.5 * expm1(ln(9) * u); thresholds at u = ln(2k+1)/ln(9).
    // Probe points keep at least 0.03 of displacement away from every threshold.
    const std::pair<double, std::int64_t> probes[] = {
        {0.0, 1}, {0.25, 1}, {0.49, 1}, {0.51, 2}, {0.75, 3}, {0.85, 3}, {0.89, 4}, {0.99, 4}};
    for (const auto& probe : probes) {
        const std::uint64_t x = coordinate_for_unit(probe.first);
        require(as_int(four.value_at(x)) == probe.second,
                "log integer [1, 4] at u=" + std::to_string(probe.first));
    }

    const std::int64_t low_limit = std::int64_t{1} << 52;
    const std::int64_t span_limit = std::int64_t{1} << 53;
    const std::vector<IntegerCase> accepted = {
        {1, 1000000, 1}, {3, 1048576, 1}, {1, span_limit, 1},                // span = 2^53
        {low_limit, low_limit + span_limit - 1, 1},                          // low = 2^52, span = 2^53
        {low_limit - 1, low_limit + 100, 1},
    };
    TestRng rng{1234};
    std::vector<std::uint64_t> grid = {0, 0x800, 0x8000000000000000ULL, kMax - 1, kMax};
    for (int i = 0; i < 1500; ++i) {
        grid.push_back(rng.next());
    }
    std::sort(grid.begin(), grid.end());
    for (const IntegerCase& item : accepted) {
        const SobolDimensionMap map = make_map(IntegerDimension("p", item.low, item.high, 1, true));
        std::int64_t previous = item.low;
        for (const std::uint64_t x : grid) {
            const std::int64_t value = as_int(map.value_at(x));
            require(value >= item.low && value <= item.high && value >= previous,
                    "log integer value left its range or lost monotonicity; low=" + std::to_string(item.low));
            previous = value;
        }
        require(as_int(map.value_at(0)) == item.low, "x = 0 is the lower bound; low=" + std::to_string(item.low));
    }
    // N6: refused when low - 0.5 or high - low + 1 is not exact in binary64.
    require_refused([&] { make_map(IntegerDimension("p", low_limit + 1, low_limit + 11, 1, true)); },
                    "low above 2^52");
    require_refused([&] { make_map(IntegerDimension("p", 1, span_limit + 1, 1, true)); }, "span above 2^53");
    require_refused([&] { make_map(IntegerDimension("p", low_limit, low_limit + span_limit, 1, true)); },
                    "span of 2^53 + 1 at the largest low");
    require_refused([&] { make_map(IntegerDimension("p", 3, kI64Max, 1, true)); }, "huge log integer range");
    const SobolDimensionMap fixed = make_map(IntegerDimension("p", 9, 9, 1, true));
    require(fixed.kind() == SobolMapKind::Constant && as_int(fixed.value_at(0)) == 9,
            "a one-point log integer is a constant, whatever its magnitude");
}

void test_repeated_indexed_mapping_is_bitwise_stable() {
    const SobolDimensionMap real = make_map(RealDimension("r", 1e-3, 1e3, std::nullopt, true));
    const SobolDimensionMap copy = real;
    for (const std::uint64_t x : u64s({0x123456789ABCDEF0ULL, 0xFEDCBA9876543210ULL, 0x8000000000000800ULL})) {
        const std::uint64_t first = bits_of(as_real(real.value_at(x)));
        require(bits_of(as_real(real.value_at(x))) == first, "repeat call");
        require(bits_of(as_real(copy.value_at(x))) == first, "copied map");
    }
}

void test_float_reference_fixture() {
    const std::string path = std::string(PFH_SOBOL_MAPPER_FIXTURES) + "/float-reference.tsv";
    {
        std::ifstream probe(path);
        require(probe.good(),
                "float-reference.tsv is missing: run `python3 -I mapper_reference.py > float-reference.tsv` "
                "on spot and commit the reviewed output (see tests/fixtures/sobol/mapper/README.md)");
    }
    const auto rows = read_tsv(path);
    // The script emits 1180 rows (22 fixed + 24 drawn coordinates for 8 linear, 10 log-real and 7
    // log-integer ranges, plus 5 boundary coordinates for each of the 6 log-real ranges that overflow).
    require(rows.size() >= 1100, "float-reference.tsv has fewer than 1100 data rows; regenerate it");
    std::size_t linear = 0;
    std::size_t log_real = 0;
    std::size_t log_integer = 0;
    std::size_t literal_branch = 0;
    std::size_t quartered_branch = 0;
    for (const auto& row : rows) {
        require(row.size() == 6, "malformed float-reference row (expected 6 columns, revision 2)");
        const std::uint64_t x = std::stoull(row[3], nullptr, 16);
        if (row[0] == "linear" || row[0] == "logreal") {
            const bool log = row[0] == "logreal";
            require(log ? (row[5] == "literal" || row[5] == "quartered") : row[5] == "-",
                    "unexpected branch tag " + row[5] + " on a " + row[0] + " row");
            const double low = double_of(std::stoull(row[1], nullptr, 16));
            const double high = double_of(std::stoull(row[2], nullptr, 16));
            const std::uint64_t expected = std::stoull(row[4], nullptr, 16);
            const SobolDimensionMap map = make_map(RealDimension("r", low, high, std::nullopt, log));
            const std::uint64_t got = bits_of(as_real(map.value_at(x)));
            require(got == expected, row[0] + " mismatch: low=" + row[1] + " high=" + row[2] + " x=" + row[3] +
                                         " got=" + hex64(got) + " expected=" + row[4] + " branch=" + row[5]);
            ++(log ? log_real : linear);
            if (log) {
                ++(row[5] == "literal" ? literal_branch : quartered_branch);
            }
        } else if (row[0] == "logint") {
            const std::int64_t low = std::stoll(row[1]);
            const std::int64_t high = std::stoll(row[2]);
            const SobolDimensionMap map = make_map(IntegerDimension("i", low, high, 1, true));
            const std::int64_t got = as_int(map.value_at(x));
            require(got == std::stoll(row[4]), "logint mismatch: low=" + row[1] + " high=" + row[2] + " x=" + row[3] +
                                                   " got=" + std::to_string(got) + " expected=" + row[4]);
            ++log_integer;
        } else {
            throw std::runtime_error("unknown float-reference kind " + row[0]);
        }
    }
    require(linear > 0 && log_real > 0 && log_integer > 0, "float-reference.tsv must cover all three kinds");
    // Both branches of the revision 2 log-real rule must be witnessed, or a generator defect could hide
    // the very case the amendment exists for.
    require(literal_branch >= 60 && quartered_branch >= 60,
            "float-reference.tsv lacks witnesses for both log-real branches");
}

// ---------------------------------------------------------------------------------------------
// Sampler
// ---------------------------------------------------------------------------------------------

SearchSpace mixed_space() {
    return SearchSpace({
        RealDimension("zeta", 0.001, 100.0, std::nullopt, true),
        IntegerDimension("alpha", -50, 50),
        BooleanDimension("mid"),
        CategoricalDimension("kind", {std::string("ema"), std::string("sma"), std::string("wma")}),
        IntegerDimension("period", 2, 5000, 1, true),
        RealDimension("risk", -1.5, 2.5),
        RealDimension("grid", 0.0, 1.0, 0.125),
        RealDimension("fixed_real", 3.25, 3.25),
        IntegerDimension("fixed_int", 9, 9),
        CategoricalDimension("fixed_choice", {std::string("only")}),
    });
}

void test_columns_are_name_sorted_and_constants_take_none() {
    const SearchSpace space = mixed_space();
    const SobolSampler sampler(space, 3, SobolScramble::DigitalShift);
    const std::vector<std::string> expected = {"alpha", "grid", "kind", "mid", "period", "risk", "zeta"};
    require(sampler.columns().size() == expected.size(), "varying column count");
    for (std::size_t i = 0; i < expected.size(); ++i) {
        require(sampler.columns()[i].name == expected[i] && sampler.columns()[i].index == i,
                "column " + std::to_string(i) + " must be " + expected[i]);
    }
    require(sampler.columns()[0].kind == SobolMapKind::Integer && sampler.columns()[0].count == 101,
            "alpha metadata");
    require(sampler.columns()[1].kind == SobolMapKind::SteppedReal && sampler.columns()[1].count == 9,
            "grid metadata");
    require(sampler.columns()[4].kind == SobolMapKind::LogInteger && sampler.columns()[6].kind == SobolMapKind::LogReal,
            "log metadata");
    require(sampler.columns()[5].kind == SobolMapKind::LinearReal && sampler.columns()[5].count == 0,
            "continuous metadata");
    std::set<std::string> constant_names;
    for (const SobolConstant& constant : sampler.constants()) {
        constant_names.insert(constant.name);
    }
    require((constant_names == std::set<std::string>{"fixed_real", "fixed_int", "fixed_choice"}),
            "constants are exactly the low == high / one-choice dimensions");

    // Byte-wise UTF-8 order, as the space hash uses: digits < upper case < '_' < lower case < non-ASCII.
    std::vector<Dimension> dimensions;
    for (const std::string& name : {std::string("beta"), std::string("Alpha"), std::string("alpha"),
                                    std::string("\xC3\xBC") + "nder", std::string("10"), std::string("_x")}) {
        dimensions.push_back(BooleanDimension(name));
    }
    const SobolSampler ordered{SearchSpace(dimensions), 0, SobolScramble::None};
    const std::vector<std::string> byte_order = {"10", "Alpha", "_x", "alpha", "beta", std::string("\xC3\xBC") + "nder"};
    for (std::size_t i = 0; i < byte_order.size(); ++i) {
        require(ordered.columns()[i].name == byte_order[i], "byte-wise order at column " + std::to_string(i));
    }
    // N11: the numeric identity applies exactly when some column passes through binary64 math.
    require(sampler.uses_floating_point() && !ordered.uses_floating_point(),
            "mixed space uses floating point; Boolean-only does not");
    require(!SobolSampler(SearchSpace({IntegerDimension("k", 0, 9), BooleanDimension("b"),
                                       CategoricalDimension("c", {std::string("x"), std::string("y")}),
                                       RealDimension("fixed", 2.0, 2.0)}),
                          0, SobolScramble::None)
                 .uses_floating_point(),
            "integer, Boolean, categorical and constant-real columns are discrete");
    require(SobolSampler(SearchSpace({RealDimension("s", 0.0, 1.0, 0.5)}), 0, SobolScramble::None)
                .uses_floating_point(),
            "a stepped real still depends on the portable build recipe");
    require(SobolSampler(SearchSpace({IntegerDimension("p", 1, 100, 1, true)}), 0, SobolScramble::None)
                .uses_floating_point(),
            "a log integer is floating point");
    // Declaration order is irrelevant: reversing it changes nothing.
    std::reverse(dimensions.begin(), dimensions.end());
    const SobolSampler reversed{SearchSpace(dimensions), 0, SobolScramble::None};
    for (std::size_t i = 0; i < byte_order.size(); ++i) {
        require(reversed.columns()[i].name == byte_order[i], "declaration order must not matter");
        require(reversed.at(11).values == ordered.at(11).values, "declaration order must not change a point");
    }
}

void test_point_equals_mapper_applied_to_engine_coordinates() {
    const SearchSpace space = mixed_space();
    for (const SobolScramble scramble : {SobolScramble::None, SobolScramble::DigitalShift}) {
        for (const std::uint64_t seed : u64s({0, 42, kMax})) {
            const SobolSampler sampler(space, seed, scramble);
            for (const std::uint64_t index : u64s({0, 1, 2, 3, 1000, 4294967295ULL, 4294967296ULL,
                                                   9007199254740992ULL, 0x8000000000000000ULL, kMax - 1})) {
                const Candidate candidate = sampler.at(index);
                require(candidate.id == index, "ID is the raw index");
                require(candidate.values.size() == space.dimensions().size(), "every dimension is present");
                for (const SobolColumn& column : sampler.columns()) {
                    const Dimension* dimension = space.find(column.name);
                    require(dimension != nullptr, "column names a declared dimension");
                    const SobolDimensionMap map = make_map(*dimension);
                    const std::uint64_t coordinate = detail::sobol_coordinate64(index, column.index, seed, scramble);
                    require(map.value_at(coordinate) == *candidate.find(column.name),
                            "at() must equal the mapper applied to the engine coordinate: column " + column.name +
                                " index " + hex64(index));
                }
                for (const SobolConstant& constant : sampler.constants()) {
                    require(*candidate.find(constant.name) == constant.value, "constants are emitted verbatim");
                }
            }
        }
    }
}

std::uint64_t reference_shift(std::uint64_t seed, std::size_t column) {
    std::uint64_t z = seed + (static_cast<std::uint64_t>(column) + 1) * 0x9E3779B97F4A7C15ULL;
    z = (z ^ (z >> 30U)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27U)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31U);
}

// Checks only the engine interface the sampler relies on (N3, N4); the engine lane owns the rest.
void test_engine_dependency_contract() {
    for (const std::uint64_t seed : u64s({0, 1, kMax, 0x123456789ABCDEFULL})) {
        for (const std::size_t column : {std::size_t{0}, std::size_t{1}, std::size_t{2}, std::size_t{1023}}) {
            for (const std::uint64_t index : u64s({0, 1, 2, 3, 4294967295ULL, 4294967296ULL,
                                                   9007199254740992ULL, 0x8000000000000000ULL, kMax - 1, kMax})) {
                const std::uint64_t plain = detail::sobol_coordinate64(index, column, 0, SobolScramble::None);
                require(detail::sobol_coordinate64(index, column, seed, SobolScramble::None) == plain,
                        "scramble none must ignore the seed");
                require(detail::sobol_coordinate64(index, column, seed, SobolScramble::DigitalShift) ==
                            (plain ^ reference_shift(seed, column)),
                        "digital shift is X xor the 64-bit SplitMix output number column+1");
            }
        }
    }
}

void test_published_prefix_through_sampler() {
    // Joe-Kuo dimensions 1 and 2, Gray order, no scrambling: the van der Corput / scipy 8x2 prefix.
    const double dimension1[8] = {0.0, 0.5, 0.75, 0.25, 0.375, 0.875, 0.625, 0.125};
    const double dimension2[8] = {0.0, 0.5, 0.25, 0.75, 0.375, 0.875, 0.125, 0.625};
    SobolSampler sampler(SearchSpace({RealDimension("a", 0.0, 1.0), RealDimension("b", 0.0, 1.0)}), 0,
                         SobolScramble::None);
    for (std::uint64_t n = 0; n < 8; ++n) {
        const auto candidate = sampler.next();
        require(candidate.has_value() && candidate->id == n, "published prefix: ID is the index");
        require(as_real(*candidate->find("a")) == dimension1[n], "dimension 1 at n=" + std::to_string(n));
        require(as_real(*candidate->find("b")) == dimension2[n], "dimension 2 at n=" + std::to_string(n));
    }
    // The same column through an integer lattice and a Boolean exercises the 128-bit product path.
    const std::int64_t lattice[8] = {0, 4, 6, 2, 3, 7, 5, 1};
    const bool boolean[8] = {false, true, true, false, false, true, true, false};
    const SobolSampler ints(SearchSpace({IntegerDimension("k", 0, 7)}), 0, SobolScramble::None);
    const SobolSampler flags(SearchSpace({BooleanDimension("f")}), 0, SobolScramble::None);
    for (std::uint64_t n = 0; n < 8; ++n) {
        require(as_int(*ints.at(n).find("k")) == lattice[n], "integer lattice at n=" + std::to_string(n));
        require(as_bool(*flags.at(n).find("f")) == boolean[n], "Boolean at n=" + std::to_string(n));
    }
}

void test_indexed_purity_next_and_reset() {
    const SearchSpace space = mixed_space();
    SobolSampler sampler(space, 17, SobolScramble::DigitalShift, 0, 50);
    std::vector<Candidate> sequence;
    while (auto candidate = sampler.next()) {
        sequence.push_back(*candidate);
    }
    require(sequence.size() == 50 && sampler.generated() == 50, "budget of 50");
    require(!sampler.next().has_value(), "exhausted sampler stays exhausted");
    require(sampler.cursor() == 50, "cursor after the budget");
    for (std::uint64_t n = 0; n < 50; ++n) {
        require(sequence[n].id == n, "ID equals the raw index");
        require(sequence[n].values == sampler.at(n).values, "next() equals at()");
        require(space.is_valid(sequence[n]), "candidate must satisfy the search space");
    }
    // at() in any order, interleaved with next() and reset(), is the same pure function.
    for (const std::uint64_t n : u64s({49, 3, 49, 0, 25, 3})) {
        require(sampler.at(n).values == sequence[n].values, "at() is independent of call order");
    }
    sampler.reset();
    require(sampler.generated() == 0 && sampler.cursor() == 0, "reset rewinds the cursor");
    for (std::uint64_t n = 0; n < 50; ++n) {
        const auto candidate = sampler.next();
        require(candidate.has_value() && candidate->values == sequence[n].values && candidate->id == n,
                "reset replays the identical stream");
    }
    // A sampler that starts at first_index reproduces exactly the tail of the full stream.
    SobolSampler tail(space, 17, SobolScramble::DigitalShift, 20, 30);
    for (std::uint64_t n = 20; n < 50; ++n) {
        const auto candidate = tail.next();
        require(candidate.has_value() && candidate->id == n && candidate->values == sequence[n].values,
                "first_index continuation must reproduce rows by ID");
    }
    require(!tail.next().has_value() && tail.generated() == 30 && tail.first_index() == 20, "tail bookkeeping");
    // Same arguments, same stream; another seed, another stream.
    SobolSampler twin(space, 17, SobolScramble::DigitalShift, 0, 50);
    SobolSampler other(space, 18, SobolScramble::DigitalShift, 0, 50);
    bool differs = false;
    for (std::uint64_t n = 0; n < 50; ++n) {
        const Candidate a = twin.at(n);
        require(a.values == sequence[n].values, "twin sampler must match");
        differs = differs || other.at(n).values != sequence[n].values;
    }
    require(differs, "a different seed must change a digitally shifted stream");
}

void test_duplicates_are_kept() {
    SobolSampler sampler(SearchSpace({BooleanDimension("only")}), 4, SobolScramble::DigitalShift, 0, 64);
    std::set<bool> distinct;
    std::uint64_t issued = 0;
    while (auto candidate = sampler.next()) {
        require(candidate->id == issued, "IDs stay contiguous even when parameter vectors repeat");
        distinct.insert(as_bool(*candidate->find("only")));
        ++issued;
    }
    require(issued == 64 && distinct.size() <= 2, "64 points over two vectors: duplicates remain, none skipped");
}

void test_first_index_budget_and_reserved_id_limits() {
    const SearchSpace space({IntegerDimension("v", 0, 100)});
    const auto make = [&](std::uint64_t first, std::uint64_t budget) {
        return SobolSampler(space, 1, SobolScramble::DigitalShift, first, budget);
    };
    // The largest ID ever issued is 2^64-2; 2^64-1 is reserved (N7).
    require_refused([&] { make(kMax, 0); }, "first_index 2^64-1");
    require_refused([&] { make(kMax, 1); }, "first_index 2^64-1 with a budget");
    require_refused([&] { make(kMax - 1, 2); }, "budget above the remaining IDs at 2^64-2");
    require_refused([&] { make(kMax - 2, 3); }, "budget above the remaining IDs at 2^64-3");
    require_refused([&] { make(5, kMax); }, "budget above 2^64-1-first_index");

    SobolSampler last = make(kMax - 1, 1);
    require(last.limit() == 1, "one ID remains at 2^64-2");
    const auto only = last.next();
    require(only.has_value() && only->id == kMax - 1, "ID 2^64-2 is issuable");
    require(!last.next().has_value() && last.cursor() == kMax, "then the sampler stops at the reserved ID");

    SobolSampler pair = make(kMax - 2, 2);  // N7: next_id 2^64-3 with budget 2 is accepted
    require(pair.next()->id == kMax - 2 && pair.next()->id == kMax - 1 && !pair.next().has_value(),
            "IDs 2^64-3 and 2^64-2, nothing more");

    SobolSampler unbounded_tail = make(kMax - 3, 0);  // budget 0 = until the last issuable ID
    require(unbounded_tail.limit() == 3, "unbounded budget stops at the reserved ID");
    std::uint64_t count = 0;
    while (unbounded_tail.next().has_value()) {
        ++count;
    }
    require(count == 3, "three IDs remain at 2^64-4");

    require(make(0, 0).limit() == kMax, "an unbounded fresh run has 2^64-1 issuable IDs");
    require(make(2, 7).limit() == 7 && make(2, 7).first_index() == 2, "explicit budget is honoured");

    const SobolSampler probe = make(0, 1);
    require_throws<std::out_of_range>([&] { (void)probe.at(kMax); }, "at(2^64-1) must refuse the reserved ID");
    const Candidate high = probe.at(kMax - 1);
    require(high.id == kMax - 1 && space.is_valid(high), "at(2^64-2) is a valid candidate");
    require(high.values == make(kMax - 1, 1).at(kMax - 1).values, "a point does not depend on first_index or budget");
}

void test_column_ceiling_of_1024() {
    const auto booleans = [](std::size_t count) {
        std::vector<Dimension> dimensions;
        for (std::size_t i = 0; i < count; ++i) {
            std::string name = std::to_string(i);
            dimensions.push_back(BooleanDimension("d" + std::string(4 - name.size(), '0') + name));
        }
        return dimensions;
    };
    std::vector<Dimension> at_limit = booleans(1024);
    at_limit.push_back(IntegerDimension("const_int", 3, 3));
    at_limit.push_back(RealDimension("const_real", 1.5, 1.5));
    const SobolSampler accepted{SearchSpace(at_limit), 9, SobolScramble::DigitalShift, 0, 4};
    require(accepted.columns().size() == 1024 && accepted.constants().size() == 2,
            "1024 varying columns plus constants are accepted");
    require(accepted.columns()[0].name == "d0000" && accepted.columns()[1023].name == "d1023" &&
                accepted.columns()[1023].index == 1023,
            "column 1023 is the last name");
    require(accepted.at(1).values.size() == 1026, "every dimension is emitted");
    require_refused([&] { SobolSampler refused(SearchSpace(booleans(1025)), 9); }, "1025 varying dimensions");
}

void test_at_is_pure_across_threads() {
    const SobolSampler sampler(mixed_space(), 99, SobolScramble::DigitalShift);
    std::vector<Candidate> expected;
    for (std::uint64_t n = 0; n < 200; ++n) {
        expected.push_back(sampler.at(n));
    }
    std::vector<int> intact(4, 1);
    std::vector<std::thread> threads;
    for (std::size_t t = 0; t < intact.size(); ++t) {
        threads.emplace_back([&, t] {
            for (std::uint64_t n = 0; n < 200; ++n) {
                const std::uint64_t index = (n * 7 + t * 13) % 200;
                const Candidate candidate = sampler.at(index);
                if (candidate.id != expected[index].id || candidate.values != expected[index].values) {
                    intact[t] = 0;
                }
            }
        });
    }
    for (std::thread& thread : threads) {
        thread.join();
    }
    for (const int ok : intact) {
        require(ok == 1, "concurrent at() calls must return the single-threaded points");
    }
}

}  // namespace

int main() {
    struct Test {
        const char* name;
        void (*run)();
    };
    const Test tests[] = {
        {"contract_constants", test_contract_constants},
        {"high64_hand_derived_table", test_high64_hand_derived_table},
        {"high64_matches_wide_arithmetic", test_high64_matches_wide_arithmetic},
        {"unit53", test_unit53},
        {"boolean_split", test_boolean_split},
        {"categorical_split_and_constant", test_categorical_split_and_constant},
        {"integer_lattice_signed_and_extreme_ranges", test_integer_lattice_signed_and_extreme_ranges},
        {"unrepresentable_integer_count_is_refused", test_unrepresentable_integer_count_is_refused},
        {"authority_differential_finite_spaces", test_authority_differential_finite_spaces},
        {"whole_space_overflow_does_not_matter", test_whole_space_overflow_does_not_matter},
        {"stepped_real_matches_lattice", test_stepped_real_matches_lattice},
        {"linear_real_exact_cases", test_linear_real_exact_cases},
        {"real_mappings_stay_in_range_and_monotone", test_real_mappings_stay_in_range_and_monotone},
        {"log_real_literal_path_is_unchanged", test_log_real_literal_path_is_unchanged},
        {"log_real_quartered_witnesses", test_log_real_quartered_witnesses},
        {"log_real_branch_boundary_both_sides", test_log_real_branch_boundary_both_sides},
        {"log_real_geometric_mean_and_finite_span", test_log_real_geometric_mean_and_finite_span},
        {"log_integer_cases_and_representability_limits", test_log_integer_cases_and_representability_limits},
        {"repeated_indexed_mapping_is_bitwise_stable", test_repeated_indexed_mapping_is_bitwise_stable},
        {"float_reference_fixture", test_float_reference_fixture},
        {"columns_are_name_sorted_and_constants_take_none", test_columns_are_name_sorted_and_constants_take_none},
        {"point_equals_mapper_applied_to_engine_coordinates", test_point_equals_mapper_applied_to_engine_coordinates},
        {"engine_dependency_contract", test_engine_dependency_contract},
        {"published_prefix_through_sampler", test_published_prefix_through_sampler},
        {"indexed_purity_next_and_reset", test_indexed_purity_next_and_reset},
        {"duplicates_are_kept", test_duplicates_are_kept},
        {"first_index_budget_and_reserved_id_limits", test_first_index_budget_and_reserved_id_limits},
        {"column_ceiling_of_1024", test_column_ceiling_of_1024},
        {"at_is_pure_across_threads", test_at_is_pure_across_threads},
    };
    int failures = 0;
    for (const Test& test : tests) {
        try {
            test.run();
            std::cout << "PASS " << test.name << '\n';
        } catch (const std::exception& error) {
            ++failures;
            std::cerr << "FAIL " << test.name << ": " << error.what() << '\n';
        }
    }
    std::cout << (failures == 0 ? "all sobol mapper tests passed" : "sobol mapper tests failed") << '\n';
    return failures == 0 ? 0 : 1;
}
