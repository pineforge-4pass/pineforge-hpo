# Pair ranking and pair maps (`pineforge_hpo.sensitivity`)

**Status: the module, its tests and its fixtures are committed but have never been
executed.** Every claim about behaviour on this page is deferred to the proof that runs
them. It builds on [Neighbourhood stability of finished trials](neighbourhood.md) and uses
the same contract identifier, `pineforge-hpo-neighbourhood/v1`.

In one sentence: this module checks how stable the best results are, using the trials
already run; nearby parameter sets that were not run are listed for the app to run.

## What it computes

A neighbourhood looks at one dimension at a time. A **pair map** crosses the one-move
neighbours of two dimensions around a centre, so that the cells that lie off both axes (the
diagonals) show what happens when two parameters change together. Like the neighbourhood
it is post hoc: every cell is looked up in the rows that were supplied, nothing is
evaluated, and a cell with no row is returned in `missing_vectors` of the neighbourhood
result so that the caller can evaluate those vectors and call again.

The pair functions work on what the neighbourhood module produced. The pairs are ranked
from the centre's per-dimension profile, the cells are classified by the same lookup as
the neighbours, and no neighbour is generated here. The neighbourhood module calls
`plan_pairs` and `pair_maps` when the `pair_k` it was given is above 0 (`pair_k` is
supplied by the caller and has no default; an explicit 0 means none); they are public so
that the ranking and the maps can be read and tested on their own.

```python
from pineforge_hpo.neighbourhood import neighbourhood

report = neighbourhood(
    ["result.json"],
    centres=[(0, 0)],
    band={"mode": "absolute", "value": 1.0},
    pair_k=3,  # caller-supplied, no default; an explicit 0 means none
)
maps = report["centres"][0]["pair_maps"]
```

## Ranking (reversible default N12)

For each centre, the **not-near count** of a dimension is the number of its scored
neighbours (status `ok` or `constraint_violation`) that are not near the best row. A
constraint violation is scored and never near, so it counts; an unscorable or a
never-evaluated neighbour does not. The score of a pair of dimensions is the sum of their
two counts. Pairs are ordered by descending score, equal scores by ascending
`(first, second)` name, both compared as UTF-8 bytes, and the first `pair_k` are kept. With
fewer pairs than `pair_k`, all are kept. `pair_k` of 0 builds no map, and so does a space
with fewer than two varying dimensions. Constant dimensions never take part.

The rule has no source line in the study spec; it is a default, and a different ranking
changes which pairs are shown, not the structure of a map.

## The map

A map is an object with:

| Field | Content |
| --- | --- |
| `dimensions` | `[first, second]`, the pair in ascending name order |
| `score` | the pair's score |
| `cells` | the cells in row-major order: the first dimension is the outer loop, each dimension in its own order (ascending, or declared order for a categorical) |
| `diagonal_total` | the number of diagonal cells |
| `diagonal_evaluated` | diagonal cells that have any row |
| `diagonal_scored` | diagonal cells that are `scored_ok` or `scored_infeasible` |
| `diagonal_near` | diagonal cells that are `scored_ok` and near the best |

The values on each axis are the centre's value together with its generated neighbours on
that dimension. A neighbour that was dropped (outside the bounds, or beyond the last lattice
ordinal) is not an axis value, so a centre at an edge gives a smaller map.

Each cell has `values` (the two dimensions' values), `role`, and the fields of a
neighbourhood cell: `class`, `objective`, `gap`, `near` and `rows`. The role is `centre`
(neither value moved), `axis` (exactly one moved) or `diagonal` (both moved). The other
dimensions keep the centre's values. A cell is classified exactly like a neighbour: see
the classes and the band in the neighbourhood page.

Example. Fast 2..6 step 2 and Slow 10..30 step 10, centre (4, 20) scoring 8.0, band 1.0:
the map of (Fast, Slow) has the nine cells (2,10), (2,20), (2,30), (4,10), (4,20), (4,30),
(6,10), (6,20), (6,30), of which (4,20) is the centre, the four that share a value with it
are axis cells and (2,10), (2,30), (6,10), (6,30) are diagonal cells.

## Cost

A map of two axes that hold `a` and `b` values has `a * b - 1` cells besides the centre.
Its axis cells are the neighbours already looked up; its `(a - 1) * (b - 1)` diagonal cells
are new. A diagonal that no row covers is a vector to evaluate. The module evaluates none.

The lookups of the pair cells are counted in the work figure of the neighbourhood result
(`admission`). They are counted after the ranking and before the first of them is looked
up, because the cells depend on the ranking; a `max_work` that the figure exceeds refuses
the call with `neighbourhood_resource_exceeded`.

## What is not claimed

A pair map is a set of measured cells. It is not a test of interaction, it does not rank
the strength of an interaction, and it says nothing about cells without a row. The fixed
text of the neighbourhood result applies to it unchanged: no significance, confidence or
probability; unevaluated cells are not imputed; the reference value is the best of many
trials and is optimistic; with an adaptive sampler the cells that exist are not a random
draw; everything is in-sample; constraints, parameter relations and the sameness of the
data behind two runs are not checked.

## Functions

| Function | Use |
| --- | --- |
| `not_near_counts(profile)` | the not-near count of each dimension of a centre's `profile` |
| `rank_pairs(counts, pair_k)` | the best `pair_k` pairs as `(first, second, score)` from such counts |
| `plan_pairs(study, values, profile, pair_k)` | ranks the pairs of one centre and spans their axes as `PairPlan` objects (`first`, `second`, `score`, `first_values`, `second_values`, `lookups`); looks nothing up |
| `pair_maps(study, values, plans)` | the maps of the planned pairs at one centre |

`study` is the object `pineforge_hpo.neighbourhood.prepare` returns, with a band bound;
`values` is the centre's normalised parameter vector.
