// Native tests of the X return-statistics reducer (contract pineforge-hpo-return-stats/v1,
// legacy mode). This file was written without compiling or running anything: no claim of
// execution is made here. All native execution is scheduled on spot machines.
//
// Build this test, and src/core/return_stats.cpp, with -fno-fast-math -ffp-contract=off: the
// frozen bit patterns below assume two rounded operations for every multiply followed by an add.
//
// Evidence kinds in this file:
//   analytic   closed forms derived by hand (dyadic returns, exact sums);
//   frozen     binary64 bit patterns of the stand-alone reference transcription of the pin
//              (fixtures_x3.py of the author lane), which are NOT product measurements;
//   oracle     an independent straightforward re-implementation inside this file, compared
//              bit for bit on generated curves, with its own table-driven UTC calendar.
#include <pineforge/hpo/return_stats.hpp>

#include "../src/core/sha256.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <limits>
#include <string>
#include <string_view>
#include <thread>
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

void check_bits(int line, const char* what, double actual, double expected) {
    ++g_checks;
    if (!same_bits(actual, expected)) {
        ++g_failures;
        std::cerr << "FAIL [" << g_case << "] line " << line << ": " << what << " actual "
                  << std::hexfloat << actual << " expected " << expected << std::defaultfloat
                  << '\n';
    }
}

void check_near(int line, const char* what, double actual, double expected, double relative,
                double absolute = 0.0) {
    ++g_checks;
    const bool ok = std::isfinite(actual) && std::isfinite(expected) &&
                    std::fabs(actual - expected) <= relative * std::fabs(expected) + absolute;
    if (!ok) {
        ++g_failures;
        std::cerr << "FAIL [" << g_case << "] line " << line << ": " << what << " actual "
                  << std::setprecision(17) << actual << " expected " << expected << '\n';
    }
}

#define EXPECT(condition) check(__LINE__, (condition), #condition)
#define EXPECT_BITS(actual, expected) check_bits(__LINE__, #actual, (actual), (expected))
#define EXPECT_NEAR(actual, expected, relative) \
    check_near(__LINE__, #actual, (actual), (expected), (relative))
#define EXPECT_NULL(value) check(__LINE__, std::isnan(value), #value " is null")

// ---------------------------------------------------------------------------------------------
// Curves
// ---------------------------------------------------------------------------------------------

// Same shape as the engine's per-bar curve record: 24 bytes, extra member after the two used.
struct Point {
    std::int64_t time_ms;
    double equity;
    double open_profit;
};
using Curve = std::vector<Point>;

struct PointCompact {  // 16 bytes, no extra member
    std::int64_t time_ms;
    double equity;
};

struct PointReordered {  // time_ms and equity at other offsets than the engine's
    double open_profit;
    double equity;
    std::int64_t time_ms;
};

constexpr std::int64_t kHour = 3600000;
constexpr std::int64_t kDay = 86400000;
constexpr std::int64_t kBase = 1700000000000;  // 2023-11-14T22:13:20Z

Curve curve_of(const std::vector<double>& equities, std::int64_t start = kBase,
               std::int64_t step = kHour) {
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

std::vector<double> equity_from_returns(const std::vector<double>& returns) {
    std::vector<double> equity{100000.0};
    for (const double value : returns) {
        const double growth = 1.0 + value;
        equity.push_back(equity.back() * growth);
    }
    return equity;
}

std::vector<double> lcg_equity(int return_count) {
    std::vector<double> equity{100000.0};
    for (int index = 0; index < return_count; ++index) {
        const double value = static_cast<double>((index * 37) % 101 - 50) / 5000.0;
        const double growth = 1.0 + value;
        equity.push_back(equity.back() * growth);
    }
    return equity;
}

// ---------------------------------------------------------------------------------------------
// Running the reducer
// ---------------------------------------------------------------------------------------------

bool series_equal(const ReturnSeriesStats& a, const ReturnSeriesStats& b) {
    return a.status == b.status && a.count == b.count && a.skipped == b.skipped &&
           same_bits(a.periods_per_year, b.periods_per_year) && same_bits(a.mean, b.mean) &&
           same_bits(a.std_dev, b.std_dev) &&
           same_bits(a.sharpe_per_period, b.sharpe_per_period) && same_bits(a.skew, b.skew) &&
           same_bits(a.kurt_raw, b.kurt_raw) && a.period_failure == b.period_failure &&
           a.intervals == b.intervals && a.nonfinite_returns == b.nonfinite_returns;
}

// The published fields only: the oracle does not know the reducer's diagnostics.
bool series_fields_equal(const ReturnSeriesStats& a, const ReturnSeriesStats& b) {
    return a.status == b.status && a.count == b.count && a.skipped == b.skipped &&
           same_bits(a.periods_per_year, b.periods_per_year) && same_bits(a.mean, b.mean) &&
           same_bits(a.std_dev, b.std_dev) &&
           same_bits(a.sharpe_per_period, b.sharpe_per_period) && same_bits(a.skew, b.skew) &&
           same_bits(a.kurt_raw, b.kurt_raw);
}

// The contract invariants that hold for every series the reducer ever returns.
void check_invariants(int line, const ReturnSeriesStats& s) {
    ++g_checks;
    const int code = static_cast<int>(s.status);
    bool ok = s.status_invariant_holds();
    ok = ok && (code == 0 || code == 1 || code == 2 || code == 4 || code == 6 || code == 7 ||
                code == 8);
    ok = ok && (s.count.has_value() == s.skipped.has_value());
    if (s.count.has_value())
        ok = ok && (*s.count + *s.skipped + s.nonfinite_returns == s.intervals);
    if (s.status == ReturnStatsStatus::NonfiniteInput ||
        s.status == ReturnStatsStatus::UnsupportedTimezone) {
        ok = ok && !s.count.has_value() && std::isnan(s.periods_per_year) && std::isnan(s.mean) &&
             std::isnan(s.std_dev) && std::isnan(s.sharpe_per_period) && std::isnan(s.skew) &&
             std::isnan(s.kurt_raw);
    }
    if (s.status == ReturnStatsStatus::Ok)
        ok = ok && s.count.has_value() && *s.count >= 4;
    // The metric accessor agrees: every published value is finite exactly when every field is
    // (the status value itself is always a finite code).
    bool all_values_finite = true;
    for (std::size_t field = 0; field < kReturnStatisticCount; ++field) {
        const double published = s.value(static_cast<ReturnStatistic>(field));
        all_values_finite = all_values_finite && std::isfinite(published);
    }
    ok = ok && (all_values_finite == s.all_fields_finite());
    ok = ok && (s.value(ReturnStatistic::Status) == static_cast<double>(code));
    if (!ok)
        fail(line, "series invariant violated (status " + std::to_string(code) + ")");
}

ReturnStatsResult run(const Curve& curve, bool bar, bool monthly, std::string_view tz = {}) {
    ReturnStatsRequest request;
    request.bar = bar;
    request.monthly = monthly;
    request.chart_timezone = tz;
    const EquityPointsView view = make_equity_points_view(curve.data(), curve.size(),
                                                          &Point::time_ms, &Point::equity);
    return compute_return_stats(view, request);
}

ReturnSeriesStats bar_series(const Curve& curve, int line) {
    const ReturnStatsResult result = run(curve, true, false);
    check(line, result.bar.has_value() && !result.monthly.has_value(), "only bar was produced");
    if (!result.bar.has_value())
        return ReturnSeriesStats{};
    check_invariants(line, *result.bar);
    return *result.bar;
}

ReturnSeriesStats monthly_series(const Curve& curve, int line, std::string_view tz = {}) {
    const ReturnStatsResult result = run(curve, false, true, tz);
    check(line, result.monthly.has_value() && !result.bar.has_value(),
          "only monthly was produced");
    if (!result.monthly.has_value())
        return ReturnSeriesStats{};
    check_invariants(line, *result.monthly);
    return *result.monthly;
}

#define BAR(curve) bar_series((curve), __LINE__)
#define MONTHLY(curve) monthly_series((curve), __LINE__)

struct Frozen {
    std::int64_t count;
    std::int64_t skipped;
    double periods;
    double mean;
    double std_dev;
    double sharpe;
    double skew;
    double kurt;
    ReturnStatsStatus status;
};

void check_frozen(int line, const ReturnSeriesStats& s, const Frozen& e) {
    ++g_checks;
    if (!(s.count.has_value() && *s.count == e.count && s.skipped.has_value() &&
          *s.skipped == e.skipped && s.status == e.status))
        fail(line, "count, skipped or status differ from the frozen reference");
    check_bits(line, "periods_per_year", s.periods_per_year, e.periods);
    check_bits(line, "mean", s.mean, e.mean);
    check_bits(line, "std", s.std_dev, e.std_dev);
    check_bits(line, "sharpe_per_period", s.sharpe_per_period, e.sharpe);
    check_bits(line, "skew", s.skew, e.skew);
    check_bits(line, "kurt_raw", s.kurt_raw, e.kurt);
}

#define EXPECT_FROZEN(series, expected) check_frozen(__LINE__, (series), (expected))

bool little_endian() {
    const std::uint16_t probe = 1;
    unsigned char first = 0;
    std::memcpy(&first, &probe, 1);
    return first == 1;
}

std::string sha_of(const std::vector<double>& values) {
    return pineforge::hpo::detail::sha256(std::string_view(
        reinterpret_cast<const char*>(values.data()), values.size() * sizeof(double)));
}

// The same sanity probe as the product's, evaluated in this translation unit: the frozen bit
// patterns are meaningless when the test itself is built with floating-point contraction.
bool test_unit_contraction_free() {
    volatile double first_input = 1.0 + 0x1p-27;
    volatile double addend_input = -(1.0 + 0x1p-26);
    const double first = first_input;
    const double addend = addend_input;
    const double product = first * first;
    const double separate = addend + product;
    return separate == 0.0;
}

// ---------------------------------------------------------------------------------------------
// Names, timezone, probe, unusable views
// ---------------------------------------------------------------------------------------------

void case_metric_names() {
    g_case = "metric names";
    std::size_t seen = 0;
    for (std::size_t series = 0; series < 2; ++series) {
        for (std::size_t field = 0; field < kReturnStatisticCount; ++field) {
            const auto s = static_cast<ReturnSeries>(series);
            const auto f = static_cast<ReturnStatistic>(field);
            const auto parsed = parse_return_stats_metric(return_stats_metric_name(s, f));
            EXPECT(parsed.has_value());
            if (parsed.has_value()) {
                EXPECT(parsed->series == s);
                EXPECT(parsed->statistic == f);
            }
            ++seen;
        }
    }
    EXPECT(seen == kReturnStatsMetricCount);
    EXPECT(kReturnStatsMetricCount == 18);
    EXPECT(return_stats_metric_name(ReturnSeries::Bar, ReturnStatistic::Count) ==
           "returns.bar.count");
    EXPECT(return_stats_metric_name(ReturnSeries::Bar, ReturnStatistic::SharpePerPeriod) ==
           "returns.bar.sharpe_per_period");
    EXPECT(return_stats_metric_name(ReturnSeries::Monthly, ReturnStatistic::KurtRaw) ==
           "returns.monthly.kurt_raw");
    EXPECT(return_stats_metric_name(ReturnSeries::Monthly, ReturnStatistic::Status) ==
           "returns.monthly.status");
    EXPECT(return_stats_metric_name(ReturnSeries::Monthly, ReturnStatistic::Std) ==
           "returns.monthly.std");
    for (const char* name :
         {"", "returns", "returns.bar", "returns.bar.", "returns.bar.sharpe", "returns.bar.kurt",
          "returns.bar.kurtosis", "returns.weekly.count", "returns.bar.count ",
          " returns.bar.count", "Returns.bar.count", "returns.monthly.sharpe_annualized",
          "metrics.returns.bar.count"})
        EXPECT(!parse_return_stats_metric(name).has_value());
    EXPECT(kReturnStatsContract == "pineforge-hpo-return-stats/v1");
    EXPECT(kReturnStatsRiskFreeAnnual == 0.02);
}

void case_timezone_names() {
    g_case = "timezone names";
    EXPECT(is_utc_chart_timezone(""));
    EXPECT(is_utc_chart_timezone("UTC"));
    EXPECT(is_utc_chart_timezone("Etc/UTC"));
    for (const char* name : {"utc", "Etc/utc", "GMT", "Etc/UCT", "America/New_York", "UTC ", " UTC",
                             "Asia/Taipei", "UTC+8"})
        EXPECT(!is_utc_chart_timezone(name));
}

void case_probe() {
    g_case = "contraction probe";
    EXPECT(return_stats_contraction_free());
}

void case_not_requested() {
    g_case = "nothing requested";
    EquityPointsView unusable;  // would be refused if it were looked at
    unusable.records = nullptr;
    unusable.count = 5;
    unusable.stride = 16;
    unusable.time_offset = 0;
    unusable.equity_offset = 8;
    const ReturnStatsResult result = compute_return_stats(unusable, ReturnStatsRequest{});
    EXPECT(!result.bar.has_value());
    EXPECT(!result.monthly.has_value());
}

void case_unusable_views() {
    g_case = "unusable views";
    const Curve curve = curve_of({1.0, 2.0, 1.0, 2.0, 1.0});
    std::vector<EquityPointsView> views;
    EquityPointsView view;
    view.records = nullptr;
    view.count = 3;
    view.stride = 16;
    view.time_offset = 0;
    view.equity_offset = 8;
    views.push_back(view);  // null records with a nonzero count
    view.records = curve.data();
    view.count = curve.size();
    view.stride = 16;
    view.equity_offset = 12;  // the 8-byte equity does not fit in the stride
    views.push_back(view);
    view.stride = 0;  // zero stride
    view.equity_offset = 8;
    views.push_back(view);
    view.stride = sizeof(Point);
    view.time_offset = sizeof(Point) - 4;  // the 8-byte time does not fit
    views.push_back(view);
    view.time_offset = 0;
    view.count = static_cast<std::size_t>(std::uint64_t{1} << 53) + 1;  // not exactly countable
    views.push_back(view);
    for (const EquityPointsView& candidate : views) {
        ReturnStatsRequest request;
        request.bar = true;
        request.monthly = true;
        const ReturnStatsResult result = compute_return_stats(candidate, request);
        EXPECT(result.bar.has_value() && result.monthly.has_value());
        if (result.bar.has_value() && result.monthly.has_value()) {
            EXPECT(result.bar->status == ReturnStatsStatus::NonfiniteInput);
            EXPECT(result.monthly->status == ReturnStatsStatus::NonfiniteInput);
            EXPECT(!result.bar->count.has_value() && !result.monthly->count.has_value());
            check_invariants(__LINE__, *result.bar);
            check_invariants(__LINE__, *result.monthly);
        }
    }
}

void case_layouts() {
    g_case = "record layouts";
    const std::vector<double> equity = lcg_equity(60);
    const Curve engine_shaped = curve_of(equity);
    std::vector<PointCompact> compact;
    std::vector<PointReordered> reordered;
    for (std::size_t index = 0; index < equity.size(); ++index) {
        const std::int64_t time = kBase + static_cast<std::int64_t>(index) * kHour;
        compact.push_back({time, equity[index]});
        reordered.push_back({-1.0, equity[index], time});
    }
    ReturnStatsRequest request;
    request.bar = true;
    request.monthly = true;
    const ReturnStatsResult expected = compute_return_stats(
        make_equity_points_view(engine_shaped.data(), engine_shaped.size(), &Point::time_ms,
                                &Point::equity),
        request);
    const ReturnStatsResult from_compact = compute_return_stats(
        make_equity_points_view(compact.data(), compact.size(), &PointCompact::time_ms,
                                &PointCompact::equity),
        request);
    const ReturnStatsResult from_reordered = compute_return_stats(
        make_equity_points_view(reordered.data(), reordered.size(), &PointReordered::time_ms,
                                &PointReordered::equity),
        request);
    EXPECT(expected.bar.has_value() && from_compact.bar.has_value() &&
           from_reordered.bar.has_value());
    if (expected.bar.has_value() && from_compact.bar.has_value() &&
        from_reordered.bar.has_value() && expected.monthly.has_value() &&
        from_compact.monthly.has_value() && from_reordered.monthly.has_value()) {
        EXPECT(series_equal(*expected.bar, *from_compact.bar));
        EXPECT(series_equal(*expected.bar, *from_reordered.bar));
        EXPECT(series_equal(*expected.monthly, *from_compact.monthly));
        EXPECT(series_equal(*expected.monthly, *from_reordered.monthly));
    }
}

// ---------------------------------------------------------------------------------------------
// Frozen references (stand-alone transcription of the pin) and analytic fixtures
// ---------------------------------------------------------------------------------------------

void case_frozen_references() {
    g_case = "frozen references";
    // F1: the engine's per-bar oracle, one day apart.
    const Curve f1 = curve_of({1000.0, 1010.0, 999.9, 1009.899, 1019.99799}, kBase, kDay);
    EXPECT_FROZEN(BAR(f1),
                  (Frozen{4, 0, 0x1.6d40000000000p+8, 0x1.47ae147ae1480p-8, 0x1.47ae147ae1480p-7,
                          0x1.fa6493e34375ep-2, -0x1.279a74590331ap+0, 0x1.2aaaaaaaaaaa9p+1,
                          ReturnStatsStatus::Ok}));
    const ReturnSeriesStats f1_series = BAR(f1);
    EXPECT_NEAR(f1_series.sharpe_per_period * std::sqrt(f1_series.periods_per_year),
                9.451108474837675, 1e-14);  // engine sharpe_bar comment value

    // F2: four UTC months, noon on the fifteenth; T = 3 so the full set is not defined.
    const std::int64_t january = 1704067200000, february = 1706745600000, march = 1709251200000,
                       april = 1711929600000;
    const std::int64_t noon = 14 * kDay + 12 * kHour;
    const Curve f2 = curve_at({january + noon, february + noon, march + noon, april + noon},
                              {1000.0, 1100.0, 990.0, 1089.0});
    EXPECT_FROZEN(MONTHLY(f2),
                  (Frozen{3, 0, 0x1.8p+3, 0x1.111111111111bp-5, 0x1.d8f7208e6b830p-4,
                          0x1.18d2bb548fd65p-2, -0x1.6a09e667f3bd2p-1, kNaN,
                          ReturnStatsStatus::InsufficientCount}));

    // F3: alternating plus and minus one percent (T = 40) and {-1, 0, 1} percent cycled (T = 60).
    std::vector<double> alternating, triangle;
    for (int index = 0; index < 40; ++index)
        alternating.push_back(index % 2 == 0 ? 0.01 : -0.01);
    const std::array<double, 3> pattern = {-1.0, 0.0, 1.0};
    for (int index = 0; index < 60; ++index)
        triangle.push_back(pattern[static_cast<std::size_t>(index % 3)] * 0.01);
    EXPECT_FROZEN(BAR(curve_of(equity_from_returns(alternating))),
                  (Frozen{40, 0, 0x1.11f0000000000p+13, -0x1.3333333333333p-57,
                          0x1.4bdabc4e13333p-7, -0x1.d874900773830p-13, 0x1.e847fffffffd8p-55,
                          0x1.0000000000004p+0, ReturnStatsStatus::Ok}));
    EXPECT_FROZEN(BAR(curve_of(equity_from_returns(triangle))),
                  (Frozen{60, 0, 0x1.11f0000000000p+13, 0x1.1111111111111p-59,
                          0x1.0dceb48821b34p-7, -0x1.228d69753731bp-12, 0x1.aa16d71156a21p-52,
                          0x1.7fffffffffffcp+0, ReturnStatsStatus::Ok}));

    // F4: deterministic generated curves; the input hash separates "different input" from
    // "different reducer".
    const std::vector<double> f4_60 = lcg_equity(60);
    const std::vector<double> f4_1000 = lcg_equity(1000);
    if (little_endian()) {
        EXPECT(sha_of(f4_60) == "4bfe128145d89277727e8cfd0ace357f314581943cfd88036123d600db2c76a0");
        EXPECT(sha_of(f4_1000) ==
               "41eac7e57adfb984c4d1291227ec38973f5921c645357d1db8a3ecbe0d549702");
    }
    EXPECT_FROZEN(BAR(curve_of(f4_60)),
                  (Frozen{60, 0, 0x1.11f0000000000p+13, -0x1.95730b012c889p-14,
                          0x1.81485ed7a3c99p-8, -0x1.13c213d6d38d0p-6, 0x1.b4bc2ec868f60p-9,
                          0x1.cd01143c5c47dp+0, ReturnStatsStatus::Ok}));
    EXPECT_FROZEN(BAR(curve_of(f4_1000)),
                  (Frozen{1000, 0, 0x1.11f0000000000p+13, 0x1.0c6f7a0b5a7f0p-19,
                          0x1.7ea74d0356ec3p-8, -0x1.947d71d0cf50ap-15, -0x1.73854b1675264p-13,
                          0x1.cc9cf8a5db372p+0, ReturnStatsStatus::Ok}));
}

void case_minima() {
    g_case = "minima";
    // No point and one point: nothing to count, the bar period needs three points, the monthly
    // period is the constant 12 whenever the series can be formed.
    for (const Curve& curve : {Curve{}, curve_of({5.0})}) {
        const ReturnSeriesStats bar = BAR(curve);
        EXPECT(bar.count == 0 && bar.skipped == 0 && bar.intervals == 0);
        EXPECT(bar.status == ReturnStatsStatus::PeriodUnavailable);
        EXPECT(bar.period_failure == ReturnPeriodFailure::TooFewPoints);
        EXPECT_NULL(bar.periods_per_year);
        EXPECT_NULL(bar.mean);
        const ReturnSeriesStats monthly = MONTHLY(curve);
        EXPECT(monthly.count == 0 && monthly.skipped == 0);
        EXPECT_BITS(monthly.periods_per_year, 12.0);
        EXPECT(monthly.status == ReturnStatsStatus::InsufficientCount);
    }
    // Two points, one valid interval: T = 1 although the period is undefined (n < 3).
    {
        const ReturnSeriesStats bar = BAR(curve_of({1.0, 2.0}));
        EXPECT(bar.count == 1 && bar.skipped == 0);
        EXPECT_BITS(bar.mean, 1.0);
        EXPECT_NULL(bar.periods_per_year);
        EXPECT_NULL(bar.std_dev);
        EXPECT_NULL(bar.sharpe_per_period);
        EXPECT(bar.status == ReturnStatsStatus::PeriodUnavailable);
        const std::int64_t january = 1704067200000, february = 1706745600000;
        const ReturnSeriesStats two_months = MONTHLY(curve_at({january, february}, {1.0, 2.0}));
        EXPECT(two_months.count == 1 && two_months.skipped == 0);
        EXPECT_BITS(two_months.mean, 1.0);
        EXPECT_BITS(two_months.periods_per_year, 12.0);
        EXPECT_NULL(two_months.std_dev);
        EXPECT(two_months.status == ReturnStatsStatus::InsufficientCount);
        const ReturnSeriesStats one_month =
            MONTHLY(curve_at({january, january + kDay}, {1.0, 2.0}));
        EXPECT(one_month.count == 0 && one_month.skipped == 0);
        EXPECT_NULL(one_month.mean);
        EXPECT(one_month.status == ReturnStatsStatus::InsufficientCount);
    }
    // Two points whose prior equity is not positive: the only interval is skipped.
    {
        const ReturnSeriesStats bar = BAR(curve_of({0.0, 5.0}));
        EXPECT(bar.count == 0 && bar.skipped == 1);
        EXPECT_NULL(bar.mean);
        EXPECT(bar.status == ReturnStatsStatus::PeriodUnavailable);
    }
    // T = 2: mean, std and the Sharpe ratio exist; skew and kurtosis do not. Returns {1, -1/2}.
    {
        const ReturnSeriesStats s = BAR(curve_of({1.0, 2.0, 1.0}));
        EXPECT(s.count == 2 && s.skipped == 0);
        EXPECT_BITS(s.mean, 0.25);
        EXPECT_BITS(s.std_dev, std::sqrt(1.125));  // S2 = 2 * 0.75^2 = 1.125 exactly
        EXPECT(std::isfinite(s.sharpe_per_period));
        EXPECT_NULL(s.skew);
        EXPECT_NULL(s.kurt_raw);
        EXPECT(s.status == ReturnStatsStatus::InsufficientCount);
    }
    // T = 3: skew exists, kurtosis does not. Returns {1, -1/2, 1}: skew = -1/sqrt(2).
    {
        const ReturnSeriesStats s = BAR(curve_of({1.0, 2.0, 1.0, 2.0}));
        EXPECT(s.count == 3);
        EXPECT_BITS(s.mean, 0.5);
        EXPECT_BITS(s.std_dev, std::sqrt(0.75));
        EXPECT_NEAR(s.skew, -1.0 / std::sqrt(2.0), 1e-15);
        EXPECT_NULL(s.kurt_raw);
        EXPECT(s.status == ReturnStatsStatus::InsufficientCount);
    }
    // T = 4: the full set. Returns {1, -1/2, 1, -1/2} are dyadic, so the moments are exact.
    {
        const ReturnSeriesStats s = BAR(curve_of({1.0, 2.0, 1.0, 2.0, 1.0}));
        EXPECT(s.count == 4 && s.skipped == 0 && s.intervals == 4);
        EXPECT_BITS(s.mean, 0.25);
        EXPECT_BITS(s.std_dev, std::sqrt(0.75));
        EXPECT_BITS(s.skew, 0.0);
        EXPECT_BITS(s.kurt_raw, 1.0);
        EXPECT_NEAR(s.periods_per_year, 8766.0, 1e-14);
        EXPECT_NEAR(s.sharpe_per_period, (0.25 - 0.02 / 8766.0) / std::sqrt(0.75), 1e-13);
        EXPECT(s.status == ReturnStatsStatus::Ok);
        EXPECT_BITS(s.value(ReturnStatistic::Count), 4.0);
        EXPECT_BITS(s.value(ReturnStatistic::Status), 0.0);
        EXPECT_BITS(s.value(ReturnStatistic::KurtRaw), 1.0);
    }
}

void case_undefined_period() {
    g_case = "undefined period";
    const std::vector<double> equity = {1.0, 2.0, 1.0, 2.0, 1.0, 2.0};  // T = 5
    const ReturnSeriesStats reference = BAR(curve_of(equity));
    EXPECT(reference.status == ReturnStatsStatus::Ok);

    std::vector<Curve> bad_bases;
    bad_bases.push_back(curve_of(equity, kBase, 0));  // identical timestamps
    bad_bases.push_back(curve_of(equity, kBase, -kHour));  // decreasing timestamps
    const std::int64_t newest = std::numeric_limits<std::int64_t>::max();
    const std::int64_t oldest = std::numeric_limits<std::int64_t>::min();
    bad_bases.push_back(curve_at({-1, 0, 1, 2, 3, newest}, equity));  // last - first overflows
    bad_bases.push_back(curve_at({oldest, 0, 1, 2, 3, 4}, equity));   // last - first overflows
    const ReturnPeriodFailure expected_failure[] = {
        ReturnPeriodFailure::NonPositiveSpan, ReturnPeriodFailure::NonPositiveSpan,
        ReturnPeriodFailure::SpanOverflow, ReturnPeriodFailure::SpanOverflow};
    for (std::size_t index = 0; index < bad_bases.size(); ++index) {
        const ReturnSeriesStats s = BAR(bad_bases[index]);
        EXPECT(s.status == ReturnStatsStatus::PeriodUnavailable);
        EXPECT(s.period_failure == expected_failure[index]);
        EXPECT_NULL(s.periods_per_year);
        EXPECT_NULL(s.sharpe_per_period);
        // Everything that does not need the period is unchanged and still published.
        EXPECT(s.count == reference.count && s.skipped == reference.skipped);
        EXPECT_BITS(s.mean, reference.mean);
        EXPECT_BITS(s.std_dev, reference.std_dev);
        EXPECT_BITS(s.skew, reference.skew);
        EXPECT_BITS(s.kurt_raw, reference.kurt_raw);
    }
    // The largest representable span is a valid, tiny period, not an overflow.
    {
        const ReturnSeriesStats s = BAR(curve_at(
            {-1, 0, 1, 2, 3, std::numeric_limits<std::int64_t>::max() - 1}, equity));
        EXPECT(s.period_failure == ReturnPeriodFailure::None);
        EXPECT(std::isfinite(s.periods_per_year) && s.periods_per_year > 0.0);
        EXPECT(s.status == ReturnStatsStatus::Ok);
    }
    // Negative epochs are not rejected for their sign: ten days before 1970, one day apart.
    {
        const ReturnSeriesStats s = BAR(curve_of(equity, -10 * kDay, kDay));
        EXPECT(s.status == ReturnStatsStatus::Ok);
        EXPECT_NEAR(s.periods_per_year, 365.25, 1e-14);
        EXPECT_BITS(s.mean, reference.mean);
    }
}

void case_zero_variance() {
    g_case = "zero variance";
    // Doubling every bar gives return exactly 1 each time: variance is exactly zero.
    const ReturnSeriesStats five = BAR(curve_of({1.0, 2.0, 4.0, 8.0, 16.0, 32.0}));
    EXPECT(five.count == 5);
    EXPECT_BITS(five.mean, 1.0);
    EXPECT_BITS(five.std_dev, 0.0);  // zero std is a valid finite value
    EXPECT_NULL(five.sharpe_per_period);
    EXPECT_NULL(five.skew);
    EXPECT_NULL(five.kurt_raw);
    EXPECT(five.status == ReturnStatsStatus::ZeroVariance);
    // The same series with only three returns: the count shortfall takes precedence.
    const ReturnSeriesStats three = BAR(curve_of({1.0, 2.0, 4.0, 8.0}));
    EXPECT(three.count == 3);
    EXPECT_BITS(three.std_dev, 0.0);
    EXPECT(three.status == ReturnStatsStatus::InsufficientCount);
    // A flat curve: every return is exactly zero.
    const ReturnSeriesStats flat = BAR(curve_of(std::vector<double>(50, 100000.0), kBase, kDay));
    EXPECT(flat.count == 49 && flat.skipped == 0);
    EXPECT_BITS(flat.mean, 0.0);
    EXPECT_BITS(flat.std_dev, 0.0);
    EXPECT_NULL(flat.sharpe_per_period);
    EXPECT(flat.status == ReturnStatsStatus::ZeroVariance);
}

void case_skipped_returns() {
    g_case = "skipped returns";
    // Equity {4, -4, 2, 4, 8, 16, 8}: the interval out of -4 is skipped, the one into -4 is kept
    // (a current equity at or below zero is allowed). Returns {-2, 1, 1, 1, -1/2}: mean 1/10,
    // S2 = 7.2, S3 = -7.29, S4 = 21.546, so std = sqrt(1.8), skew = -27/32, kurtosis = 133/64.
    {
        const ReturnSeriesStats s = BAR(curve_of({4.0, -4.0, 2.0, 4.0, 8.0, 16.0, 8.0}));
        EXPECT(s.count == 5 && s.skipped == 1 && s.intervals == 6 && s.nonfinite_returns == 0);
        EXPECT_NEAR(s.mean, 0.1, 1e-15);
        EXPECT_NEAR(s.std_dev, std::sqrt(1.8), 1e-14);
        EXPECT_NEAR(s.skew, -0.84375, 1e-13);
        EXPECT_NEAR(s.kurt_raw, 2.078125, 1e-13);
        EXPECT_NEAR(s.periods_per_year, 8766.0, 1e-14);
        // Skips are data: every field is finite, so the status is zero.
        EXPECT(s.all_fields_finite());
        EXPECT(s.status == ReturnStatsStatus::Ok);
    }
    // A leading non-positive prior is skipped too, and the rest is fully finite.
    {
        const ReturnSeriesStats s = BAR(curve_of({0.0, 3.0, 6.0, 12.0, 6.0, 12.0, 24.0}));
        EXPECT(s.count == 5 && s.skipped == 1);
        EXPECT(s.status == ReturnStatsStatus::Ok);
    }
    // Nothing valid at all: four non-positive priors.
    {
        const ReturnSeriesStats s = BAR(curve_of({0.0, 0.0, -1.0, 5.0}));
        EXPECT(s.count == 0 && s.skipped == 3);
        EXPECT_NULL(s.mean);
        EXPECT(s.status == ReturnStatsStatus::InsufficientCount);
    }
    // A negative zero prior is not positive either.
    {
        const ReturnSeriesStats s = BAR(curve_of({-0.0, 1.0, 2.0}));
        EXPECT(s.count == 1 && s.skipped == 1);
    }
}

void case_nonfinite_input() {
    g_case = "nonfinite input";
    const std::vector<double> base = {1.0, 2.0, 1.0, 2.0, 1.0, 2.0};
    for (const double bad : {kNaN, kInf, -kInf}) {
        for (const std::size_t position : {std::size_t{0}, std::size_t{2}, std::size_t{5}}) {
            std::vector<double> equity = base;
            equity[position] = bad;
            const Curve curve = curve_of(equity, kBase, 15 * kDay);
            const ReturnStatsResult result = run(curve, true, true);
            EXPECT(result.bar.has_value() && result.monthly.has_value());
            if (!result.bar.has_value() || !result.monthly.has_value())
                continue;
            for (const ReturnSeriesStats* s : {&*result.bar, &*result.monthly}) {
                check_invariants(__LINE__, *s);
                EXPECT(s->status == ReturnStatsStatus::NonfiniteInput);
                EXPECT(!s->count.has_value() && !s->skipped.has_value());
                EXPECT_NULL(s->mean);
                EXPECT_NULL(s->periods_per_year);
            }
            // Nonfinite input outranks the unsupported-timezone status.
            const ReturnSeriesStats zoned = monthly_series(curve, __LINE__, "America/New_York");
            EXPECT(zoned.status == ReturnStatsStatus::NonfiniteInput);
        }
    }
}

void case_derived_nonfinite() {
    g_case = "derived nonfinite";
    const double tiny = std::numeric_limits<double>::denorm_min();
    // A return overflows (1 / denorm_min = inf): it is neither counted nor skipped, and no
    // statistic of the remaining subset is published.
    {
        const ReturnSeriesStats s = BAR(curve_of({tiny, 1.0, 1.0, 1.0, 1.0, 1.0}));
        EXPECT(s.count == 4 && s.skipped == 0 && s.nonfinite_returns == 1 && s.intervals == 5);
        EXPECT(s.status == ReturnStatsStatus::DerivedNonfinite);
        EXPECT_NULL(s.mean);
        EXPECT_NULL(s.std_dev);
        EXPECT_NULL(s.sharpe_per_period);
        EXPECT_NULL(s.skew);
        EXPECT_NULL(s.kurt_raw);
        EXPECT(std::isfinite(s.periods_per_year));
    }
    // The nonfinite return outranks an unavailable period (n = 2).
    {
        const ReturnSeriesStats s = BAR(curve_of({tiny, 1.0}));
        EXPECT(s.count == 0 && s.nonfinite_returns == 1);
        EXPECT(s.status == ReturnStatsStatus::DerivedNonfinite);
        EXPECT(s.period_failure == ReturnPeriodFailure::TooFewPoints);
    }
    // The sum of finite returns overflows: the mean and everything after it is null.
    {
        const ReturnSeriesStats s = BAR(curve_of({1e-8, 1e300, 1e-8, 1e300}));
        EXPECT(s.count == 3 && s.nonfinite_returns == 0);
        EXPECT(s.status == ReturnStatsStatus::DerivedNonfinite);
        EXPECT_NULL(s.mean);
        EXPECT_NULL(s.std_dev);
        EXPECT_NULL(s.skew);
        EXPECT_NULL(s.kurt_raw);
    }
    // S2 overflows while the mean is finite: the mean stays published, the rest is null.
    {
        const ReturnSeriesStats s = BAR(curve_of({1.0, 1e160, 1.0, 1e160, 1.0, 1e160}));
        EXPECT(s.count == 5);
        EXPECT(s.status == ReturnStatsStatus::DerivedNonfinite);
        EXPECT_NEAR(s.mean, 6e159, 1e-12);
        EXPECT_NULL(s.std_dev);
        EXPECT_NULL(s.sharpe_per_period);
        EXPECT_NULL(s.skew);
        EXPECT_NULL(s.kurt_raw);
    }
    // Only the fourth power overflows (d^4 > DBL_MAX while d^3 and d^2 are finite): kurtosis is
    // null, every statistic that does not need S4 is published. Returns {+1e80, -1, -1e80} twice,
    // each -1e80 followed by a skipped interval.
    {
        const ReturnSeriesStats s =
            BAR(curve_of({1.0, 1e80, 1.0, -1e80, 1.0, 1e80, 1.0, -1e80, 1.0}));
        EXPECT(s.count == 6 && s.skipped == 2 && s.intervals == 8);
        EXPECT(s.status == ReturnStatsStatus::DerivedNonfinite);
        EXPECT_BITS(s.mean, 0.0);
        EXPECT(std::isfinite(s.std_dev) && s.std_dev > 0.0);
        EXPECT(std::isfinite(s.sharpe_per_period));
        EXPECT(std::isfinite(s.skew) && std::fabs(s.skew) < 1e-9);
        EXPECT_NULL(s.kurt_raw);
    }
    // Underflow of a ratio is a valid return of -1: equity falls from 1e300 to a denormal.
    {
        const ReturnSeriesStats s =
            BAR(curve_of({1e300, 1e-320, 1e-320, 1e-320, 1e-320, 1e-320}));
        EXPECT(s.count == 5 && s.nonfinite_returns == 0);
        EXPECT_NEAR(s.mean, -0.2, 1e-15);
        EXPECT_NEAR(s.std_dev, std::sqrt(0.2), 1e-14);
        EXPECT_NEAR(s.skew, -1.5, 1e-13);
        EXPECT_NEAR(s.kurt_raw, 3.25, 1e-13);
        EXPECT(s.status == ReturnStatsStatus::Ok);
    }
    // Power-of-two rescaling of the whole curve changes no ratio: results are bit-identical.
    {
        const ReturnSeriesStats reference = BAR(curve_of({1.0, 2.0, 1.0, 2.0, 1.0}));
        for (const int exponent : {996, -1000, 0}) {
            const ReturnSeriesStats scaled = BAR(curve_of({std::ldexp(1.0, exponent),
                                                           std::ldexp(2.0, exponent),
                                                           std::ldexp(1.0, exponent),
                                                           std::ldexp(2.0, exponent),
                                                           std::ldexp(1.0, exponent)}));
            EXPECT(series_equal(scaled, reference));
        }
    }
}

// ---------------------------------------------------------------------------------------------
// UTC months
// ---------------------------------------------------------------------------------------------

struct MonthSpan {
    std::int64_t first_ms;
    std::int64_t last_ms;
};

// One point on the first and one on the last instant of every month. Month k ends at 100 * 2^k
// and opens at three times that, so every monthly return is exactly 1 if, and only if, both
// boundary instants of each month fall into that month's bucket.
void check_month_chain(const char* name, const std::vector<MonthSpan>& months) {
    g_case = name;
    std::vector<std::int64_t> times;
    std::vector<double> equity;
    double month_end = 100.0;
    for (const MonthSpan& month : months) {
        times.push_back(month.first_ms);
        equity.push_back(3.0 * month_end);
        times.push_back(month.last_ms);
        equity.push_back(month_end);
        month_end *= 2.0;
    }
    const ReturnSeriesStats s = MONTHLY(curve_at(times, equity));
    const std::int64_t returns = static_cast<std::int64_t>(months.size()) - 1;
    EXPECT(s.count.has_value() && *s.count == returns);
    EXPECT(s.skipped.has_value() && *s.skipped == 0);
    EXPECT_BITS(s.periods_per_year, 12.0);
    EXPECT_BITS(s.mean, 1.0);
    if (returns >= 2)
        EXPECT_BITS(s.std_dev, 0.0);
}

void case_month_boundaries() {
    check_month_chain("months 2023-12..2024-03 (year change, leap February)",
                      {{1701388800000, 1704067199999},
                       {1704067200000, 1706745599999},
                       {1706745600000, 1709251199999},
                       {1709251200000, 1711929599999}});
    check_month_chain("months 1969-11..1970-02 (negative epochs, floor not truncation)",
                      {{-5270400000, -2678400001},
                       {-2678400000, -1},
                       {0, 2678399999},
                       {2678400000, 5097599999}});
    check_month_chain("months 1900-02..1900-03 (century, not a leap year)",
                      {{-2206310400000, -2203891200001}, {-2203891200000, -2201212800001}});
    check_month_chain("months 2000-02..2000-03 (century, leap year)",
                      {{949363200000, 951868799999}, {951868800000, 954547199999}});
    check_month_chain("months 2100-02..2100-03 (century, not a leap year)",
                      {{4105123200000, 4107542399999}, {4107542400000, 4110220799999}});

    g_case = "month buckets are maximal runs";
    // A curve that is not time ordered forms one bucket per maximal run (as the engine's walk).
    const std::int64_t january = 1704067200000, february = 1706745600000;
    const ReturnSeriesStats s = MONTHLY(curve_at(
        {january + kDay, february + kDay, january + 2 * kDay, february + 2 * kDay},
        {100.0, 200.0, 400.0, 800.0}));
    EXPECT(s.count == 3 && s.skipped == 0);
    EXPECT_BITS(s.mean, 1.0);
}

void case_utc_range() {
    g_case = "UTC range";
    const std::int64_t lowest = kReturnStatsUtcMinMs, highest = kReturnStatsUtcMaxMs;
    {
        const ReturnSeriesStats s = MONTHLY(curve_at({lowest, highest}, {1.0, 2.0}));
        EXPECT(s.count == 1 && s.period_failure == ReturnPeriodFailure::None);
        EXPECT_BITS(s.mean, 1.0);
        EXPECT(s.status == ReturnStatsStatus::InsufficientCount);
    }
    const std::int64_t outside[] = {highest + 1, lowest - 1,
                                    std::numeric_limits<std::int64_t>::max(),
                                    std::numeric_limits<std::int64_t>::min()};
    for (const std::int64_t time : outside) {
        // Out-of-range timestamp at the start, in the middle and at the end of the curve.
        for (std::size_t position = 0; position < 3; ++position) {
            std::vector<std::int64_t> times = {0, kDay, 2 * kDay};
            times[position] = time;
            const ReturnSeriesStats s = MONTHLY(curve_at(times, {1.0, 2.0, 4.0}));
            EXPECT(s.status == ReturnStatsStatus::PeriodUnavailable);
            EXPECT(s.period_failure == ReturnPeriodFailure::UtcRange);
            EXPECT(!s.count.has_value() && !s.skipped.has_value());
            EXPECT_NULL(s.periods_per_year);
            EXPECT_NULL(s.mean);
        }
    }
    // The bar series of the same curve does not need a calendar and is unaffected.
    const ReturnStatsResult both = run(curve_at({0, kDay, std::numeric_limits<std::int64_t>::max()},
                                                {1.0, 2.0, 4.0}), true, true);
    EXPECT(both.bar.has_value() && both.monthly.has_value());
    if (both.bar.has_value() && both.monthly.has_value()) {
        EXPECT(both.bar->count == 2);
        EXPECT(both.monthly->period_failure == ReturnPeriodFailure::UtcRange);
    }
}

void case_timezone_monthly() {
    g_case = "monthly timezone";
    const std::int64_t january = 1704067200000;
    const Curve curve = curve_at({january, january + 31 * kDay, january + 60 * kDay,
                                  january + 91 * kDay, january + 121 * kDay},
                                 {1.0, 2.0, 1.0, 2.0, 1.0});
    const ReturnStatsResult zoned = run(curve, true, true, "America/New_York");
    EXPECT(zoned.bar.has_value() && zoned.monthly.has_value());
    if (zoned.bar.has_value() && zoned.monthly.has_value()) {
        check_invariants(__LINE__, *zoned.bar);
        check_invariants(__LINE__, *zoned.monthly);
        EXPECT(zoned.bar->status != ReturnStatsStatus::UnsupportedTimezone);
        EXPECT(zoned.bar->count.has_value());
        EXPECT(zoned.monthly->status == ReturnStatsStatus::UnsupportedTimezone);
        EXPECT(!zoned.monthly->count.has_value());
        EXPECT_NULL(zoned.monthly->periods_per_year);
    }
    const ReturnSeriesStats empty = MONTHLY(curve);
    const ReturnSeriesStats utc = monthly_series(curve, __LINE__, "UTC");
    const ReturnSeriesStats etc_utc = monthly_series(curve, __LINE__, "Etc/UTC");
    EXPECT(series_equal(empty, utc));
    EXPECT(series_equal(empty, etc_utc));
    EXPECT(empty.status != ReturnStatsStatus::UnsupportedTimezone);
}

// ---------------------------------------------------------------------------------------------
// Status precedence and the iff invariant
// ---------------------------------------------------------------------------------------------

void case_status_invariant() {
    g_case = "status invariant";
    ReturnSeriesStats s;  // the null series
    EXPECT(s.status == ReturnStatsStatus::NonfiniteInput);
    EXPECT(s.status_invariant_holds());
    // An undefined field behind status 0 is never published as qualified.
    s.status = ReturnStatsStatus::Ok;
    EXPECT(!s.status_invariant_holds());
    s.enforce_status_invariant();
    EXPECT(s.status == ReturnStatsStatus::DerivedNonfinite);
    EXPECT(s.status_invariant_holds());
    // A nonzero status with a null field is left alone ...
    s.status = ReturnStatsStatus::InsufficientCount;
    s.enforce_status_invariant();
    EXPECT(s.status == ReturnStatsStatus::InsufficientCount);
    // ... and an all-finite series is status 0 whatever status it was given.
    s.count = 10;
    s.skipped = 0;
    s.periods_per_year = 8766.0;
    s.mean = 0.001;
    s.std_dev = 0.01;
    s.sharpe_per_period = 0.1;
    s.skew = -0.2;
    s.kurt_raw = 3.1;
    EXPECT(s.all_fields_finite());
    EXPECT(!s.status_invariant_holds());
    s.enforce_status_invariant();
    EXPECT(s.status == ReturnStatsStatus::Ok);
    EXPECT(s.status_invariant_holds());
    s.kurt_raw = kInf;  // an infinity is not a finite field
    EXPECT(!s.all_fields_finite());
}

void case_precedence() {
    g_case = "status precedence";
    const double tiny = std::numeric_limits<double>::denorm_min();
    // 4 over 7 is covered in the nonfinite-input case. 8 over 6 in the derived case.
    // 6 over 1: one valid return but no period.
    EXPECT(BAR(curve_of({1.0, 2.0})).status == ReturnStatsStatus::PeriodUnavailable);
    // 1 over 2: zero variance with fewer than four returns.
    EXPECT(BAR(curve_of({5.0, 5.0, 5.0, 5.0})).status == ReturnStatsStatus::InsufficientCount);
    // 6 over 2: zero variance, enough returns, but the period is undefined.
    EXPECT(BAR(curve_of({5.0, 5.0, 5.0, 5.0, 5.0, 5.0}, kBase, 0)).status ==
           ReturnStatsStatus::PeriodUnavailable);
    // 8 over 6 and over 2.
    EXPECT(BAR(curve_of({tiny, 1.0, 1.0, 1.0, 1.0, 1.0}, kBase, 0)).status ==
           ReturnStatsStatus::DerivedNonfinite);
}

// ---------------------------------------------------------------------------------------------
// Repeats and concurrency
// ---------------------------------------------------------------------------------------------

void case_repeats_and_threads() {
    g_case = "repeats and threads";
    const Curve curve = curve_of(lcg_equity(1000));
    const ReturnStatsResult baseline = run(curve, true, true);
    EXPECT(baseline.bar.has_value() && baseline.monthly.has_value());
    if (!baseline.bar.has_value() || !baseline.monthly.has_value())
        return;
    for (int repeat = 0; repeat < 3; ++repeat) {
        const ReturnStatsResult again = run(curve, true, true);
        EXPECT(again.bar.has_value() && series_equal(*again.bar, *baseline.bar));
        EXPECT(again.monthly.has_value() && series_equal(*again.monthly, *baseline.monthly));
    }
    // Eight threads share the immutable curve; every result must match the serial one bit for bit.
    std::atomic<int> mismatches{0};
    std::vector<std::thread> workers;
    for (int worker = 0; worker < 8; ++worker) {
        workers.emplace_back([&curve, &baseline, &mismatches] {
            for (int iteration = 0; iteration < 25; ++iteration) {
                const ReturnStatsResult result = run(curve, true, true);
                if (!result.bar.has_value() || !result.monthly.has_value() ||
                    !series_equal(*result.bar, *baseline.bar) ||
                    !series_equal(*result.monthly, *baseline.monthly))
                    mismatches.fetch_add(1);
            }
        });
    }
    for (std::thread& worker : workers)
        worker.join();
    EXPECT(mismatches.load() == 0);
}

// ---------------------------------------------------------------------------------------------
// Differential sweep against an independent oracle
// ---------------------------------------------------------------------------------------------

class Lcg {
public:
    explicit Lcg(std::uint64_t seed) : state_(seed) {}
    std::uint64_t next() {
        state_ = state_ * 6364136223846793005ULL + 1442695040888963407ULL;
        return state_ >> 11;
    }
    std::uint64_t below(std::uint64_t bound) { return next() % bound; }
    double unit() { return static_cast<double>(next() % 1000000007ULL) / 1000000007.0; }

private:
    std::uint64_t state_;
};

bool oracle_leap(std::int64_t year) {
    return (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
}

int oracle_month_length(std::int64_t year, int month) {
    static const int lengths[12] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    return lengths[month] + ((month == 1 && oracle_leap(year)) ? 1 : 0);
}

// Calendar by walking years and months from 1970-01-01; shares nothing with the product.
std::int64_t oracle_month_id(std::int64_t time_ms) {
    std::int64_t days = time_ms / kDay;
    if (time_ms % kDay < 0)
        --days;
    std::int64_t year = 1970;
    while (days < 0) {
        --year;
        days += oracle_leap(year) ? 366 : 365;
    }
    while (days >= (oracle_leap(year) ? 366 : 365)) {
        days -= oracle_leap(year) ? 366 : 365;
        ++year;
    }
    int month = 0;
    while (days >= oracle_month_length(year, month)) {
        days -= oracle_month_length(year, month);
        ++month;
    }
    return year * 12 + month;
}

std::vector<std::int64_t> oracle_month_starts(std::int64_t first_year, std::int64_t last_year) {
    std::int64_t days = 0;
    for (std::int64_t year = 1970; year < first_year; ++year)
        days += oracle_leap(year) ? 366 : 365;
    for (std::int64_t year = first_year; year < 1970; ++year)
        days -= oracle_leap(year) ? 366 : 365;
    std::vector<std::int64_t> starts;
    for (std::int64_t year = first_year; year <= last_year; ++year) {
        for (int month = 0; month < 12; ++month) {
            starts.push_back(days * kDay);
            days += oracle_month_length(year, month);
        }
    }
    return starts;
}

std::vector<double> oracle_month_ends(const Curve& curve) {
    std::vector<double> ends;
    if (curve.empty())
        return ends;
    std::int64_t current = oracle_month_id(curve[0].time_ms);
    double last = curve[0].equity;
    for (std::size_t index = 1; index < curve.size(); ++index) {
        const std::int64_t id = oracle_month_id(curve[index].time_ms);
        if (id != current) {
            ends.push_back(last);
            current = id;
        }
        last = curve[index].equity;
    }
    ends.push_back(last);
    return ends;
}

// The pin, written the long way over a vector of simple returns. Moderate magnitudes only.
ReturnSeriesStats oracle_stats(const std::vector<double>& points, double periods_per_year) {
    std::vector<double> returns;
    std::int64_t skipped = 0;
    for (std::size_t index = 1; index < points.size(); ++index) {
        if (points[index - 1] > 0.0)
            returns.push_back(points[index] / points[index - 1] - 1.0);
        else
            ++skipped;
    }
    ReturnSeriesStats out;
    const std::size_t total = returns.size();
    out.count = static_cast<std::int64_t>(total);
    out.skipped = skipped;
    out.periods_per_year = periods_per_year;
    double s2 = 0.0;
    if (total >= 1) {
        double sum = 0.0;
        for (const double value : returns)
            sum += value;
        const double mean = sum / static_cast<double>(total);
        out.mean = mean;
        double s3 = 0.0, s4 = 0.0;
        for (const double value : returns) {
            const double d = value - mean;
            const double d2 = d * d;
            const double d3 = d2 * d;
            const double d4 = d2 * d2;
            s2 += d2;
            s3 += d3;
            s4 += d4;
        }
        if (total >= 2) {
            out.std_dev = std::sqrt(s2 / static_cast<double>(total - 1));
            if (out.std_dev > 0.0 && std::isfinite(periods_per_year)) {
                const double risk_free = 0.02 / periods_per_year;
                out.sharpe_per_period = (mean - risk_free) / out.std_dev;
            }
        }
        if (total >= 3 && s2 > 0.0) {
            const double m2 = s2 / static_cast<double>(total);
            out.skew = (s3 / static_cast<double>(total)) / (m2 * std::sqrt(m2));
        }
        if (total >= 4 && s2 > 0.0) {
            const double m2 = s2 / static_cast<double>(total);
            out.kurt_raw = (s4 / static_cast<double>(total)) / (m2 * m2);
        }
    }
    if (!std::isfinite(periods_per_year))
        out.status = ReturnStatsStatus::PeriodUnavailable;
    else if (total < 4)
        out.status = ReturnStatsStatus::InsufficientCount;
    else if (!(s2 > 0.0))
        out.status = ReturnStatsStatus::ZeroVariance;
    else
        out.status = ReturnStatsStatus::Ok;
    return out;
}

double oracle_bar_period(const Curve& curve) {
    if (curve.size() < 3)
        return kNaN;
    const std::int64_t span = curve.back().time_ms - curve.front().time_ms;
    if (span <= 0)
        return kNaN;
    const double years = static_cast<double>(span) / (365.25 * 86400.0 * 1000.0);
    return static_cast<double>(curve.size() - 1) / years;
}

Curve random_curve(Lcg& rng, const std::vector<std::int64_t>& month_starts, bool time_ordered) {
    const std::size_t length = static_cast<std::size_t>(rng.below(121));
    static const std::int64_t offsets[5] = {-2, -1, 0, 1, 2};
    std::vector<std::int64_t> times;
    for (std::size_t index = 0; index < length; ++index) {
        const std::int64_t start = month_starts[rng.below(month_starts.size())];
        times.push_back(rng.below(2) == 0
                            ? start + offsets[rng.below(5)]
                            : start + static_cast<std::int64_t>(rng.below(28 * kDay)));
    }
    if (time_ordered)
        std::sort(times.begin(), times.end());
    Curve curve;
    double level = 100.0;
    for (std::size_t index = 0; index < length; ++index) {
        const double step = (rng.unit() - 0.5) * 0.2;
        const double growth = 1.0 + step;
        level = level * growth;
        double value = level;
        if (rng.below(25) == 0)
            value = 0.0;
        else if (rng.below(40) == 0)
            value = -50.0;
        curve.push_back({times[index], value, 0.0});
    }
    return curve;
}

void case_differential_sweep() {
    g_case = "differential sweep";
    std::vector<std::int64_t> starts = oracle_month_starts(1995, 2005);
    const std::vector<std::int64_t> pre_epoch = oracle_month_starts(1965, 1975);
    starts.insert(starts.end(), pre_epoch.begin(), pre_epoch.end());
    const std::vector<std::int64_t> century = oracle_month_starts(1899, 1901);
    starts.insert(starts.end(), century.begin(), century.end());
    Lcg rng(20261009);
    int compared = 0;
    for (int sample = 0; sample < 600; ++sample) {
        const bool ordered = sample % 5 != 4;  // every fifth curve is not time ordered
        const Curve curve = random_curve(rng, starts, ordered);
        const ReturnStatsResult result = run(curve, true, true);
        if (!result.bar.has_value() || !result.monthly.has_value()) {
            fail(__LINE__, "sweep produced no series");
            continue;
        }
        check_invariants(__LINE__, *result.bar);
        check_invariants(__LINE__, *result.monthly);
        std::vector<double> equity;
        for (const Point& point : curve)
            equity.push_back(point.equity);
        const ReturnSeriesStats expected_bar = oracle_stats(equity, oracle_bar_period(curve));
        const ReturnSeriesStats expected_monthly = oracle_stats(oracle_month_ends(curve), 12.0);
        check(__LINE__, series_fields_equal(*result.bar, expected_bar),
              ("bar series differs from the oracle on sample " + std::to_string(sample)).c_str());
        check(__LINE__, series_fields_equal(*result.monthly, expected_monthly),
              ("monthly series differs from the oracle on sample " + std::to_string(sample))
                  .c_str());
        ++compared;
    }
    EXPECT(compared == 600);
}

}  // namespace

int main() {
    if (!test_unit_contraction_free()) {
        std::cerr << "test_return_stats must be built with -ffp-contract=off -fno-fast-math: this "
                     "translation unit fuses multiply-add, so its frozen bit patterns are not "
                     "comparable\n";
        return 2;
    }
    case_metric_names();
    case_timezone_names();
    case_probe();
    case_not_requested();
    case_unusable_views();
    case_layouts();
    case_frozen_references();
    case_minima();
    case_undefined_period();
    case_zero_variance();
    case_skipped_returns();
    case_nonfinite_input();
    case_derived_nonfinite();
    case_month_boundaries();
    case_utc_range();
    case_timezone_monthly();
    case_status_invariant();
    case_precedence();
    case_repeats_and_threads();
    case_differential_sweep();
    if (g_failures != 0) {
        std::cerr << g_failures << " of " << g_checks << " checks failed\n";
        return 1;
    }
    std::cout << "return_stats: " << g_checks << " checks passed\n";
    return 0;
}
