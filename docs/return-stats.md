# X return statistics: reducer contract (legacy mode)

**Status: code and tests are committed but have never been compiled or executed.** Every
execution claim in this page is deferred to the spot proof listed at the end. The reducer is
not wired into the trial executor, the command line or the build; the integration needs are
listed below. This page implements the pinned contract `pineforge-hpo-return-stats/v1`,
revision 1 (authority: TOP's methods-x dispositions of 2026-10-09, pin file SHA-256
`e0c6f0f4c4855f6c27886fd86f976c68aba8ee395b7803a4bd1d6208ebd34753`). Selected-window mode
waits for the frozen window interface and is not implemented here.

## What the reducer is

`compute_return_stats` in `pineforge/hpo/return_stats.hpp` is a pure function over a borrowed,
strided view of an ordered array of `(time_ms, equity)` records. The engine's per-bar curve
record (`pf_equity_point_t`, 24 bytes with an extra `open_profit` member) is read in place
through the view; nothing is copied, no return vector or compact curve is built, and no
pointer is kept after the call. The function allocates nothing, throws nothing and holds no
state, so concurrent calls on shared immutable input are safe. It includes no engine header.

Only the requested series are computed. With neither series requested the function returns at
once and does not read the view.

## Series and returns

| Series | Points used | Period basis P |
| --- | --- | --- |
| `bar` | every adjacent pair of curve points | `(n - 1) / span_years` for `n >= 3` and a positive span |
| `monthly` | consecutive last points of UTC calendar-month buckets | `12` whenever the series can be formed |

* `span_years = (last_time - first_time) / (365.25 * 86400 * 1000)`; the timestamp difference
  is taken without signed overflow. Missing bars are never synthesized.
* A monthly bucket is a maximal run of consecutive points in one UTC month, which is the
  calendar bucket of a time-ordered curve. Months are floored, so negative epochs are valid
  (`-1` ms is December 1969). A timestamp outside `kReturnStatsUtcMinMs` to
  `kReturnStatsUtcMaxMs` (about 34,800 years each way) is refused explicitly: the monthly
  series gets status 6 with every field null. The bar series does not need a calendar.
* A chart timezone other than empty, `UTC` or `Etc/UTC` makes the monthly series undefined
  (status 7, every field null). The bar series is unaffected. The reducer never changes
  process timezone state.
* For finite endpoints and a positive prior equity, `r = current / prior - 1` in binary64.
  A prior equity at or below zero skips the interval and increments `skipped`. A current
  equity at or below zero is allowed. Nothing is imputed or repaired.
* Any nonfinite input equity anywhere in the curve invalidates both series (status 4, every
  field null). A view that cannot be read (null records with a nonzero count, a layout that
  does not fit its stride, more than 2^53 records) is reported the same way instead of read.
* `count` is T, the number of valid finite returns of the series. It does not depend on
  whether P is available. With no input or return failure, `T = intervals - skipped`. A
  return that is not finite (a ratio overflow) is neither counted nor skipped, and it makes
  every statistic null with status 8 rather than a silently dropped sample.

## Statistics

Over the valid returns, in ascending index order, with fixed risk-free rate 0.02 per year:

```
rf       = 0.02 / P                        (simple division)
mean     = sum(r) / T
d        = r - mean
S2      += d * d
S3      += (d * d) * d
S4      += (d * d) * (d * d)
std      = sqrt(S2 / (T - 1))              (sample deviation)
sharpe_per_period = (mean - rf) / std      (excess, unannualised)
skew     = (S3 / T) / ((S2 / T) * sqrt(S2 / T))   (population g1)
kurt_raw = (S4 / T) / ((S2 / T) * (S2 / T))       (population b2, normal = 3)
```

The mean is raw; only the Sharpe ratio is excess. Operations are `+ - * /` and `sqrt` in
scalar binary64: no reassociation, no vectorized or parallel reduction, no long double, and no
fused multiply-add. The two passes recompute each return from the borrowed curve.

| Field | Defined when |
| --- | --- |
| `mean` | `T >= 1` |
| `std` | `T >= 2` (an exactly zero value is valid and finite) |
| `sharpe_per_period` | `T >= 2`, `std > 0` and P available |
| `skew` | `T >= 3` and `S2 > 0` |
| `kurt_raw` | `T >= 4` and `S2 > 0` |

An undefined field is NaN inside the reducer and JSON null on the wire; an infinity is never
published. Every derived ratio, sum, moment and result is checked for finiteness. A field whose
inputs overflow is null while fields that do not depend on the overflowed quantity are kept: a
nonfinite sum makes the mean and everything after it null; a nonfinite `S2` nulls std, Sharpe,
skew and kurtosis; a nonfinite `S3` or `S4` nulls only skew or kurtosis. Because the formulas
divide by powers of the variance, a variance too small to square in binary64 nulls the
affected ratio instead of publishing an infinity. Values in the subnormal range lose precision
and are data, not rejected.

## Status

One numeric code per series. The first matching row wins.

| Code | Meaning |
| --- | --- |
| 4 | an input equity is not finite, or the view is unusable (all fields null) |
| 7 | monthly series for a chart timezone other than UTC (all fields null) |
| 8 | a derived return, sum, moment or ratio is not finite |
| 6 | the period or time basis is unavailable |
| 1 | fewer than four valid returns: the full set is undefined |
| 2 | the computed variance is exactly zero |
| 0 | otherwise |

**Status 0 holds if and only if every other field of the series is finite.** The reducer
enforces this as its last step: a status 0 with an undefined field becomes 8, and an
all-finite series is 0. A skipped return and a tiny finite variance are data: they have no
status of their own and a fully finite series with such features has status 0. Codes 3 and 5 of
the earlier proposal are not used. When the period is unavailable the independently defined
fields (count, skipped, mean, std, skew, kurtosis) are still published.

## Wire names

Exactly eighteen: `returns.{bar,monthly}.{count,skipped,periods_per_year,mean,std,sharpe_per_period,skew,kurt_raw,status}`.
`return_stats_metric_name` and `parse_return_stats_metric` own the table; `ReturnSeriesStats`
names the standard deviation `std_dev` because `std` is a namespace, and
`ReturnSeriesStats::value` returns each field as the double the metric layer publishes (NaN
for null). A trial without a report never reaches the reducer; every requested metric,
including status, is null for it.

## Determinism and build requirements

* Build `src/core/return_stats.cpp` and its test with `-fno-fast-math -ffp-contract=off`. The
  source refuses `-ffast-math` and `-ffinite-math-only` at compile time and asserts binary64
  evaluation without excess precision. Every product is stored before it is added so that a
  conforming compiler has no expression to fuse.
* `return_stats_contraction_free()` is a sanity probe of the translation unit, not an
  attestation of it and not a proof of cross-architecture equality. Equality is claimed only
  for build pairs that the spot evidence lists.
* The statistics identity (reducer source digest, compiler identity and version, actual
  translation-unit compile flags, contract string) belongs to a separate lane and is
  independent of the TPE checkpoint identity. This reducer neither reads nor changes
  `numeric_build_identity` or `kTpeAlgorithmRevision`.

## Integration needs (shared seams not edited here)

* `CMakeLists.txt`: add `src/core/return_stats.cpp` to `pineforge_hpo_core` and give that
  source the per-source options of the other numeric sources
  (`-fno-fast-math;-ffp-contract=off`, and likely `-frounding-math;-fno-builtin;-fno-lto` for
  consistency); the identity lane decides how the flags enter the statistics identity.
* `tests/CMakeLists.txt`: register `test_return_stats.cpp` linked to `PineForgeHPO::core`
  (which carries `Threads::Threads`) with `-fno-fast-math -ffp-contract=off` and
  `-Wall -Wextra -Wpedantic`, as a CTest case.
* `src/engine_adapter/trial_executor.cpp`: call `compute_return_stats` inside `copy_report`
  while the report still owns the curve, using
  `make_equity_points_view(curve, n, &pf_equity_point_t::time_ms, &pf_equity_point_t::equity)`,
  and store only the resulting fields in the report snapshot.
* Metric resolution (`ReportSnapshot::metric()` and `--record-metric` validation), the
  request derivation, the once-per-result `return_stats` object, and public documentation
  (`docs/api.md`, `docs/study-spec.md`, `README.md`, `CHANGELOG.md`) belong to the single
  integration lane.
* Refuse to start (or fail a trial) when `return_stats_contraction_free()` returns false; the
  policy and the typed error code are the integration lane's choice.

## Proof still to be produced (spot only, nothing run yet)

The tests in `tests/test_return_stats.cpp` combine hand-derived analytic cases, the frozen
bit patterns of the author lane's stand-alone reference transcription (`fixtures_x3.py`, not
product measurements), and a differential sweep against an independent in-file oracle with its
own table-driven UTC calendar. They must be built and run on a spot machine, on x86-64 and on
aarch64 separately, before any claim is made. Still required beyond them: independent
high-precision (arbitrary-precision) comparison for magnitudes the unit tests cannot hold,
the full command-line off-byte comparison, reconciliation with the engine's canonical curve and
its `sharpe_bar` and `sharpe_monthly` under named matching builds, repeat and worker-count
(1, 2, 4, 8) invariance on the real executor, and the added time per trial against identical
off trials at about 8,760 and 2,000,000 script bars with the realized counts and the
measurement scope. No cost claim is made without both measurements.
