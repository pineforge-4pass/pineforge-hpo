# Holdout comparison of two finished studies

This page documents `python/pineforge_hpo/holdout.py`. Its tests run with the package's
unit tests; an independent oracle in `tests/g3_oracles` recomputes its numbers with
exact arithmetic and is run by hand.

## What it computes

`holdout_compare` takes the finished rows of two studies: the SELECTION source (the study the user
tuned on) and the HOLDOUT source (a second study the user ran, outside this module, on data the
user says was set aside). It joins their rows by the exact parameter vector and reports, for the
`k` best eligible vectors of the selection source, what the holdout source recorded for the same
vectors: the two values, the two ranks, their difference, and over the matched vectors a rank
correlation and the share whose holdout value lies in the better half of the pool. It runs no
trial, writes no file, keeps no state and changes no native result or trials file: the only side
effect is reading the two files named. It is a library function; there is no command-line flag and
no field in a native result.

```python
from pineforge_hpo.holdout import HoldoutError, holdout_compare

try:
    report = holdout_compare(
        "selection_result.json",   # the study the user tuned on
        "holdout_rows.jsonl",      # the second study the user ran
        metric="objective",        # or the exact name of a recorded metric
        direction="maximize",      # or "minimize"
        k=10,
        max_work=50_000_000,
    )
except HoldoutError as refusal:
    print(refusal.code, refusal.detail, refusal.ids, refusal.names)
else:
    print(report["followed"])
    print(report["unmatched"])
    print(report["statistics"])
```

Signature:
`holdout_compare(selection, holdout, *, metric, direction, k, space=None, max_work=None)`.

| Argument | Meaning |
| --- | --- |
| `selection` | Source 0: the study the user tuned on. A path (text or `os.PathLike`) to a complete result JSON or to a rows JSONL file, an already parsed result dictionary, or an already parsed list of rows. |
| `holdout` | Source 1: the second study, in the same four shapes. |
| `metric` | Exactly `"objective"`, or the exact name of a recorded metric (a key of the rows' `metrics` object, spelled as recorded). Required and keyword-only, with no default. |
| `direction` | Exactly `maximize` or `minimize`. Required and keyword-only, with no default. `None` only for `metric="objective"`, and only when both sources record a direction and the two agree. For a named metric the module does not know the sign convention and refuses `None` (see choice H1 below). |
| `k` | A positive integer, required and keyword-only, with no default: how many of the best eligible selection vectors are followed. Above the number of eligible selection vectors it is clipped. |
| `space` | `None` (the default), a recorded-space dict or a `StudySpec`, used only when no row and no result records a space. A passed space is compared with no recorded one. |
| `max_work` | `None` (no bound, the default) or a positive integer bound on the admission figure (see [Resource rule](#resource-rule)). |

Leaving out `metric`, `direction` or `k` raises Python's `TypeError` before the function body runs;
passing `None` is a value, and the module refuses it (see [Refusals](#refusals)).

## What "holdout" means here

The module splits no data and runs no trial. Two finished studies exist as rows already written;
the caller says which one is the selection study and which is the second study. One study's rows
partitioned into a selection part and a holdout part is not supported and cannot be sound in this
system: a study runs one dataset per run, so a partition of its trials is not held-out data, and no
row field records a fold or a split to partition by. The module cannot know what data either study
used; a value recorded by the second study is only as held-out as the caller made it.

## Input

* **Sources.** A complete result (`trials_out` `all`, rows `warm_start_trials` followed by
  `trials`), a rows JSONL file, or a list of rows, read by the shared reader. A best-k or summary
  result is refused.
* **Ancestor rows.** A complete result may carry the rows of an earlier study it continued from.
  The module counts those ancestor rows for each source and reports the counts. It refuses a
  holdout source that carries an ancestor row whose trial id also appears among the selection
  source's rows: such a holdout source may be reporting the selection study's own rows, and the
  trial ids are the only witness available, so an id that repeats by a coincidence of numbering is
  refused like a real overlap. A rows source has no ancestor rows by construction.
  A path source is read twice to count ancestors. If the second read fails or yields
  a different number of rows from the first read, the call refuses
  `holdout_input_invalid` naming that source, with empty `ids` and `names`. This
  includes a pipe whose first read consumed its data. The comparison counts ancestor
  and own rows for a complete result, all rows for a rows file, and zero for an empty
  path. The comparison is by row count: a file replaced between the reads by another
  with the same row count is not detected. Parsed objects need no second read.
* **Strict reading.** Duplicate JSON keys, the tokens `NaN`, `Infinity` and `-Infinity`, and a
  number that overflows to infinity are refused wherever they stand in a file.
* **Rows.** `trial_id` is an integer from 0 to 2^64 - 2 and unique inside a source; `status` is one
  of `ok`, `constraint_violation`, `objective_error`, `constraint_error`, `engine_error`,
  `trial_error`, `trial_timeout`, `pruned`, `partial`; `feasible` equals (status is `ok`);
  `objective` is a finite number or `null` (finite on an `ok` or `constraint_violation` row);
  `metrics`, `parameters`, `backtest` and `space`, when present, are objects.
* **Space.** The rows and results of BOTH sources must share one canonical space (parameter
  dimensions, objective expression, direction, constraints); a difference refuses
  `holdout_space_mismatch`. When nothing records a space, the passed `space` is used; with none
  passed, `holdout_space_missing`. A row or a source that records no space is not checked against
  the recorded one, and a passed space is never examined when anything else records one.
* **Context, per source.** `ok` rows that carry a `backtest` object must agree inside one source on
  `input_bars_processed`, `script_bars_processed`, `input_tf_seconds` and `script_tf_seconds`, and
  every row that carries a `space_hash` must carry the same one. The check runs on each source on
  its own and is NEVER applied between the two sources, so it cannot establish that the two studies
  kept the same strategy, the same fixed inputs and the same timeframe, and it cannot establish
  that the data one study ran on is disjoint from the data the other ran on. A space hash names the
  parameter space and the objective, not the data, so two studies in the same space normally carry
  the same hash, and an equal hash says nothing about the data. The check is necessary, not
  sufficient.
* **Values.** A number is converted with Python's correctly rounded `float`, so an integer above
  2^53 may round, and an integer too large in magnitude for binary64 refuses the call.

## Which rows count

Eligible means status `ok` and a finite metric value. Reasons are applied to each row in this order,
the first that applies decides:

| Row | Treatment | Reason in the result |
| --- | --- | --- |
| status other than `ok` | excluded | `status_<status>` |
| `ok`, `metric` is a name, `metrics` absent or the name absent | excluded | `metric_not_recorded` |
| `ok`, `metric` is a name, the cell `null` | excluded | `metric_unavailable` |
| `ok`, a finite value | eligible | none |

For `metric="objective"` every `ok` row is eligible, because an `ok` row always has a finite
objective. A metric cell that is a non-finite number refuses the call whatever the status; an `ok`
row whose cell is a boolean, string, array or object refuses too, while the same cell in a row of
another status is left alone if the digest's key-sorted JSON writer can write it.
A cell that this writer cannot write, including an object whose keys have mixed types
and cannot be sorted, or an integer too large for binary64, refuses the call in any
row whatever its status. A row whose parameter vector cannot be keyed refuses the call.

## The join

The join key is the exact parameter vector: `vector_key(parameters, space)`, the canonical key of
the typed scalars, reals as their binary64 bit pattern with `-0.0` taken as `+0.0`. Equality is
exact; there is no tolerance and no lattice snapping, so a near-equal vector is a different vector.
Repeated vectors are normal in a discrete space, so they are not refused for being repeated: two
eligible rows of one source that share a vector refuse the call only when their metric values
differ (`holdout_duplicate_vector`, with the ids of all the vector's eligible rows named), and
otherwise the vector is represented by its lowest trial id and counted once.
When several vectors conflict, every eligible row of every conflicting vector of the
first conflicting source (selection first) is named; the other source is not examined.

A followed vector with no eligible row in the second source is not dropped and not
counted as zero: it is listed in
`unmatched` with the reason `absent_in_holdout` (no row holds the vector) or
`holdout_row_not_eligible` (rows hold it and none is eligible), and the ids of those rows are listed
with it.

## What is computed

The eligible selection vectors are ranked by the metric in the direction, ties broken by ascending
representative trial id; ranks are distinct 1-based integers. The followed vectors are the best
`min(k, n_sel)` of them. The POOL is the eligible holdout vectors whose vector matches the vector
of at least one eligible selection vector, of any rank. Pool vectors are ranked by the metric in the
direction; vectors with equal values share a MID-RANK: a tie group at positions `p..q` gets
`(p + q) / 2`, an exact integer or half-integer.

For each matched followed vector: the selection value and rank, the holdout trial id, value and
mid-rank, the difference `holdout - selection` (one IEEE-754 subtraction), and `improved` (whether
the holdout value is strictly better in the direction). An unmatched vector carries none of the
three holdout keys at all — no key, never a zero — and is listed with its reason instead.

Over the matched followed vectors, rank the matched vectors twice more, within the matched set
alone, by their selection values and by their holdout values, each time giving equal values the
mid-rank of the positions they occupy; call the two rank vectors `x_i` and `y_i`. Then

```text
Sxy = n * SUM(x_i * y_i) - SUM(x_i) * SUM(y_i)
Sxx = n * SUM(x_i^2)    - (SUM(x_i))^2
Syy = n * SUM(y_i^2)    - (SUM(y_i))^2
rho = Sxy / sqrt(Sxx * Syy)
```

This is Pearson's correlation of the two rank vectors, that is the rank correlation of the matched
pairs with ties mid-ranked on both sides, and it is derived in EXACT rationals
(`fractions.Fraction`); the exact numerator, the two sums and the two rank vectors are reported as
integer fractions so the reader can re-derive the value by hand. `spearman` is the signed binary64
of `sqrt(Sxy^2 / (Sxx * Syy))`, one correctly rounded conversion and one correctly rounded square
root. It is `null` when fewer than three vectors are matched or when one rank vector is constant
(its `spearman_undefined` names the reason, and that reason is reported before any zero
correlation). No p-value, no significance test, no threshold and no verdict is computed.

A pool vector is in the BETTER HALF when its mid-rank is at most `(M + 1) / 2`, with `M` the pool
size, compared exactly; a vector equal to the middle value is included. `better_half` reports the
number of followed vectors whose pool vector is in the better half over the matched count, both as
integers and as their binary64 quotient (`null` when nothing is matched).

Nothing is ranked outside this: no significance, no threshold, no verdict, no sampling of any
kind. A difference that is not finite refuses the call (see [Refusals](#refusals)).

## Result

| Field | Content |
| --- | --- |
| `contract` | `pineforge-hpo-holdout/v1` |
| `status` | `ok` (a refused call raises, see [Refusals](#refusals)) |
| `metric`, `direction`, `k` | as given, and the direction actually used |
| `k_followed` | `min(k, n_sel)` |
| `n_sel`, `n_hol` | the eligible vectors of each source |
| `selection`, `holdout` | per source: `index`, `rows`, `ancestor_rows`, `rows_eligible`, `vectors_eligible`, `rows_excluded`, `excluded` (per reason `{count, ids}`, ids ascending), `space_hash` |
| `followed` | one entry per matched followed vector, ascending selection rank: selection trial id, vector key, selection value and rank, holdout trial id, holdout value, holdout rank (mid-rank), difference, improved |
| `unmatched` | one entry per unmatched followed vector, ascending selection rank: selection trial id, vector key, selection value and rank, reason, the holdout ids that hold the vector |
| `n_matched` | the number of matched followed vectors |
| `pool` | `size`, `median_rank_cut`, and `holdout_only` (`count` and ascending ids of the eligible holdout vectors whose vector matches no eligible selection vector) |
| `statistics` | `spearman` (or `null`), `spearman_terms` (`xy`, `xx`, `yy` as fractions), `spearman_ranks` (the two rank vectors as fractions), `spearman_undefined` (or `null`), `better_half` (numerator, denominator, value) |
| `algorithm` | `exact_join_midrank_v1` |
| `input_sha256` | digest of the rows read, both sources, see below |
| `admission` | `rows_read`, `selection_pairs`, `holdout_pairs`, `work`, `max_work` |
| `interpretation` | the fixed text of [What is not claimed](#what-is-not-claimed) |

Every mid-rank, every term of the correlation, the two rank vectors and the median cut are reported
as a `[numerator, denominator]` pair of integers in lowest terms, so no half-integer is hidden in a
binary64.

`input_sha256` is the SHA-256 (lowercase hex) of the concatenation, sorted ascending, of one
32-byte digest per row read of both sources. Each per-row digest is the SHA-256 of the compact
(no spaces), key-sorted, ASCII-only JSON of `[source_index, trial_id, status, value, vector_key]`,
where `value` is the metric value as its shortest round-trip binary64 text (an integer cell as its
float, `10.0`); `null` when the row has no value (absent `metrics`, absent key, `null` cell, `null`
objective); and, in a row that is not `ok`, a cell that is not a number written exactly as read.
Every row read is covered, excluded rows included; the direction, `k` and the metric name are not.
Row order, file layout and repeated calls give the same digest.

## Determinism

Row order inside a source, the layout of a file and repeated calls give the same document and the
same `input_sha256`. The two argument positions are fixed: the first source is always the
selection, the second always the holdout. Every list is ordered by an explicit rule, every count is
exact, and nothing is random.

## Refusals

A refused call raises `HoldoutError` with `code`, `detail`, `ids` (offending rows as
`(source_index, trial_id)` pairs, ascending) and `names` (offending metric, parameter or argument
names). `document()` renders it as the refused result document
`{"contract": ..., "status": "refused", "refusal": {"code", "detail", "ids", "names"}}`. The codes
belong to this module; they are not codes of the shared failure catalog.

| Code | Trigger |
| --- | --- |
| `holdout_input_invalid` | a source that cannot be read; a malformed row, unknown status, `feasible` disagreeing with the status, invalid or repeated `trial_id`; a duplicate key, `NaN` or `Infinity` token or overflow; a malformed recorded space or dimension; a passed `space` whose content is malformed, when no row or result records a space; a row whose parameter vector cannot be keyed; a path source whose second read raises or yields a different number of rows than the first (the refusal names that source, with empty `ids` and `names`) |
| `holdout_rows_incomplete` | a result with `trials_out` other than `all`, or `trials_completed` or `warm_start.trials` disagreeing with the rows |
| `holdout_space_missing` | no row or result records a space and none was passed |
| `holdout_space_mismatch` | the two sources' rows and results record more than one canonical space |
| `holdout_context_mismatch` | inside one source: `ok` rows that disagree on the `backtest` counters, or rows with different `space_hash` values |
| `holdout_ancestor_rows` | the holdout source carries an ancestor row whose trial id appears among the selection source's rows |
| `holdout_parameter_missing` | `metric`, `direction` or `k` given as `None`; `direction=None` with a named metric; `direction=None` for `metric="objective"` when either source records no direction or the two disagree |
| `holdout_parameter_invalid` | a `metric` that is not a string; a `k` that is not an integer or is below 1; a `direction` other than `maximize` or `minimize`; a `space` that is not `None`, a dict or a `StudySpec`; a `max_work` that is not `None` or a positive integer |
| `holdout_metric_unknown` | the named metric is recorded in no row of the selection source, or in no row of the holdout source (each source is judged on its own, and only when it holds at least one row) |
| `holdout_value_invalid` | a metric cell that is a non-finite number anywhere; a cell that is not a number in an `ok` row; a cell that cannot be written as JSON, or an integer too large for binary64, in any row; a difference that is not finite |
| `holdout_no_eligible_rows` | the selection source has no eligible row (an empty or wholly ineligible holdout source does not refuse) |
| `holdout_duplicate_vector` | two eligible rows of one source share a parameter vector and disagree on the metric value |
| `holdout_resource_exceeded` | the admission figure is above a `max_work` the caller supplied |

The cell check uses the digest's key-sorted JSON writer. An object cell with keys of
mixed types that cannot be sorted is refused as `holdout_value_invalid` in any row,
like any cell this writer cannot write.

The checks run in this order and the first defect decides the code: the arguments
(missing before invalid, in the order `metric`, `k`, `direction`, `max_work`, then the
type of `space`); reading the sources; the space of both sources; the direction rule;
the context of each source; the second read of each path source, selection first; the
ancestor rows of the holdout source; the key of every row; the metric name; the metric values; the eligible rows of the selection source;
the repeated vectors; the admission figure; and the finite difference of each matched
vector. The missing-direction argument check applies only to a string `metric` other
than `"objective"`: `metric=7`, `direction=None`, `k=1` instead refuses as
`holdout_parameter_invalid`, naming `metric`.

When several vectors conflict, every eligible row of every conflicting vector of the
first conflicting source (selection first) is named; the other source is not examined.

## Resource rule

For `n` vectors the reference ranking compares at most `n(n - 1) / 2` unordered pairs, the same
shape as the Pareto front's reference. The admission figure is

```text
work = rows_read + n_sel * (n_sel - 1) / 2 + n_hol * (n_hol - 1) / 2
```

`n_sel` and `n_hol` count eligible vectors, so a repeated vector is not paid for twice. It is a
bound on the reference cost of ordering the two sources, not a count of the module's comparisons:
the pool is no larger than the eligible holdout vectors and the matched set is no larger than either
set charged (it is a subset of both the followed selection vectors and the pool), so, with the
ranking of the selection source itself, the module's ranking comparisons stay within
twice the two pair terms. The figure is exact once the exclusions
are known and before any pair is compared. The call is refused as `holdout_resource_exceeded` only
when the caller supplied a `max_work` and the figure is above it; a figure equal to `max_work` is
admitted. There is no built-in cap. A faster exact algorithm may replace the reference only if its
ranks and mid-ranks equal the reference's on every fixture and every seeded random case. Neither
time nor memory has been measured.

## Choices of contract v1

Each choice is a reversible default of this contract version, and a later version may change one
of them.

| ID | Choice |
| --- | --- |
| H1 | `direction` has no default; `None` is accepted only for `metric="objective"` and only when both sources record a direction and the two agree (the recorded direction of a result is its own `direction` or its space's, and the two must already agree or the reader refuses). For a named metric the sign convention is the caller's and `None` refuses. |
| H2 | `metric="objective"` reads `row.objective`; a named metric reads `row.metrics[name]`. The alternative — accepting a metric name that is also the name of an objective field — is not taken, because `objective` names the scalar the study optimised and nothing else. |
| H3 | `k` has no default and is clipped to the eligible selection vector count, never refused for being too large (the rule `top_ids` follows); the alternative, refusing a `k` above the count, would make a caller re-read the study to learn a number the module already has. |
| H4 | The two sources must share one canonical space (parameter dimensions, objective expression, direction, constraints). A holdout study run with a different objective expression or different constraints is refused; a later version may compare only the parameter dimensions. |
| H5 | A row whose parameter vector cannot be keyed refuses the whole call, whatever its status (the reading of `neighbourhood`'s row-against-space check). The alternative — excluding that row — is not taken because a row with no key could silently leave the pool. |
| H6 | `holdout_metric_unknown` fires when the name is recorded in no row of the selection source, or in no row of the holdout source (each judged on its own, and only for a source that holds at least one row): a typo, or a second study that never recorded the metric. The alternative — refusing only against the selection source and reporting the other as all-unmatched — is recorded here: it would report a metric the holdout study never recorded as an unmatched-vector story instead of naming it. |
| H7 | `input_sha256` is one flat digest over both sources' rows, with the source index inside each per-row digest. The alternative — a two-level hash, source digests then a final hash — was not taken because the flat form is order-insensitive in one step and simpler to re-derive. |
| H8 | Only ELIGIBLE rows count for the duplicate rule and for the pool; an ineligible row sharing a vector is excluded by its own reason and never refuses. The alternative — letting an ineligible row make a vector ambiguous — is not taken, because the ineligible row carries no value to join. |
| H9 | The pool is defined by the vectors of every eligible selection vector, of any rank, not only of the followed ones. The alternative — the followed vectors only — is not taken because then the pool, its mid-ranks and the cut would change with `k` although the second study did not change. |
| H10 | Holdout ranks are mid-ranks of tie groups; the selection ranks are distinct 1-based integers with ties broken by ascending representative trial id. The alternative — mid-ranks on the selection side too — is not taken because the selection side must give a total order to select the followed vectors; the correlation of the matched pairs mid-ranks BOTH sides, which is a different pair of numbers. |
| H11 | A pool vector is in the better half when its mid-rank is at most `(M + 1) / 2`; a value equal to the middle is included. The alternative — a strict cut — is recorded here: it would drop the tie group that straddles the cut and report 1 of 4 instead of 3 of 4 when the pool holds four vectors in two halves. |
| H12 | `better_half` counts over the matched followed vectors; an unmatched vector is neither a numerator nor a denominator, so absence is never reported as failure. The alternative — the denominator `k` — is recorded here. |
| H13 | The rank correlation is Pearson's over two rank vectors of the matched pairs, mid-ranked on both sides, computed in exact rationals, reported as a binary64 with its exact terms and rank vectors; it is `null` below three matched vectors or with a constant rank vector. The alternatives (mid-ranks on one side only, or ranks taken among all eligible selection vectors or over the pool) are not taken: they mix ranks of different scales, so an unmatched vector added to a study would change the value, and a tie in the selection values would make it depend on trial-id numbering. |
| H14 | The difference is `holdout - selection` in the module's own arithmetic; `improved` carries the direction's meaning separately, so a negative difference can be an improvement under `minimize`. The alternative, a signed improvement, is not taken because the two numbers answer different questions. |
| H15 | Module-local `holdout_*` refusal codes, a library function only, no command-line flag and no field in a native result; the shared row reader is used. |
| H16 | Exactly two sources in v1, positional (selection first). The alternative — one source partitioned by the caller — cannot be sound in this system (see [What "holdout" means here](#what-holdout-means-here)), and more than two would need a rule for which pair is compared. |

## What is not claimed

The `interpretation` field of every result holds this text, word for word:

```text
(a) The module splits no data and runs no trial: it reads two sets of rows that two finished studies already wrote, and joins them by the exact parameter vector. It cannot know what data either study used, so a value recorded by the second study is only as held-out as the caller made it. (b) A study is a holdout only when the caller ran it on data set aside before the selection rows were read; the module cannot check that, and reading the best selection rows while the second study's values are in view contaminates it. (c) Equality of parameter vectors is exact: no tolerance and no lattice snapping, so a near-equal vector is a different vector and is reported unmatched, never merged. (d) No significance test, no threshold and no verdict: a few vectors give a noisy rank correlation, and the module states no probability, interval or error bar. (e) The selection side is the best of many trials, so its values are optimistic; every number is in-sample for the selection source and only as held-out as the caller made it for the other. (f) v1 analyses finished studies; native in-loop walk-forward comes later. (g) The scoring-context check is necessary, not sufficient, and it runs on each source on its own, never between the two studies: a space hash names the parameter space and the objective, not the data, so an equal hash says nothing about the data, and this module requires the two studies to share that space, so their hashes are normally equal and can differ only through a space-hash version change. (h) When a source records no space, its rows are keyed with the space the other source records or the caller passes, so the module cannot tell whether the second study ran in that space. (i) A complete result may carry the ancestor rows of a study it continued from: the module counts them for each source, reports the counts, and refuses a holdout source whose ancestor rows repeat a trial id of the selection source, because such a source may be reporting the selection study's own rows; an overlap that the trial ids do not show cannot be detected, and an id that repeats by a coincidence of numbering is refused like a real one. (j) No claim is made about the data of either source, about the run that produced it, or about any strategy.
```

In plain words: the module joins two sets of finished rows by exact parameter vector equality and
says what the second study recorded for the vectors the first scored best; it splits nothing, runs
nothing and decides nothing, and the numbers are only as held-out as the caller's own arrangement
made them.

## What an independent oracle confirms

`tests/g3_oracles/holdout_oracle.py` uses the same eligibility ladder as the module.
Its independent arithmetic and rank routes rank vectors by COUNTING the vectors
strictly before them instead of sorting, intersect the two sources' key sets
to build the pool, take mid-ranks from tie-group bounds, and mid-rank the matched
vectors twice more for the two rank vectors of the correlation. The oracle computes
the rank correlation in the two-pass deviations form of exact rationals, multiplying
each deviations sum by the matched count before comparing it with this module's term.
It reaches the binary64 rank correlation by a high-precision square root of its own
exact ratio, rounded once. These routes differ from the module's ordered scan, strict
ranks and one-pass sums. Agreement compares the eligible rows and reasons, followed
and unmatched lists, pool, mid-ranks, difference, statistics and digest recipe. The
independence applies to the arithmetic and rank routes. The oracle constructs its own
digest byte string; no digest is derived by hand.

The oracle is evidence for the order of the refusal checks only through three argument
cases: `metric=7`, `k=None`, `direction="maximize"` refuses as
`holdout_parameter_missing`, naming `k`; `metric="m"`, `direction=None`, `k=0`
refuses as `holdout_parameter_missing`, naming `direction`; and `metric=7`,
`direction=None`, `k=1` refuses as `holdout_parameter_invalid`, naming `metric`.

It is NOT evidence for: the choice of the statistic itself (Pearson over
mid-ranked pairs, the median cut, the matched denominator), which rests on the hand-derived
fixtures and this page; whether the intermediate sums were computed as exact rationals or as
binary64, which can differ only above about `n = 9 * 10^3` matched vectors, a size v1
does not bound; the ancestor count of a source passed as a file path, which the oracle's fixtures
never pass; the key strings for dimension kinds its fixtures do not use; the key-sorting and
ASCII-only parts of the digest recipe, which no fixture exercises; and the wording of the
interpretation text. Its controls are a reordered tie, a value nudged by one unit in the
last place, an edited key, a one-sided reordering of the selection values and a two-unit
shift of the computed rank correlation, each applied to the oracle's own copy only, and
each of which the comparison must detect; the invariance test that
must agree — the two sources' backtest counters swapped — is a test of the module, not a control.
