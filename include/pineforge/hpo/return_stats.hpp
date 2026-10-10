#pragma once

#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <string_view>

namespace pineforge::hpo {

/// Contract string of the result-level `return_stats` object (legacy mode, version 1).
inline constexpr std::string_view kReturnStatsContract = "pineforge-hpo-return-stats/v1";

/// Fixed annual risk-free rate. The per-period rate is this value divided by the series'
/// periods per year; it is not configurable in contract version 1.
inline constexpr double kReturnStatsRiskFreeAnnual = 0.02;

/// Earliest Unix millisecond timestamp whose UTC month can be derived (inclusive).
///
/// The monthly series refuses a timestamp outside `[kReturnStatsUtcMinMs, kReturnStatsUtcMaxMs]`
/// explicitly (status 6) instead of converting it with unproved arithmetic. The range is
/// `floor(time_ms / 1000)` within plus or minus (2^40 - 1) seconds, about 34,800 years each way.
/// Negative epochs inside the range are valid: months are floored, never truncated toward zero.
inline constexpr std::int64_t kReturnStatsUtcMinMs = -1099511627775000;

/// Latest Unix millisecond timestamp whose UTC month can be derived (inclusive).
inline constexpr std::int64_t kReturnStatsUtcMaxMs = 1099511627775999;

/// Number of published fields per series.
inline constexpr std::size_t kReturnStatisticCount = 9;

/// Number of metric names of this contract (two series times nine fields).
inline constexpr std::size_t kReturnStatsMetricCount = 18;

/// Return frequency of one statistics series.
enum class ReturnSeries : std::uint8_t {
    Bar,      ///< Simple returns between adjacent curve points.
    Monthly,  ///< Simple returns between consecutive last points of UTC calendar-month buckets.
};

/// One published field of a series. Declaration order is the wire order of the metric names.
enum class ReturnStatistic : std::uint8_t {
    Count,            ///< Number of valid returns of the series (T), never a bar counter.
    Skipped,          ///< Intervals skipped because the prior equity is not positive.
    PeriodsPerYear,   ///< Annualization basis P of the series; reported, never applied.
    Mean,             ///< Raw mean of the valid returns.
    Std,              ///< Sample standard deviation (T - 1 divisor).
    SharpePerPeriod,  ///< Unannualised excess Sharpe ratio.
    Skew,             ///< Population-moment skewness g1.
    KurtRaw,          ///< Population-moment raw kurtosis b2 (a normal distribution gives 3).
    Status,           ///< Numeric ReturnStatsStatus code.
};

/// Stable numeric status of one series. Codes 3 and 5 of the draft proposal are not used.
///
/// When several causes apply the first match in this order wins: NonfiniteInput,
/// UnsupportedTimezone, DerivedNonfinite, PeriodUnavailable, InsufficientCount,
/// ZeroVariance, Ok. Status Ok holds if and only if every numeric field of the series is
/// finite; a skipped return or a tiny finite variance is data and never a status.
enum class ReturnStatsStatus : std::uint8_t {
    Ok = 0,                 ///< Every field is finite.
    InsufficientCount = 1,  ///< Fewer than four valid returns: the full statistic set is undefined.
    ZeroVariance = 2,       ///< The computed variance is exactly zero.
    NonfiniteInput = 4,     ///< An input equity is not finite, or the view is unusable; all null.
    PeriodUnavailable = 6,  ///< The period or time basis is unavailable.
    UnsupportedTimezone = 7,  ///< Monthly series requested for a chart timezone other than UTC.
    DerivedNonfinite = 8,   ///< A derived return, sum, moment or ratio is not finite.
};

/// Why the period or time basis of a series is unavailable. Diagnostic only; not on the wire.
enum class ReturnPeriodFailure : std::uint8_t {
    None,             ///< The period is available.
    TooFewPoints,     ///< Bar series with fewer than three points.
    NonPositiveSpan,  ///< Bar series whose last timestamp does not exceed its first.
    SpanOverflow,     ///< Bar series whose timestamp difference overflows 64 bits.
    UtcRange,         ///< Monthly series with a timestamp outside the supported UTC range.
};

/// Borrowed, strided view of an ordered array of records holding `(time_ms, equity)`.
///
/// The view is how the engine's per-bar curve record is read in place: the record may hold more
/// members than these two and the reducer never copies or retains the curve. Build it with
/// make_equity_points_view. The pointed-to memory must stay valid and unchanged for the duration
/// of the compute_return_stats call only.
struct EquityPointsView {
    const void* records = nullptr;  ///< Address of record 0; may be null only when `count` is 0.
    std::size_t count = 0;          ///< Number of records (points).
    std::size_t stride = 0;         ///< Bytes between consecutive records.
    std::size_t time_offset = 0;    ///< Byte offset of the 64-bit signed `time_ms` member.
    std::size_t equity_offset = 0;  ///< Byte offset of the binary64 `equity` member.
};

/// Builds an EquityPointsView over `count` records of type `Record` from pointers to its
/// `time_ms` and `equity` members, for example `&pf_equity_point_t::time_ms`.
template <typename Record>
EquityPointsView make_equity_points_view(const Record* records,
                                         std::size_t count,
                                         std::int64_t Record::*time_ms,
                                         double Record::*equity) noexcept {
    EquityPointsView view;
    view.records = records;
    view.count = count;
    view.stride = sizeof(Record);
    if (records != nullptr && count != 0) {
        const auto* base = reinterpret_cast<const unsigned char*>(records);
        view.time_offset = static_cast<std::size_t>(
            reinterpret_cast<const unsigned char*>(&(records->*time_ms)) - base);
        view.equity_offset = static_cast<std::size_t>(
            reinterpret_cast<const unsigned char*>(&(records->*equity)) - base);
    }
    return view;
}

/// Selects the series to compute. A series that is not requested costs nothing.
struct ReturnStatsRequest {
    bool bar = false;      ///< Compute the bar series.
    bool monthly = false;  ///< Compute the monthly series.
    /// Chart timezone name. Empty, `UTC` and `Etc/UTC` select UTC; any other name makes the
    /// monthly series undefined (status 7). The reducer never changes process timezone state.
    std::string_view chart_timezone;
};

/// All published fields of one series plus diagnostics.
///
/// Undefined numeric fields are NaN and become JSON null on the wire; an infinity is never
/// stored. The `std_dev` member is the wire field `std`, renamed only because `std` names a
/// namespace.
struct ReturnSeriesStats {
    /// Series status; a default-constructed series is the null series with status 4.
    ReturnStatsStatus status = ReturnStatsStatus::NonfiniteInput;
    std::optional<std::int64_t> count;    ///< T; empty when the series is not interpretable.
    std::optional<std::int64_t> skipped;  ///< Skipped intervals; empty with `count`.
    double periods_per_year = std::numeric_limits<double>::quiet_NaN();  ///< P or NaN.
    double mean = std::numeric_limits<double>::quiet_NaN();               ///< Mean or NaN.
    double std_dev = std::numeric_limits<double>::quiet_NaN();            ///< Wire field `std`.
    double sharpe_per_period = std::numeric_limits<double>::quiet_NaN();  ///< Sharpe or NaN.
    double skew = std::numeric_limits<double>::quiet_NaN();               ///< Skewness or NaN.
    double kurt_raw = std::numeric_limits<double>::quiet_NaN();           ///< Raw kurtosis or NaN.

    /// Diagnostic, not on the wire: the reason behind status PeriodUnavailable.
    ReturnPeriodFailure period_failure = ReturnPeriodFailure::None;
    /// Diagnostic, not on the wire: intervals examined, equal to T plus skipped plus
    /// nonfinite_returns whenever `count` is present.
    std::int64_t intervals = 0;
    /// Diagnostic, not on the wire: intervals whose return was not finite. Such an interval is
    /// neither counted in T nor in `skipped`, and it makes the statistics null (status 8).
    std::int64_t nonfinite_returns = 0;

    /// Returns the field as the double the metric layer publishes; NaN stands for null.
    double value(ReturnStatistic statistic) const noexcept;

    /// True when `count`, `skipped` and all six real fields are finite.
    bool all_fields_finite() const noexcept;

    /// True when status is Ok exactly when every other field is finite.
    bool status_invariant_holds() const noexcept;

    /// Final step of every computation: sets status Ok when all fields are finite, and
    /// replaces a status Ok that has an undefined field with DerivedNonfinite, so that no
    /// series is ever published as qualified while a field is null.
    void enforce_status_invariant() noexcept;
};

/// The series that were requested; an absent series was not requested.
struct ReturnStatsResult {
    std::optional<ReturnSeriesStats> bar;      ///< Present when ReturnStatsRequest::bar is set.
    std::optional<ReturnSeriesStats> monthly;  ///< Present when ReturnStatsRequest::monthly is set.
};

/// Computes the requested return statistics from a borrowed equity curve.
///
/// A pure function: it allocates nothing, throws nothing, keeps no state and retains no pointer,
/// so it is safe to call concurrently on shared immutable input. Operation order is fixed and
/// sequential (ascending index, centred two-pass); the translation unit must be built with
/// floating-point contraction and fast-math disabled. When neither series is requested no work
/// is done and the view is not read. An unusable view (null records with a nonzero count, a
/// layout that does not fit its stride, or more than 2^53 records) yields status 4 for the
/// requested series instead of being read.
ReturnStatsResult compute_return_stats(const EquityPointsView& points,
                                       const ReturnStatsRequest& request) noexcept;

/// True for the chart timezone names that select UTC bucketing: empty, `UTC` and `Etc/UTC`.
bool is_utc_chart_timezone(std::string_view chart_timezone) noexcept;

/// Returns the wire metric name, for example `returns.bar.sharpe_per_period`.
std::string_view return_stats_metric_name(ReturnSeries series, ReturnStatistic statistic) noexcept;

/// One of the eighteen metric names, decomposed.
struct ReturnStatsMetric {
    ReturnSeries series = ReturnSeries::Bar;           ///< Series of the metric.
    ReturnStatistic statistic = ReturnStatistic::Count;  ///< Field of the metric.
};

/// Resolves a metric name; returns an empty value for every name outside the eighteen.
std::optional<ReturnStatsMetric> parse_return_stats_metric(std::string_view name) noexcept;

/// Sanity probe of this translation unit's build: true when a multiply followed by an add is
/// evaluated as two rounded operations (no contraction) and explicit fma is fused. This is a
/// fail-closed check, not an attestation of the whole translation unit and not a proof of
/// cross-architecture equality.
bool return_stats_contraction_free() noexcept;

}  // namespace pineforge::hpo
