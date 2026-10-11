"""Pair ranking and pair maps for the neighbourhood of finished trials (version 1).

A pair map crosses the one-move neighbours of two dimensions around a centre, so
that the cells off the two axes (the diagonals) show whether two parameters
interact. It is built after the fact, like the neighbourhood itself: the cells
are looked up in the rows that were supplied and nothing is evaluated.

The module works on what ``neighbourhood`` produced. The pairs are ranked from
the per-dimension profile of a centre, the map cells are classified by
``Study.cell``, and no neighbour is generated here. ``neighbourhood`` calls
``plan_pairs`` and ``pair_maps`` when ``pair_k`` is above 0; they are public so
that the ranking and the maps can be read and tested on their own. See
``docs/sensitivity.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .neighbourhood import (
    NEVER_EVALUATED,
    SCORED_INFEASIBLE,
    SCORED_OK,
    Study,
    name_key,
)

__all__ = ["PairPlan", "not_near_counts", "pair_maps", "plan_pairs", "rank_pairs"]

_SCORED = (SCORED_OK, SCORED_INFEASIBLE)
_ROLES = ("centre", "axis", "diagonal")


@dataclass(frozen=True)
class PairPlan:
    """A ranked pair of dimensions and the values that span its map at a centre.

    ``first`` sorts before ``second`` by byte-wise name. ``first_values`` and
    ``second_values`` hold the centre's value and its generated neighbours on
    that dimension, in the dimension's own order (ascending, or declared order).
    """

    first: str
    second: str
    score: int
    first_values: tuple
    second_values: tuple

    @property
    def lookups(self) -> int:
        """Cells to look up: every cell of the map except the centre cell."""
        return len(self.first_values) * len(self.second_values) - 1


def not_near_counts(profile: dict) -> dict:
    """Per dimension, the scored neighbours of a centre that are not near the best.

    A scored neighbour is one with status ok or constraint violation. An
    infeasible neighbour is scored and never near. Unscorable and never
    evaluated neighbours are not counted.
    """
    return {
        name: sum(
            1 for cell in cells if cell["class"] in _SCORED and not cell["near"]
        )
        for name, cells in profile.items()
    }


def rank_pairs(counts: dict, pair_k: int) -> list:
    """The ``pair_k`` best pairs as (first, second, score), best first.

    The score of a pair is the sum of the not-near counts of its two dimensions.
    Pairs are ordered by descending score, then by ascending (first, second)
    name, both byte-wise. Fewer pairs are returned when fewer exist.
    """
    names = sorted(counts, key=name_key)
    pairs = [
        (counts[first] + counts[second], first, second)
        for position, first in enumerate(names)
        for second in names[position + 1 :]
    ]
    pairs.sort(key=lambda pair: (-pair[0], name_key(pair[1]), name_key(pair[2])))
    return [(first, second, score) for score, first, second in pairs[:pair_k]]


def _axis(study: Study, values: dict, profile: dict, name: str) -> tuple:
    reached = [values[name]] + [cell["value"] for cell in profile[name]]
    return tuple(study.sort_values(name, reached))


def plan_pairs(study: Study, values: dict, profile: dict, pair_k: int) -> list:
    """Rank the pairs of one centre and span their maps; looks nothing up.

    ``values`` is the centre's normalised vector and ``profile`` its
    per-dimension neighbour cells as ``neighbourhood`` builds them.
    """
    return [
        PairPlan(
            first,
            second,
            score,
            _axis(study, values, profile, first),
            _axis(study, values, profile, second),
        )
        for first, second, score in rank_pairs(not_near_counts(profile), pair_k)
    ]


def _pair_map(study: Study, values: dict, plan: PairPlan) -> dict:
    first, second = plan.first, plan.second
    at_first = study.rank(first, values[first])
    at_second = study.rank(second, values[second])
    cells = []
    diagonal = evaluated = scored = near = 0
    for left in plan.first_values:
        for right in plan.second_values:
            moved = (study.rank(first, left) != at_first) + (
                study.rank(second, right) != at_second
            )
            cell = study.cell({**values, first: left, second: right})
            cells.append(
                {
                    "values": {first: left, second: right},
                    "role": _ROLES[moved],
                    **cell,
                }
            )
            if moved == 2:
                diagonal += 1
                evaluated += cell["class"] != NEVER_EVALUATED
                scored += cell["class"] in _SCORED
                near += cell["near"]
    return {
        "dimensions": [first, second],
        "score": plan.score,
        "cells": cells,
        "diagonal_total": diagonal,
        "diagonal_evaluated": evaluated,
        "diagonal_scored": scored,
        "diagonal_near": near,
    }


def pair_maps(study: Study, values: dict, plans: list) -> list:
    """The maps of the planned pairs at one centre, in plan order.

    Cells are in row-major order: the first dimension is the outer loop, each
    dimension ascending. A cell is the centre, an axis cell (one dimension moved)
    or a diagonal cell (both moved), and carries the class, objective, gap, near
    flag and row ids of ``Study.cell``. The diagonal totals count the diagonal
    cells, those with any row, those scored and those scored near the best.
    """
    return [_pair_map(study, values, plan) for plan in plans]
