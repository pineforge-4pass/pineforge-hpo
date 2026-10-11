# Neighbourhood stability of finished trials (`pineforge_hpo.neighbourhood`)

**Status: the module, its tests and its fixtures are committed but have never been
executed.** Every claim about behaviour on this page is deferred to the proof that runs
them. The contract identifier is `pineforge-hpo-neighbourhood/v1`. The pair maps are
described in [Pair ranking and pair maps](sensitivity.md).

In one sentence: this module checks how stable the best results are, using the trials
already run; nearby parameter sets that were not run are listed for the app to run. (The
roadmap's word "evaluate" describes the loop in the app, which runs the listed vectors and
calls this module again; the extra cost of that loop belongs to the app, not to this
module.)

## What it computes

Given finished trial rows, a candidate (a centre) and a band, the module lists the
parameter vectors that lie one move away from the centre on a single dimension, looks each
one up in the rows, and reports how many of them score near the best row. It also gives
per-parameter profiles and the neighbour vectors that no row covers.

Version 1 is post hoc. It reads rows that already exist and **evaluates nothing**: it
starts no engine, runs no strategy, writes no candidates file and changes no result or
trials file. A neighbour that has no row is not imputed; it is returned in
`missing_vectors`, and the caller may evaluate those vectors (for example with the
`candidates` sampler) and call again with both results as sources.

Everything is in-sample, single-objective and window-agnostic. The module is a library,
not a command, and uses only the Python standard library. No `log`, `exp` or `pow`
function is called anywhere in it.

## Calling it

```python
from pineforge_hpo.neighbourhood import neighbourhood, top_ids

centres = top_ids(["result.json"], 5)
report = neighbourhood(
    ["result.json"],
    centres=centres,
    band={"mode": "absolute", "value": 0.5},
    fraction=0.1,      # only if a continuous real dimension varies
    ratio=1.5,         # only if a log dimension varies
    pair_k=0,          # pair maps per centre; caller-supplied, no default; an explicit 0 means none
    max_work=None,     # optional cap on rows plus lookups
    space=None,        # only if no row or result records its space
)
```

| Argument | Meaning |
| --- | --- |
| `sources` | A non-empty list. Each item is a path to a complete result JSON or to a rows JSONL file, a parsed result, or a parsed list of rows. The position in the list is the source index. |
| `centres` | A list of `(source, trial_id)` pairs. Each must be a row with status `ok`, listed once. |
| `band` | `{"mode": "absolute" or "relative", "value": v}` with `v` finite and at least 0. |
| `fraction` | A number above 0 and at most 1; needed when a continuous real dimension varies. |
| `ratio` | A finite number above 1; needed when a log integer or log real dimension varies. |
| `pair_k` | An integer of at least 0, supplied by the caller, with no default. An explicit 0 means none: no pair map is built. Leaving it out raises `TypeError`; `None` is refused as `neighbourhood_parameter_missing`. |
| `max_work` | `None`, or a positive integer. |
| `space` | A recorded-space dict or a `StudySpec`, used only when no row or result records a space. A recorded space is never compared with a passed one. |

There is no default for the band, the fraction, the ratio, `pair_k` or `max_work`. The
caller supplies `pair_k`, and an explicit 0 means none. An application that wants defaults
chooses them itself.

`top_ids(sources, k, space=None)` returns the `(source, trial_id)` of the `k` best
distinct parameter vectors among the `ok` rows, best first. Equal objectives go to the
lower `(source, trial_id)`, and a vector that several rows share is represented by its
lowest identity. It reads and refuses its sources exactly as `neighbourhood` does.

## Sources and identity

A complete result (`trials_out` `all`, rows `warm_start_trials` followed by `trials`), a
rows JSONL file and a list of rows are all read by the shared rows reader with the checks
the continuation loader applies: strict JSON (no duplicate keys, no `NaN` or `Infinity`
token, no number that overflows), the nine statuses, `feasible` equal to status `ok`, a
finite objective for `ok` and `constraint_violation` rows, unique ids inside a source.

A row is identified by `(source_index, trial_id)`. Ids of two results can collide (a run
of the `candidates` sampler counts from 0), so the source index is part of the identity,
and the reference value is taken over the union of all sources.

All recorded spaces (a row's own, else its result's) must have an equal canonical form,
which includes the objective expression, its direction and its constraints. Every row's
`parameters` must be exactly the dimensions of the space, each value valid for its
dimension: an integer on its lattice, a real inside its bounds (a stepped real within
four units in the last place of a lattice point is read as that point), a boolean, a
categorical choice. Rows that carry a `backtest` object and have status `ok` must agree on
`input_bars_processed`, `script_bars_processed`, `input_tf_seconds` and `script_tf_seconds`,
and every row that carries a `space_hash` must carry the same one. This check is necessary
and not sufficient: equal counters and hashes do not prove the same data, window, fixed
inputs or strategy.

## Neighbours

For a centre with value `v` on a dimension, one move changes that dimension and leaves the
others as they are. A neighbour outside the bounds, or beyond the last lattice ordinal, is
dropped and counted, never clamped. Dimensions are taken in byte-wise UTF-8 name order;
within a dimension the neighbours are listed lower first, a categorical in declared order.

| Dimension | Neighbours | Dropped |
| --- | --- | --- |
| integer, step `s` | `v - s`, `v + s` | outside `[low, high]` |
| integer, log | `v / rho` and `v * rho`, each rounded half up with exact rational arithmetic (the binary64 value of `rho` is taken as exact) | outside `[low, high]`, or equal to `v` |
| real with a step | the ordinals one below and one above the centre's, decoded by the engine's fused multiply-add; the last ordinal snaps to `high` | an ordinal below 0 or above the last ordinal |
| real, continuous | `v - delta`, `v + delta` with `delta = fraction * (high - low)` in binary64 | outside `[low, high]`, or equal to `v` |
| real, log | `v / rho`, `v * rho` in binary64 | outside `[low, high]`, or equal to `v` |
| boolean | the other value | never |
| categorical | every other choice, in declared order (equal choices count once) | never |
| exactly one legal value | none; listed in `constant_dimensions` | n/a |

A dimension has exactly one legal value when `low == high`, when an integer or stepped-real
lattice has a single ordinal, or when a categorical has one distinct choice.

## Looking neighbours up

A generated vector is matched to rows by the canonical key of its parameters: typed
scalars, reals as binary64 bit patterns, with `-0.0` and `+0.0` one value. The match is
exact; there is no tolerance. The class of a neighbour:

| Class | Meaning |
| --- | --- |
| `scored_ok` | a row with status `ok`; it has an objective and a gap |
| `scored_infeasible` | a row with status `constraint_violation`; scored and never near; its objective is shown, its gap is null |
| `unscorable` | only rows with another status (`objective_error`, `constraint_error`, `engine_error`, `trial_error`, `trial_timeout`, `pruned`, `partial`); the recorded objective of its first row is shown, and the objective of a `pruned` row is a prefix value |
| `never_evaluated` | no row |

Several rows may share one vector. Their scored rows must agree exactly in status and
objective, else the call is refused; the outcome of a vector comes from its scored rows and,
if it has none, from its unscorable ones. A cell lists every row of its vector in
`rows`, lowest `(source, trial_id)` first. `duplicate_rows` counts the rows beyond the
first of each vector.

## Reference value and band

`R` is the best objective over every `ok` row of every source (largest when maximizing,
smallest when minimizing; the direction comes from the recorded space). The band
`{"mode", "value"}` gives a distance `delta`: the value itself (`absolute`) or
`value * abs(R)` (`relative`, one binary64 multiplication). The gap of a scored ok row is
`R - objective` (maximize) or `objective - R` (minimize), one subtraction, and the row is
**near** when `gap <= delta`: the band is inclusive. A relative band with `R == 0` is
refused. No threshold is built in.

## Result

The result is a JSON-ready dict; written with sorted keys and shortest round-trip numbers
its bytes depend only on the rows, the space, the centres and the caller's arguments.

| Field | Content |
| --- | --- |
| `contract`, `status` | `pineforge-hpo-neighbourhood/v1` and `ok` |
| `direction` | `maximize` or `minimize` |
| `reference` | `objective`, and the lowest `(source, trial_id)` that has it |
| `band` | `mode`, `value`, `delta` |
| `rules` | the `fraction`, `ratio` and `pair_k` given, and for each varying dimension the rule that moved it: `step`, `ratio_rounded`, `ordinal`, `fraction`, `ratio`, `other_value` or `other_choices` |
| `sources` | per source: `index`, `rows`, `space_hash` (the hash its rows record, else null) |
| `duplicate_rows`, `constant_dimensions` | as described above |
| `missing_vectors` | the full parameter vectors of every `never_evaluated` cell of every centre and pair map, without repeats, ordered by dimension in name order and by value within a dimension |
| `admission` | `rows`, `lookups`, `work` (their sum) and `max_work` |
| `input_sha256` | SHA-256 over the sorted 32-byte digests of the rows; each digest is the SHA-256 of the compact ASCII JSON of `[source, trial_id, status, objective, vector key]` |
| `interpretation` | the fixed text below |
| `centres` | one entry per centre, ascending `(source, trial_id)` |

Each centre entry has `source`, `trial_id`, `objective`, `centre_gap`, `centre_near`, and:

* `counts`: `geometric` (neighbours generated), `scored_ok`, `scored_infeasible`,
  `unscorable`, `never_evaluated`, `near`, and `better_neighbours` (scored ok neighbours
  strictly better than the centre).
* `dropped`: `bounds` and `same_as_centre`.
* `share_near_best`: `near` over `scored_ok + scored_infeasible`, as `numerator`,
  `denominator` and the binary64 quotient `value`; null when the denominator is 0.
* `coverage`: scored plus unscorable neighbours over `geometric`, in the same form; null
  when `geometric` is 0.
* `profile`: per varying dimension, the list of its neighbours, each with `value`, `class`,
  `objective` (the recorded number or null), `gap` (null unless `scored_ok`), `near` and
  `rows`.
* `pair_maps`: see [Pair ranking and pair maps](sensitivity.md).

## Refusals

A refusal raises `NeighbourhoodError` with a `code`, a sentence `detail`, the offending
rows as `(source_index, trial_id)` pairs in `ids` and offending dimension or argument
names in `names`; `document()` returns the same as a JSON-ready dict. No number of the
analysis is returned with a refusal. The codes belong to this module; they are not codes
of the engine's failure catalog.

| Code | When |
| --- | --- |
| `neighbourhood_input_invalid` | `sources` is not a non-empty list; a row is malformed; a parameter is off the space or its lattice, or a row's parameters differ from the dimensions; the recorded space or a dimension is malformed |
| `neighbourhood_rows_incomplete` | a result with `trials_out` other than `all`, or `trials_completed` or `warm_start.trials` disagreeing with the rows |
| `neighbourhood_space_missing` | no row or result records its space and none was passed |
| `neighbourhood_space_mismatch` | recorded spaces differ, or a result's direction differs from the space's |
| `neighbourhood_context_mismatch` | ok rows disagree on the `backtest` counters, or rows record different `space_hash` values |
| `neighbourhood_parameter_missing` | no band, no centres, no `pair_k`, or no fraction or ratio for a varying dimension that needs one |
| `neighbourhood_parameter_invalid` | a malformed band, fraction, ratio, `pair_k`, `max_work` or centre |
| `neighbourhood_band_degenerate` | a relative band while `R` is 0 |
| `neighbourhood_centre_invalid` | a centre is absent, is not a row with status `ok`, or is listed twice |
| `neighbourhood_no_varying_dimension` | every dimension has exactly one legal value |
| `neighbourhood_no_feasible_rows` | no row has status `ok`, so `R` is undefined |
| `neighbourhood_duplicate_conflict` | scored rows of one vector disagree in status or objective |
| `neighbourhood_resource_exceeded` | the work figure is above a supplied `max_work` |

When several defects exist, the first of this list of stages is reported: caller arguments,
reading the sources, space and direction, context, dimensions, rows against the space,
duplicate conflicts, no ok row, no varying dimension, a missing fraction or ratio, a
relative band at a zero reference, centres, work above `max_work`.

## Resource rule

The work figure is the number of rows read plus the number of lookups. The lookups are the
axis neighbours of every centre plus, for each ranked pair, the cells of its map except the
centre cell. The axis part is counted before the first lookup. The pair cells depend on the
ranking, which needs the axis lookups, so they are counted after the ranking and before the
first of them is looked up. The call is refused only against a `max_work` the caller
supplied; no cap is built in. Rows are read in full before the figure is known. Missing
vectors are returned in full, so an application that hands them to the engine splits a list
longer than the engine accepts.

## What is not claimed

The result says, in these fixed words:

> This describes the trials that were supplied and nothing else. It states no
> significance, confidence or probability. A neighbour that was never evaluated is not
> imputed, and nothing is said about it. The reference value is the best of many trials,
> so it is optimistic. With an adaptive sampler the neighbours that exist are not a random
> draw: the share near the best describes its stated neighbourhood only when coverage is 1.
> Every number is in-sample. Constraints, relations between parameters and whether two runs
> used the same data are not checked.

## Choices (reversible defaults)

These are the choices the contract fixes for version 1. They are defaults chosen so that
work could start; where the study spec has no sentence behind one, that is said. A
different reading changes outputs, not the structure of the result.

| ID | Choice |
| --- | --- |
| N1 | Version 1 evaluates nothing: it analyses the rows supplied and returns never-evaluated neighbour vectors as `missing_vectors`. |
| N2 | Radius one, axis-aligned: exactly one dimension differs from the centre. |
| N3 | Integer and stepped real: one lattice step, `v - step` and `v + step`. |
| N4 | Continuous real: a caller-supplied fraction of `high - low`, no default value. |
| N5 | Log integer and log real: a caller-supplied ratio above 1, no default; `v / rho` and `v * rho` (binary64 for a log real, exact rational arithmetic rounded half up for a log integer). The study spec defines no step for log geometry, so this has no source line there. |
| N6 | Boolean: the other value. Categorical: every other choice, in declared order. |
| N7 | A neighbour outside the bounds or beyond the last ordinal is dropped and counted, never clamped. |
| N8 | A never-evaluated neighbour is not imputed; it is excluded from numerator and denominator and reported as a count and as `missing_vectors`. |
| N9 | The share's denominator is the scored neighbours (`ok` or `constraint_violation`); its numerator the scored `ok` ones inside the band. Error, timeout, pruned and partial rows are reported apart. No source line. |
| N10 | `R` is the best `ok` objective over all supplied rows; the band is caller-supplied, absolute or relative to `abs(R)`, inclusive, with no default; a relative band at `R == 0` refuses. The form of "near the best" has no source line. |
| N11 | Duplicate vectors are grouped by the canonical key; scored duplicates must agree exactly or the call refuses. |
| N12 | `pair_k` is caller-supplied, with no default (an explicit 0 means none); pairs are ranked by the sum of the two dimensions' not-near counts, ties by ascending name pair. No source line for the ranking. |
| N13 | Centres are `(source, id)` pairs, each a scored `ok` row; `top_ids(k)` helps; sources are an ordered list identified by `(source_index, trial_id)`. |
| N14 | The same-context check uses what rows carry: equal canonical space and direction, equal `backtest` counters among scored rows that have them. Necessary, not sufficient. |
| N15 | Module-local `neighbourhood_*` codes, library functions only, one shared reader used by both analysis modules. |
| N16 | `-0.0` and `+0.0` are one value in a parameter key. |

Choices made while writing the module where the contract was silent: a stepped real
within four units in the last place of a lattice point is read as that point (as the
continuation loader reads it); equal categorical choices count once; the band must be an
object with exactly the keys `mode` and `value`; `max_work` must be at least 1; the report
of `sources` shows the `space_hash` that rows record; the refusal order above.

## What an independent oracle confirms

`tests/g2_oracles/neighbourhood_oracle.py` locates rows by a partition over the dimensions on
which each row differs from the centre, and names classes from a status table, instead of this
module's keyed index and ladder; it derives the counts, the missing-vector set and the pair maps
from different structures. Where it and this module agree, that agreement is evidence for the
location of rows, the naming of classes, the counts, the missing-vector set and the pair maps.

It is NOT evidence for:

- the arithmetic of the gap, the band distance and the share and coverage ratios: the oracle
  computes them in exact rationals and rounds once, and for finite doubles one IEEE subtraction,
  multiplication or division gives the same bits, so agreement checks the sign convention, the
  inclusive band test and the counts that feed the ratios, not the arithmetic itself. Those
  rest on the hand-derived `NF` fixtures; the random studies add nothing for them, since each
  one compares this module with the same oracle;
- the near-flag recheck, which is a CONSISTENCY CHECK ONLY: it re-evaluates the same comparison the
  classification used, so it can detect an internal inconsistency and can never show a
  classification to be wrong.

`tests/g2_oracles/neighbourhood_real_rows.py` compares real rows. Read its exit code as: 0 AGREE;
1 DISAGREE, or a failure; 2 the module refused (`MODULE REFUSED`), which is also the code of a
usage error, such as a missing `--pair-k`; 5 the input is not native output; 6 INCONCLUSIVE,
returned only when there is no disagreement but at least one comparison was set aside -- the
count and its ids are always printed. A run that compared nothing never exits 0.
