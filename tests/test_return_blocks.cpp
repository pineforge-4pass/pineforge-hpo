// Native tests of the X return-block layout and moments (contract pineforge-hpo-return-blocks/v1).
//
// Build this test, and src/core/return_stats.cpp, with -fno-fast-math -ffp-contract=off: every
// literal below assumes one rounded division and subtraction per return, and one rounded product
// per squared deviation, with no contraction.
//
// The expected values here are not read from anywhere: every one of them is derived by hand in a
// comment beside it, from the curve fed to the reducer. Nothing in this file calls the reducer to
// produce the value it then compares against, except the two checks that are explicitly about
// repetition rather than about correctness (the determinism case and the layout invariance case):
// those compare two runs of the same pure function with each other and expect equality.
//
// The curves below are dyadic on purpose: every equity and every return is a sum of powers of two
// that binary64 holds exactly, so each count, sum, mean and centred moment has an exact rational
// value that can be written out as a literal.
#include <pineforge/hpo/return_stats.hpp>

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <limits>
#include <string>
#include <vector>

namespace {

using namespace pineforge::hpo;

const double kNaN = std::numeric_limits<double>::quiet_NaN();
const double kInf = std::numeric_limits<double>::infinity();

int g_checks = 0;
int g_failures = 0;
const char* g_case = "";

std::uint64_t bits_of(double value) {
    std::uint64_t bits = 0;
    std::memcpy(&bits, &value, sizeof bits);
    return bits;
}

bool same_bits(double actual, double expected) {
    return (std::isnan(actual) && std::isnan(expected)) || bits_of(actual) == bits_of(expected);
}

void fail(int line, const std::string& message) {
    ++g_failures;
    std::cerr << "FAIL [" << g_case << "] line " << line << ": " << message << '\n';
}

void check(int line, bool condition, const char* expression) {
    ++g_checks;
    if (!condition)
        fail(line, expression);
}

void check_ints(int line,
                const char* what,
                const std::vector<std::int64_t>& actual,
                const std::vector<std::int64_t>& expected) {
    ++g_checks;
    if (actual == expected)
        return;
    ++g_failures;
    std::cerr << "FAIL [" << g_case << "] line " << line << ": " << what << " has "
              << actual.size() << " entries {";
    for (std::size_t index = 0; index < actual.size(); ++index)
        std::cerr << (index == 0 ? "" : ", ") << actual[index];
    std::cerr << "} where {";
    for (std::size_t index = 0; index < expected.size(); ++index)
        std::cerr << (index == 0 ? "" : ", ") << expected[index];
    std::cerr << "} was derived\n";
}

void check_bits_vector(int line,
                       const char* what,
                       const std::vector<double>& actual,
                       const std::vector<double>& expected) {
    ++g_checks;
    if (actual.size() != expected.size()) {
        ++g_failures;
        std::cerr << "FAIL [" << g_case << "] line " << line << ": " << what << " has "
                  << actual.size() << " entries where " << expected.size() << " were derived\n";
        return;
    }
    for (std::size_t index = 0; index < actual.size(); ++index) {
        if (same_bits(actual[index], expected[index]))
            continue;
        ++g_failures;
        std::cerr << "FAIL [" << g_case << "] line " << line << ": " << what << '[' << index
                  << "] actual " << std::hexfloat << actual[index] << " expected "
                  << expected[index] << std::defaultfloat << '\n';
    }
}

#define EXPECT(condition) check(__LINE__, (condition), #condition)
#define EXPECT_INTS(actual, expected) check_ints(__LINE__, #actual, (actual), (expected))
#define EXPECT_BITS_VECTOR(actual, expected) \
    check_bits_vector(__LINE__, #actual, (actual), (expected))

// The same probe the statistics test uses: a multiply followed by an add must be two rounded
// operations, or the frozen dyadic literals of this file would not describe the build.
bool unit_contraction_free() {
    volatile double first_input = 1.0 + 0x1p-27;
    volatile double addend_input = -(1.0 + 0x1p-26);
    const double first = first_input;
    const double addend = addend_input;
    const double product = first * first;
    return addend + product == 0.0;
}

// ---------------------------------------------------------------------------------------------
// Curves
// ---------------------------------------------------------------------------------------------

// Same shape as the engine's per-bar curve record: 24 bytes with a third member, so the reducer's
// offsets are exercised rather than assumed.
struct Point {
    std::int64_t time_ms;
    double equity;
    double open_profit;
};
using Curve = std::vector<Point>;

constexpr std::int64_t kMillisecond = 1000;

Curve curve_of(const std::vector<double>& equities, std::int64_t start = 0,
               std::int64_t step = kMillisecond) {
    Curve curve;
    for (std::size_t index = 0; index < equities.size(); ++index)
        curve.push_back({start + static_cast<std::int64_t>(index) * step, equities[index], 0.0});
    return curve;
}

Curve curve_at(const std::vector<std::int64_t>& times, const std::vector<double>& equities) {
    Curve curve;
    for (std::size_t index = 0; index < equities.size(); ++index)
        curve.push_back({times[index], equities[index], 0.0});
    return curve;
}

struct Reduction {
    ReturnBlockLayout layout;
    ReturnBlockMoments moments;
};

EquityPointsView view_of(const Curve& curve) {
    return make_equity_points_view(curve.data(), curve.size(), &Point::time_ms, &Point::equity);
}

Reduction reduce(const Curve& curve, std::uint32_t blocks) {
    const EquityPointsView view = view_of(curve);
    Reduction reduction;
    reduction.layout = compute_block_layout(view, blocks);
    reduction.moments = reduce_return_blocks(view, reduction.layout);
    return reduction;
}

// ---------------------------------------------------------------------------------------------
// Contract surface
// ---------------------------------------------------------------------------------------------

void case_contract_surface() {
    g_case = "contract surface";
    EXPECT(kReturnBlocksContract == "pineforge-hpo-return-blocks/v1");
    EXPECT(return_blocks_status_text(ReturnBlocksStatus::Ok) == "ok");
    EXPECT(return_blocks_status_text(ReturnBlocksStatus::NonPositiveEquity) ==
           "non_positive_equity");
    EXPECT(return_blocks_status_text(ReturnBlocksStatus::Nonfinite) == "non_finite");
    // The three statuses that are library outcomes carry no wire text at all, so none of them may
    // ever be printed as a payload status: a payload carrying moment arrays is published only with
    // `ok`, and the two other wire statuses above are published status-only.
    EXPECT(return_blocks_status_text(ReturnBlocksStatus::NotRequested).empty());
    EXPECT(return_blocks_status_text(ReturnBlocksStatus::UnusableCurve).empty());
    EXPECT(return_blocks_status_text(ReturnBlocksStatus::UnusableLayout).empty());

    // Defaults: an unrequested snapshot carries no layout and no moments.
    const ReturnBlockLayout layout;
    EXPECT(layout.blocks == 0);
    EXPECT(layout.intervals == 0);
    EXPECT(!layout.usable);
    EXPECT(layout.n_b.empty());
    EXPECT(layout.boundary_time_ms.empty());
    const ReturnBlockMoments moments;
    EXPECT(moments.status == ReturnBlocksStatus::NotRequested);
    EXPECT(moments.count.empty());
    EXPECT(moments.sum.empty());
    EXPECT(moments.m2.empty());
}

// ---------------------------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------------------------

void case_layout_rules() {
    g_case = "layout rules";

    // K = 5 intervals (six points), S = 2: a_0 = 0, a_1 = ceil(1 * 5 / 2) = 3, a_2 = 5, so the
    // sizes are (3, 2); times are 0, 1000, ..., 5000, so the opening points are 0, 3000, 5000.
    {
        const Curve curve = curve_of({1.0, 1.0, 1.0, 1.0, 1.0, 1.0});
        const Reduction reduction = reduce(curve, 2);
        EXPECT(reduction.layout.usable);
        EXPECT(reduction.layout.blocks == 2);
        EXPECT(reduction.layout.intervals == 5);
        EXPECT_INTS(reduction.layout.n_b, (std::vector<std::int64_t>{3, 2}));
        EXPECT_INTS(reduction.layout.boundary_time_ms, (std::vector<std::int64_t>{0, 3000, 5000}));
    }

    // K = 8 (nine points), S = 4: a_b = 2b, so every block holds 2 intervals and the openings are
    // points 0, 2, 4, 6 and 8.
    {
        const Curve curve = curve_of({1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0});
        const Reduction reduction = reduce(curve, 4);
        EXPECT(reduction.layout.usable);
        EXPECT_INTS(reduction.layout.n_b, (std::vector<std::int64_t>{2, 2, 2, 2}));
        EXPECT_INTS(reduction.layout.boundary_time_ms,
                    (std::vector<std::int64_t>{0, 2000, 4000, 6000, 8000}));
    }

    // K = 33 (thirty-four points), S = 16: a_b = 2b for b = 0 and a_b = 2b + 1 for b = 1..16,
    // because ceil(b * 33 / 16) = 2b + ceil(b / 16) and b / 16 is above 0 up to b = 16; so the
    // first block holds 3 intervals, the other fifteen hold 2, and 3 + 15 * 2 = 33. The sizes
    // differ by one, which is the spread the module accepts.
    {
        std::vector<double> equities(34, 1.0);
        const Curve curve = curve_of(equities);
        const Reduction reduction = reduce(curve, 16);
        EXPECT(reduction.layout.usable);
        EXPECT(reduction.layout.blocks == 16);
        EXPECT(reduction.layout.intervals == 33);
        std::vector<std::int64_t> expected_sizes{3};
        for (int index = 0; index < 15; ++index)
            expected_sizes.push_back(2);
        EXPECT_INTS(reduction.layout.n_b, expected_sizes);
        std::vector<std::int64_t> expected_times{0, 3000};
        for (int index = 2; index <= 16; ++index)
            expected_times.push_back(static_cast<std::int64_t>(2 * index + 1) * 1000);
        EXPECT_INTS(reduction.layout.boundary_time_ms, expected_times);
        // The last opening point is the last point of the curve.
        if (reduction.layout.usable) {
            EXPECT(reduction.layout.boundary_time_ms.back() == curve.back().time_ms);
        }
        // The smallest half of the blocks holds at least two intervals: S / 2 = 8 blocks of the
        // fifteen two-interval blocks add up to 16 >= 2, which is what the module requires.
        if (reduction.layout.usable) {
            EXPECT(reduction.layout.n_b.front() - reduction.layout.n_b.back() == 1);
        }
    }

    // K = 8 (nine points), S = 8: every block holds one interval and the smallest half holds 4 >= 2
    // intervals, so the request is served even though every block has a single interval and every
    // m2 below is 0 by rule.
    {
        const Curve curve = curve_of({1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0});
        const Reduction reduction = reduce(curve, 8);
        EXPECT(reduction.layout.usable);
        EXPECT_INTS(reduction.layout.n_b, (std::vector<std::int64_t>{1, 1, 1, 1, 1, 1, 1, 1}));
        EXPECT_INTS(reduction.layout.boundary_time_ms,
                    (std::vector<std::int64_t>{0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000}));
    }

    // K = 4 (five points), S = 2: a_b = 2b and each block holds two intervals, so the smallest
    // half is one block of two intervals and the request is served.
    {
        const Curve curve = curve_of({1.0, 1.0, 1.0, 1.0, 1.0});
        const Reduction reduction = reduce(curve, 2);
        EXPECT(reduction.layout.usable);
        EXPECT_INTS(reduction.layout.n_b, (std::vector<std::int64_t>{2, 2}));
    }
}

void case_refused_layouts() {
    g_case = "refused layouts";

    // K = 3, S = 2: the sizes are (2, 1) and the smallest half is one block of a single interval,
    // which the module refuses (a half could hold fewer than two intervals), so the request itself
    // is not servable and no payload may be built from it.
    {
        const Curve curve = curve_of({1.0, 1.0, 1.0, 1.0});
        const Reduction reduction = reduce(curve, 2);
        EXPECT(!reduction.layout.usable);
        EXPECT(reduction.layout.n_b.empty());
        EXPECT(reduction.layout.boundary_time_ms.empty());
        EXPECT(reduction.moments.status == ReturnBlocksStatus::UnusableLayout);
        EXPECT(reduction.moments.count.empty());
    }

    // K = 2, S = 2: the sizes are (1, 1) and the smallest half holds one interval.
    {
        const Curve curve = curve_of({1.0, 1.0, 1.0});
        EXPECT(!reduce(curve, 2).layout.usable);
    }

    // K = 2 < S = 4: an interval count below the block count.
    {
        const Curve curve = curve_of({1.0, 1.0, 1.0});
        const Reduction reduction = reduce(curve, 4);
        EXPECT(!reduction.layout.usable);
        EXPECT(reduction.moments.status == ReturnBlocksStatus::UnusableLayout);
    }

    // S must be even and at least 2.
    {
        const Curve curve = curve_of({1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0});
        EXPECT(!reduce(curve, 3).layout.usable);
        EXPECT(!reduce(curve, 1).layout.usable);
        EXPECT(!reduce(curve, 0).layout.usable);
    }

    // A curve with no interval at all: no points, and a single point (K = 0).
    {
        const Curve empty;
        EXPECT(!reduce(empty, 2).layout.usable);
        EXPECT(reduce(empty, 2).moments.status == ReturnBlocksStatus::UnusableCurve);
        const Curve single = curve_of({1.0});
        EXPECT(!reduce(single, 2).layout.usable);
    }

    // A view whose layout cannot be read: the equity offset does not fit inside the stride.
    {
        const Curve curve = curve_of({1.0, 1.0, 1.0, 1.0, 1.0});
        EquityPointsView broken;
        broken.records = curve.data();
        broken.count = curve.size();
        broken.stride = 8;  // too small for an 8-byte time followed by an 8-byte equity
        broken.time_offset = 0;
        broken.equity_offset = 8;
        const ReturnBlockLayout broken_layout = compute_block_layout(broken, 2);
        EXPECT(!broken_layout.usable);
        const ReturnBlockMoments moments = reduce_return_blocks(broken, broken_layout);
        EXPECT(moments.status == ReturnBlocksStatus::UnusableCurve);
        EXPECT(moments.count.empty());
    }
}

void case_layouts_are_independent_of_equity() {
    g_case = "layout depends on the curve length only";
    // The layout is a function of the interval count and S, never of the equities: two curves with
    // the same number of points and different equities must give the identical layout. This
    // compares two runs of the same pure function and expects equality; it is an invariance check,
    // not an oracle for the sizes, which the literals above fix.
    std::vector<double> flat(34, 1.0);
    std::vector<double> varied;
    for (std::size_t index = 0; index < 34; ++index) {
        const double drift = (static_cast<double>((index * 37) % 101) - 50.0) / 5000.0;
        varied.push_back(100000.0 * (1.0 + drift));
    }
    const ReturnBlockLayout flat_layout = reduce(curve_of(flat), 16).layout;
    const ReturnBlockLayout varied_layout = reduce(curve_of(varied), 16).layout;
    EXPECT(flat_layout.usable && varied_layout.usable);
    EXPECT_INTS(varied_layout.n_b, flat_layout.n_b);
    EXPECT_INTS(varied_layout.boundary_time_ms, flat_layout.boundary_time_ms);
}

void case_boundary_times_are_copied() {
    g_case = "boundary times";
    // K = 4, S = 2 over unevenly spaced points: the opening points are 0, 2 and 4, so the
    // boundaries are the timestamps of those points, unchanged.
    const Curve curve = curve_at({5, 15, 25, 35, 45}, {1.0, 1.0, 1.0, 1.0, 1.0});
    const Reduction reduction = reduce(curve, 2);
    EXPECT(reduction.layout.usable);
    EXPECT_INTS(reduction.layout.boundary_time_ms, (std::vector<std::int64_t>{5, 25, 45}));
    if (reduction.layout.usable) {
        EXPECT(reduction.layout.boundary_time_ms.back() == curve.back().time_ms);
        for (std::size_t index = 1; index < reduction.layout.boundary_time_ms.size(); ++index) {
            EXPECT(reduction.layout.boundary_time_ms[index] >=
                   reduction.layout.boundary_time_ms[index - 1]);
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Moments
// ---------------------------------------------------------------------------------------------

void case_two_blocks_of_two() {
    g_case = "two blocks of two";
    // Equities 1, 2, 1.5, 1.5, 1.875 hold K = 4 intervals and S = 2 gives the sizes (2, 2).
    // Returns, all exact in binary64:
    //   r0 = 2 / 1 - 1     = 1
    //   r1 = 1.5 / 2 - 1   = -0.25
    //   r2 = 1.5 / 1.5 - 1 = 0
    //   r3 = 1.875 / 1.5 - 1 = 1.25 - 1 = 0.25
    // Block 0 = (r0, r1): count 2, sum 1 + (-0.25) = 0.75, mean 0.75 / 2 = 0.375,
    //   m2 = (1 - 0.375)^2 + (-0.25 - 0.375)^2 = 0.625^2 + 0.625^2 = 0.390625 + 0.390625 = 0.78125.
    // Block 1 = (r2, r3): count 2, sum 0 + 0.25 = 0.25, mean 0.125,
    //   m2 = (0 - 0.125)^2 + (0.25 - 0.125)^2 = 0.015625 + 0.015625 = 0.03125.
    const Curve curve = curve_of({1.0, 2.0, 1.5, 1.5, 1.875});
    const Reduction reduction = reduce(curve, 2);
    EXPECT(reduction.layout.usable);
    EXPECT(reduction.moments.status == ReturnBlocksStatus::Ok);
    EXPECT_INTS(reduction.moments.count, (std::vector<std::int64_t>{2, 2}));
    EXPECT_BITS_VECTOR(reduction.moments.sum, (std::vector<double>{0.75, 0.25}));
    EXPECT_BITS_VECTOR(reduction.moments.m2, (std::vector<double>{0.78125, 0.03125}));
}

void case_two_blocks_of_four() {
    g_case = "two blocks of four";
    // K = 8 holds S = 2 as two blocks of four. The equities double through the first block and
    // then move by -0.5, +0.25 and -0.75, every value dyadic:
    //   equities 1, 1.5, 2.25, 5.625, 22.5, 67.5, 33.75, 42.1875, 10.546875
    //   returns  0.5, 0.5, 1.5, 3, 2, -0.5, 0.25, -0.75
    // Block 0 = (0.5, 0.5, 1.5, 3): count 4, sum 5.5, mean 5.5 / 4 = 1.375,
    //   m2 = 0.875^2 + 0.875^2 + 0.125^2 + 1.625^2
    //      = 0.765625 + 0.765625 + 0.015625 + 2.640625 = 4.1875.
    // Block 1 = (2, -0.5, 0.25, -0.75): count 4, sum 1, mean 0.25,
    //   m2 = 1.75^2 + 0.75^2 + 0 + 1^2 = 3.0625 + 0.5625 + 0 + 1 = 4.625.
    const Curve curve = curve_of(
        {1.0, 1.5, 2.25, 5.625, 22.5, 67.5, 33.75, 42.1875, 10.546875});
    const Reduction reduction = reduce(curve, 2);
    EXPECT(reduction.layout.usable);
    EXPECT(reduction.moments.status == ReturnBlocksStatus::Ok);
    EXPECT_INTS(reduction.moments.count, (std::vector<std::int64_t>{4, 4}));
    EXPECT_BITS_VECTOR(reduction.moments.sum, (std::vector<double>{5.5, 1.0}));
    EXPECT_BITS_VECTOR(reduction.moments.m2, (std::vector<double>{4.1875, 4.625}));
}

void case_single_interval_blocks() {
    g_case = "blocks of one interval";
    // Equities 1, 1.5, 0.75, 0.75, 1.5, 3 give K = 5 and S = 4 gives the sizes (2, 1, 1, 1):
    // a = (0, 2, 3, 4, 5) because ceil(b * 5 / 4) is 0, 2, 3, 4, 5. Returns, all exact:
    //   r0 = 0.5, r1 = -0.5, r2 = 0, r3 = 1, r4 = 1
    // Block 0 = (r0, r1): count 2, sum 0.5 + (-0.5) = 0, mean 0,
    //   m2 = 0.5^2 + (-0.5)^2 = 0.25 + 0.25 = 0.5.
    // Blocks 1, 2 and 3 hold one interval each: count 1, so m2 is 0 by rule, and the sums are
    //   0, 1 and 1.
    const Curve curve = curve_of({1.0, 1.5, 0.75, 0.75, 1.5, 3.0});
    const Reduction reduction = reduce(curve, 4);
    EXPECT(reduction.layout.usable);
    EXPECT_INTS(reduction.layout.n_b, (std::vector<std::int64_t>{2, 1, 1, 1}));
    EXPECT(reduction.moments.status == ReturnBlocksStatus::Ok);
    EXPECT_INTS(reduction.moments.count, (std::vector<std::int64_t>{2, 1, 1, 1}));
    EXPECT_BITS_VECTOR(reduction.moments.sum, (std::vector<double>{0.0, 0.0, 1.0, 1.0}));
    EXPECT_BITS_VECTOR(reduction.moments.m2, (std::vector<double>{0.5, 0.0, 0.0, 0.0}));
}

void case_constant_curves() {
    g_case = "constant curves";
    // A constant equity curve: every ratio is 1 and every return is 1 - 1 = 0, so each block of
    // two has count 2, sum 0 and both squared deviations 0.
    {
        const Curve curve = curve_of({2.0, 2.0, 2.0, 2.0, 2.0});
        const Reduction reduction = reduce(curve, 2);
        EXPECT(reduction.moments.status == ReturnBlocksStatus::Ok);
        EXPECT_INTS(reduction.moments.count, (std::vector<std::int64_t>{2, 2}));
        EXPECT_BITS_VECTOR(reduction.moments.sum, (std::vector<double>{0.0, 0.0}));
        EXPECT_BITS_VECTOR(reduction.moments.m2, (std::vector<double>{0.0, 0.0}));
    }
    // A curve whose returns are the constant 1: 2 / 1 - 1, 4 / 2 - 1, 8 / 4 - 1 and 16 / 8 - 1 are
    // all exactly 1, so each block of two has count 2, sum 2, mean 1 and m2 = 0 + 0 = 0. A block
    // with a mean equal to every observation is the case where a sum-of-squares formula would
    // still be 0 but a wrong centred pass could drift; the exact expectation is 0.
    {
        const Curve curve = curve_of({1.0, 2.0, 4.0, 8.0, 16.0});
        const Reduction reduction = reduce(curve, 2);
        EXPECT(reduction.moments.status == ReturnBlocksStatus::Ok);
        EXPECT_INTS(reduction.moments.count, (std::vector<std::int64_t>{2, 2}));
        EXPECT_BITS_VECTOR(reduction.moments.sum, (std::vector<double>{2.0, 2.0}));
        EXPECT_BITS_VECTOR(reduction.moments.m2, (std::vector<double>{0.0, 0.0}));
    }
}

void case_skipped_and_negative_equity() {
    g_case = "non-positive equity";

    // Equities 1, 0, 1, 1, 1: K = 4, S = 2 gives (2, 2). r0 = 0 / 1 - 1 = -1 is usable, because a
    // current equity at or below zero is allowed; the interval after it has the prior 0, which is
    // not positive, so it is skipped and contributes nothing to count, sum or m2:
    //   block 0 = (r0, skipped): count 1, sum -1, m2 = 0 by rule
    //   block 1 = (0, 0): count 2, sum 0, mean 0, m2 = 0 + 0 = 0
    // The status is non_positive_equity, which the module refuses before it reads the arrays.
    {
        const Curve curve = curve_of({1.0, 0.0, 1.0, 1.0, 1.0});
        const Reduction reduction = reduce(curve, 2);
        EXPECT(reduction.layout.usable);
        EXPECT(reduction.moments.status == ReturnBlocksStatus::NonPositiveEquity);
        EXPECT_INTS(reduction.moments.count, (std::vector<std::int64_t>{1, 2}));
        EXPECT_BITS_VECTOR(reduction.moments.sum, (std::vector<double>{-1.0, 0.0}));
        EXPECT_BITS_VECTOR(reduction.moments.m2, (std::vector<double>{0.0, 0.0}));
    }

    // Equities 1, 0, 0, 0, 0: every interval but the first has a non-positive prior, so the second
    // block has count 0 and its sum and m2 stay 0. A zero count is reachable only with a status
    // other than ok, which is exactly why the module refuses the row before it checks the counts
    // against the declared sizes.
    {
        const Curve curve = curve_of({1.0, 0.0, 0.0, 0.0, 0.0});
        const Reduction reduction = reduce(curve, 2);
        EXPECT(reduction.moments.status == ReturnBlocksStatus::NonPositiveEquity);
        EXPECT_INTS(reduction.moments.count, (std::vector<std::int64_t>{1, 0}));
        EXPECT_BITS_VECTOR(reduction.moments.sum, (std::vector<double>{-1.0, 0.0}));
        EXPECT_BITS_VECTOR(reduction.moments.m2, (std::vector<double>{0.0, 0.0}));
    }

    // Equities 1, -0.5, 1, 1, 1: the first return is -0.5 / 1 - 1 = -1.5 and is counted, so a
    // current below zero is data rather than a skip. The interval after it has the prior -0.5 and
    // is skipped, which is what makes the status non_positive_equity.
    {
        const Curve curve = curve_of({1.0, -0.5, 1.0, 1.0, 1.0});
        const Reduction reduction = reduce(curve, 2);
        EXPECT(reduction.moments.status == ReturnBlocksStatus::NonPositiveEquity);
        EXPECT_INTS(reduction.moments.count, (std::vector<std::int64_t>{1, 2}));
        EXPECT_BITS_VECTOR(reduction.moments.sum, (std::vector<double>{-1.5, 0.0}));
    }
}

void case_nonfinite_inputs() {
    g_case = "non-finite input";

    // A NaN equity makes the two intervals that touch it non-finite: block 0 has count 0 (the
    // nan is the current of interval 0 and the prior of interval 1) and block 1 has count 2 with
    // both returns 0. The status is non_finite, not a skip and not an error.
    {
        const Curve curve = curve_of({1.0, kNaN, 1.0, 1.0, 1.0});
        const Reduction reduction = reduce(curve, 2);
        EXPECT(reduction.layout.usable);
        EXPECT(reduction.moments.status == ReturnBlocksStatus::Nonfinite);
        EXPECT_INTS(reduction.moments.count, (std::vector<std::int64_t>{0, 2}));
        EXPECT_BITS_VECTOR(reduction.moments.sum, (std::vector<double>{0.0, 0.0}));
    }
    // An infinity in the curve is the same status.
    {
        const Curve curve = curve_of({1.0, kInf, 1.0, 1.0, 1.0});
        EXPECT(reduce(curve, 2).moments.status == ReturnBlocksStatus::Nonfinite);
    }
    // Equities 1e-300, 1e300, 1e300, 1e300, 1e300 are all finite, but the first return is not:
    // 1e300 / 1e-300 overflows binary64, so the ratio is infinite and the return is never repaired
    // or dropped. Only that one interval is lost, so block 0 keeps the one usable interval that
    // follows it (its return is 1e300 / 1e300 - 1 = 0) and block 1 holds two of them, while the
    // status is non_finite.
    {
        const Curve curve = curve_of({1e-300, 1e300, 1e300, 1e300, 1e300});
        const Reduction reduction = reduce(curve, 2);
        EXPECT(reduction.moments.status == ReturnBlocksStatus::Nonfinite);
        EXPECT_INTS(reduction.moments.count, (std::vector<std::int64_t>{1, 2}));
    }
}

void case_skipped_and_nonfinite_precedence() {
    g_case = "non-finite outranks a skipped interval";
    // Equities 1, 0, 1, NaN, 1 hold K = 4 intervals and S = 2 gives the sizes (2, 2). The second
    // interval has the prior 0, which is not positive, so it is skipped; the third and fourth
    // intervals touch the NaN and are not finite. Both causes are present at once, and the intended
    // rule (the design note: a non-finite value is the more severe) is that a non-finite interval
    // wins and never hides behind a skipped one, so the status is Nonfinite, not
    // NonPositiveEquity.
    const Curve curve = curve_of({1.0, 0.0, 1.0, kNaN, 1.0});
    const Reduction reduction = reduce(curve, 2);
    EXPECT(reduction.layout.usable);
    EXPECT(reduction.moments.status == ReturnBlocksStatus::Nonfinite);
    // Block 0 = (valid, skipped): the one usable return is 0 / 1 - 1 = -1, so count 1 with m2 = 0
    // by rule. Block 1 = (nonfinite, nonfinite): count 0, sum 0, m2 0.
    EXPECT_INTS(reduction.moments.count, (std::vector<std::int64_t>{1, 0}));
    EXPECT_BITS_VECTOR(reduction.moments.sum, (std::vector<double>{-1.0, 0.0}));
    EXPECT_BITS_VECTOR(reduction.moments.m2, (std::vector<double>{0.0, 0.0}));
}

void case_time_order_is_required() {
    g_case = "opening times must not decrease";
    // K = 4 (five points), S = 2: the opening points are 0, 2 and 4, so the layout copies the
    // timestamps of those points. A later opening point stamped before an earlier one cannot be
    // served, because the module requires non-decreasing opening times, so the layout is refused
    // rather than published with times the module would reject.
    {
        const Curve curve = curve_at({100, 10, 5, 30, 40}, {1.0, 2.0, 4.0, 8.0, 16.0});
        const Reduction reduction = reduce(curve, 2);
        EXPECT(!reduction.layout.usable);
        EXPECT(reduction.moments.status == ReturnBlocksStatus::UnusableLayout);
        EXPECT(reduction.layout.boundary_time_ms.empty());
    }
    // Equal opening times are allowed: the opening points share one timestamp and every later one
    // is not below the previous, so the request is served.
    {
        const Curve curve = curve_at({7, 7, 7, 7, 7}, {1.0, 2.0, 4.0, 8.0, 16.0});
        const Reduction reduction = reduce(curve, 2);
        EXPECT(reduction.layout.usable);
        EXPECT_INTS(reduction.layout.boundary_time_ms, (std::vector<std::int64_t>{7, 7, 7}));
        EXPECT(reduction.moments.status == ReturnBlocksStatus::Ok);
    }
}

void case_near_constant_rounding() {
    g_case = "near-constant rounding";
    // The returns of this curve are all near one common value and none is exactly representable in
    // binary64: 100.1, 100.2, ... are not dyadic. A near-constant series is exactly where the
    // order of the recipe shows in the bits, because the centred second moment is a small
    // difference of nearly equal numbers (small against the mean), so a reordered pass or a
    // sum-of-squares formula gives different bits that a loose tolerance would hide. The
    // expectations below are an independent transcription of the recipe, not a call to the reducer:
    // ratio = current / prior, return = ratio - 1, sum by ascending addition, mean = sum / count,
    // then the squared deviation of each return from that mean, each product stored before it is
    // added, in a second ascending pass. Every intermediate is a named double.
    const double e0 = 100.0;
    const double e1 = 100.1;
    const double e2 = 100.2;
    const double e3 = 100.3;
    const double e4 = 100.4;
    const double e5 = 100.5;
    const Curve curve = curve_of({e0, e1, e2, e3, e4, e5});
    const Reduction reduction = reduce(curve, 2);
    // K = 5, S = 2 -> a_b = 0, 3, 5, so block 0 holds intervals 0, 1, 2 and block 1 holds 3, 4.
    EXPECT(reduction.layout.usable);
    EXPECT(reduction.moments.status == ReturnBlocksStatus::Ok);

    const double r0 = e1 / e0 - 1.0;
    const double r1 = e2 / e1 - 1.0;
    const double r2 = e3 / e2 - 1.0;
    const double r3 = e4 / e3 - 1.0;
    const double r4 = e5 / e4 - 1.0;

    // Block 0: three returns, summed in ascending order.
    const double t0 = 0.0 + r0;
    const double t1 = t0 + r1;
    const double t2 = t1 + r2;
    const double sum0 = t2;
    const double mean0 = sum0 / 3.0;
    const double d00 = r0 - mean0;
    const double d01 = r1 - mean0;
    const double d02 = r2 - mean0;
    const double s00 = d00 * d00;
    const double s01 = d01 * d01;
    const double s02 = d02 * d02;
    const double q00 = 0.0 + s00;
    const double q01 = q00 + s01;
    const double m2_0 = q01 + s02;

    // Block 1: two returns.
    const double u0 = 0.0 + r3;
    const double u1 = u0 + r4;
    const double sum1 = u1;
    const double mean1 = sum1 / 2.0;
    const double d10 = r3 - mean1;
    const double d11 = r4 - mean1;
    const double s10 = d10 * d10;
    const double s11 = d11 * d11;
    const double q10 = 0.0 + s10;
    const double m2_1 = q10 + s11;

    EXPECT_INTS(reduction.moments.count, (std::vector<std::int64_t>{3, 2}));
    EXPECT_BITS_VECTOR(reduction.moments.sum, (std::vector<double>{sum0, sum1}));
    EXPECT_BITS_VECTOR(reduction.moments.m2, (std::vector<double>{m2_0, m2_1}));
}

// ---------------------------------------------------------------------------------------------
// Refusal of a layout the reducer was not given by compute_block_layout
// ---------------------------------------------------------------------------------------------

void case_reducer_rejects_foreign_layouts() {
    g_case = "foreign layout";

    const Curve curve = curve_of({1.0, 2.0, 1.5, 1.5, 1.875});
    const EquityPointsView view = view_of(curve);
    const ReturnBlockLayout good = compute_block_layout(view, 2);
    EXPECT(good.usable);

    // Sizes that are not the rule's own: a_1 = ceil(1 * 4 / 2) = 2, so the first block cannot hold
    // one interval.
    {
        ReturnBlockLayout altered = good;
        altered.n_b = {1, 3};
        const ReturnBlockMoments moments = reduce_return_blocks(view, altered);
        EXPECT(moments.status == ReturnBlocksStatus::UnusableLayout);
        EXPECT(moments.count.empty());
    }
    // An interval count that is not this curve's.
    {
        ReturnBlockLayout altered = good;
        altered.intervals = 5;
        EXPECT(reduce_return_blocks(view, altered).status == ReturnBlocksStatus::UnusableLayout);
    }
    // A block count that does not match the sizes.
    {
        ReturnBlockLayout altered = good;
        altered.blocks = 4;
        EXPECT(reduce_return_blocks(view, altered).status == ReturnBlocksStatus::UnusableLayout);
    }
    // Boundary times that are missing.
    {
        ReturnBlockLayout altered = good;
        altered.boundary_time_ms.clear();
        EXPECT(reduce_return_blocks(view, altered).status == ReturnBlocksStatus::UnusableLayout);
    }
    // A layout marked unusable, and the default one: neither carries sizes, so neither may reduce.
    {
        ReturnBlockLayout altered = good;
        altered.usable = false;
        EXPECT(reduce_return_blocks(view, altered).status == ReturnBlocksStatus::UnusableLayout);
        EXPECT(reduce_return_blocks(view, ReturnBlockLayout{}).status ==
               ReturnBlocksStatus::UnusableLayout);
    }
    // A forged layout with more blocks than intervals (S = 8 above K = 4) marked usable: this rule
    // can never produce it, so it is refused even though its declared sizes add up.
    {
        ReturnBlockLayout altered = good;
        altered.blocks = 8;
        altered.n_b = {1, 1, 1, 1, 0, 0, 0, 0};
        altered.boundary_time_ms = {0, 0, 0, 0, 0, 0, 0, 0, 0};
        EXPECT(reduce_return_blocks(view, altered).status == ReturnBlocksStatus::UnusableLayout);
    }
    // A forged odd S marked usable.
    {
        ReturnBlockLayout altered = good;
        altered.blocks = 3;
        altered.n_b = {2, 1, 1};
        altered.boundary_time_ms = {0, 0, 0, 0};
        EXPECT(reduce_return_blocks(view, altered).status == ReturnBlocksStatus::UnusableLayout);
    }
    // A forged opening timestamp: the sizes are the rule's own but the second boundary time is
    // wrong (the rule gives 0, 2000, 4000), which the module would not catch but this reducer must.
    {
        ReturnBlockLayout altered = good;
        altered.boundary_time_ms = {0, 3000, 4000};
        EXPECT(reduce_return_blocks(view, altered).status == ReturnBlocksStatus::UnusableLayout);
    }
    // A forged layout with K = 2 and S = 4 marked usable and sizes {1, 0, 1, 0}: those sizes are
    // the floor rule's own for S above K and add up, so a size-by-size check would accept them,
    // but S is above K and the request cannot be served.
    {
        const Curve pair = curve_of({1.0, 2.0, 4.0});
        ReturnBlockLayout forged;
        forged.blocks = 4;
        forged.intervals = 2;
        forged.usable = true;
        forged.n_b = {1, 0, 1, 0};
        forged.boundary_time_ms = {0, 0, 0, 0, 0};
        EXPECT(reduce_return_blocks(view_of(pair), forged).status ==
               ReturnBlocksStatus::UnusableLayout);
    }
    // A layout built for another curve of the same length is the same layout, and is accepted:
    // the sizes and times are the ones the rule gives for K = 4 and S = 2.
    {
        const Curve other = curve_of({3.0, 3.0, 3.0, 3.0, 3.0});
        EXPECT(other.size() == curve.size());
        const ReturnBlockLayout fresh = compute_block_layout(view_of(other), 2);
        EXPECT(reduce_return_blocks(view, fresh).status == ReturnBlocksStatus::Ok);
    }
}

// ---------------------------------------------------------------------------------------------
// Repetition
// ---------------------------------------------------------------------------------------------

void case_determinism() {
    g_case = "determinism";
    // The same curve reduced twice must give the same status, the same counts and bitwise the same
    // sums and moments: the reducer is a pure function of the view and the layout, and this case
    // exists to catch any hidden state, ordering dependence or thread-local it might grow. This
    // compares two runs of the function under test and expects equality; it is a determinism check
    // and not an oracle, and no literal is derived from it.
    Curve curve;
    for (std::size_t index = 0; index < 34; ++index) {
        const double drift = (static_cast<double>((index * 37) % 101) - 50.0) / 5000.0;
        curve.push_back(
            {static_cast<std::int64_t>(index) * kMillisecond, 100000.0 * (1.0 + drift), 0.0});
    }
    const Reduction first = reduce(curve, 16);
    const Reduction second = reduce(curve, 16);
    EXPECT(first.layout.usable && second.layout.usable);
    EXPECT(first.moments.status == second.moments.status);
    EXPECT_INTS(second.moments.count, first.moments.count);
    EXPECT_BITS_VECTOR(second.moments.sum, first.moments.sum);
    EXPECT_BITS_VECTOR(second.moments.m2, first.moments.m2);
    // The same input read through an independent copy of the curve, to catch a retained pointer or
    // an aliasing assumption about the caller's storage.
    const Curve copy = curve;
    const Reduction third = reduce(copy, 16);
    EXPECT_BITS_VECTOR(third.moments.sum, first.moments.sum);
    EXPECT_BITS_VECTOR(third.moments.m2, first.moments.m2);
}

// Two regimes of the rounded-arithmetic checker's later suite are named here so that the phase
// that owns that checker keeps them: "tiny spread around a large mean" (a centred pass that
// ignores the rounded mean's own error cannot cover it) and "constant series". This file tests a
// constant curve and a constant non-zero return above; the checker's sampled bound and its two
// named regimes belong to the curve-to-moments proof, not to this reducer's unit tests.

}  // namespace

int main() {
    if (!unit_contraction_free()) {
        std::cerr << "test_return_blocks must be built with -ffp-contract=off -fno-fast-math: this "
                     "translation unit fuses multiply-add, so its exact literals are not "
                     "comparable\n";
        return 2;
    }
    case_contract_surface();
    case_layout_rules();
    case_refused_layouts();
    case_layouts_are_independent_of_equity();
    case_boundary_times_are_copied();
    case_time_order_is_required();
    case_two_blocks_of_two();
    case_two_blocks_of_four();
    case_single_interval_blocks();
    case_constant_curves();
    case_near_constant_rounding();
    case_skipped_and_negative_equity();
    case_nonfinite_inputs();
    case_skipped_and_nonfinite_precedence();
    case_reducer_rejects_foreign_layouts();
    case_determinism();
    if (g_failures != 0) {
        std::cerr << g_failures << " of " << g_checks << " checks failed\n";
        return 1;
    }
    std::cout << "return_blocks: " << g_checks << " checks passed\n";
    return 0;
}
