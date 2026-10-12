# Probability of backtest overfitting (PBO) from block moments

**Status: the module `python/pineforge_hpo/pbo.py`, its tests and its fixtures are committed
but have never been executed.** Every execution claim in this page is deferred to the proof
phase. The module is a pure, standard-library-only reference: any faster implementation must
reproduce its results bit for bit.

**Native input.** The native runner accepts `--pbo-blocks S` to publish a table of block moments
(count, sum and centred second moment per block, one record per completed trial) and its
result-level layout. This integration still needs execution verification. Publishing the
payload does not prove PBO on real data or validate a market interpretation.

The method is combinatorially symmetric cross-validation (CSCV) and the probability of backtest
overfitting of Bailey, Borwein, Lopez de Prado and Zhu, "The Probability of Backtest
Overfitting" (revised version of February 27, 2015). Page numbers below are that paper's.

## Requesting native blocks

Append `--pbo-blocks 16`, for example, to a native `pineforge-hpo-native run` command. This is a
native flag only; there is no StudySpec key or Python `run` option for it. Without the flag the
runner does no block reduction and adds no block keys; result bytes keep their existing format.

`S` must be an even integer from 2 to 4294967294. An invalid value is refused before loading a
plugin, dataset or trial, with exit 1 and `hpo_study_spec_invalid`, reason `study`. A requested
run also needs the bound return-statistics build identity; an unbound build is refused before
those inputs are loaded with `hpo_toolchain_unavailable`, reason `native_runner`.

Blocks use the full-window reported equity curve, without an added anchor: `K` is its point
count minus one. Interval `k` is the simple return `E[k+1] / E[k] - 1` and belongs to block
`floor(k*S/K)`. Block `b` opens at point `ceil(b*K/S)`; the layout's `boundary_time_ms` copies
the curve times at those points, including point `K` as the last boundary. `n_b` counts all
intervals in each block. The block-size rules below must hold, including `K >= S` and at least
two intervals in the smallest half. A curve too short for the request is refused with exit 1
and `hpo_study_spec_invalid`, reason `study`.

Only completed `ok` and `constraint_violation` trials carry `block_returns`. Prefix rungs,
pruned trials, failed trials and partial trials carry no full-window payload. An `ok` payload
contains `status`, integer `count[S]`, numeric `sum[S]` and numeric `m2[S]`. The moments are
reduced in interval order: sum, divide by count to get the mean, then sum the squared deviations
from that mean; a one-interval block has `m2 = 0`.
An unusable curve from which no reduction ran carries no fabricated payload or layout.

A non-positive prior equity skips an interval and gives a status-only payload
`{"status": "non_positive_equity"}`. A non-finite equity, derived return or moment gives
`{"status": "non_finite"}`. Neither status carries arrays or changes a successful trial into
a failed one. The Python module refuses the former as `pbo_non_positive_equity` and the latter
as `pbo_rows_incomplete`, naming the affected trial ids.

The result-level `return_blocks` object has exactly seven wire keys: `contract`, `series`, `S`,
`K`, `n_b`, `boundary_time_ms` and `numeric_build_identity`. Its contract is
`pineforge-hpo-return-blocks/v1` and its series is `bar`. Counts, sizes and boundary times are
JSON integers. The layout comes from the lowest-id eligible completed full-window snapshot
with a usable layout, including a constraint-violating trial; it is kept even if trial retention
discards that row. Every completed layout must agree with it and every published count array
must equal its `n_b`. A disagreement refuses the result with exit 1 and `hpo_report_invalid`.
Without such a snapshot the result omits `return_blocks` and does not claim usable PBO input.

Blocks and return statistics share the return-statistics numeric build identity. Adding the
block reducer grew the hashed reducer source and changed that identity value. Evidence naming
the previous value applies to its own version; it does not validate the new value.

## What is computed

The input is an N x S table of block moments, one record per trial, never the T x N return
matrix. The S blocks are disjoint, consecutive groups of the return intervals of one series.

1. Enumerate all C(S, S/2) ordered splits (paper, eq. 2.3). The training set of a split is a
   subset of S/2 blocks in their original order and the testing set is the complement. Every
   training set is also used once as a testing set.
2. For each split and each trial, merge the trial's block moments over the training blocks
   (and, separately, over the testing blocks) into one pooled `(n, sum, M2)` and compute the
   ranking statistic (default D1).
3. The training winner is the trial with the largest training statistic (ties: default D4).
   Its out-of-sample rank among all N trials, `rank`, is the number of trials with a smaller
   testing statistic plus one, with ties by mid-rank. `omega = rank / (N + 1)` lies strictly
   between 0 and 1, and `lambda = ln(omega / (1 - omega))`.
4. Collect the lambdas of all splits. `n_lt0`, `n_eq0` and `n_gt0` count the splits with
   lambda below, equal to and above 0 (weighted, see D4). The primary rate `pbo` is
   `(n_lt0 + n_eq0) / n_splits`, the share of splits whose winner did not beat the median
   out of sample (default D3). `rate_lt0 = n_lt0 / n_splits` is reported next to it.

`lambda > 0` means the winner out-performed the median of the trials out of sample (p. 13).
The paper's customary 0.05 reference (p. 14) is not applied by the module: there is no
pass/fail threshold.

## Input

`load_moment_table(parts, S=..., roster=...)` takes the rows in parts. A part is a pair
`(declared object, rows)`: the result-level `return_blocks` object that the rows of the part were
reduced under (next section), and a list of row objects. A call whose rows all come from one run
passes a list of one pair. A row is:

```
{"trial_id": 7, "status": "ok",
 "block_returns": {"status": "ok", "count": [..S ints..], "sum": [..S numbers..], "m2": [..S numbers..]}}
```

The parts and the roster are required inputs that declare what the rows must be. Neither has a
default: a call that leaves one out, or a part whose declared object is `None`, is refused with
`pbo_parameter_missing`. `pbo` takes the same parts and does not take a table (see The merged
table).

* `count[b]` is the number of intervals in block `b`, `sum[b]` the sum of their returns and
  `m2[b]` the sum of squared deviations from the block mean.
* The module never subsets: one refused row refuses the whole roster (default D5). Rows may
  come in any order; they are sorted by `trial_id` (all integers or all strings, unique).
* Row statuses (default D5). A row with status `ok` or `constraint_violation` is computed: a trial
  that violated a constraint was still tried, so it belongs to the roster (the paper's p. 24). A
  row refuses, with the code of its own named under Refusals, when its status is a failed-trial
  status (`error`, `objective_error`, `constraint_error`, `engine_error`, `trial_error` or
  `trial_timeout`), `pruned` or `partial`, or when its `block_returns` status is
  `non_positive_equity` (a prior equity at or below zero skipped an interval). A `non_finite`
  payload refuses as incomplete, with the affected trial ids. A
  missing, non-text or unknown row status, a missing or null payload, or any other payload status
  than `ok` refuses as incomplete.
* Input rules of this module: `S` is an even integer from 2; the roster holds at least 2 trials;
  every row has a status as above and its `block_returns` has status `ok`; the three arrays have
  length `S`; every count is an integer from 1 to 2^53 - 1 and equals the declared `n_b` of its
  block; every sum and `m2` is finite and `m2` is not negative; a one-interval block has
  `m2 = 0`; the `S/2` smallest blocks hold at least 2 intervals together (otherwise the sample
  deviation of a half is undefined).
* Block sizes may differ by at most one interval (default D2): the largest `n_b` minus the
  smallest is at most 1, and a larger spread refuses with `pbo_block_sizes_uneven`. This is a
  deviation from the paper's equal blocks and is labelled: when the sizes differ, the result says
  `equal_blocks: false` and names `block_counts`. The module checks the spread and that the sizes
  add up to `K`, and nothing else about the layout: block **placement**, that is which blocks
  hold the extra interval, is not checked. The floor rule of the pin (dossier clause 3.2:
  interval `k` belongs to block `floor(k S / K)`) places them in one fixed way, and a layout it
  would never produce is accepted when its sizes pass. The fixture `uneven_s2_n3.json` is such a
  layout: it has `n_b = (2, 3)` for `K = 5`, where the floor rule gives `(3, 2)`.

### The roster, the parts and the declared object

Contract clause 3.5 gives the eligibility checks to PBO, so the module does not trust the caller
to have checked the lineage. The caller knows which rows came from which part, so the module is
handed the parts and what each part declares, and checks what it can then see.

* `roster` is the declared trial set: a list (or tuple or set) of at least 2 trial ids, all
  integers or all strings, none repeated. The rows of all the parts together must be exactly
  these trials, no more and no fewer. A declared trial without a row (a gap, an absent part), a
  row that is not declared (an unknown part) and a trial id that occurs in more than one row, in
  one part or in two (a duplicate), refuse with `pbo_roster_mismatch`, in any mix: the refusal
  names every id of the three kinds, once each, and its `detail` gives the count and the first id
  of each kind that is present (clause 3.5 lists the gap, the duplicate, the absent and the
  unknown part as differences of the declared trial set).
* The declared object of a part is the result-level `return_blocks` object of the run that
  produced the rows of that part, plus one key that the caller adds, `dataset_identity`: exactly
  these keys (an absent or an unknown key refuses):

  | Key | Meaning |
  | --- | --- |
  | `contract` | `pineforge-hpo-return-blocks/v1` |
  | `series` | `bar` (the only series this version reads) |
  | `S` | the number of blocks; it equals the `S` of the call |
  | `K` | the number of return intervals, an integer of at least `S` that equals the sum of `n_b` |
  | `n_b` | `S` integers from 1: the number of intervals in each block; the largest minus the smallest is at most 1 (default D2); only that spread and the sum are checked, not which blocks are the larger ones |
  | `boundary_time_ms` | `S + 1` integers that do not decrease: the time of every block boundary, in milliseconds |
  | `numeric_build_identity` | non-empty text: the identity of the build that reduced the rows |
  | `dataset_identity` | non-empty text: the identity of the dataset the rows were reduced over. It is **not** a key of the wire: the caller states it for each part (clause 3.5 refuses a dataset that differs between parts, and `K` and the time axis alone cannot tell two datasets of the same length apart). The module compares the texts and cannot know that a text names the data |

  Every row's `count` array must equal the `n_b` of the object of its own part. A row whose
  count differs, whatever the other rows hold, refuses with `pbo_block_mismatch` (naming its
  part when the call has several); the first row no longer defines the counts.
* The objects of all the parts must agree: clause 3.5 refuses a `return_blocks`, dataset, window
  or identity that differs between parts. The module compares the declared objects of the parts
  field by field, in this order: `K` and `dataset_identity` (`pbo_dataset_mismatch`), `n_b`
  (`pbo_return_blocks_mismatch`), `boundary_time_ms`, the window (`pbo_window_mismatch`) and
  `numeric_build_identity` (`pbo_identity_mismatch`). A field that is not the same in every part
  refuses before any row is read, with the code of the first such field. The refusal names every
  differing key in `names` and has one entry per code in `refusals`; it names no trial, because
  no trial is the offender, and carries no coverage, because no row was examined. Every part is
  compared, also a part that holds no row, and the refusal does not depend on the order of the
  parts (only its `detail`, which names part positions, does). With one part there is nothing to
  compare.
* A contract other than `pineforge-hpo-return-blocks/v1`, a series other than `bar`, or an `S`
  other than the call's refuses with `pbo_return_blocks_mismatch`, in whichever part it is. A
  malformed object refuses with `pbo_input_invalid` and the position of its part (the first
  malformed one, in the order given), and an array of the wrong length with `pbo_block_mismatch`.

What the module cannot know: the grouping of the rows into parts, and the object declared for
each part, are the caller's. It checks that the parts agree and that every row matches the object
of its own part; it cannot tell that a part's declared object is the one its rows were really
reduced under, and rows that the caller puts into one part are taken to belong to that part's
object. The same holds for the dataset: the module sees `K`, the times of the block boundaries
and the `dataset_identity` text that the caller states, so two datasets that carry the same
length, the same time axis and the same text are not distinguished by anything the module can see.

### Native wire

The pinned wire (dossier clause 3.3) fixes the row key `block_returns` (`status`, `count[S]`,
`sum[S]`, `m2[S]`) and the result-level object `return_blocks` (contract
`pineforge-hpo-return-blocks/v1`, series, `S`, `K`, `n_b[]`, `boundary_time_ms[S+1]` and "own
numeric identity"). The native writer uses the following texts and names, matching the module's
input checks. A different spelling refuses instead of computing a number.

| Reading | What the module does | If the wire says otherwise |
| --- | --- | --- |
| `ok` is the status text of a usable `block_returns` payload; `non_positive_equity` and `non_finite` are status-only payloads | computes `ok`; refuses `non_positive_equity` as `pbo_non_positive_equity` and `non_finite` as `pbo_rows_incomplete` | every row refuses (`pbo_rows_incomplete`) |
| `numeric_build_identity` is the key that holds the "own numeric identity" of the pin | requires that key in the declared object | the declared object refuses (`pbo_input_invalid`: an absent key and an unknown key) |
| `bar` is the text of the series | accepts the series `bar` only | the declared object refuses (`pbo_return_blocks_mismatch`) |
| a `return_blocks` key on each row, as the carrier of the object of the part the row came from | removed: an earlier version of this module read it, but the pinned row wire carries only `block_returns` | the module reads no key of a row except `trial_id`, `status` and `block_returns` and ignores the others; the declared object of a part is handed over with the part |

### The merged table

`load_moment_table` returns a `MomentTable`: the rows of all the parts, sorted by trial id, with
the one declared object that the parts agreed on. `pbo` does not accept a table; it takes the
parts. A table has lost its parts (it holds one declared object for all its rows), so it could
show no difference between parts, and a table built by hand is only what its builder declares.
A table checks itself whenever it is built, by whoever builds it: at least 2 trials (an empty
table refuses with `pbo_too_few_trials`), unique ascending ids of one type, `sums` and `m2` with
one row of `S` entries per trial, counts equal to the `n_b` of its declared object, finite
moments, and the declared object itself (including the half-size rule). Its `input_sha256` is
not an argument: it is computed from the content, so it cannot disagree with it.

## Defaults

Seven reversible defaults are adopted. Each names the alternative that a ruling could choose
instead.

| ID | Default | Alternative |
| --- | --- | --- |
| D1 | Ranking statistic: pooled mean over sample sigma across the intervals of the union of blocks (not an average of block Sharpe ratios), risk-free rate 0, unannualised. PBO ranks this statistic, not the objective of the study; any difference must be labelled | the excess Sharpe ratio of the study, which needs the period basis in the payload |
| D2 | Block sizes may differ by at most one interval (labelled a deviation from the paper's equal blocks: `equal_blocks` and `block_counts` in the result); a larger spread refuses. Only the spread and the sum are checked, not which blocks are the larger ones | refuse unless S divides the number of intervals |
| D3 | Mass at lambda = 0 is reported: `n_lt0`, `n_eq0`, `n_gt0` and both rates; the primary rate is lambda <= 0 (the conservative reading of the closed integral on p. 13) | primary rate lambda < 0 |
| D4 | Ties: the training winner is a mixture with weight 1/k on each of k tied trials (order-invariant; counts become fractional); the out-of-sample rank is the mid-rank; equality is exact binary64 equality, with no tolerance | lowest trial id wins |
| D5 | Roster: every trial of the lineage, constraint-violating ones included (the paper's p. 24: the trials that were tried); any error, pruned, partial or non-positive-equity row refuses, each with a code of its own; no subset PBO. Frequent refusals are expected and are never relaxed silently | none named |
| D6 | Adaptive rosters are computed only as `diagnostic_adaptive_roster`: the samplers `tpe`, `dlib_global` and `candidates`, and any study whose result had a no-improvement early stop, which is its own input (the `early_stop` object that sits beside the sampler in the native result) and not a sampler name; the label `paper_conformant` is used only for `grid`, `random` and `sobol` without an early stop; an unknown or missing sampler refuses, and so does a call that does not say whether there was an early stop (`pbo` and `roster_label` alike: there is no default) | none named |
| D7 | The block payload is the block-centred `M2`, merged left to right in block order by the Chan/Pebay rule with a pinned operation order and no fused operations; a sum of squares is rejected as unstable on near-constant series | `(n, sum, sum of squares)` |

### Merge and statistic (D1, D7)

For blocks `a` before `b` with counts `na`, `nb`, sums `sa`, `sb` and `M2a`, `M2b`, in binary64:

```
n  = na + nb
d  = sb / nb - sa / na
t  = d * d
t  = t * na
t  = t * nb
t  = t / n
M2 = (M2a + M2b) + t
sum = sa + sb
```

Blocks of a union are merged left to right in ascending block order, starting from the first
block's own `(n, sum, M2)`. Each line is a separate binary64 operation, so nothing is fused. The
term `t` is never negative, so `M2` never is. The statistic of a pooled `(n, sum, M2)` is

```
statistic = (sum / n) / sqrt(M2 / (n - 1))
```

A half whose pooled `M2` is zero (or whose sample variance underflows to zero) refuses with
`pbo_zero_variance`; a flat trial is never imputed.

## Result

`pbo(parts, S=..., roster=..., sampler=..., early_stop=..., max_work=None, with_lambdas=False)`
takes the parts and the declared roster, and returns a plain, JSON-serialisable dict. A call
with one part passes a list of one `(declared object, rows)` pair. `sampler` is the sampler kind
of the study and `early_stop` is the `early_stop` object of the result, or `None` when the result
has none (`True` and `False` are accepted too); both are required, because the roster label
depends on them:

| Field | Meaning |
| --- | --- |
| `contract` | `pineforge-hpo-pbo/v1` |
| `algorithm` | `cscv-all-ordered-splits/v1` |
| `status` | `ok` (a refusal raises `PboError` and returns nothing partial) |
| `S`, `N`, `n_splits` | blocks, trials and `C(S, S/2)` exactly |
| `equal_blocks`, `block_counts` | whether every block holds the same number of intervals, and the interval count of every block (`n_b`); `equal_blocks: false` labels the deviation of default D2 |
| `statistic` | `pooled-mean-over-sample-sigma/v1` |
| `tie_rules` | `equality: exact_binary64`, `train_best: mixture_weight_1_over_k`, `oos_rank: mid_rank` |
| `n_lt0`, `n_eq0`, `n_gt0` | weighted split counts; an integer unless a tie made it fractional |
| `pbo`, `rate_le0`, `rate_lt0` | the primary rate (equal to `rate_le0`) and the strict rate |
| `calibrated` | always `false` |
| `interpretation` | fixed words, see below |
| `sampler`, `early_stop`, `roster_label` | the declared sampler, whether the study had an early stop (a boolean), and the label of default D6: `diagnostic_adaptive_roster` whenever the sampler is adaptive or `early_stop` is true, else `paper_conformant` |
| `work_estimate` | the admission figure |
| `input_sha256` | digest of the canonical input (the rows' moments) |
| `return_blocks_sha256` | SHA-256 of the compact, key-sorted, ASCII JSON of the validated declared object that all the parts declared |
| `coverage` | `{declared, supplied, usable}`: the trials the roster declares, the rows supplied and the declared trials whose row had no problem; equal to `N` three times in a result, because a refusal is raised otherwise |
| `refusals` | always `[]` in a result; a refusal lists its reasons in `PboError.refusals` (see Refusals) |
| `lambdas`, `lambda_tie_sizes` | only with `with_lambdas=True`: one entry per tied winner of each split, in the order of `iter_ordered_splits`, and the size `k` of its tie (its weight is `1/k`) |

`interpretation` reads: a deterministic frequency over the re-labellings of one dataset into
training and testing halves; the composition of the trial roster moves it; for adaptive or
correlated trials it is a diagnostic, not a probability under independent strategy draws.

`input_sha256` is the SHA-256 over the concatenation, in ascending byte order, of the raw 32-byte
SHA-256 digests of the rows. A row's digest is taken over the compact, key-sorted, ASCII JSON of
`[trial_id, count, sum, m2]`, with counts as integers and sums and `m2` as binary64 numbers. It
does not depend on the row order.

The order of the splits is fixed: the half that holds block 0 is taken in lexicographic order of
its block indices, first as training set and then as testing set. For S = 4 that is training
`{0,1}`, `{2,3}`, `{0,2}`, `{1,3}`, `{0,3}`, `{1,2}`. Each complementary pair of halves is
evaluated once and used in both directions.

## Exactness

* Exact and platform independent: `n_splits`, the work figure, the enumeration of splits, the
  integer rank arithmetic that classifies every split as lambda below, equal to or above 0,
  therefore every count and every rate (an exact fraction converted once to binary64), and the
  odds `omega / (1 - omega)`, which are formed as a quotient of two integers (correctly rounded).
* Binary64 operations in the pinned order: the merge and the statistic. Addition, subtraction,
  multiplication, division and square root are correctly rounded in IEEE 754 binary64, nothing is
  fused, and no tolerance is used anywhere, so equal inputs give equal bits on conforming
  platforms. Ties are decided by exact equality of these values.
* Depends on the platform math library: only the logarithm that turns the odds into a lambda
  value (the `lambdas` list). The counts and rates do not depend on it. `ln(1)` is exactly 0.

## What is not claimed

* The result is not calibrated: there is no standard error and no pass/fail threshold, and it is
  not a significance level.
* It is a deterministic frequency over re-labellings of one dataset. The roster moves it, so
  hiding or adding trials changes it (p. 24). For adaptive or correlated trials it is a
  diagnostic, not a probability under independent strategy draws.
* The blocks of one series are not independent (the closing equity of one block is the base of
  the next; p. 23).
* The ranked statistic is the one of D1, not necessarily the objective the trials were chosen
  by.
* Left open and not decided here: whether N below the paper's "N >> 10" needs a floor above the
  refusal of N < 2 (the paper gives none); the paper's own inconsistencies (12,780 splits on pp.
  11 and 22 where eq. 2.3 gives C(16, 8) = 12,870, which is used; "mean" on p. 28 versus the
  median elsewhere); noise-level and planted-winner checks, which are descriptive only and never
  an acceptance oracle; execution verification of the native curve-to-moments path; and
  window-selected input, which is not part of this version.
* The module checks the parts against the roster and against each other; it cannot know that the
  roster is the lineage's true trial set, only that the rows of all the parts are exactly the
  roster. It sees the parts the caller hands over and the object the caller declares for each: a
  difference between parts in `return_blocks`, dataset, window or identity is found when the
  caller kept the parts apart and declared each one's object, and not otherwise (see the limits
  under Input).

## Refusals

A refusal raises `PboError(code, detail, ids, names)`; `ids` are the offending trial ids in
ascending order (possibly empty) and `names` the offending call parameters or object keys
(possibly empty). Two more attributes carry what the contract asks a refusal to name (clause 3.5:
coverage counts per reason, and the offending ids):

* `refusals` lists every kind of problem that was found, in the order below, as
  `{"code", "count", "ids"}` (the number of rows or ids of that kind, and which); the first entry
  is the raised `code` with its `ids`. A refusal that was not found among rows has the one entry
  of its own code (counting its ids, or 1 when it has none). A difference between parts has one
  entry per code, in the order of the comparison (`K`, `dataset_identity`, `n_b`,
  `boundary_time_ms`, `numeric_build_identity`), whose `count` is the number of differing keys of
  that code and whose `ids` are empty.
* `coverage` is `{"declared", "supplied", "usable"}` when the rows were examined (the trials the
  roster declares, the rows supplied, and the declared trials whose row had no problem; a trial
  with more than one row is not usable), else `None`. A refusal raised after the rows were
  accepted (`pbo_zero_variance`, `pbo_resource_exceeded`, a pooled value that is not finite)
  carries it too, with all three numbers equal to `N`; a refusal raised before the rows could be
  counted (a call parameter, a declared object, a difference between parts, the rows of a part
  that are not a list, a row that is not an object with a trial id, trial ids that mix types)
  has `None`.

`PboError.document()` renders the refusal as a JSON-ready dict with `status: "refused"`, `code`,
`detail`, `ids`, `names`, `coverage` and `refusals`, and no number of the analysis. When several
kinds of row problem occur together, the first kind in the order
`pbo_input_invalid`, `pbo_roster_mismatch`, `pbo_status_error`, `pbo_status_pruned`,
`pbo_status_partial`, `pbo_non_positive_equity`, `pbo_rows_incomplete`, `pbo_block_mismatch` is
reported with all of its ids, so a refusal does not depend on the row order. The checks on the
call itself run before any row is read, in this order: `S`, `max_work`, `early_stop`, the
sampler, the roster, then the parts: the shape of `parts`, the declared object of each part in
the order given (the first malformed one refuses), and the comparison of the declared objects of
the parts with each other.

Every `pbo_*` code above is a module-local member of `pineforge_hpo.pbo.CODES`, not a member of the
release-gated failure catalog (`hpo_failure_codes.json`), as is the case for the sibling G2 modules
(their pins fix that: module codes are not catalog members, and a new `hpo_*` code touches shared,
release-gated files). Promoting these codes into the catalog is a separate, later change with its
own regeneration, `since` values and `hpo_*` names, and only if this module's API ships as a
protocol of its own.

| Code | Raised when |
| --- | --- |
| `pbo_input_invalid` | `parts` is not a list of `(declared object, rows)` pairs (a table is not accepted either); the rows of a part are not a list of objects; a trial id is missing, mistyped or mixed; a count, sum or `m2` is not valid; `S` is not an integer from 2; `max_work` is not a non-negative number; the roster is not a list of unique, uniformly typed ids; `early_stop` is not `None`, a boolean or an object; a declared object is malformed (not an object, an absent or unknown key, a field of the wrong type or value, a `K` that is not the sum of `n_b`, boundary times that decrease); a half could hold fewer than 2 intervals; a merged value is not finite |
| `pbo_parameter_missing` | `roster` is absent or `None`, `parts` is `None`, the declared object of a part is `None` (`names` holds `return_blocks`), or `early_stop` is not passed at all to `pbo` or to `roster_label` (`names` holds which; `None` is a value for `early_stop`: no early stop) |
| `pbo_roster_mismatch` | the rows of all the parts together are not exactly the roster: a declared trial has no row, a row is not declared, or a trial id occurs in more than one row (`ids` holds every id of the three kinds; the `detail` says which kinds) |
| `pbo_return_blocks_mismatch` | the contract, the series or `S` of a declared object is not the one this version reads, or the block layout `n_b` differs between the parts while `K` is equal |
| `pbo_block_sizes_uneven` | the largest `n_b` minus the smallest is more than 1 (default D2) in a declared object |
| `pbo_dataset_mismatch` | the interval count `K` or the `dataset_identity` of the declared objects differs between the parts |
| `pbo_window_mismatch` | `boundary_time_ms` differs between the parts |
| `pbo_identity_mismatch` | `numeric_build_identity` differs between the parts |
| `pbo_status_error` | a row has a failed-trial status: `error`, `objective_error`, `constraint_error`, `engine_error`, `trial_error` or `trial_timeout` |
| `pbo_status_pruned` | a row has status `pruned` |
| `pbo_status_partial` | a row has status `partial` |
| `pbo_non_positive_equity` | a row's `block_returns` status is `non_positive_equity` |
| `pbo_rows_incomplete` | a row has no status, a status that is not text or not a trial status, no `block_returns` payload, a null payload, or a payload status other than `ok` and `non_positive_equity` |
| `pbo_odd_s` | `S` is odd |
| `pbo_too_few_trials` | the roster declares fewer than 2 trials |
| `pbo_zero_variance` | a half of some split has zero sample variance for some trial |
| `pbo_block_mismatch` | an array does not have `S` entries (a row's, or the object's `n_b` or `boundary_time_ms`), or a row's counts differ from the `n_b` of the object of its own part |
| `pbo_resource_exceeded` | the work figure is above the caller's `max_work` |
| `pbo_sampler_unknown` | the sampler is missing or not one of `grid`, `random`, `sobol`, `tpe`, `dlib_global`, `candidates` (`early_stop` is not a sampler name) |

## Resource rule

`n_splits = C(S, S/2)` is computed exactly (`math.comb`) before any split is evaluated, together
with the work figure of `estimate_work(S, N)`:

```
merges      = (n_splits / 2) * N * (S - 2)
comparisons = 2 * n_splits * N
work        = merges + comparisons
```

Each complementary pair is evaluated once, so a half costs `S/2 - 1` merges per trial; the
selected trial's rank is a count over the N testing statistics, not a sort. For S = 16 and
N = 1000 that is 90,090,000 merges and 25,740,000 comparisons; for S = 24 and N = 1000 about
3.0e10 merges. The module refuses with `pbo_resource_exceeded` only when the caller passes
`max_work` and the figure is larger. There is no built-in cap: without `max_work` a very large S
simply runs for as long as it takes.

Memory is the table of N x S sums and N x S `M2` values plus S counts, and O(N) for the
statistics of one pair of halves. The lambda lists, which grow with `n_splits`, exist only when
`with_lambdas=True`.

## Programming interface

`load_moment_table`, `pbo`, `MomentTable`, `merge_moments`, `pooled_moments`,
`ranking_statistic`, `estimate_work`, `iter_ordered_splits`, `roster_label` and `PboError` in
`pineforge_hpo.pbo`. The refusal codes are module-local, like those of `pareto` and
`neighbourhood`: they are listed in `pineforge_hpo.pbo.CODES` and are not members of the failure
catalog (see [Stable failure codes](failure-codes.md)). The tests are in
`tests/python/test_pbo.py`; their fixtures are the small JSON files in
`tests/python/fixtures/pbo/`, each with its hand count written in the test that uses it. The
native wire tests are in `tests/test_pbo_blocks_cli.py`, using supplied synthetic equity curves
and hand-derived moment literals. These contracts remain subject to execution verification.

## What an independent oracle confirms

`tests/g2_oracles/pbo_oracle.py` recomputes this module's quantities by a DIFFERENT method: a
bitmask scan and Pascal's triangle instead of combinations; an exact two-pass and
within-plus-between decomposition instead of a pairwise merge; the exact
`sign(mean) * mean^2 / var` without the square root; exact mid-ranks and odds. Where it and this
module agree, that agreement is evidence for the enumeration of splits, the pooled moments, the
counts, the rates, the odds behind each lambda and the tie sizes -- including pooled means that
are negative, zero or change sign. The ties that are compared with the module are those through
identical block moments (the fixture `tie_s2_n3`, and the random cases, which copy whole trials
and single blocks from earlier trials) and ties at a zero mean (kept in the random cases when
they occur). A random case in which two trials tie exactly through different block moments,
other than at a zero mean, is set aside, replaced and counted: no case compares such a tie with
the module.

It is NOT evidence for what the two routes share:

- the logarithm that turns the odds into a lambda: both sides call the platform's `math.log` on
  the same correctly rounded odds, and so does the module's test, so agreement on the lambdas
  confirms the odds and the order of the list, not the logarithm;
- the conversion of a count or a rate to binary64: both sides convert the same exact fraction
  once;
- the work figure: the oracle replays the accounting formula of the Resource rule above by
  loops, so agreement is by definition, anchored at the four points that `test_work_figures`
  pins.

The seeded random cases do not reach these either. Each one whose result the oracle computes
compares ten fields of the result (`n_splits`, the counts, the rates, the lambdas, the tie sizes,
`work_estimate`, `equal_blocks`, `block_counts`, `pbo` and `coverage`) of the module with this
same oracle, not the whole document, and the three items above are the same on both sides. They
rest on the hand-derived fixture values of `tests/python/test_pbo.py` and, for the logarithm, on
the platform's math library, and must be cited as that.

`tests/g2_oracles/pbo_real_rows.py` compares a real lineage. Read its exit code as: 0 AGREE; 1
DISAGREE, or a failure; 2 a refusal, either the module's (`MODULE REFUSED`) or the script's own
for a result that keeps only some of its trials (`REFUSED BY THE SCRIPT`), which is also the code
of a usage error; 3 BLOCKED, no row carries a `block_returns` payload, as with native input that
never requested blocks, so nothing was compared; 4 the oracle was skipped
as too large (`--oracle-limit`), so the module's numbers are not cross-checked; 5 the rows are
not native output; 6 INCONCLUSIVE (nothing disagrees, but a comparison that exact ties touch
differs inside the range they allow). A run that compared nothing never exits 0.
