# Post-hoc Pareto front

**Status: the native-proof PBO run uses synthetic data only; PBO on real data is not yet proven.**
Every statement about behaviour below is a statement of what the
source is meant to do; the proof on real study output is deferred. The contract id is
`pineforge-hpo-pareto-front/v1`, the algorithm name is `exact_all_pairs_v1`.

## What it computes

`pareto_front` takes the finished rows of one study and two or more axes, each a metric
name that the study recorded per trial (`--record-metric`, see
[Study spec](study-spec.md)) with a direction chosen by the caller. It returns the rows
that no other eligible row dominates, as a plain dictionary. It runs no trial, writes no
file, keeps no state and changes no native result or trials file: the only side effect is
reading the one file named. It is a library function; there is no command-line flag and no
field in a native result.

```python
from pineforge_hpo.pareto import ParetoError, pareto_front

try:
    front = pareto_front(
        "result.json",
        [
            {"name": "metrics.all.net_profit_pct", "direction": "maximize"},
            {"name": "metrics.equity.max_equity_drawdown_pct", "direction": "minimize"},
        ],
        with_domination_counts=True,
        max_work=50_000_000,
    )
except ParetoError as refusal:
    print(refusal.code, refusal.detail, refusal.ids, refusal.names)
else:
    print(front["pareto_front"])
```

The directions above are the caller's choice. The module does not know the sign
convention of any metric (the study spec itself applies `abs(...)` to a drawdown in an
objective but compares the raw metric in a constraint), so it never infers a direction.

Signature: `pareto_front(source, axes, *, with_domination_counts=False, max_work=None)`.

| Argument | Meaning |
| --- | --- |
| `source` | A path (text or `os.PathLike`) to a complete result JSON or to a rows JSONL file, an already parsed result dictionary, or an already parsed list of rows. Exactly one source. |
| `axes` | A list (or tuple) of at least two objects `{"name": ..., "direction": ...}`. `name` is a key of the rows' `metrics` object, spelled exactly as recorded. `direction` is exactly `maximize` or `minimize`. Names are distinct. No other field is allowed and there is no default direction. |
| `with_domination_counts` | `True` or `False`; adds the domination counts to the result (default `False`). |
| `max_work` | `None` (no bound, the default) or a positive integer bound on the admission figure (see [Resource rule](#resource-rule)). |

## Input

* **Sources.** A complete result is read as its `warm_start_trials` followed by its
  `trials`. A result must keep every trial: `trials_out` other than `all` is refused, and so
  is a `trials_completed` or a `warm_start.trials` that disagrees with the rows (a best-k
  or summary result holds a subset chosen by the scalar objective, and a front over it
  would be wrong). A JSONL file holds one row per non-blank line; a bad line is refused
  with its line number.
* **Strict reading.** Duplicate JSON keys, the tokens `NaN`, `Infinity` and `-Infinity`,
  and a number that overflows to infinity are refused wherever they stand in a file.
* **Rows.** `trial_id` is an integer from 0 to 2^64 - 2 and unique; `status` is one of
  `ok`, `constraint_violation`, `objective_error`, `constraint_error`, `engine_error`,
  `trial_error`, `trial_timeout`, `pruned`, `partial`; `feasible` is a boolean equal to
  (status is `ok`); `objective` is a finite number or `null` (and finite on an `ok` or
  `constraint_violation` row); `metrics`, when present, is an object. A row with
  `"metrics": null` is refused: write no `metrics` member instead.
* **Context.** Rows that carry a `space_hash` must carry the same one, and the `ok` rows
  that carry a `backtest` object must agree on `input_bars_processed`,
  `script_bars_processed`, `input_tf_seconds` and `script_tf_seconds`. These checks are
  necessary, not sufficient: equal counters and hashes do not prove the same data window,
  fixed inputs or strategy.
* **Spaces.** The module needs no recorded space and never compares spaces.
* **Values.** A number is converted with Python's correctly rounded `float`, so an integer
  above 2^53 may round (2^53 + 1 becomes 2^53). Native-written numbers round-trip exactly.

## Which rows compete

Only rows with status `ok` compete. Reasons are applied to each row in this order, the
first that applies decides:

| Row | Treatment | Reason in the result |
| --- | --- | --- |
| status `constraint_violation` | excluded | `status_constraint_violation` |
| status `objective_error`, `constraint_error`, `engine_error`, `trial_error`, `trial_timeout` | excluded | `status_<status>` |
| status `pruned` (its metrics are prefix values) | excluded | `status_pruned` |
| status `partial` | excluded | `status_partial` |
| status `ok`, no `metrics` object or an axis key absent | excluded | `metric_not_recorded` |
| status `ok`, an axis value `null` | excluded | `metric_unavailable` |
| status `ok`, every axis value a finite number | eligible | none |

An `ok` row whose axis value is a boolean, a string, an array, an object, a non-finite
number or an integer too large for binary64 refuses the whole call (`pareto_input_invalid`),
even when another axis of that row is absent. A non-finite number in an axis cell of any
row refuses too, as the `NaN` token does when the same data is read from a file; other
value types in a row that does not compete are left alone. Rows of a binary-warm
ancestor carry no `metrics` and so are excluded as `metric_not_recorded`.

## Dominance and the front

For two eligible rows a and b, a is no worse than b on an axis when a >= b (maximize) or
a <= b (minimize), and strictly better when > or <. **a dominates b** when a is no worse on
every axis and strictly better on at least one. The front is the set of eligible rows
that no other eligible row dominates.

* Comparison is exact on binary64 numbers. There is no tolerance. `-0.0` equals `+0.0`.
* Equal vectors do not dominate each other: all of them stay on the front, and rows with
  duplicate parameter vectors all stay too. `n_distinct_vectors_on_front` counts the
  distinct metric vectors among them.
* The reference algorithm compares every unordered pair of eligible rows in both
  directions. A faster exact algorithm may replace it only if its ids equal the
  reference's on every fixture and every seeded random case.
* `domination_counts[b]` is the number of eligible rows that dominate b; it is 0 exactly
  for the rows on the front.

## Result

| Field | Content |
| --- | --- |
| `contract` | `pineforge-hpo-pareto-front/v1` |
| `status` | `ok` (a refused call raises, see [Refusals](#refusals)) |
| `axes` | the axes as given, in request order |
| `rows_read` | the number of rows read, all statuses |
| `rows_eligible`, `rows_excluded` | counts; they add up to `rows_read` |
| `excluded` | an object keyed by reason (only reasons that occurred), each `{count, ids}` with ids ascending, and, for `metric_not_recorded` and `metric_unavailable`, `axes`: the axis names involved, in request order |
| `pareto_front` | the ids of the front, ascending |
| `n_front` | the number of rows on the front |
| `n_distinct_vectors_on_front` | the number of distinct metric vectors among them |
| `domination_counts` | only when requested: an object from each eligible id to its count, ids ascending (JSON writes the ids as strings) |
| `algorithm` | `exact_all_pairs_v1` |
| `input_sha256` | digest of the rows read, see below |
| `interpretation` | the fixed text of [What is not claimed](#what-is-not-claimed) |

`input_sha256` is the SHA-256 (lowercase hex) of the concatenation of the sorted per-row
digests. A per-row digest is the 32-byte SHA-256 of the compact (no spaces), key-sorted,
ASCII-only JSON of `[trial_id, status, {axis name: value}]`, where the value is the
number written as its shortest round-trip binary64 text (an integer is written as a
float, `10.0`), and `null` when the axis key or the whole `metrics` object is absent or
the value is `null`. Every row read is covered, excluded rows included; the directions
are not. Row order, axis order and repeated calls give the same digest.

Example, ten rows with axes profit (maximize) and drawdown (minimize), written as
(profit, drawdown): 0 `ok` (10, 5); 1 `ok` (12, 8); 2 `ok` (8, 3); 3 `ok` (9, 6);
4 `ok` (12, 8); 5 `ok` (7, 3); 6 `constraint_violation` (20, 1); 7 `engine_error` with
null values; 8 `ok` (11, null); 9 `ok` (6, 2). Row 6 is excluded because it would
dominate every eligible row; row 8 for a null drawdown. Row 0 dominates row 3 and row 2
dominates row 5. The result holds `rows_read` 10, `rows_eligible` 7, `rows_excluded` 3,
`pareto_front` `[0, 1, 2, 4, 9]`, `n_front` 5 and `n_distinct_vectors_on_front` 4 (rows 1
and 4 are equal and both stay). With counts requested, `domination_counts` is
`{0: 0, 1: 0, 2: 0, 3: 1, 4: 0, 5: 1, 9: 0}`.

## Determinism

Row order, axis order, how a file is split into lines and repeated calls give the same
`pareto_front`, counts and `input_sha256`; only the echoed `axes` (and the axis lists
inside `excluded`) follow the request order. Output is ascending by trial id. Nothing is
random.

## Refusals

A refused call raises `ParetoError` with `code`, `detail`, `ids` (offending trial ids,
ascending) and `names` (offending axis or field names); no front is returned.
`ParetoError.document()` renders it as the refused result document:
`{"contract": ..., "status": "refused", "refusal": {"code", "detail", "ids", "names"}}`.
The codes belong to this module; they are not codes of the shared failure catalog.

| Code | Trigger |
| --- | --- |
| `pareto_input_invalid` | a source that cannot be read; a malformed row, unknown status, `feasible` that disagrees with the status, invalid or repeated `trial_id`; a duplicate key, `NaN` or `Infinity` token or overflow; an `ok` row whose axis value is a boolean, string, array, object or non-finite number; a non-finite number in an axis cell of any row; an axis cell that cannot be written as JSON |
| `pareto_rows_incomplete` | a result with `trials_out` other than `all`, or `trials_completed` or `warm_start.trials` disagreeing with the rows |
| `pareto_context_mismatch` | rows with different `space_hash` values, or `ok` rows that disagree on the `backtest` counters |
| `pareto_parameter_missing` | no axes, or an axis without a name or without a direction (absent or `null`) |
| `pareto_parameter_invalid` | axes that are not a list of objects, an axis with other fields than `name` and `direction`, a name that is not a string, fewer than two axes, a repeated axis name, a direction word other than `maximize` or `minimize`, a `with_domination_counts` that is not a boolean, a `max_work` that is not `None` or a positive integer |
| `pareto_axis_unknown` | an axis name recorded in no row's `metrics` object (any status counts as recording it); checked only when at least one row was read |
| `pareto_no_eligible_rows` | no row remains after the exclusions, or the source holds no rows |
| `pareto_resource_exceeded` | the admission figure is above a `max_work` the caller supplied |

The checks run in this order and the first defect decides the code: axes and options
(`pareto_parameter_missing` before `pareto_parameter_invalid`); the source
(`pareto_input_invalid`, `pareto_rows_incomplete`); the context (`pareto_context_mismatch`);
the axis names (`pareto_axis_unknown`); the axis values (`pareto_input_invalid`);
`pareto_no_eligible_rows`; the admission figure (`pareto_resource_exceeded`).

## Resource rule

For N eligible rows the reference compares N(N - 1) / 2 unordered pairs, each in at most
as many comparisons as there are axes, in both directions (N = 10,000 gives 49,995,000
pairs). The admission figure is

```text
work = rows_read + N * (N - 1) / 2
```

It is exact once the exclusions are known and before any pair is compared. The call is
refused as `pareto_resource_exceeded` only when the caller supplied a `max_work` and the
figure is above it; a figure equal to `max_work` is admitted. There is no built-in cap.
A faster algorithm would not change the figure. Memory is expected to be N times the
number of axes in values, plus the ids and the lists of excluded ids; neither time nor memory
has been measured.

## Choices of contract v1

Each choice is a reversible default of this contract version, and a later version may
change one of them.

| ID | Choice |
| --- | --- |
| P1 | Axes are keys of each row's `metrics` object, spelled exactly as recorded; at least two; each axis carries `maximize` or `minimize`, supplied by the caller, with no default (no metric direction exists in the HPO layer). |
| P2 | Only rows with status `ok` compete; every other status is excluded and reported with its reason and ids (a pruned row holds prefix values; a `constraint_violation` row is rejected by the study's own constraints). |
| P3 | A missing axis value (absent key, absent `metrics`, or `null`) excludes the row and reports it; the call is refused only when an axis is recorded in no row or no row remains. |
| P4 | An `ok` row's axis value that is not a finite number or `null` (boolean, string, array, object, `NaN` or `Infinity` token, overflow) refuses the input. |
| P5 | a dominates b when it is no worse on every axis and strictly better on at least one, in each axis's direction; exact binary64, no tolerance, -0.0 equals +0.0; equal vectors do not dominate each other and all stay. |
| P6 | Duplicate parameter vectors keep every row; the number of distinct metric vectors on the front is reported. |
| P7 | The reference algorithm is exact all-pairs; a faster exact algorithm is allowed only if its ids equal the reference's on every fixture and every seeded random case; output is ascending by trial id. |
| P8 | `pareto_front` ids always; `domination_counts` only when requested (default off); no ranks, hypervolume or crowding in v1. |
| P9 | Exactly one source in v1: one complete result or one rows file. |
| P10 | The front is a returned document with its own contract id; it is never written into a native result or trials file. |
| P11 | Module-local `pareto_*` refusal codes, a library function only, and the shared row reader. |
| P12 | `pareto_resource_exceeded` only against a caller-supplied `max_work`; the figure is rows read + N(N - 1) / 2; no built-in cap. |

Not part of this contract: a Pareto sampler or any multi-objective search, a vector
objective, a command-line flag, a field in a native result, several sources at once, and
any rank, hypervolume or crowding value.

## What is not claimed

The `interpretation` field of every result holds this text, word for word. The bracketed
labels (`P-F5`, `P-F8`, `P-F13`) and `clause 2(e)` are reference labels of the contract's
source notes and are kept so that the text stays exactly as pinned.

```text
(a) No Pareto sampler and no multi-objective search (P-F8). (b) The front is over the trials that a scalar-objective study chose to evaluate: TPE learns from one value and abandons constraint-violating trials (study-spec.md:765-767, 790-794), so the sampler never searched the other axes. The front is the front of the evaluated rows, not the true front, and its extent depends on the sampler, the seed and the budget. (c) A row excluded for a missing value, or for violating a constraint, may belong to the true front. (d) In-sample: no holdout (study-spec.md:376-377). (e) No claim of spread, diversity, optimality or hypervolume; equal vectors are not merged. (f) Directions are the caller's; the module does not know a metric's sign convention (P-F5). (g) Rows of one lineage may come from different scoring contexts that the space hash cannot detect (P-F13); the clause 2(e) checks are necessary, not sufficient.
```

In plain words: there is no Pareto sampler; the front is the front of the rows the study
happened to evaluate, and a sampler that learns from one value never searched the other
axes, so it is not the true front and depends on the sampler, the seed and the budget; an
excluded row may belong to the true front; the numbers are in-sample with no holdout;
spread, diversity, optimality and hypervolume are not claimed and equal vectors are not
merged; directions are the caller's; and the checks on the scoring context are
necessary, not sufficient.

## What an independent oracle confirms

`tests/g2_oracles/pareto_oracle.py` decides eligibility and refusals from the contract's own
tables rather than replaying this module's sequential per-row branches, and computes the front,
the distinct vectors and the figure by its own route. Where it and this module agree, that
agreement is evidence for which rows are eligible, the reason and ids of each exclusion, the
front, the counts, the number of distinct vectors and the figure.

It is NOT evidence where the oracle repeats this module's own reading, because the pin does not
state it:

- the order in which the refusals are checked (an unknown axis, then an unusable axis value, then
  no eligible row), as documented under Refusals;
- the refusal of a non-finite number in an axis cell of a row that is not `ok`, as documented
  under Which rows compete (the pin names only `ok` rows and the file tokens, and no oracle case
  exercises this reading).

Those rest on this page and on the module's own tests of them, and must be cited as that.

`tests/g2_oracles/pareto_real_rows.py` compares a real study. Only exit 0 (`RESULT: AGREE`) is the
pin's agreement. Exit 3 is an agreement on a study the pin does not name (`--other-study`,
`RESULT: AGREE ON ANOTHER STUDY`), and it is never exit 0. The other non-zero codes are 1 a
disagreement or a failed pin property, 2 a refusal by the module (also the code of a usage error),
5 a gate that failed and 7 a failed native run; the exact table is in
`tests/g2_oracles/README.md`.
