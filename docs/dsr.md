# Deflated Sharpe ratio of one selected trial

The module is `python/pineforge_hpo/dsr.py`. The contract id is
`pineforge-hpo-dsr/v1` and the algorithm name is `deflated_sharpe_exact_v1`.

## What it computes

`dsr` takes the finished rows of one study, the name of a return series and the id of one trial
the caller selected, and returns the deflated Sharpe ratio of that trial: the statistic of Bailey
and López de Prado, "The Deflated Sharpe Ratio" (2014), which **under the paper's assumptions**
is the probability that the trial's true Sharpe ratio is above the expected maximum Sharpe ratio
of N unskilled trials of the same kind, over a trial count N (the caller's `n_trials`,
or by default every row read). The module's fixed interpretation records that the trials
of a tuned study are not independent, so the number is a deflated reading under a stated N
and not a significance test of the strategy. It runs no
trial, writes no file, keeps no state and changes no native result: the only side effect is
reading the one file named. It is a library function; there is no command-line flag and no field
in a native result.
v1 analyses finished studies; native in-loop walk-forward comes later.

```python
from pineforge_hpo.dsr import DsrError, dsr

try:
    report = dsr("result.json", series="bar", selected=7)
except DsrError as refusal:
    print(refusal.code, refusal.detail, refusal.ids, refusal.names)
else:
    print(report["dsr"], report["sr0"], report["n_trials"])
```

Signature: `dsr(source, *, series, selected, n_trials=None, max_work=None)`.

| Argument | Meaning |
| --- | --- |
| `source` | A path (text or `os.PathLike`) to a complete result JSON or to a rows JSONL file, an already parsed result dictionary, or an already parsed list of rows. Exactly one source. |
| `series` | Exactly `bar` or `monthly`. No default: the caller names the series the number is about. |
| `selected` | The `trial_id` of the trial the caller selected, an integer from 0 to 2^64 - 2. No default; the module never picks a winner and never reads the study's objective. |
| `n_trials` | `None` (the default: N is the number of rows read, any status, warm-start ancestors included) or an integer from 2 to 2^50, at least the number of rows read: the number of trials the selection was made from, counting trials **outside this file** too. The result records where N came from. |
| `max_work` | `None` (no bound, the default) or a positive integer bound on the admission figure (see [Resource rule](#resource-rule)). |

## Input

* **Sources.** A complete result is read as its `warm_start_trials` followed by its `trials`. A
  result must keep every trial: `trials_out` other than `all` is refused, and so is a
  `trials_completed` or a `warm_start.trials` that disagrees with the rows. A JSONL file holds
  one row per non-blank line; a bad line is refused with its line number.
* **Strict reading.** Duplicate JSON keys, the tokens `NaN`, `Infinity` and `-Infinity`, and a
  number that overflows to infinity are refused wherever they stand in a file.
* **Rows.** `trial_id` is an integer from 0 to 2^64 - 2 and unique; `status` is one of `ok`,
  `constraint_violation`, `objective_error`, `constraint_error`, `engine_error`, `trial_error`,
  `trial_timeout`, `pruned`, `partial`; `feasible` is a boolean equal to (status is `ok`); every
  row carries an `objective` member, a finite number or `null`, and finite on an `ok` or a
  `constraint_violation` row; `metrics`, when present, is an object. A row with `"metrics": null`
  is refused: write no `metrics` member instead.
* **Context.** Rows that carry a `space_hash` must carry the same one, and the `ok` rows that
  carry a `backtest` object must agree on `input_bars_processed`, `script_bars_processed`,
  `input_tf_seconds` and `script_tf_seconds`. These checks are necessary, not sufficient: equal
  counters and hashes do not prove the same data window, fixed inputs or strategy.
* **Spaces.** The module needs no recorded space and never compares spaces. It uses the shared
  reader (`read_source`, `check_context`) and nothing else of it.
* **Values read.** From each row: `trial_id`, `status` and the five entries
  `returns.<series>.{sharpe_per_period,count,skew,kurt_raw,status}`. Nothing else is read. The
  study's objective is never read, so it is never taken for a Sharpe ratio; the Sharpe input is
  always the recorded `sharpe_per_period`, which is the excess ratio per period and is **not**
  annualised. `periods_per_year`, `mean`, `std` and `skipped` are not read either.
* **Numbers.** The producer writes an integral metric value without a decimal point, so a value
  entry can arrive as a JSON integer; an integer entry is converted with Python's `float`, and an
  integer above 2^53 rounds to the nearest binary64 (2^53 + 1 becomes 2^53). A boolean is not a
  number.

## Which rows compete

A series entry of any row, whatever its status, that cannot be written as key-sorted JSON
(for example `float("nan")`, a set or a function in a parsed source) refuses the call as
`dsr_input_invalid`,
with the union of the offending trial ids in ascending order in `ids` and the offending entry
keys in ascending alphabetical order in `names`, before any row is excluded. An object whose
keys cannot be sorted in any of the five entries also refuses. An integer beyond
the interpreter's integer-to-text limit in a value entry is not refused: its digest cell is
`["other", "<unwritable>"]`. An integer beyond the interpreter's integer-to-text limit in the
`status` entry is refused so `statuses` can always be written; a smaller large integer there,
such as `10**400`, is a status that is not ok.

Only rows with status `ok` compete. Reasons are applied to each row in this order, the first
that applies decides:

| Row | Treatment | Reason in the result |
| --- | --- | --- |
| any status other than `ok` | excluded | `status_<status>` (for example `status_pruned`) |
| `ok`, no `metrics` object or a `returns.<series>.*` key absent | excluded | `returns_not_recorded` |
| `ok`, the five keys present and the `status` entry not the integer 0 | excluded | `returns_status_nonzero` (the entry `statuses` lists the distinct status entries (see [Result](#result))) |
| `ok`, the five keys present, `status` 0, and a value entry `null`, not a number, an integer whose `float()` overflows, or a `count` outside 2..2^53 as an integer | excluded | `returns_unavailable` |
| `ok`, the five keys present, `status` 0, and the four values usable | eligible | none |

`count` is T, the number of valid finite returns of the series. The status entry is the
producer's own code for the series: `status` is 0 if and only if every other field of that series
is finite, so a nonzero status explains a row completely (a short curve, a zero variance, a
missing period basis, a non-finite value, a non-UTC month). A binary warm-start ancestor carries
no statistics at all and is excluded as `returns_not_recorded`. Every excluded row is named, with
its reason and its id, in the result. Eligible rows alone feed V and the selected row's
statistics; every row read still counts toward default N, because an excluded trial was
still searched. The reader can always see which rows were left out of the moments.

## The statistic

Let SR, T, g3, g4 be the selected row's recorded `sharpe_per_period`, `count`, `skew` and
`kurt_raw`; let N be the trial count and V the variance across the eligible trials of their
per-period Sharpe ratios. For the eligible rows `x[0] .. x[n-1]` in ascending trial id order:

```text
V = ( n * SUM(x*x) - (SUM(x))^2 ) / ( n * (n - 1) )        sample variance, exact rational
```

and with `G = 0.5772156649015329` and `E = 2.718281828459045`:

```text
SR0 = sqrt(V) * ( (1 - G) * Phi_inv(1 - 1/N) + G * Phi_inv(1 - 1/(N*E)) )
den = 1 - g3*SR + ((g4 - 1)/4) * SR^2
z   = (SR - SR0) * sqrt(T - 1) / sqrt(den)
DSR = Phi(z)
```

`SR0` is the expected maximum Sharpe ratio of N unskilled trials. `skew` is the population
skewness g1 and `kurt_raw` the population kurtosis b2 with normal = 3, the convention the
denominator is written for; no conversion is made. `Phi` and `Phi_inv` come from
`statistics.NormalDist`, and **under the paper's assumptions** `DSR` reads as the probability
that the trial's true Sharpe ratio is above the expected maximum of N unskilled trials; the
interpretation records that the trials of a tuned study are not independent, so it is a deflated
reading under a stated N and not a significance test. The probabilistic Sharpe ratio (the same
expression with a caller-given benchmark in place of `SR0`) is not computed here.

## Exactness

* **Exact and platform independent:** `V` and `den`. Both are exact rationals over the exact
  binary64 inputs and are each rounded once to binary64 (round-to-nearest, ties to even), so two
  algebraic forms of the same expression give the same rational and the same bits. The rounded
  `den` is the value reported and the value the square root uses, so `den` has one value. `SR`,
  `T`, `N`, `g3` and `g4` are the recorded values, unchanged.
* **Binary64 in a pinned order:** the rest of the computation above (all but V and den).
  Every step is one addition, subtraction, multiplication, division or square root
  in scalar binary64; nothing is fused and no operation is re-associated. `sqrt` is
  correctly rounded (IEEE 754). The tests compare these results only within 8 ulps
  or the propagated bounds below, so the binary64 order is documented, not pinned
  bit for bit.
* **Depends on the platform's normal distribution functions:** `SR0`, `z` and `DSR`, through
  `statistics.NormalDist`, whose `cdf` and `inv_cdf` are floating-point approximations with no
  correctly-rounded guarantee. An independent oracle with exact rationals and a 40-digit decimal
  series accepts these three within the stated bounds (8 ulps of `SR0` for `sr0`, the
  propagated `8*ulp(SR0)*sqrt(T-1)/sqrt(den) + 4*ulp(|z|)` for `z`, and `phi(z)*tol_z + 1e-15`
  for `dsr`, where `phi` is the standard normal density and `tol_z` is the `z` bound).
  The unit tests also check hand-derived normal-table values within 5e-4.
* **Guards.** The call is refused as `dsr_zero_sharpe_variance` when V is exactly zero **or**
  rounds to `0.0`, and as `dsr_statistic_undefined` when `den` is zero or negative on the exact
  rational value, when `den` rounds to `0.0`, when T < 2, or when any intermediate is not finite.
  Both the rational and the rounded value are checked, so a positive `den` below the smallest
  subnormal is a refusal and not a division by a rounded zero.
* **N and V are refused where they are undefined:** fewer than two eligible rows (and an
  `n_trials` below 2) refuse before the formula, because `Phi_inv(1 - 1/1)` is the inverse of the
  distribution function at 0 and has no finite value. `n_trials` is admitted up to 2^50, well
  below about 6.6e15, where `1 - 1/(N*E)` rounds to `1.0` in binary64 and the inverse of the
  distribution function at that argument would not exist. A `dsr` of exactly `0.0` or `1.0` is a
  valid saturation of a finite `z`, not a clamp or an error.

## Result

| Field | Content |
| --- | --- |
| `contract` | `pineforge-hpo-dsr/v1` |
| `status` | `ok` (a refused call raises, see [Refusals](#refusals)) |
| `series` | the series as given |
| `selected` | the selected trial id |
| `sharpe_per_period`, `count`, `skew`, `kurt_raw` | the selected row's recorded SR, T, g3 and g4, unchanged |
| `n_trials`, `n_trials_source` | N and where it came from: `caller` or `rows_read` |
| `variance_basis`, `variance` | `sample_n_minus_1`, and V |
| `sr0`, `den`, `z`, `dsr` | the statistic of [The statistic](#the-statistic) |
| `selected_is_max` | whether the selected row's Sharpe ratio equals the largest of the eligible rows (exact binary64; ties all count) |
| `rows_read`, `rows_eligible`, `rows_excluded` | the counts; they add up |
| `eligible` | the eligible ids, ascending |
| `excluded` | an object whose keys are the reasons **in ascending order** (only reasons that occurred), each `{count, ids}` with ids ascending, and `statuses` for `returns_status_nonzero` (the distinct status entries of those rows: integers first in ascending numeric order, then the entries that are not integers in the order of their JSON text; two entries are one when their JSON text is equal, so `1`, `1.0` and `true` are three) |
| `algorithm` | `deflated_sharpe_exact_v1` |
| `input_sha256` | digest of the rows read, below |
| `interpretation` | the fixed text of [What is not claimed](#what-is-not-claimed) |

No key is absent and no key is `null` in a result.

`input_sha256` is the SHA-256 (lowercase hex) of the concatenation of the sorted per-row digests.
A per-row digest is the 32-byte SHA-256 of the compact (no spaces), key-sorted, ASCII-only JSON
of `[trial_id, status, {the five "returns.<series>.*" cells}]`. A cell is `null` when the key, the
`metrics` object or the value is absent or `null`; otherwise: a boolean is `["bool", <value>]`, an
integer is the integer, a finite float is its shortest round-trip binary64 text, and any other
value is `["other", <its JSON text>]`. An integer beyond the interpreter's integer-to-text limit
in a value entry uses `["other", "<unwritable>"]`. Every row read is covered, excluded rows
included. Row order, the selection, `n_trials` and the `series` text are not covered, because the
five key names carry the series; a caller who record-keeps the selection records it beside the
digest. Two rows that differ only in whether a key is absent or `null` have the same cell for it
and the same digest; rows that differ in type do not.

## Determinism

Row order, how a file is split into lines and repeated calls give the same document: the eligible
rows are summed in ascending trial id order, every id list is ascending, the `excluded` keys are
ascending, and every key is written in the order above. Only `series` and `selected` are echoed
as given. Nothing is random.

## Refusals

A refused call raises `DsrError` with `code`, `detail`, `ids` (offending trial ids, ascending)
and `names` (offending parameter or field names); no number of the analysis is returned.
`DsrError.document()` renders it as the refused result document:
`{"contract": ..., "status": "refused", "refusal": {"code", "detail", "ids", "names"}}`. The
codes belong to this module; they are not codes of the shared failure catalog.

| Code | Trigger |
| --- | --- |
| `dsr_input_invalid` | a source that cannot be read; a malformed row, unknown status, `feasible` that disagrees with the status, invalid or repeated `trial_id`; a duplicate key, a `NaN` or `Infinity` token or an overflow; a `"metrics": null`; a row with no `objective` member, or no finite objective on an `ok` or a `constraint_violation` row; a series entry of any row that cannot be written as key-sorted JSON, including an object whose keys cannot be sorted in any of the five entries (`ids` is the union of the offending trial ids ascending; `names` lists the offending entry keys in ascending alphabetical order); an integer beyond the interpreter's integer-to-text limit in a value entry is not refused; an integer beyond that limit in the `status` entry is refused, while a smaller large integer there is a status that is not ok |
| `dsr_rows_incomplete` | a best-k or summary result whose `trials_out` is not `all`, or whose counts disagree with its rows |
| `dsr_context_mismatch` | rows with different `space_hash` values, or `ok` rows that disagree on the `backtest` counters |
| `dsr_parameter_invalid` | `series` not exactly `bar` or `monthly`; `selected` not an integer in 0..2^64 - 2; `n_trials` not `None` or an integer in 2..2^50; `max_work` not `None` or a positive integer |
| `dsr_selected_unknown` | no row carries the selected `trial_id` |
| `dsr_selected_not_eligible` | the selected row exists but was excluded (the refusal names its reason) |
| `dsr_no_eligible_rows` | no row remains after the exclusions, or the source holds no rows |
| `dsr_too_few_eligible_rows` | exactly one row is eligible |
| `dsr_trials_inconsistent` | `n_trials` is below the number of rows read |
| `dsr_zero_sharpe_variance` | the eligible rows' Sharpe ratios have an exactly zero sample variance, or one that rounds to `0.0` |
| `dsr_statistic_undefined` | `den` is zero or negative, or rounds to `0.0`, or T < 2, or an intermediate is not finite |
| `dsr_resource_exceeded` | the admission figure is above a `max_work` the caller supplied |

The checks run in this order and the first defect decides the code: the parameters (series,
selected, `n_trials`, `max_work`); the source and series cells; the eligible count; the selection; `n_trials`
against the number of rows read; the admission figure; the statistic.

## Resource rule

The admission figure is

```text
work = rows read + number of eligible rows
```

It is exact once the exclusions are known and it is checked **before** the statistic is computed.
The call is refused as `dsr_resource_exceeded` only when the caller supplied a `max_work` and the
figure is above it; a figure equal to `max_work` is admitted. There is no built-in cap. Memory is
the eligible values plus one accumulator per exact sum, and the exact sums are integers graded by
one common power of two: a sum of the ratios can need about 2100 bits in the worst case and a sum
of squares about twice that, so neither time nor memory has been measured.

## Choices of contract v1

Each choice is a reversible default of this contract version, and a later version may change one
of them. The `Xn` ids are this module's own and do not collide with the `P` of the Pareto page or
the `D` of the PBO page.

| ID | Choice |
| --- | --- |
| X1 | `series` and `selected` have no default; the module never picks a winner and never reads the study's objective. |
| X2 | The Sharpe input is the recorded `sharpe_per_period`: excess, per period, not annualised. |
| X3 | The skewness and kurtosis inputs are the recorded population g1 and b2 (normal = 3), the convention the denominator is written for. |
| X4 | A row is eligible when its status is `ok`, the five `returns.<series>.*` keys are present, the series status entry is exactly the integer 0 and the four values are usable; everything else is excluded with one of four reason strings. |
| X5 | N is `n_trials` when given, else the number of rows read, including excluded trials and warm-start ancestors; the result says which. The default counts every trial tried and is the conservative choice. A caller's `n_trials` is in 2..2^50 and must be at least the rows read. |
| X6 | V is the sample variance (divisor `n - 1`) as an exact rational rounded once; the result records `variance_basis`. |
| X7 | N = 1 and V = 0 are refused, never replaced by a substitute value. |
| X8 | `den` is an exact rational rounded once, guarded `den > 0` on the rational and on the rounded value; a non-positive or rounded-zero `den`, or a non-finite intermediate, refuses; a saturated `0.0` or `1.0` is valid. |
| X9 | The selected row must be eligible; `selected_is_max` is reported and never required; no rank in v1. |
| X10 | The result is a returned document with its own contract id and module-local `dsr_*` codes; no command-line flag and no native field. |
| X11 | `input_sha256` covers the rows read only, not the selection or the trial count. |
| X12 | `dsr_resource_exceeded` only against a caller-supplied `max_work`; the figure is rows read + eligible rows, checked before the statistic; no built-in cap. |

Not part of this contract: the probabilistic Sharpe ratio, an annualised Sharpe ratio, a Sharpe
ratio taken from the objective, several sources at once, a sweep read as one call, any threshold
or pass/fail verdict, and any out-of-sample reading.
v1 analyses finished studies; native in-loop walk-forward comes later.

## What is not claimed

The `interpretation` field of every result holds this text, word for word:

```text
(a) The number is conditional on the trial count N and on the variance V, and both are read from the finished rows of this one source: the default N is the number of rows read, every trial of the record whatever its status, so it is the conservative count of the trials this file holds, and V uses only the eligible rows. If the selection was made over trials this source does not hold — a sweep split over several studies, or a grid whose other arms were written elsewhere — then N counts only the trials this file holds, and the number is too favourable, unless the caller passes n_trials. (b) The module assumes that the returns of the selected trial are the ones its sharpe_per_period was computed on; it reads a recorded number and cannot check what data any trial saw, so the recording is taken as given. (c) The trials of a tuned study are not independent: the search correlates them, the runs share a dataset and a window the module cannot see, and the N of the paper is an effective number of independent trials, not a count of runs. The number is therefore a deflated reading under a stated N, and it is not a significance test of the strategy. (d) No threshold is applied: 0.95 and every other level are the caller's to set, and the module reports a number only. (e) The number is in-sample: it says nothing about how the strategy would perform on data the study did not see, and no out-of-sample performance is claimed. (f) v1 analyses finished studies; native in-loop walk-forward comes later.
```

In plain words: The number is conditional on the trial count N and on the variance V, and both
are read from the finished rows of this one source: the default N is the number of rows read,
every trial of the record whatever its status, so it is the conservative count of the trials
this file holds, and V uses only the eligible rows. If the selection was made over trials this
source does not hold — a sweep split over several studies, or a grid whose other arms were
written elsewhere — then N counts only the trials this file holds, and the number is too
favourable, unless the caller passes n_trials. It assumes the returns of the selected trial
are the ones the Sharpe ratio was computed on, which the module cannot
check; trials are not independent in a tuned study and the paper's N is an effective number, so
the number is not a significance test of the strategy; no threshold such as 0.95 is applied; and
the statistic says nothing about out-of-sample performance. Also not claimed: that the selected
row was the best trial (only `selected_is_max` is reported), anything about an excluded row, that
the recorded statistics are correct, and that two rows come from the same data window — the
context check is necessary, not sufficient.

## Programming interface

`dsr` and `DsrError` in `pineforge_hpo.dsr`, with `CONTRACT`, `ALGORITHM`, `CODES` and
`INTERPRETATION`. The refusal codes are module-local and are not members of the failure catalog.
The tests and their inline fixtures are in `tests/python/test_dsr.py`, with hand derivations
for all-eligible fixtures. The expected values for fixtures with excluded rows were computed
by the independent oracle (exact rationals for V and `den`, decimal series for the normal
functions).

## What an independent oracle confirms

`tests/g3_oracles/dsr_oracle.py` recomputes the document by a different method: the eligibility
and the refusals from the tables on this page rather than by replaying the module's branches;
V in exact rationals by the centred form where the module uses the raw form; `den` in exact
rationals; `Phi` and `Phi_inv` from a decimal series and a bisection, where the module uses
`statistics.NormalDist`. Where it and the module agree, that agreement is evidence for the
eligibility of every row and the reason of every exclusion, V and `den` exactly, `SR0`, `z` and
`DSR` within the stated bounds, `selected_is_max`, the counts and, apart from that one digest,
the digest-independent part of the document.

It is NOT evidence where the oracle repeats the module's own reading: the order in which the
refusals are checked; the order of the eligibility ladder (its check 2 against its check 1); the
two arguments `1 - 1/N` and `1 - 1/(N*E)`, which the oracle reproduces with the module's own
roundings; the one rounding of V and `den`, which both sides perform on the same exact rational; the
`input_sha256` recipe, which the oracle reproduces and compares for one fixture only (a pruned
row whose skew is an integer beyond the interpreter's integer-to-text limit), and the
`interpretation` text, which is neither recomputed nor compared; and `Phi_inv` itself, which
only the module calls — the stated bounds stand in for it, and the
oracle's accuracy is the reason the bounds are the module's error and not a shared one.

The [0.1, 0.2, 0.3] fixture separates a raw one-pass binary64 V, the saturation fixture
a two-pass binary64 V and the monthly fixture a binary64 den; this does not establish
the route against every binary64 implementation.
