// X return statistics, legacy mode (contract pineforge-hpo-return-stats/v1).
//
// A pure reducer over a borrowed (time_ms, equity) array. It reads the engine-owned curve in
// place, allocates nothing, keeps no state and uses only + - * / and sqrt in a fixed sequential
// order: ascending index, centred two-pass. Build this translation unit with floating-point
// contraction and fast-math disabled (-fno-fast-math -ffp-contract=off); the product of a
// multiplication is always stored before it is added so that a conforming compiler has no
// expression to fuse, and the flags stay required for compilers that fuse across statements.
#include <pineforge/hpo/return_stats.hpp>

#include <array>
#include <cfloat>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <optional>
#include <string_view>

#if defined(__FAST_MATH__) || (defined(__FINITE_MATH_ONLY__) && __FINITE_MATH_ONLY__)
#error "return_stats requires IEEE-754 semantics: build without -ffast-math or -ffinite-math-only"
#endif

static_assert(sizeof(double) == 8 && std::numeric_limits<double>::is_iec559 &&
                  std::numeric_limits<double>::digits == 53,
              "return_stats requires IEEE-754 binary64");
static_assert(sizeof(std::int64_t) == 8, "return_stats reads 64-bit timestamps");
static_assert(FLT_EVAL_METHOD == 0,
              "return_stats requires binary64 evaluation without excess precision");

namespace pineforge::hpo {
namespace {

constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();
// Both factors are exact in binary64, so the constant is exact: 31,557,600,000 ms.
constexpr double kMillisecondsPerYear = 365.25 * 86400.0 * 1000.0;
constexpr double kMonthsPerYear = 12.0;
constexpr std::uint64_t kMaxExactCount = std::uint64_t{1} << 53;
constexpr std::int64_t kMillisecondsPerDay = 86400000;
// Exclusive end of the supported UTC range, the first unsupported instant.
constexpr std::int64_t kUtcEndMs = kReturnStatsUtcMaxMs + 1;

constexpr std::array<std::string_view, kReturnStatsMetricCount> kMetricNames = {{
    "returns.bar.count",
    "returns.bar.skipped",
    "returns.bar.periods_per_year",
    "returns.bar.mean",
    "returns.bar.std",
    "returns.bar.sharpe_per_period",
    "returns.bar.skew",
    "returns.bar.kurt_raw",
    "returns.bar.status",
    "returns.monthly.count",
    "returns.monthly.skipped",
    "returns.monthly.periods_per_year",
    "returns.monthly.mean",
    "returns.monthly.std",
    "returns.monthly.sharpe_per_period",
    "returns.monthly.skew",
    "returns.monthly.kurt_raw",
    "returns.monthly.status",
}};

// ---------------------------------------------------------------------------------------------
// Borrowed input
// ---------------------------------------------------------------------------------------------

// An eight-byte copy that stays an inline load when the numeric flags include -fno-builtin,
// which would turn std::memcpy into a library call per point.
inline void copy_eight_bytes(void* destination, const unsigned char* source) noexcept {
#if defined(__GNUC__) || defined(__clang__)
    __builtin_memcpy(destination, source, 8);
#else
    std::memcpy(destination, source, 8);
#endif
}

class PointReader {
public:
    explicit PointReader(const EquityPointsView& view) noexcept
        : base_(static_cast<const unsigned char*>(view.records)),
          count_(view.count),
          stride_(view.stride),
          time_offset_(view.time_offset),
          equity_offset_(view.equity_offset) {}

    std::size_t size() const noexcept { return count_; }

    std::int64_t time_ms(std::size_t index) const noexcept {
        std::int64_t value;
        copy_eight_bytes(&value, base_ + index * stride_ + time_offset_);
        return value;
    }

    double equity(std::size_t index) const noexcept {
        double value;
        copy_eight_bytes(&value, base_ + index * stride_ + equity_offset_);
        return value;
    }

private:
    const unsigned char* base_;
    std::size_t count_;
    std::size_t stride_;
    std::size_t time_offset_;
    std::size_t equity_offset_;
};

// Structural validation only: it never reads the pointed-to memory.
bool view_is_usable(const EquityPointsView& view) noexcept {
    if (view.count == 0)
        return true;
    if (view.records == nullptr)
        return false;
    if (static_cast<std::uint64_t>(view.count) > kMaxExactCount)
        return false;
    constexpr std::size_t kValueSize = 8;
    if (view.time_offset > view.stride || view.stride - view.time_offset < kValueSize)
        return false;
    if (view.equity_offset > view.stride || view.stride - view.equity_offset < kValueSize)
        return false;
    return view.count <= std::numeric_limits<std::size_t>::max() / view.stride;
}

bool all_equities_finite(const PointReader& in) noexcept {
    for (std::size_t index = 0; index < in.size(); ++index) {
        if (!std::isfinite(in.equity(index)))
            return false;
    }
    return true;
}

// ---------------------------------------------------------------------------------------------
// Signed timestamp arithmetic and the UTC calendar
// ---------------------------------------------------------------------------------------------

// minuend - subtrahend without signed overflow; false when the difference is not representable.
bool checked_difference(std::int64_t minuend,
                        std::int64_t subtrahend,
                        std::int64_t& result) noexcept {
    constexpr std::int64_t kMin = std::numeric_limits<std::int64_t>::min();
    constexpr std::int64_t kMax = std::numeric_limits<std::int64_t>::max();
    if (subtrahend > 0 ? minuend < kMin + subtrahend : minuend > kMax + subtrahend)
        return false;
    result = minuend - subtrahend;
    return true;
}

// Floor division for a positive denominator; truncation toward zero would shift negative epochs.
std::int64_t floor_div(std::int64_t numerator, std::int64_t denominator) noexcept {
    std::int64_t quotient = numerator / denominator;
    if (numerator % denominator < 0)
        --quotient;
    return quotient;
}

// Howard Hinnant's civil_from_days, exact for the day counts of the supported range.
void civil_from_days(std::int64_t days, std::int64_t& year, unsigned& month) noexcept {
    days += 719468;
    const std::int64_t era = (days >= 0 ? days : days - 146096) / 146097;
    const unsigned day_of_era = static_cast<unsigned>(days - era * 146097);  // [0, 146096]
    const unsigned year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36524 - day_of_era / 146096) / 365;
    const unsigned day_of_year =
        day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);  // [0, 365]
    const unsigned shifted_month = (5 * day_of_year + 2) / 153;                  // [0, 11]
    month = shifted_month < 10 ? shifted_month + 3 : shifted_month - 9;          // [1, 12]
    year = static_cast<std::int64_t>(year_of_era) + era * 400 + (month <= 2 ? 1 : 0);
}

// Howard Hinnant's days_from_civil: days since 1970-01-01 of the first of the given month.
std::int64_t first_day_of_month(std::int64_t year, unsigned month) noexcept {
    year -= month <= 2 ? 1 : 0;
    const std::int64_t era = (year >= 0 ? year : year - 399) / 400;
    const unsigned year_of_era = static_cast<unsigned>(year - era * 400);  // [0, 399]
    const unsigned day_of_year = (153 * (month > 2 ? month - 3 : month + 9) + 2) / 5;
    const unsigned day_of_era =
        year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;  // [0, 146096]
    return era * 146097 + static_cast<std::int64_t>(day_of_era) - 719468;
}

// The half-open UTC month interval [begin_ms, end_ms) of the current bucket, clamped to the
// supported range so that a point inside the interval is supported by construction.
struct UtcMonth {
    std::int64_t begin_ms = 0;
    std::int64_t end_ms = 0;
};

bool utc_month_containing(std::int64_t time_ms, UtcMonth& month) noexcept {
    if (time_ms < kReturnStatsUtcMinMs || time_ms >= kUtcEndMs)
        return false;
    const std::int64_t days = floor_div(floor_div(time_ms, 1000), 86400);
    std::int64_t year = 0;
    unsigned month_of_year = 0;
    civil_from_days(days, year, month_of_year);
    const std::int64_t begin_day = first_day_of_month(year, month_of_year);
    const std::int64_t end_day = month_of_year == 12 ? first_day_of_month(year + 1, 1)
                                                     : first_day_of_month(year, month_of_year + 1);
    const std::int64_t begin = begin_day * kMillisecondsPerDay;
    const std::int64_t end = end_day * kMillisecondsPerDay;
    month.begin_ms = begin < kReturnStatsUtcMinMs ? kReturnStatsUtcMinMs : begin;
    month.end_ms = end > kUtcEndMs ? kUtcEndMs : end;
    return true;
}

// ---------------------------------------------------------------------------------------------
// Returns
// ---------------------------------------------------------------------------------------------

enum class IntervalKind { Skipped, NonfiniteReturn, Valid };

// r = current / prior - 1 in binary64 for a positive prior; any other prior skips the interval.
// A current equity at or below zero is allowed. Inputs are finite, so a nonfinite result is a
// derived overflow and is never repaired, imputed or silently dropped.
IntervalKind classify_interval(double prior, double current, double& simple_return) noexcept {
    if (!(prior > 0.0))
        return IntervalKind::Skipped;
    const double ratio = current / prior;
    const double value = ratio - 1.0;
    if (!std::isfinite(value))
        return IntervalKind::NonfiniteReturn;
    simple_return = value;
    return IntervalKind::Valid;
}

// Adjacent curve points.
class BarWalk {
public:
    explicit BarWalk(const PointReader& in) noexcept : in_(in) {}

    template <typename Visit>
    bool operator()(Visit&& visit) const noexcept {
        const std::size_t count = in_.size();
        if (count < 2)
            return true;
        double prior = in_.equity(0);
        for (std::size_t index = 1; index < count; ++index) {
            const double current = in_.equity(index);
            visit(prior, current);
            prior = current;
        }
        return true;
    }

private:
    const PointReader& in_;
};

// Consecutive last points of UTC calendar-month buckets. A bucket is a maximal run of
// consecutive points in one month, which is the calendar bucket of a time-ordered curve.
// Returns false when a timestamp lies outside the supported UTC range.
class MonthlyWalk {
public:
    explicit MonthlyWalk(const PointReader& in) noexcept : in_(in) {}

    template <typename Visit>
    bool operator()(Visit&& visit) const noexcept {
        const std::size_t count = in_.size();
        UtcMonth month;
        double run_end = 0.0;
        double previous_end = 0.0;
        bool have_previous = false;
        for (std::size_t index = 0; index < count; ++index) {
            const std::int64_t time_ms = in_.time_ms(index);
            if (index == 0 || time_ms < month.begin_ms || time_ms >= month.end_ms) {
                if (!utc_month_containing(time_ms, month))
                    return false;
                if (index != 0) {
                    if (have_previous)
                        visit(previous_end, run_end);
                    previous_end = run_end;
                    have_previous = true;
                }
            }
            run_end = in_.equity(index);
        }
        if (have_previous)
            visit(previous_end, run_end);
        return true;
    }

private:
    const PointReader& in_;
};

struct Tally {
    std::int64_t valid = 0;
    std::int64_t skipped = 0;
    std::int64_t nonfinite = 0;
    double sum = 0.0;
};

struct CentredSums {
    double s2 = 0.0;
    double s3 = 0.0;
    double s4 = 0.0;
};

// First pass: classify every interval and sum the valid returns in ascending order.
template <typename Walk>
bool tally_returns(const Walk& walk, Tally& tally) noexcept {
    return walk([&tally](double prior, double current) {
        double value = 0.0;
        switch (classify_interval(prior, current, value)) {
        case IntervalKind::Skipped:
            ++tally.skipped;
            break;
        case IntervalKind::NonfiniteReturn:
            ++tally.nonfinite;
            break;
        case IntervalKind::Valid:
            ++tally.valid;
            tally.sum += value;
            break;
        }
    });
}

// Second pass: centred sums over the same classification. Each product is stored before it
// is added; S3 += (d*d)*d and S4 += (d*d)*(d*d) with d*d computed once.
template <typename Walk>
CentredSums centred_sums(const Walk& walk, double mean) noexcept {
    CentredSums sums;
    walk([&sums, mean](double prior, double current) {
        double value = 0.0;
        if (classify_interval(prior, current, value) != IntervalKind::Valid)
            return;
        const double deviation = value - mean;
        const double square = deviation * deviation;
        const double cube = square * deviation;
        const double fourth = square * square;
        sums.s2 += square;
        sums.s3 += cube;
        sums.s4 += fourth;
    });
    return sums;
}

// numerator / denominator for a finite numerator and a finite positive denominator, published
// only when the quotient is finite as well.
bool finite_quotient(double numerator, double denominator, double& quotient) noexcept {
    if (!std::isfinite(numerator) || !std::isfinite(denominator) || !(denominator > 0.0))
        return false;
    const double value = numerator / denominator;
    if (!std::isfinite(value))
        return false;
    quotient = value;
    return true;
}

// Fills one series from its walk. `periods_per_year` and `failure` describe the period basis.
template <typename Walk>
ReturnSeriesStats evaluate_series(const Walk& walk,
                                  double periods_per_year,
                                  ReturnPeriodFailure failure) noexcept {
    ReturnSeriesStats out;

    Tally tally;
    if (!tally_returns(walk, tally)) {
        // A timestamp outside the supported UTC range: the monthly series cannot be formed,
        // so nothing about it is interpretable and every field except status is null.
        out.status = ReturnStatsStatus::PeriodUnavailable;
        out.period_failure = ReturnPeriodFailure::UtcRange;
        out.enforce_status_invariant();
        return out;
    }

    out.count = tally.valid;
    out.skipped = tally.skipped;
    out.nonfinite_returns = tally.nonfinite;
    out.intervals = tally.valid + tally.skipped + tally.nonfinite;
    out.period_failure = failure;

    bool derived_nonfinite = false;
    bool zero_variance = false;

    bool period_valid = false;
    double risk_free_per_period = kNaN;
    if (failure == ReturnPeriodFailure::None) {
        if (std::isfinite(periods_per_year) && periods_per_year > 0.0) {
            out.periods_per_year = periods_per_year;
            risk_free_per_period = kReturnStatsRiskFreeAnnual / periods_per_year;
            period_valid = true;
        } else {
            derived_nonfinite = true;
        }
    }

    const double count = static_cast<double>(tally.valid);
    if (tally.nonfinite > 0) {
        // A return overflowed: publishing statistics of the remaining subset would mislead.
        derived_nonfinite = true;
    } else if (tally.valid >= 1) {
        const double mean = tally.sum / count;
        if (!std::isfinite(mean)) {
            derived_nonfinite = true;
        } else {
            out.mean = mean;
            if (tally.valid >= 2) {
                const CentredSums sums = centred_sums(walk, mean);
                if (!std::isfinite(sums.s2)) {
                    // std, sharpe, skew and kurtosis all need S2.
                    derived_nonfinite = true;
                } else {
                    const double std_dev =
                        std::sqrt(sums.s2 / static_cast<double>(tally.valid - 1));
                    out.std_dev = std_dev;
                    if (!(std_dev > 0.0)) {
                        zero_variance = true;
                    } else if (period_valid) {
                        double sharpe = 0.0;
                        if (finite_quotient(mean - risk_free_per_period, std_dev, sharpe))
                            out.sharpe_per_period = sharpe;
                        else
                            derived_nonfinite = true;
                    }
                    if (sums.s2 > 0.0) {
                        const double population_variance = sums.s2 / count;
                        if (tally.valid >= 3) {
                            const double scale =
                                population_variance * std::sqrt(population_variance);
                            double skew = 0.0;
                            if (finite_quotient(sums.s3 / count, scale, skew))
                                out.skew = skew;
                            else
                                derived_nonfinite = true;
                        }
                        if (tally.valid >= 4) {
                            const double scale = population_variance * population_variance;
                            double kurtosis = 0.0;
                            if (finite_quotient(sums.s4 / count, scale, kurtosis))
                                out.kurt_raw = kurtosis;
                            else
                                derived_nonfinite = true;
                        }
                    }
                }
            }
        }
    }

    if (derived_nonfinite)
        out.status = ReturnStatsStatus::DerivedNonfinite;
    else if (failure != ReturnPeriodFailure::None)
        out.status = ReturnStatsStatus::PeriodUnavailable;
    else if (tally.valid < 4)
        out.status = ReturnStatsStatus::InsufficientCount;
    else if (zero_variance)
        out.status = ReturnStatsStatus::ZeroVariance;
    else
        out.status = ReturnStatsStatus::Ok;
    out.enforce_status_invariant();
    return out;
}

// Bar period basis: P = (n - 1) / span_years for n >= 3 and a positive span, with the
// timestamp difference taken without signed overflow.
ReturnPeriodFailure bar_period(const PointReader& in, double& periods_per_year) noexcept {
    const std::size_t count = in.size();
    if (count < 3)
        return ReturnPeriodFailure::TooFewPoints;
    std::int64_t span_ms = 0;
    if (!checked_difference(in.time_ms(count - 1), in.time_ms(0), span_ms))
        return ReturnPeriodFailure::SpanOverflow;
    if (span_ms <= 0)
        return ReturnPeriodFailure::NonPositiveSpan;
    const double span_years = static_cast<double>(span_ms) / kMillisecondsPerYear;
    periods_per_year = static_cast<double>(count - 1) / span_years;
    return ReturnPeriodFailure::None;
}

ReturnSeriesStats compute_bar(const PointReader& in) noexcept {
    double periods_per_year = kNaN;
    const ReturnPeriodFailure failure = bar_period(in, periods_per_year);
    const BarWalk walk(in);
    return evaluate_series(walk, periods_per_year, failure);
}

ReturnSeriesStats compute_monthly(const PointReader& in) noexcept {
    const MonthlyWalk walk(in);
    return evaluate_series(walk, kMonthsPerYear, ReturnPeriodFailure::None);
}

}  // namespace

// ---------------------------------------------------------------------------------------------
// ReturnSeriesStats
// ---------------------------------------------------------------------------------------------

double ReturnSeriesStats::value(ReturnStatistic statistic) const noexcept {
    switch (statistic) {
    case ReturnStatistic::Count:
        return count ? static_cast<double>(*count) : kNaN;
    case ReturnStatistic::Skipped:
        return skipped ? static_cast<double>(*skipped) : kNaN;
    case ReturnStatistic::PeriodsPerYear:
        return periods_per_year;
    case ReturnStatistic::Mean:
        return mean;
    case ReturnStatistic::Std:
        return std_dev;
    case ReturnStatistic::SharpePerPeriod:
        return sharpe_per_period;
    case ReturnStatistic::Skew:
        return skew;
    case ReturnStatistic::KurtRaw:
        return kurt_raw;
    case ReturnStatistic::Status:
        return static_cast<double>(static_cast<int>(status));
    }
    return kNaN;
}

bool ReturnSeriesStats::all_fields_finite() const noexcept {
    return count.has_value() && skipped.has_value() && std::isfinite(periods_per_year) &&
           std::isfinite(mean) && std::isfinite(std_dev) && std::isfinite(sharpe_per_period) &&
           std::isfinite(skew) && std::isfinite(kurt_raw);
}

bool ReturnSeriesStats::status_invariant_holds() const noexcept {
    return (status == ReturnStatsStatus::Ok) == all_fields_finite();
}

void ReturnSeriesStats::enforce_status_invariant() noexcept {
    if (all_fields_finite())
        status = ReturnStatsStatus::Ok;
    else if (status == ReturnStatsStatus::Ok)
        status = ReturnStatsStatus::DerivedNonfinite;
}

// ---------------------------------------------------------------------------------------------
// Public functions
// ---------------------------------------------------------------------------------------------

ReturnStatsResult compute_return_stats(const EquityPointsView& points,
                                       const ReturnStatsRequest& request) noexcept {
    ReturnStatsResult result;
    if (!request.bar && !request.monthly)
        return result;

    const bool usable = view_is_usable(points);
    const PointReader reader(points);
    const bool finite = usable && all_equities_finite(reader);

    if (request.bar) {
        if (finite)
            result.bar = compute_bar(reader);
        else
            result.bar = ReturnSeriesStats{};
    }
    if (request.monthly) {
        if (!finite) {
            result.monthly = ReturnSeriesStats{};
        } else if (!is_utc_chart_timezone(request.chart_timezone)) {
            ReturnSeriesStats unsupported;
            unsupported.status = ReturnStatsStatus::UnsupportedTimezone;
            result.monthly = unsupported;
        } else {
            result.monthly = compute_monthly(reader);
        }
    }
    return result;
}

bool is_utc_chart_timezone(std::string_view chart_timezone) noexcept {
    return chart_timezone.empty() || chart_timezone == "UTC" || chart_timezone == "Etc/UTC";
}

std::string_view return_stats_metric_name(ReturnSeries series,
                                          ReturnStatistic statistic) noexcept {
    return kMetricNames[static_cast<std::size_t>(series) * kReturnStatisticCount +
                        static_cast<std::size_t>(statistic)];
}

std::optional<ReturnStatsMetric> parse_return_stats_metric(std::string_view name) noexcept {
    for (std::size_t index = 0; index < kMetricNames.size(); ++index) {
        if (name == kMetricNames[index]) {
            ReturnStatsMetric metric;
            metric.series = static_cast<ReturnSeries>(index / kReturnStatisticCount);
            metric.statistic = static_cast<ReturnStatistic>(index % kReturnStatisticCount);
            return metric;
        }
    }
    return std::nullopt;
}

bool return_stats_contraction_free() noexcept {
    // (1 + 2^-27)^2 = 1 + 2^-26 + 2^-54 rounds to 1 + 2^-26, so a rounded product added to
    // -(1 + 2^-26) gives 0 while a fused multiply-add gives exactly 2^-54. The volatile reads keep
    // the compiler from folding the expression away.
    volatile double first_input = 1.0 + 0x1p-27;
    volatile double addend_input = -(1.0 + 0x1p-26);
    const double first = first_input;
    const double addend = addend_input;
    const double product = first * first;
    const double separate = addend + product;
    const double fused = std::fma(first, first, addend);
    return separate == 0.0 && fused == 0x1p-54;
}

}  // namespace pineforge::hpo
