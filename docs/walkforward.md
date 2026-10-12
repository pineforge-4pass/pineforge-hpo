# Walk-forward analysis of finished per-window studies (`pineforge_hpo.walkforward`)

This page documents python/pineforge_hpo/walkforward.py. Its tests run with the package's unit tests;
an independent oracle in tests/g3_oracles recomputes its numbers with exact arithmetic and is run by hand.
The contract id is
`pineforge-hpo-walkforward/v1` and the algorithm name is `exact_per_window_v1`.

## What it computes

`walkforward` summarises a sequence of finished studies, one per time window, that the caller's own
application has already run. For each window it reports the row this contract's rule selects, for each
successive pair of supplied windows it reports whether the next window's rows hold a row with the
previous window's selected parameter vector (that row with its rank, or no row with a reason), and it
lists how the selected vectors differ across the windows.

Version 1 is post hoc. It **runs no window and evaluates nothing**: it starts no engine, runs no
strategy, writes no file, keeps no state and changes no native result or trials file. The only side
effect is reading the files named. It is a library function; there is no command-line flag and no
field in a native result.

```python
from pineforge_hpo.walkforward import WalkforwardError, walkforward

try:
    report = walkforward(
        ["2024-01", "2024-02", "2024-03"],
        {
            "2024-01": "result-2024-01.json",
            "2024-02": "result-2024-02.json",
            # "2024-03" was never run: it is reported as never_run
        },
        metric="objective",
        direction="maximize",
        space=None,
        max_work=10_000_000,
    )
except WalkforwardError as refusal:
    print(refusal.code, refusal.detail, refusal.ids, refusal.names)
else:
    print(report["per_window"], report["carry_over"], report["stability"])
```

Signature: `walkforward(windows, sources, *, metric, direction, space=None, max_work=None)`. `windows`
and `sources` are required positionally and `metric` and `direction` are required keyword arguments,
all with no default: leaving one out raises Python's `TypeError`, and a `None` passed in is refused as
`walkforward_parameter_missing`.

| Argument | Meaning |
| --- | --- |
| `windows` | The plan: a list (or tuple) of at least one distinct, non-empty text label, in plan order. The caller declares the expected windows; no label is derived from a file name, a path, a row field, a trial id or a time value. |
| `sources` | A mapping label → source. A source is a path to a complete result JSON or a rows JSONL file, a parsed result, or a parsed list of rows. A key not in `windows` is refused; a plan label with no key is `never_run`. |
| `metric` | `"objective"` or a metric name spelled as recorded in the rows' `metrics` object. |
| `direction` | `maximize` or `minimize`. Required, no default. |
| `space` | A recorded-space dict or a `StudySpec`, used only when no row or result records a space. |
| `max_work` | `None`, or a positive integer bound on the admission figure. |

## Input

A finished study for one window is a complete result (`trials_out` `all`), a rows JSONL file, or a
parsed result/list of rows, read by the shared rows reader with the checks every continuation load
applies: strict JSON, the nine statuses, `feasible` equal to status `ok`, a finite objective for `ok`
and `constraint_violation` rows, unique ids inside a source. All recorded spaces of the supplied
windows must have an equal canonical form (which includes the objective, its direction and its
constraints), else the call is refused naming the labels of every window involved. Every row of a
supplied window that records a `space_hash` must record the same one (a result's own top-level
`space_hash` is not read); a difference is refused the same way, naming the labels and `space_hash`.
The reference hash is the `space_hash` of the row with the lowest (plan position of its window,
`trial_id`) among the rows that record one. The involved windows are the reference row's window and
every window holding a row whose `space_hash` differs from it. `ids` are the trial ids of those
differing rows, ascending: one id per differing row, so an id that occurs in several windows is
listed once for each. Bar and timeframe counters are compared inside one window
only, never across windows: windows cover different bars by construction, and their timeframes are
properties of each window's own study, so nothing here claims the windows overlap, touch or were run
over the data their labels suggest. For `metric="objective"` the passed direction must equal every
recorded direction, checked in this order: the common space's recorded objective direction first
(a passed space's direction is not compared), then each supplied source's own recorded direction, in
plan order; else the call is refused.

## Which rows compete

Only rows with status `ok` and a usable metric cell compete. The first reason that applies decides:

| Row | Treatment | Reason in the result |
| --- | --- | --- |
| status not `ok` | excluded | `status_<status>` |
| `ok`, the cell absent | excluded | `metric_not_recorded` |
| `ok`, the cell `null` | excluded | `metric_unavailable` |
| `ok`, the cell not a finite number (a bool, string, array, object, `NaN` or `Infinity` token, overflow) | the whole call is refused | `walkforward_input_invalid` |
| `ok`, the cell a finite number | eligible | none |

The cell is the row's `objective` when `metric` is `"objective"`, else `metrics[metric]`. Integers
read as binary64; an integer above 2^53 may round. A `true`/`false` is not a number here.

## Selection, carry-over and stability

**Selection.** Per window: the eligible row with the best value in `direction`, ties to the lowest
`trial_id` — for `maximize` the largest value wins and for `minimize` the smallest. A window with no
eligible row is listed with `no_eligible_rows` and `selected: null`; that is not an error. When no
supplied window has an eligible row, the call is refused `walkforward_no_eligible_rows`.

**Carry-over.** For two supplied windows adjacent in the plan whose first window has a selected row,
the previous window's selected vector is looked up in the next window's rows by the exact canonical
parameter key (no tolerance, `-0.0` equals `+0.0`, an integer in a real dimension is that real).
Three outcomes:

* an eligible row with that key — or several that all record the same value, in which case the lowest
  `trial_id` is reported: `row_found` true, with that row's `trial_id`, its `value` and its
  **mid-rank** among the next window's eligible rows — `better + (equal + 1) / 2`, where `better`
  counts strictly better values (strictly greater for `maximize`, strictly less for `minimize`) and
  `equal` counts values equal to it (including itself), so a unique best is 1.0 and two tied at the
  top are 1.5;
* rows with that key but none of them eligible: `row_found` false, with the reason of the lowest-id
  such row (`status_<status>`, `metric_not_recorded` or `metric_unavailable`);
* no row with that key: `row_found` false, reason `no_row`.

When `row_found` is false, `parameters` reports the source window's selected parameters: the
vector that was looked up. When true, it reports the matching target row's parameters as recorded.

The pair is reported whenever both windows are supplied, they are adjacent in the plan and the first
has a selected row — including when the next window has no eligible row at all. Two eligible rows of
the target window that carry the key and record **different** values refuse the call
(`walkforward_duplicate_vector`), because the value and the rank would then depend on an arbitrary
pick; equal values are not a refusal. A vector with no eligible row is never counted as zero and never
dropped. A `never_run` window between two supplied windows breaks the chain: the gap is listed in
`chain_breaks`, never skipped silently. A supplied window with no selected row is the source of no
pair and is **not** a break: its own block says `no_eligible_rows` and it is named in
`stability.windows_without_selection`. The module evaluates nothing.

**Stability.** Per dimension of the space: the selected values in plan order, as the rows record them,
the number of distinct typed scalars (an integer in a real dimension is one value with that real, and
`-0.0` equals `+0.0`), and, for integer and real dimensions, the exact minimum and maximum — the value
at the earliest window among those equal to the extreme (`null` for boolean and categorical).
Both numeric kinds use exact comparison of the recorded values, without converting integers to
binary64 for the extrema. Dimension names follow code-point order, which equals UTF-8 byte order
for encodable names. No clustering, no averaging of parameters, no interpolation, no claim of robustness.
For real dimensions, `distinct` uses binary64 identity, while the extrema use exact numeric
comparison of the recorded values: `minimum != maximum` with `distinct == 1` is possible.

## Result

| Field | Content |
| --- | --- |
| `contract` | `pineforge-hpo-walkforward/v1` |
| `status` | `ok` (a refused call raises) |
| `metric`, `direction` | the caller's values |
| `windows` | the plan as a list, in plan order, including when a tuple was passed |
| `supplied` | the plan labels with a source, in plan order |
| `never_run` | the plan labels with no source, in plan order |
| `rows_read`, `rows_eligible`, `rows_excluded` | counts over the supplied windows |
| `per_window` | one entry per supplied window: label, the three counts, `excluded` by reason with ids, `selected` (`trial_id`, `value`, `parameters`) or `null`, `no_eligible_rows` |
| `carry_over` | one entry per pair: labels, the vector key and parameters, `row_found`, `trial_id`, `value`, `better`, `equal`, `rank_mid`, `reason` |
| `chain_breaks` | pairs of consecutive supplied windows not adjacent in the plan, with the skipped labels |
| `stability` | per dimension: `kind`, `values`, `distinct`, `minimum`, `maximum`, plus `windows` and `windows_without_selection` |
| `work` | the admission figure |
| `algorithm` | `exact_per_window_v1` |
| `input_sha256` | digest of every row read, see below |
| `interpretation` | the fixed text of "What is not claimed" |

`input_sha256` is the SHA-256 of the concatenation of the sorted per-row digests. A per-row digest is
the 32-byte SHA-256 of the compact, key-sorted, ASCII-only JSON of
`[window label, trial_id, status, cell]`, where the cell is the number written as its shortest
round-trip binary64 text (an integer as a float, `10.0`) or `null` when absent or `null`. Every row
read is covered, all statuses; the metric name and the direction are not in the digest, and neither is
the plan order of `windows` — an entry holds its window's label, and the per-row digests are sorted
before they are joined.

## Determinism

Row order inside a window, the key order of `sources` and repeated calls give the same document and
the same digest. The plan order of `windows` and the caller's labels are part of the input: the
listings and the plan order of `per_window`, `supplied`, `never_run`, `carry_over` and `chain_breaks`
follow the plan, while `input_sha256` does not depend on it. Nothing is random; no `log`, `exp` or
`pow` is called.
The labels in a `walkforward_window_unknown` refusal follow code-point order, using the string form
of non-string keys; labels with the same text are ordered by the name of their type.

## Refusals

A refused call raises `WalkforwardError`; `WalkforwardError.document()` renders it as
`{"contract": ..., "status": "refused", "refusal": {"code", "detail", "ids", "names"}}`. The codes
are module-local and are not codes of the shared failure catalog.

| Code | Trigger |
| --- | --- |
| `walkforward_parameter_missing` | a required argument passed as `None` (leaving one out is Python's `TypeError`) |
| `walkforward_parameter_invalid` | a plan that is not a list of distinct non-empty text labels; `sources` not a mapping; a bad metric, direction, space or `max_work` |
| `walkforward_window_unknown` | a `sources` key not in the plan |
| `walkforward_no_supplied_sources` | every plan window has no source |
| `walkforward_input_invalid` | a source the reader refuses; an `ok` row's metric cell that is not a finite number and not `null`; a cell of any row that is a non-finite number or cannot be written as JSON; a row whose `parameters` are absent or do not fit the space |
| `walkforward_rows_incomplete` | a result that keeps only some of its trials |
| `walkforward_space_missing` | nothing records a space and none was passed |
| `walkforward_space_mismatch` | the supplied windows record different spaces, or rows record different `space_hash` values |
| `walkforward_context_mismatch` | inside one window, `ok` rows with a `backtest` object disagree on the bar and timeframe counters, or rows disagree on `space_hash` |
| `walkforward_direction_mismatch` | `metric` is `"objective"` and a recorded direction differs from the passed one |
| `walkforward_metric_unknown` | the metric name is recorded in no row of any supplied window |
| `walkforward_duplicate_vector` | two eligible rows of a carry-over target window carry the carried vector and record different values |
| `walkforward_no_eligible_rows` | no supplied window has an eligible row |
| `walkforward_resource_exceeded` | the admission figure is above a caller-supplied `max_work` |

The first defect decides the code: options; an undeclared label; no source; reading each window
together with that window's own context check; the space (recorded spaces, then `space_hash`); the
direction; the metric name; the cells; the parameters; duplicate vectors; no eligible row; the figure.

## Resource rule

For `e_w` eligible rows in supplied window `w` and the carry-over pairs `(i, j)`:

```text
work = rows_read + Σ_w e_w * (e_w - 1) / 2 + Σ_pairs e_j
```

It is a declared figure of this contract version, of the same shape as the admission figure of the
Pareto front, and it is computed from the row counts and the eligible counts alone, so it needs none of
the comparisons it bounds. The call is refused as `walkforward_resource_exceeded` only when the caller
supplied a `max_work` and the figure is above it; a figure equal to `max_work` is admitted. There is no
built-in cap. Neither time nor memory has been measured.

## Choices of contract v1

| ID | Choice |
| --- | --- |
| W1 | The window plan is the caller's ordered list of distinct non-empty text labels; every label in the result is the caller's key. |
| W2 | A `sources` key not in the plan is refused; a plan label with no source is `never_run`, in plan order, never an error. |
| W3 | A plan with no supplied source is refused; a plan of one window is allowed. |
| W4 | Each window is read on its own, with its own context check; the recorded spaces must be equal across the supplied windows, and every row that records a `space_hash` must record the same one (a result's own top-level `space_hash` is not read); the bar and timeframe counters are compared inside a window only. |
| W5 | The metric is `"objective"` or a recorded name; the direction is required, and for `"objective"` it must agree with every recorded direction (the common space's own checked first, then each source's). |
| W6 | Per window one selected row, ties by ascending trial id; no eligible row is listed, not an error; none anywhere refuses the call. |
| W7 | Carry-over only between two supplied windows adjacent in the plan whose first window has a selected row; a never-run window breaks the chain and the gap is listed; a window with no selected row is the source of no pair and is not a break. |
| W8 | The carry-over is a lookup by the exact parameter key; it reports a row (`row_found` true) or reports no row with a reason (`row_found` false), never a zero; the module evaluates nothing. |
| W9 | Two eligible rows of a target window that carry the carried vector and record different values refuse the call, naming the ids; equal values report the lowest id. |
| W10 | Stability gives the selected values in plan order as the rows record them, the distinct count and, for integer and real dimensions, the exact minimum and maximum; no averaging. |
| W11 | The result is a returned document with its own contract id; module-local codes; a library function only. |
| W12 | `walkforward_resource_exceeded` only against a caller-supplied `max_work`; no built-in cap. |

Not part of this contract: a native runner change, a multi-dataset runner, running a window or a
study, evaluating a parameter vector, an out-of-sample measure, a threshold, a verdict, a robustness
score, a command-line flag, a field in a native result, several sources per window, and any
clustering or averaging of parameters.

## What is not claimed

The `interpretation` field of every result holds this text, word for word.

```text
(a) This module runs no window and evaluates nothing. The studies were run by the caller's own application; this module reads rows that already exist.
(b) No claim is made about what data any window's study used. The context checks are necessary, not sufficient: equal bar counters, one shared space and one shared space hash do not prove the same data, the same fixed inputs or the same strategy, and the windows may overlap, touch or be separated.
(c) Each window's selected value is in-sample for that window: it is the best scored row of that window's rows.
(d) A carry-over value is a row that the user's own study recorded, for the vector this contract's rule selects in the previous window, and nothing more. No value here is claimed to be out of sample. When the previous window has a selected vector the pair is always reported, and a target window that holds no eligible row with that vector is reported with row_found false and a reason: that is not a zero and not a drop.
(e) The number of windows is small. The stability table is a summary of the selected values in window order; it is not a trajectory, not a robustness measure, and no threshold and no verdict is produced.
(f) v1 analyses finished studies; native in-loop walk-forward comes later.
```

## What an independent oracle confirms

`tests/g3_oracles/walkforward_oracle.py` decides eligibility, the selected row, the ranks, the
carry-over outcomes, the stability counts, the admission figure and each refusal from its own
fixture tables rather than replaying this module's branches, and computes the ranks in
exact rationals. Where it and this module agree, that agreement is evidence for the selected rows, the
exclusion reasons and ids, the mid-ranks, the `chain_breaks` and `never_run` lists, the stability
counts and the admission figure. It is NOT evidence for the wording of `interpretation`, for the order
in which the refusals are checked, for which of the two direction names the product maps to "better",
for the selection formula itself (an error in both routes at once would not be caught), for a recorded
metric beyond the cases the oracle's own eligibility table exercises, or for the digest of a row whose
status is not `ok` and whose cell is a string, an array or an object (no test input writes one).
