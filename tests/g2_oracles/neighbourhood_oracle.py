#!/usr/bin/env python3
"""Independent oracle for ``pineforge_hpo.neighbourhood`` and ``pineforge_hpo.sensitivity``.

The module generates the vectors one move from a centre with per-kind rules, looks them up in an
index keyed by canonical JSON, and ranks pairs with a sort. This script reaches the same results by
other routes and never takes a number from the module:

* the legal values of every finite dimension are enumerated (integer lattices by ordinal, stepped
  reals by one rounding of the exact value k*step + low with the endpoint snap, booleans, typed
  categoricals), and a neighbour is the adjacent ordinal or the other value;
* log integers use integer arithmetic on the ratio's exact numerator and denominator (and are
  cross-checked with Decimal), continuous and log reals use exact rational arithmetic and refuse to
  judge a value that is not exactly a binary64 number (none of the pinned cases is such a value);
* the rows are filed by typed-tuple distance from the centre (the set of dimensions on which a
  row's vector differs from the centre's) and a cell is read off that partition, not looked up in
  an index; a row's vector is identified by a typed tuple, not by canonical JSON;
* a cell's class comes from the contract's table of classes (ok, constraint violation, any other
  status, no row), the scored rows of a vector are checked to agree, and the counts, the share
  near the best, the coverage, the diagonal totals and the never-evaluated vectors are
  recomputed from the finished cells and the row keys instead of being accumulated as the
  module does;
* pairs are ranked by repeated selection of the best remaining pair, not by sorting;
* the one-move sets of random small spaces are also listed by enumerating the whole product of the
  finite domains and keeping the vectors that differ from the centre in exactly one dimension
  (adjacent ordinal for lattices), and compared with the rule-based sets.

Phase 1 needs no module: the checker's numbers are compared with the values the test files pin for
NF1-NF6 (including the sensitivity fixtures NF5 and NF5b), the generation table, the pair ranking,
``top_ids`` and the refusal rules (hand-derived there, re-derived here), with a hand-derived
perturbation of NF1, and with NF7, which is derived by hand in this file (unscorable neighbours,
a pruned neighbour whose recorded objective is a prefix value, scored vectors with an unscorable
duplicate of lower and of higher identity, and the two orders of an ok row and a constraint
violation on one vector). The random studies run the oracle on random rows of random small spaces
(scored duplicates in agreement, unscorable ones not), where phase 2 compares the whole document
of the module with the oracle's. Phase 2 imports ``pineforge_hpo.neighbourhood`` and
``.sensitivity`` only after phase 1 is finished and compares the module with the checker on the
same inputs.

Usage: ``python3 tests/g2_oracles/neighbourhood_oracle.py [--no-module] [--cases N] [--seed S]``.
Exit code 0 only if every comparison agreed, every section made the comparisons it must make and the
module was compared; 1 for any failure; 2 for a usage error; 3 when ``--no-module`` ran phase 1 only
(the checker against the pinned values, the module NOT compared). A run that compared nothing
with the module is never exit 0.
"""

from __future__ import annotations

import argparse
import copy
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction
import itertools
import json
import math
from pathlib import Path
import random
import sys

# Write nothing: no bytecode cache beside this script or inside the package that phase 2 imports.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
NEIGHBOURHOOD_DIR = ROOT / "tests" / "python" / "fixtures" / "neighbourhood"
SENSITIVITY_DIR = ROOT / "tests" / "python" / "fixtures" / "sensitivity"
# Exit code of a run that did not compare the module (--no-module); 0 is kept for a full pass.
NO_MODULE_EXIT = 3

CLASSES = ("scored_ok", "scored_infeasible", "unscorable", "never_evaluated")
SCORED_CLASSES = ("scored_ok", "scored_infeasible")
# The contract's table of classes (clause 4 of the proposed dossier, pin N8-N9): an ok row is
# scored ok, a constraint violation is scored infeasible, every other status is unscorable. A
# vector with no row is never evaluated. Written out as data so that the classification is a
# lookup, not a ladder of tests.
STATUS_CLASS = {
    "ok": "scored_ok",
    "constraint_violation": "scored_infeasible",
    "objective_error": "unscorable",
    "constraint_error": "unscorable",
    "engine_error": "unscorable",
    "trial_error": "unscorable",
    "trial_timeout": "unscorable",
    "pruned": "unscorable",
    "partial": "unscorable",
}
ROLES = ("centre", "axis", "diagonal")
# the labels the module reports for each kind of rule
RULE_LABELS = {
    "integer": "step",
    "log_integer": "ratio_rounded",
    "stepped_real": "ordinal",
    "continuous": "fraction",
    "log_real": "ratio",
    "boolean": "other_value",
    "categorical": "other_choices",
    "constant": "constant",
}
ABSOLUTE_1 = {"mode": "absolute", "value": 1.0}
OBJECTIVE = {"expression": "metrics.all.net_profit_pct", "direction": "maximize", "constraints": []}

# ---------------------------------------------------------------------------------------------
# Harness: every comparison is counted and printed, a disagreement is recorded and never swallowed.
# ---------------------------------------------------------------------------------------------


def same(a, b, strict: bool = False) -> bool:
    """Exact equality: numbers by value (int, float and Fraction mix; with ``strict`` the types must
    match too), bool only with bool, containers element by element (list and tuple are
    interchangeable), the rest by type and value."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    numeric = (int, float, Fraction)
    if isinstance(a, numeric) and isinstance(b, numeric):
        if strict and type(a) is not type(b):
            return False
        return a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y, strict) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        if strict:
            # the keys must match in type too: a count keyed by 3.0 is not a count keyed by 3
            first = {(type(key), key): value for key, value in a.items()}
            second = {(type(key), key): value for key, value in b.items()}
            return first.keys() == second.keys() and all(
                same(first[key], second[key], strict) for key in first
            )
        return a.keys() == b.keys() and all(same(a[key], b[key], strict) for key in a)
    return type(a) is type(b) and a == b


def show(value, limit: int = 150) -> str:
    if isinstance(value, Fraction):
        text = (
            str(value.numerator)
            if value.denominator == 1
            else f"{value.numerator}/{value.denominator}"
        )
    elif isinstance(value, list):
        text = "[" + ", ".join(show(item, limit) for item in value) + "]"
    elif isinstance(value, tuple):
        text = "(" + ", ".join(show(item, limit) for item in value) + ")"
    elif isinstance(value, dict):
        text = (
            "{" + ", ".join(f"{show(k, limit)}: {show(v, limit)}" for k, v in value.items()) + "}"
        )
    else:
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


class Ledger:
    def __init__(self, echo: bool = True, strict: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.failures: list[str] = []
        self.name = ""
        self.made = 0
        self.echo = echo
        self.strict = strict

    def say(self, text: str) -> None:
        if self.echo:
            print(text)

    def begin(self, name: str) -> None:
        self.name = name
        self.made = 0
        self.say(f"\n== {name}")

    def end(self, expected: int) -> None:
        self.say(f"-- {self.name}: {self.made} comparisons made, {expected} expected")
        if self.made != expected:
            self.fail(f"{self.name}: made {self.made} comparisons, expected {expected}")

    def fail(self, message: str) -> None:
        self.failures.append(message)
        self.say(f"FAIL {message}")

    def note(self, message: str) -> None:
        self.say(f"   note: {message}")

    def check(self, kind: str, label: str, condition: bool) -> bool:
        """Record a comparison whose verdict the caller decided with a primitive operator."""
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.made += 1
        if not condition:
            self.failures.append(f"{self.name}: {label}")
        self.say(f"{'AGREE   ' if condition else 'DISAGREE'} {kind:8s} {label}")
        return condition

    def compare(
        self, kind: str, label: str, checker, other, source: str = "", *, quiet: bool = False
    ) -> bool:
        """Compare the checker's value with ``other`` (kind: fixture, internal, control, module)."""
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.made += 1
        agree = same(checker, other, self.strict)
        if not agree:
            self.failures.append(f"{self.name}: {label}")
        if agree and quiet:
            return True
        status = "AGREE   " if agree else "DISAGREE"
        where = f" [{source}]" if source else ""
        self.say(
            f"{status} {kind:8s} {label} | checker={show(checker)} | {kind}={show(other)}{where}"
        )
        return agree


def harness_self_test() -> None:
    """Abort before any comparison if the helpers cannot tell different things apart. The verdicts
    here use ``is``, not ``same``, so a broken ``same`` cannot vouch for itself."""
    matching = [
        (1, 1.0),
        (Fraction(1, 2), 0.5),
        ([1, (2, 3)], (1, [2, 3])),
        ({"a": [1]}, {"a": (1,)}),
        (None, None),
        (True, True),
    ]
    differing = [
        (3, 4),
        (0.5, 0.25),
        (Fraction(1, 2), Fraction(1, 3)),
        (True, 1),
        ([1, 2], [1, 3]),
        ({"a": 1}, {"a": 2}),
        (None, 0),
        ("a", "b"),
        ((1,), (1, 2)),
        (float("nan"), float("nan")),
    ]
    for left, right in matching:
        if same(left, right) is not True:
            raise SystemExit(f"harness self-test failed: same({left!r}, {right!r}) must be True")
    for left, right in differing:
        if same(left, right) is not False:
            raise SystemExit(f"harness self-test failed: same({left!r}, {right!r}) must be False")
    for left, right in ((1, 1.0), (Fraction(1, 2), 0.5), (1.0, Fraction(1)), ({3: 1}, {3.0: 1})):
        if same(left, right, True) is not False:
            raise SystemExit(
                f"harness self-test failed: strict same({left!r}, {right!r}) must be False"
            )
    if (
        same(1.5, 1.5, True) is not True
        or same([1, 2.0], (1, 2.0), True) is not True
        or same({3: 1.5}, {3: 1.5}, True) is not True
    ):
        raise SystemExit(
            "harness self-test failed: strict same must accept equal values of equal types"
        )
    probe = Ledger(echo=False)
    probe.name = "probe"
    probe.compare("control", "equal", 1, 1)
    probe.compare("control", "different", 1, 2)
    if probe.failures != ["probe: different"] or probe.counts != {"control": 2} or probe.made != 2:
        raise SystemExit("harness self-test failed: the ledger must record exactly the one failure")
    strict_probe = Ledger(echo=False, strict=True)
    strict_probe.name = "strict probe"
    strict_probe.compare("control", "int against float", 1, 1.0)
    if strict_probe.failures != ["strict probe: int against float"]:
        raise SystemExit(
            "harness self-test failed: a strict ledger must reject an int against a float"
        )
    print("harness self-test passed (same() and the ledger tell different values apart)")


# ---------------------------------------------------------------------------------------------
# The oracle: domains and neighbour rules.
# ---------------------------------------------------------------------------------------------


class InexactOracle(Exception):
    """The oracle's exact arithmetic does not land on a binary64 value, so it declines to judge."""


class Declined:
    """The marker ``analyse`` leaves where the oracle declined to compute a figure (see its
    ``set_aside``). It equals nothing but itself, so whoever meets it must set the figure aside
    and not compare it."""

    def __repr__(self) -> str:
        return "DECLINED"


DECLINED = Declined()


def exact_float(value: Fraction) -> float:
    number = float(value)
    if Fraction(number) != value:
        raise InexactOracle(f"{value} is not a binary64 value")
    return number


def stepped_real_lattice(low: float, high: float, step: float) -> list:
    """The legal values of a stepped real: low + k*step for k = 0, 1, ... each by one rounding of
    the exact value, while it does not exceed high; the first value above high ends the lattice,
    as ``high`` itself when it is at most one representable value above it (the endpoint snap)."""
    exact_low = Fraction(low)
    exact_step = Fraction(step)
    values: list = []
    ordinal = 0
    while True:
        if len(values) > 1_000_000:
            raise InexactOracle("too many ordinals to enumerate")
        value = float(ordinal * exact_step + exact_low)
        if value <= high:
            values.append(value)
        elif value <= math.nextafter(high, math.inf):
            values.append(high)
            break
        else:
            break
        ordinal += 1
    return values


class Domain:
    """One dimension of a recorded space: its kind, its rule and what it needs to find
    neighbours."""

    def __init__(self, name: str, spec: dict) -> None:
        self.name = name
        self.kind = spec["kind"]
        self.log = bool(spec.get("log", False))
        self.low = spec.get("low")
        self.high = spec.get("high")
        self.step = spec.get("step")
        self.lattice: list | None = None
        self.choices: list = []
        if self.kind == "integer":
            self.step = 1 if self.step is None else self.step
            span = (self.high - self.low) // self.step
            self.rule = "constant" if span == 0 else ("log_integer" if self.log else "integer")
        elif self.kind == "real":
            self.low = float(self.low)
            self.high = float(self.high)
            if self.low == self.high:
                self.rule = "constant"
                self.lattice = [self.low]
            elif self.step is None:
                self.rule = "log_real" if self.log else "continuous"
            else:
                self.step = float(self.step)
                self.lattice = stepped_real_lattice(self.low, self.high, self.step)
                self.rule = "constant" if len(self.lattice) == 1 else "stepped_real"
        elif self.kind == "boolean":
            self.rule = "boolean"
        elif self.kind == "categorical":
            for choice in spec["choices"]:
                if not any(
                    type(choice) is type(other) and choice == other for other in self.choices
                ):
                    self.choices.append(choice)
            self.rule = "constant" if len(self.choices) == 1 else "categorical"
        else:
            raise ValueError(f"unknown dimension kind {self.kind!r}")


def tag(domain: Domain, value) -> tuple:
    """The typed identity of a value inside its dimension (-0.0 is 0.0; 1, 1.0 and True differ)."""
    if domain.kind == "integer":
        return ("i", value)
    if domain.kind == "real":
        return ("r", value + 0.0)
    if domain.kind == "boolean":
        return ("b", value)
    for position, choice in enumerate(domain.choices):
        if type(value) is type(choice) and value == choice:
            return ("c", position)
    raise ValueError(f"{value!r} is not a choice of {domain.name}")


def canon(domain: Domain, raw):
    """The value as the dimension holds it (a lattice point for a stepped real, a declared choice
    for a categorical); ValueError when the row's value is not a legal value of the dimension."""
    # The validity rules below restate the loader's (continuation.py, ``_parameters``): an integer
    # is an int on the lattice, a real an int or float inside the bounds, a boolean a bool, a
    # categorical a declared choice of the same type with an int accepted for a float choice. The
    # pin names the loader and not these rules, so they are a restatement, not an independent
    # reading; they change no number on a valid row.
    if domain.kind == "integer":
        if (
            type(raw) is int
            and domain.low <= raw <= domain.high
            and (raw - domain.low) % domain.step == 0
        ):
            return raw
    elif domain.kind == "real":
        if type(raw) in (int, float):
            number = float(raw)
            if domain.lattice is not None:
                # The loader's rule, restated in exact arithmetic: a value within four units in
                # the last place (of the larger of the two) of the nearest lattice point is that
                # point. The 4 and the larger-of-two convention are the loader's (clause 2b
                # points at it); the pin does not derive them, so this is a restatement, not an
                # independent reading.
                nearest = min(
                    domain.lattice, key=lambda value: abs(Fraction(value) - Fraction(number))
                )
                slack = 4 * max(Fraction(math.ulp(number)), Fraction(math.ulp(nearest)))
                if abs(Fraction(nearest) - Fraction(number)) <= slack:
                    return nearest + 0.0
            elif domain.low <= number <= domain.high:
                return number + 0.0
    elif domain.kind == "boolean":
        if type(raw) is bool:
            return raw
    else:
        for choice in domain.choices:
            if type(raw) is type(choice) and raw == choice:
                return choice
        if type(raw) is int:
            for choice in domain.choices:
                if type(choice) is float and float(raw) == choice:
                    return choice
    raise ValueError(f"{raw!r} is not a legal value of {domain.name}")


def within_exact(domain: Domain, value, candidates: list) -> tuple:
    """Keep the exact candidates inside [low, high]; every candidate must be a binary64 value."""
    centre = Fraction(value)
    kept: list = []
    bounds = same_count = 0
    for candidate in candidates:
        number = exact_float(candidate)
        if candidate == centre:
            same_count += 1
        elif Fraction(domain.low) <= candidate <= Fraction(domain.high):
            kept.append(number)
        else:
            bounds += 1
    return kept, bounds, same_count


def neighbours(domain: Domain, value, fraction=None, ratio=None) -> tuple:
    """(values lower first, moves dropped by the bounds, moves that land on the value itself)."""
    rule = domain.rule
    if rule == "constant":
        return [], 0, 0
    if rule == "integer":
        ordinal = (value - domain.low) // domain.step
        last = (domain.high - domain.low) // domain.step
        kept: list = []
        bounds = 0
        for target in (ordinal - 1, ordinal + 1):
            if 0 <= target <= last:
                kept.append(domain.low + target * domain.step)
            else:
                bounds += 1
        return kept, bounds, 0
    if rule == "stepped_real":
        ordinal = domain.lattice.index(value)
        kept = []
        bounds = 0
        for target in (ordinal - 1, ordinal + 1):
            if 0 <= target < len(domain.lattice):
                kept.append(domain.lattice[target])
            else:
                bounds += 1
        return kept, bounds, 0
    if rule == "continuous":
        delta = Fraction(fraction) * (Fraction(domain.high) - Fraction(domain.low))
        return within_exact(domain, value, [Fraction(value) - delta, Fraction(value) + delta])
    if rule == "log_real":
        return within_exact(
            domain, value, [Fraction(value) / Fraction(ratio), Fraction(value) * Fraction(ratio)]
        )
    if rule == "log_integer":
        numerator, denominator = float(ratio).as_integer_ratio()  # ratio = numerator / denominator
        lower = (2 * value * denominator + numerator) // (2 * numerator)  # v / ratio, half up
        upper = (2 * value * numerator + denominator) // (2 * denominator)  # v * ratio, half up
        kept = []
        bounds = same_count = 0
        for candidate in (lower, upper):
            if candidate == value:
                same_count += 1
            elif domain.low <= candidate <= domain.high:
                kept.append(candidate)
            else:
                bounds += 1
        return kept, bounds, same_count
    if rule == "boolean":
        return [not value], 0, 0
    return [c for c in domain.choices if not (type(c) is type(value) and c == value)], 0, 0


def log_integer_by_decimal(domain: Domain, value: int, ratio: float) -> tuple:
    """The log integer rule a second way: Decimal at 120 digits, rounded half up."""
    kept: list = []
    bounds = same_count = 0
    with localcontext() as context:
        context.prec = 120
        exact = Decimal(ratio)
        for raw in (Decimal(value) / exact, Decimal(value) * exact):
            candidate = int(raw.quantize(Decimal(1), rounding=ROUND_HALF_UP))
            if candidate == value:
                same_count += 1
            elif domain.low <= candidate <= domain.high:
                kept.append(candidate)
            else:
                bounds += 1
    return kept, bounds, same_count


def real_rule_by_floats(domain: Domain, value: float, fraction, ratio) -> tuple:
    """The continuous and log real rules a second way: plain binary64 arithmetic."""
    if domain.rule == "continuous":
        delta = fraction * (domain.high - domain.low)
        candidates = (value - delta, value + delta)
    else:
        candidates = (value / ratio, value * ratio)
    kept: list = []
    bounds = same_count = 0
    for candidate in candidates:
        if candidate == value:
            same_count += 1
        elif domain.low <= candidate <= domain.high:
            kept.append(candidate)
        else:
            bounds += 1
    return kept, bounds, same_count


# ---------------------------------------------------------------------------------------------
# The oracle: rows, classification, pair ranking and maps.
# ---------------------------------------------------------------------------------------------


class Study:
    """The rows of several parsed results over one recorded space. Rows are never looked up in an
    index: ``partition`` files every row under its typed-tuple distance from a centre, ``rows_at``
    reads one cell off that partition and ``classify`` turns the rows of a cell into its class,
    by the contract's table of classes."""

    def __init__(self, sources: list) -> None:
        space = sources[0]["space"]
        self.direction = space["objective"]["direction"]
        # orientation: multiplied by this sign, a larger objective is always a better one
        self.sign = 1 if self.direction == "maximize" else -1
        specs = space["parameters"]
        self.names = sorted(specs, key=lambda name: name.encode("utf-8"))
        self.domains = {name: Domain(name, specs[name]) for name in self.names}
        self.varying = [n for n in self.names if self.domains[n].rule != "constant"]
        self.constants = [n for n in self.names if self.domains[n].rule == "constant"]
        self.rows: list = []
        self.sources: list = []
        for index, source in enumerate(sources):
            hashes = []
            for trial in sorted(source["trials"], key=lambda item: item["trial_id"]):
                values = {
                    name: canon(self.domains[name], trial["parameters"][name])
                    for name in self.names
                }
                self.rows.append(
                    {
                        "source": index,
                        "id": trial["trial_id"],
                        "status": trial["status"],
                        "objective": (
                            None if trial["objective"] is None else float(trial["objective"])
                        ),
                        "values": values,
                        "key": self.key(values),
                    }
                )
                if trial.get("space_hash") is not None:
                    hashes.append(trial["space_hash"])
            self.sources.append(
                {
                    "index": index,
                    "rows": len(source["trials"]),
                    "space_hash": hashes[0] if hashes else None,
                }
            )
        self.rows.sort(key=lambda row: (row["source"], row["id"]))
        # The reference is the head of the ok rows sorted best first, ties by lower identity: one
        # ordering gives both its objective and its row.
        ok = [row for row in self.rows if row["status"] == "ok"]
        best = sorted(
            ok, key=lambda row: (-self.sign * row["objective"], row["source"], row["id"])
        )[0]
        self.reference = best["objective"]
        self.reference_id = (best["source"], best["id"])
        keys = [row["key"] for row in self.rows]
        self.duplicate_rows = len(keys) - len(set(keys))
        self.delta = None

    def key(self, values: dict) -> tuple:
        return tuple(tag(self.domains[name], values[name]) for name in self.names)

    def bind(self, band: dict) -> None:
        """The band distance: the value itself, or the value times abs(reference) rounded once
        from the exact product (the contract's one multiplication)."""
        if band["mode"] == "absolute":
            self.delta = float(band["value"])
        else:
            self.delta = float(Fraction(band["value"]) * abs(Fraction(self.reference)))

    def gap(self, objective: float) -> float:
        """The distance from the reference: the oriented difference, rounded once from the exact
        value (the contract's one subtraction); never negative for an ok objective."""
        return float(self.sign * (Fraction(self.reference) - Fraction(objective)))

    def better(self, objective: float, other: float) -> bool:
        """Strictly better, compared exactly on the oriented values."""
        return self.sign * Fraction(objective) > self.sign * Fraction(other)

    def partition(self, centre_key: tuple) -> list:
        """Every row with the set of dimensions on which its typed vector differs from the
        centre's."""
        return [
            (
                row,
                frozenset(
                    name
                    for name, here, there in zip(self.names, row["key"], centre_key)
                    if here != there
                ),
            )
            for row in self.rows
        ]

    def rows_at(self, parted: list, wanted: dict) -> list:
        """The rows of one cell: those that agree with the centre except on the dimensions named in
        ``wanted``, and that carry the given typed tag on each of them (a tag equal to the
        centre's own is the centre's value). Rows keep their identity order."""
        place = {name: self.names.index(name) for name in wanted}
        free = set(wanted)
        return [
            row
            for row, apart in parted
            if apart <= free
            and all(row["key"][place[name]] == mark for name, mark in wanted.items())
        ]

    def classify(self, rows: list) -> dict:
        """One cell from the rows that hold its vector, in identity order, by the contract's
        table. A scored row speaks for the vector, and all scored rows must agree in status and
        objective (the contract refuses a vector whose scored rows differ); a vector with only
        unscorable rows is shown by its lowest-identity row; a vector with no row is never
        evaluated. A scored ok row is near when its gap does not exceed the band distance (the
        contract's inclusive test)."""
        listed = [[row["source"], row["id"]] for row in rows]
        scored = [row for row in rows if STATUS_CLASS[row["status"]] in SCORED_CLASSES]
        if scored:
            outcomes = {(row["status"], row["objective"]) for row in scored}
            if len(outcomes) != 1:
                raise ValueError(
                    "the scored rows of one vector disagree, which the contract refuses"
                )
            ((status, objective),) = outcomes
            if STATUS_CLASS[status] == "scored_ok":
                gap = self.gap(objective)
                return {
                    "class": "scored_ok",
                    "objective": objective,
                    "gap": gap,
                    "near": gap <= self.delta,
                    "rows": listed,
                }
            return {
                "class": "scored_infeasible",
                "objective": objective,
                "gap": None,
                "near": False,
                "rows": listed,
            }
        if rows:
            shown = min(rows, key=lambda row: (row["source"], row["id"]))
            return {
                "class": "unscorable",
                "objective": shown["objective"],
                "gap": None,
                "near": False,
                "rows": listed,
            }
        return {
            "class": "never_evaluated",
            "objective": None,
            "gap": None,
            "near": False,
            "rows": [],
        }


def ratio_of(numerator: int, denominator: int):
    """{numerator, denominator, value} with the value rounded once from the exact fraction, or None
    when the denominator is zero (the contract reports null)."""
    if denominator == 0:
        return None
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": float(Fraction(numerator, denominator)),
    }


def not_near_counts(profile: dict) -> dict:
    """Per dimension: the scored neighbours that are not near the best, counted as the scored ones
    minus the near ones (near is only ever true for a scored ok neighbour, so the near ones are
    among the scored ones; infeasible neighbours are scored and never near)."""
    return {
        name: sum(1 for cell in cells if cell["class"] in SCORED_CLASSES)
        - sum(1 for cell in cells if cell["near"])
        for name, cells in profile.items()
    }


def rank_pairs(counts: dict, pair_k: int) -> list:
    """The best ``pair_k`` pairs as (first, second, score): highest score first, ties by ascending
    byte-wise names. Found by repeated selection of the best remaining pair, not by sorting."""
    names = sorted(counts, key=lambda name: name.encode("utf-8"))
    remaining = [
        (counts[first] + counts[second], first, second)
        for index, first in enumerate(names)
        for second in names[index + 1 :]
    ]

    def better(left: tuple, right: tuple) -> bool:
        if left[0] != right[0]:
            return left[0] > right[0]
        return (left[1].encode("utf-8"), left[2].encode("utf-8")) < (
            right[1].encode("utf-8"),
            right[2].encode("utf-8"),
        )

    chosen: list = []
    while remaining and len(chosen) < pair_k:
        best = remaining[0]
        for candidate in remaining[1:]:
            if better(candidate, best):
                best = candidate
        remaining.remove(best)
        chosen.append((best[1], best[2], best[0]))
    return chosen


def pair_map(
    study: Study, parted: list, values: dict, profile: dict, first: str, second: str, score: int
) -> dict:
    """The map of one pair: the product of the two spans (the centre's value and its neighbours,
    in the dimension's own order, which is the order of the typed tag), each cell read off the
    partition of the rows, a cell's role taken from its distance to the centre (0 centre, 1 axis,
    2 diagonal), and the diagonal totals counted over the finished cell list."""
    spans = []
    for name in (first, second):
        domain = study.domains[name]
        reached = [values[name]] + [cell["value"] for cell in profile[name]]
        spans.append(sorted(reached, key=lambda value, domain=domain: tag(domain, value)))
    centre = (tag(study.domains[first], values[first]), tag(study.domains[second], values[second]))
    cells: list = []
    for left, right in itertools.product(*spans):
        here = (tag(study.domains[first], left), tag(study.domains[second], right))
        moved = sum(1 for now, then in zip(here, centre) if now != then)
        cell = study.classify(study.rows_at(parted, {first: here[0], second: here[1]}))
        cells.append({"values": {first: left, second: right}, "role": ROLES[moved], **cell})
    diagonal = [cell for cell in cells if cell["role"] == "diagonal"]
    return {
        "dimensions": [first, second],
        "score": score,
        "cells": cells,
        "diagonal_total": len(diagonal),
        "diagonal_evaluated": sum(1 for cell in diagonal if cell["class"] != "never_evaluated"),
        "diagonal_scored": sum(1 for cell in diagonal if cell["class"] in SCORED_CLASSES),
        "diagonal_near": sum(1 for cell in diagonal if cell["near"]),
    }


def analyse(
    sources: list,
    centres: list,
    band: dict,
    fraction=None,
    ratio=None,
    *,
    pair_k: int,
    set_aside: list | None = None,
) -> dict:
    """The neighbourhood document of the pin (N1-N16) for parsed results, by the oracle's routes.
    ``pair_k`` is required and keyword-only, as it is in the module (pin N12): no call here may
    leave it out and take 0, so each one states the value it compares.
    Every cell is read off a partition of the rows by typed-tuple distance from the centre and
    classified by the contract's table; the counts, the share near the best and the coverage are
    recomputed from the finished cells' own values (the near flags are tested again with the
    same band arithmetic, a consistency check of the oracle); a never-evaluated vector is one
    whose typed key is in no row, which is checked against the cell's class.

    A continuous or log real dimension whose moves are not exactly binary64 numbers is declined
    (``InexactOracle``). Without ``set_aside`` the decline ends the call. With a list it sets aside
    only that centre and dimension: ``(source, trial_id, dimension, reason)`` is appended to the
    list, and ``DECLINED`` stands in the document for every figure that cannot be stated without
    the dimension's cells: its profile entry; the counts, dropped, share_near_best and coverage
    of that centre; its pair maps when ``pair_k`` asks for any (the pairs are ranked by every
    dimension's count); and the document's missing_vectors, lookups and work. Everything else is
    computed as it is without a decline."""
    study = Study(sources)
    study.bind(band)
    by_id = {(row["source"], row["id"]): row for row in study.rows}
    chosen = sorted(
        (by_id[tuple(centre)] for centre in centres), key=lambda r: (r["source"], r["id"])
    )
    evaluated = {row["key"] for row in study.rows}
    results: list = []
    missing: dict = {}
    axis_lookups = pair_lookups = 0
    any_declined = False

    def note(vector: dict, klass: str) -> None:
        unseen = study.key(vector) not in evaluated
        if unseen != (klass == "never_evaluated"):
            raise AssertionError(
                "oracle accounting error: the partition and the key set disagree about a cell"
            )
        if unseen:
            order = tuple(tag(study.domains[name], vector[name]) for name in study.names)
            missing[order] = {name: vector[name] for name in study.names}

    for row in chosen:
        values = row["values"]
        parted = study.partition(row["key"])
        profile: dict = {}
        bounds_total = same_total = 0
        declined: list = []
        for name in study.varying:
            try:
                kept, bounds, same_count = neighbours(
                    study.domains[name], values[name], fraction, ratio
                )
            except InexactOracle as error:
                if set_aside is None:
                    raise
                set_aside.append((row["source"], row["id"], name, str(error)))
                declined.append(name)
                profile[name] = DECLINED
                continue
            bounds_total += bounds
            same_total += same_count
            profile[name] = [
                {
                    "value": value,
                    **study.classify(
                        study.rows_at(parted, {name: tag(study.domains[name], value)})
                    ),
                }
                for value in kept
            ]
            axis_lookups += len(kept)
            for cell in profile[name]:
                note({**values, name: cell["value"]}, cell["class"])
        gap = study.gap(row["objective"])
        if declined:
            any_declined = True
            results.append(
                {
                    "source": row["source"],
                    "trial_id": row["id"],
                    "objective": row["objective"],
                    "centre_gap": gap,
                    "centre_near": gap <= study.delta,
                    "counts": DECLINED,
                    "dropped": DECLINED,
                    "share_near_best": DECLINED,
                    "coverage": DECLINED,
                    "profile": profile,
                    "pair_maps": DECLINED if pair_k else [],
                }
            )
            continue
        maps: list = []
        if pair_k:
            for first, second, score in rank_pairs(not_near_counts(profile), pair_k):
                one = pair_map(study, parted, values, profile, first, second, score)
                maps.append(one)
                pair_lookups += len(one["cells"]) - 1
                for cell in one["cells"]:
                    note({**values, **cell["values"]}, cell["class"])
        cells = [cell for name in study.varying for cell in profile[name]]
        by_class = {klass: [cell for cell in cells if cell["class"] == klass] for klass in CLASSES}
        # The band test again, with the arithmetic that ``classify`` used: a check of the oracle
        # against itself (the flags and the counts agree), not an independent reading of the band.
        near_cells = [
            cell for cell in by_class["scored_ok"] if study.gap(cell["objective"]) <= study.delta
        ]
        if len(near_cells) != sum(1 for cell in cells if cell["near"]):
            raise AssertionError(
                "oracle accounting error: the near flags differ from a fresh band test"
            )
        scored = by_class["scored_ok"] + by_class["scored_infeasible"]
        counts = {
            "geometric": len(cells),
            **{klass: len(listed) for klass, listed in by_class.items()},
        }
        counts["near"] = len(near_cells)
        counts["better_neighbours"] = sum(
            1 for cell in by_class["scored_ok"] if study.better(cell["objective"], row["objective"])
        )
        results.append(
            {
                "source": row["source"],
                "trial_id": row["id"],
                "objective": row["objective"],
                "centre_gap": gap,
                "centre_near": gap <= study.delta,
                "counts": counts,
                "dropped": {"bounds": bounds_total, "same_as_centre": same_total},
                "share_near_best": ratio_of(len(near_cells), len(scored)),
                "coverage": ratio_of(len(scored) + len(by_class["unscorable"]), len(cells)),
                "profile": profile,
                "pair_maps": maps,
            }
        )
    lookups = axis_lookups + pair_lookups
    return {
        "direction": study.direction,
        "reference": {
            "objective": study.reference,
            "source": study.reference_id[0],
            "trial_id": study.reference_id[1],
        },
        "band": {"mode": band["mode"], "value": float(band["value"]), "delta": study.delta},
        "rules": {
            "fraction": fraction,
            "ratio": ratio,
            "pair_k": pair_k,
            "by_dimension": {name: RULE_LABELS[study.domains[name].rule] for name in study.varying},
        },
        "sources": study.sources,
        "duplicate_rows": study.duplicate_rows,
        "constant_dimensions": list(study.constants),
        "missing_vectors": (
            DECLINED if any_declined else [missing[order] for order in sorted(missing)]
        ),
        "admission": {
            "rows": len(study.rows),
            "lookups": DECLINED if any_declined else lookups,
            "work": DECLINED if any_declined else len(study.rows) + lookups,
            "max_work": None,
        },
        "centres": results,
    }


def oracle_top_ids(sources: list, k: int) -> list:
    """The (source, trial) of the k best distinct vectors: best objective first, lower identity
    first."""
    study = Study(sources)
    ok = [row for row in study.rows if row["status"] == "ok"]
    sign = -1.0 if study.direction == "maximize" else 1.0
    ok.sort(key=lambda row: (sign * row["objective"], row["source"], row["id"]))
    seen: set = set()
    chosen: list = []
    for row in ok:
        if row["key"] in seen:
            continue
        seen.add(row["key"])
        chosen.append((row["source"], row["id"]))
        if len(chosen) == k:
            break
    return chosen


def duplicate_conflict_ids(sources: list) -> list:
    """Rows of one vector that were scored (ok or constraint violation) but disagree in status or
    objective; every scored row of such a vector is named. A vector conflicts when the set of the
    (status, objective) pairs of its scored rows holds more than one element."""
    study = Study(sources)
    named: list = []
    for key in {row["key"] for row in study.rows}:
        scored = [
            row
            for row in study.rows
            if row["key"] == key and STATUS_CLASS[row["status"]] in SCORED_CLASSES
        ]
        if len({(row["status"], row["objective"]) for row in scored}) > 1:
            named.extend((row["source"], row["id"]) for row in scored)
    return sorted(named)


def centre_offenders(sources: list, centres: list) -> list:
    """Centres that are absent, not a row with status ok, or listed again."""
    study = Study(sources)
    present = {(row["source"], row["id"]): row for row in study.rows}
    seen: set = set()
    named: list = []
    for centre in centres:
        centre = tuple(centre)
        if centre in seen:
            named.append(centre)
            continue
        seen.add(centre)
        row = present.get(centre)
        if row is None or row["status"] != "ok":
            named.append(centre)
    return sorted(named)


def finite_values(domain: Domain) -> list:
    if domain.kind == "integer":
        return list(range(domain.low, domain.high + 1, domain.step))
    if domain.kind == "real":
        return list(domain.lattice)
    if domain.kind == "boolean":
        return [False, True]
    return list(domain.choices)


def one_move_from_product(specs: dict, centre: dict) -> set:
    """Every vector of the full finite product that differs from the centre in exactly one dimension
    (and, for a lattice, by one ordinal), as typed tuples in byte-wise name order."""
    names = sorted(specs, key=lambda name: name.encode("utf-8"))
    domains = {name: Domain(name, specs[name]) for name in names}
    legal = {name: finite_values(domains[name]) for name in names}
    found: set = set()
    for combo in itertools.product(*(legal[name] for name in names)):
        moved = [
            name
            for name, value in zip(names, combo)
            if tag(domains[name], value) != tag(domains[name], centre[name])
        ]
        if len(moved) != 1:
            continue
        name = moved[0]
        domain = domains[name]
        if domain.kind in ("integer", "real"):
            positions = [tag(domain, value) for value in legal[name]]
            here = positions.index(tag(domain, centre[name]))
            there = positions.index(tag(domain, combo[names.index(name)]))
            if abs(here - there) != 1:
                continue
        found.add(tuple(typed_tag(domains[n], v) for n, v in zip(names, combo)))
    return found


def typed_tag(domain: Domain, value) -> tuple:
    """The tag of a value with its Python type name, so that 4 and 4.0 are different vectors."""
    return tag(domain, value) + (type(value).__name__,)


def vectors_as_tags(specs: dict, vectors: list) -> set:
    names = sorted(specs, key=lambda name: name.encode("utf-8"))
    domains = {name: Domain(name, specs[name]) for name in names}
    return {tuple(typed_tag(domains[name], vector[name]) for name in names) for vector in vectors}


# ---------------------------------------------------------------------------------------------
# Values the test files pin. Each was re-derived by hand from the fixture rows by the author of
# this oracle (not copied unchecked); ``source`` names the test that pins it.
# ---------------------------------------------------------------------------------------------


def cell(value, klass, objective, gap, near, rows) -> dict:
    return {
        "value": value,
        "class": klass,
        "objective": objective,
        "gap": gap,
        "near": near,
        "rows": rows,
    }


def at(*path):
    def getter(result):
        value = result
        for step in path:
            value = value[step]
        return value

    return getter


def centre_at(*path):
    return at("centres", 0, *path)


def project(pair: dict) -> list:
    first, second = pair["dimensions"]
    return [
        (
            item["values"][first],
            item["values"][second],
            item["role"],
            item["class"],
            item["objective"],
            item["gap"],
            item["near"],
            item["rows"],
        )
        for item in pair["cells"]
    ]


def diagonal_totals(pair: dict) -> dict:
    return {
        key: pair[key]
        for key in ("diagonal_total", "diagonal_evaluated", "diagonal_scored", "diagonal_near")
    }


def fixture(name: str) -> dict:
    folder = SENSITIVITY_DIR if name.startswith("nf5") else NEIGHBOURHOOD_DIR
    return json.loads((folder / name).read_text(encoding="utf-8"))


def row_of(trial_id: int, parameters: dict, objective: float = 1.0, status: str = "ok") -> dict:
    return {
        "trial_id": trial_id,
        "status": status,
        "feasible": status == "ok",
        "objective": objective,
        "parameters": parameters,
    }


NF1_COUNTS = {
    "geometric": 5,
    "scored_ok": 2,
    "scored_infeasible": 1,
    "unscorable": 1,
    "never_evaluated": 1,
    "near": 1,
    "better_neighbours": 0,
}
NF1_PROFILE = {
    "Fast": [
        cell(2, "scored_ok", 9.5, 1.0, True, [[0, 1]]),
        cell(6, "scored_ok", 7.0, 3.5, False, [[0, 2]]),
    ],
    "Flag": [cell(True, "never_evaluated", None, None, False, [])],
    "Mode": [
        cell("sma", "scored_infeasible", 12.0, None, False, [[0, 3]]),
        cell("wma", "unscorable", None, None, False, [[0, 4]]),
    ],
}
NF1_SHARE = {"numerator": 1, "denominator": 3, "value": 0.3333333333333333}
NF1_MISSING = [{"Fast": 4, "Flag": True, "Mode": "ema"}]
NF2_PROFILE_ONE = {
    "Lr": [cell(4.0, "never_evaluated", None, None, False, [])],
    "Mult": [cell(1.5, "scored_ok", 4.0, 1.0, False, [[0, 1]])],
    "Thr": [cell(0.625, "scored_ok", 5.0, 0.0, True, [[0, 2]])],
}
NF3_PROFILE = {
    "Fast": [
        cell(2, "scored_ok", 5.0, 2.0, False, [[0, 1]]),
        cell(6, "scored_ok", 6.5, 0.5, True, [[1, 0]]),
    ],
    "Mode": [cell("sma", "unscorable", None, None, False, [[1, 1]])],
}
NF3_COUNTS = {
    "geometric": 3,
    "scored_ok": 2,
    "scored_infeasible": 0,
    "unscorable": 1,
    "never_evaluated": 0,
    "near": 1,
    "better_neighbours": 1,
}
NEVER = ("never_evaluated", None, None, False, [])
NF5_CELLS = [
    (2, 10, "diagonal", "scored_ok", 8.0, 0.0, True, [[0, 5]]),
    (2, 20, "axis", "scored_ok", 7.0, 1.0, True, [[0, 1]]),
    (2, 30, "diagonal", *NEVER),
    (4, 10, "axis", "scored_ok", 7.5, 0.5, True, [[0, 3]]),
    (4, 20, "centre", "scored_ok", 8.0, 0.0, True, [[0, 0]]),
    (4, 30, "axis", "scored_ok", 4.0, 4.0, False, [[0, 4]]),
    (6, 10, "diagonal", "unscorable", None, None, False, [[0, 7]]),
    (6, 20, "axis", "scored_ok", 6.0, 2.0, False, [[0, 2]]),
    (6, 30, "diagonal", "scored_infeasible", 9.0, None, False, [[0, 6]]),
]
NF5B_MAPS = [
    [
        (2, 10, "diagonal", *NEVER),
        (2, 20, "axis", "scored_ok", 7.0, 1.0, True, [[0, 1]]),
        (2, 30, "diagonal", *NEVER),
        (4, 10, "axis", "scored_ok", 7.5, 0.5, True, [[0, 3]]),
        (4, 20, "centre", "scored_ok", 8.0, 0.0, True, [[0, 0]]),
        (4, 30, "axis", "scored_ok", 4.0, 4.0, False, [[0, 4]]),
        (6, 10, "diagonal", *NEVER),
        (6, 20, "axis", "scored_ok", 6.0, 2.0, False, [[0, 2]]),
        (6, 30, "diagonal", *NEVER),
    ],
    [
        (2, "ema", "axis", "scored_ok", 7.0, 1.0, True, [[0, 1]]),
        (2, "sma", "diagonal", *NEVER),
        (4, "ema", "centre", "scored_ok", 8.0, 0.0, True, [[0, 0]]),
        (4, "sma", "axis", "scored_ok", 7.5, 0.5, True, [[0, 5]]),
        (6, "ema", "axis", "scored_ok", 6.0, 2.0, False, [[0, 2]]),
        (6, "sma", "diagonal", *NEVER),
    ],
    [
        ("ema", 10, "axis", "scored_ok", 7.5, 0.5, True, [[0, 3]]),
        ("ema", 20, "centre", "scored_ok", 8.0, 0.0, True, [[0, 0]]),
        ("ema", 30, "axis", "scored_ok", 4.0, 4.0, False, [[0, 4]]),
        ("sma", 10, "diagonal", *NEVER),
        ("sma", 20, "axis", "scored_ok", 7.5, 0.5, True, [[0, 5]]),
        ("sma", 30, "diagonal", *NEVER),
    ],
]
NF5B_MISSING = [
    {"Fast": 2, "Mode": "ema", "Slow": 10},
    {"Fast": 2, "Mode": "ema", "Slow": 30},
    {"Fast": 2, "Mode": "sma", "Slow": 20},
    {"Fast": 4, "Mode": "sma", "Slow": 10},
    {"Fast": 4, "Mode": "sma", "Slow": 30},
    {"Fast": 6, "Mode": "ema", "Slow": 10},
    {"Fast": 6, "Mode": "ema", "Slow": 30},
    {"Fast": 6, "Mode": "sma", "Slow": 20},
]
NF5B_PAIR_NAMES = [["Fast", "Slow"], ["Fast", "Mode"], ["Mode", "Slow"]]

# NF7 (finding P1-1): the cases no fixture of the module's tests shows, derived by hand in this
# file from the contract (clauses 4 and 6, N9, N11) and not from the module. Space as NF1, band
# absolute 1.0, centre 0:0 = (Fast 4, Flag false, Mode ema) at 10.0, reference 10.5 at 0:10.
# Neighbours of the centre:
#   Fast 6   rows 0:2 pruned (recorded prefix objective 14.0, better than the best) and 0:3
#            trial_timeout (null): only unscorable rows, so the class is unscorable and the
#            vector is shown by its lowest-identity row: objective 14.0, rows [0:2, 0:3]
#   Flag true rows 0:4 engine_error (null) and 0:5 pruned (7.0): unscorable, shown by 0:4:
#            objective null
#   Fast 2   row 0:1 ok 9.5: gap 10.5 - 9.5 = 1.0, near (the test is inclusive)
#   Mode sma rows 0:6 engine_error (lower identity) and 0:7 ok 8.0: the scored row speaks, gap
#            2.5, not near, both rows listed
#   Mode wma rows 0:8 constraint_violation 12.0 and 0:9 engine_error (higher identity): scored
#            infeasible, objective 12.0, gap null, both rows listed
# Counts: 5 neighbours; scored ok 2, infeasible 1, unscorable 2, never evaluated 0; near 1;
# better than the centre's 10.0: none. Share near the best 1 of 3 scored; coverage (3 scored + 2
# unscorable) of 5. Four vectors hold two rows, so 4 duplicate rows; 11 rows, 5 lookups, work 16;
# the centre's gap is 0.5.
NF7_SPACE = {
    "parameters": {
        "Fast": {"kind": "integer", "low": 2, "high": 10, "step": 2, "log": False},
        "Flag": {"kind": "boolean"},
        "Mode": {"kind": "categorical", "choices": ["ema", "sma", "wma"]},
    },
    "objective": OBJECTIVE,
}
NF7_PROFILE = {
    "Fast": [
        cell(2, "scored_ok", 9.5, 1.0, True, [[0, 1]]),
        cell(6, "unscorable", 14.0, None, False, [[0, 2], [0, 3]]),
    ],
    "Flag": [cell(True, "unscorable", None, None, False, [[0, 4], [0, 5]])],
    "Mode": [
        cell("sma", "scored_ok", 8.0, 2.5, False, [[0, 6], [0, 7]]),
        cell("wma", "scored_infeasible", 12.0, None, False, [[0, 8], [0, 9]]),
    ],
}
NF7_COUNTS = {
    "geometric": 5,
    "scored_ok": 2,
    "scored_infeasible": 1,
    "unscorable": 2,
    "never_evaluated": 0,
    "near": 1,
    "better_neighbours": 0,
}


def nf7_source() -> list:
    """The rows of NF7 as one parsed result."""

    def vector(fast, flag, mode) -> dict:
        return {"Fast": fast, "Flag": flag, "Mode": mode}

    rows = [
        row_of(0, vector(4, False, "ema"), 10.0),
        row_of(1, vector(2, False, "ema"), 9.5),
        row_of(2, vector(6, False, "ema"), 14.0, "pruned"),
        row_of(3, vector(6, False, "ema"), None, "trial_timeout"),
        row_of(4, vector(4, True, "ema"), None, "engine_error"),
        row_of(5, vector(4, True, "ema"), 7.0, "pruned"),
        row_of(6, vector(4, False, "sma"), None, "engine_error"),
        row_of(7, vector(4, False, "sma"), 8.0),
        row_of(8, vector(4, False, "wma"), 12.0, "constraint_violation"),
        row_of(9, vector(4, False, "wma"), None, "engine_error"),
        row_of(10, vector(8, True, "sma"), 10.5),
    ]
    return [{"space": copy.deepcopy(NF7_SPACE), "trials": rows}]


def nf7_cv_then_ok() -> list:
    """NF7 with row 0:9 an ok row at 12.0: on the vector of 0:8 (constraint violation, 12.0) the
    scored rows now differ in status, the lower identity being the constraint violation."""
    sources = nf7_source()
    sources[0]["trials"][9].update(status="ok", feasible=True, objective=12.0)
    return sources


def nf7_ok_then_cv() -> list:
    """NF7 with row 0:6 an ok row and row 0:7 a constraint violation, both at 8.0: on that vector
    the scored rows now differ in status, the lower identity being the ok row."""
    sources = nf7_source()
    sources[0]["trials"][6].update(status="ok", feasible=True, objective=8.0)
    sources[0]["trials"][7].update(status="constraint_violation", feasible=False, objective=8.0)
    return sources


# (label, builder, the scored rows the contract names: every scored row of the conflicting vector)
NF7_CONFLICTS = [
    (
        "NF7 with a constraint violation 0:8 then an ok row 0:9 on one vector",
        nf7_cv_then_ok,
        [(0, 8), (0, 9)],
    ),
    (
        "NF7 with an ok row 0:6 then a constraint violation 0:7 on one vector",
        nf7_ok_then_cv,
        [(0, 6), (0, 7)],
    ),
]

WHOLE_SPACE = {
    "parameters": {
        "A": {"kind": "integer", "low": 2, "high": 10, "step": 2, "log": False},
        "B": {"kind": "integer", "low": 1, "high": 1000, "step": 1, "log": True},
        "C": {"kind": "real", "low": 0.5, "high": 2.0, "step": 0.5, "log": False},
        "D": {"kind": "real", "low": 0.0, "high": 1.0, "step": None, "log": False},
        "E": {"kind": "real", "low": 0.125, "high": 8.0, "step": None, "log": True},
        "F": {"kind": "boolean"},
        "G": {"kind": "categorical", "choices": ["ema", "sma", "wma"]},
        "H": {"kind": "integer", "low": 5, "high": 5, "step": 1, "log": False},
        "I": {"kind": "categorical", "choices": ["only"]},
    },
    "objective": OBJECTIVE,
}
WHOLE_CENTRE = {
    "A": 6,
    "B": 7,
    "C": 1.0,
    "D": 0.5,
    "E": 1.0,
    "F": False,
    "G": "sma",
    "H": 5,
    "I": "only",
}


def profile_values(name: str):
    return lambda result: [item["value"] for item in result["centres"][0]["profile"][name]]


def build_cases() -> list:
    """Every worked fixture run: (name, source, build sources, centres, band, params, expects)."""
    cases: list = []

    def add(name, source, build, centres, band, expects, **params):
        cases.append(
            {
                "name": name,
                "source": source,
                "build": build,
                "centres": centres,
                "band": band,
                "params": params,
                "expects": expects,
            }
        )

    nf1 = lambda: [fixture("nf1_result.json")]  # noqa: E731
    add(
        "NF1 absolute band 1.0",
        "test_neighbourhood.py NF1Tests.test_absolute_band",
        nf1,
        [(0, 0)],
        ABSOLUTE_1,
        [
            ("direction", at("direction"), "maximize"),
            ("reference", at("reference"), {"objective": 10.5, "source": 0, "trial_id": 5}),
            ("band", at("band"), {"mode": "absolute", "value": 1.0, "delta": 1.0}),
            (
                "rule by dimension",
                at("rules", "by_dimension"),
                {"Fast": "step", "Flag": "other_value", "Mode": "other_choices"},
            ),
            ("sources", at("sources"), [{"index": 0, "rows": 6, "space_hash": None}]),
            ("duplicate_rows", at("duplicate_rows"), 0),
            ("constant_dimensions", at("constant_dimensions"), []),
            ("missing_vectors", at("missing_vectors"), NF1_MISSING),
            ("admission", at("admission"), {"rows": 6, "lookups": 5, "work": 11, "max_work": None}),
            ("centre objective", centre_at("objective"), 10.0),
            ("centre_gap", centre_at("centre_gap"), 0.5),
            ("centre_near", centre_at("centre_near"), True),
            ("counts", centre_at("counts"), NF1_COUNTS),
            ("dropped", centre_at("dropped"), {"bounds": 0, "same_as_centre": 0}),
            ("share_near_best", centre_at("share_near_best"), NF1_SHARE),
            ("coverage", centre_at("coverage"), {"numerator": 4, "denominator": 5, "value": 0.8}),
            ("profile", centre_at("profile"), NF1_PROFILE),
            ("pair_maps", centre_at("pair_maps"), []),
        ],
        pair_k=0,
    )
    add(
        "NF1 relative band 0.125",
        "test_neighbourhood.py NF1Tests.test_relative_band_gives_the_same_classes",
        nf1,
        [(0, 0)],
        {"mode": "relative", "value": 0.125},
        [
            ("band", at("band"), {"mode": "relative", "value": 0.125, "delta": 1.3125}),
            ("profile", centre_at("profile"), NF1_PROFILE),
            ("share_near_best", centre_at("share_near_best"), NF1_SHARE),
        ],
        pair_k=0,
    )
    add(
        "NF1 band 4.0",
        "test_neighbourhood.py NF1Tests.test_wide_band_counts_the_far_neighbour",
        nf1,
        [(0, 0)],
        {"mode": "absolute", "value": 4.0},
        [
            ("near", centre_at("counts", "near"), 2),
            (
                "share_near_best",
                centre_at("share_near_best"),
                {"numerator": 2, "denominator": 3, "value": 0.6666666666666666},
            ),
        ],
        pair_k=0,
    )
    add(
        "NF1 band 0",
        "test_neighbourhood.py NF1Tests.test_band_value_zero_keeps_only_the_best",
        nf1,
        [(0, 0)],
        {"mode": "absolute", "value": 0},
        [
            ("near", centre_at("counts", "near"), 0),
            ("centre_near", centre_at("centre_near"), False),
        ],
        pair_k=0,
    )
    add(
        "NF1 read twice",
        "test_neighbourhood.py NF1Tests.test_same_file_twice_is_two_sources_with_duplicates",
        lambda: [fixture("nf1_result.json"), fixture("nf1_result.json")],
        [(0, 0)],
        ABSOLUTE_1,
        [
            ("duplicate_rows", at("duplicate_rows"), 6),
            ("reference", at("reference"), {"objective": 10.5, "source": 0, "trial_id": 5}),
            (
                "sources",
                at("sources"),
                [
                    {"index": 0, "rows": 6, "space_hash": None},
                    {"index": 1, "rows": 6, "space_hash": None},
                ],
            ),
            ("counts", centre_at("counts"), NF1_COUNTS),
            (
                "Fast 2 cell",
                centre_at("profile", "Fast", 0),
                cell(2, "scored_ok", 9.5, 1.0, True, [[0, 1], [1, 1]]),
            ),
            ("Mode wma rows", centre_at("profile", "Mode", 1, "rows"), [[0, 4], [1, 4]]),
            ("missing_vectors", at("missing_vectors"), NF1_MISSING),
        ],
        pair_k=0,
    )
    nf2_params = {"fraction": 0.25, "ratio": 2.0}
    band_half = {"mode": "absolute", "value": 0.5}
    add(
        "NF2 one source",
        "test_neighbourhood.py NF2Tests.test_one_source",
        lambda: [fixture("nf2_source0.json")],
        [(0, 0)],
        band_half,
        [
            ("reference", at("reference"), {"objective": 5.0, "source": 0, "trial_id": 0}),
            (
                "rule by dimension",
                at("rules", "by_dimension"),
                {"Lr": "ratio", "Mult": "ordinal", "Thr": "fraction"},
            ),
            ("dropped", centre_at("dropped"), {"bounds": 3, "same_as_centre": 0}),
            ("centre_gap", centre_at("centre_gap"), 0.0),
            ("centre_near", centre_at("centre_near"), True),
            ("profile", centre_at("profile"), NF2_PROFILE_ONE),
            (
                "counts",
                centre_at("counts"),
                {
                    "geometric": 3,
                    "scored_ok": 2,
                    "scored_infeasible": 0,
                    "unscorable": 0,
                    "never_evaluated": 1,
                    "near": 1,
                    "better_neighbours": 0,
                },
            ),
            (
                "share_near_best",
                centre_at("share_near_best"),
                {"numerator": 1, "denominator": 2, "value": 0.5},
            ),
            (
                "coverage",
                centre_at("coverage"),
                {"numerator": 2, "denominator": 3, "value": 0.6666666666666666},
            ),
            ("missing_vectors", at("missing_vectors"), [{"Lr": 4.0, "Mult": 2.0, "Thr": 0.875}]),
        ],
        pair_k=0,
        **nf2_params,
    )
    add(
        "NF2 two sources",
        "test_neighbourhood.py NF2Tests.test_second_source_changes_the_reference_and_fills_the_gap",
        lambda: [fixture("nf2_source0.json"), fixture("nf2_source1.json")],
        [(0, 0)],
        band_half,
        [
            ("reference", at("reference"), {"objective": 5.5, "source": 1, "trial_id": 0}),
            ("centre_gap", centre_at("centre_gap"), 0.5),
            ("centre_near", centre_at("centre_near"), True),
            (
                "profile",
                centre_at("profile"),
                {
                    "Lr": [cell(4.0, "scored_ok", 5.5, 0.0, True, [[1, 0]])],
                    "Mult": [cell(1.5, "scored_ok", 4.0, 1.5, False, [[0, 1]])],
                    "Thr": [cell(0.625, "scored_ok", 5.0, 0.5, True, [[0, 2]])],
                },
            ),
            (
                "counts",
                centre_at("counts"),
                {
                    "geometric": 3,
                    "scored_ok": 3,
                    "scored_infeasible": 0,
                    "unscorable": 0,
                    "never_evaluated": 0,
                    "near": 2,
                    "better_neighbours": 1,
                },
            ),
            (
                "share_near_best",
                centre_at("share_near_best"),
                {"numerator": 2, "denominator": 3, "value": 0.6666666666666666},
            ),
            ("coverage", centre_at("coverage"), {"numerator": 3, "denominator": 3, "value": 1.0}),
            ("missing_vectors", at("missing_vectors"), []),
            (
                "sources",
                at("sources"),
                [
                    {"index": 0, "rows": 4, "space_hash": None},
                    {"index": 1, "rows": 1, "space_hash": None},
                ],
            ),
        ],
        pair_k=0,
        **nf2_params,
    )
    add(
        "NF3 duplicates and colliding ids",
        "test_neighbourhood.py NF3Tests.test_base",
        lambda: [fixture("nf3_source0.json"), fixture("nf3_source1.json")],
        [(0, 0)],
        ABSOLUTE_1,
        [
            ("reference", at("reference"), {"objective": 7.0, "source": 0, "trial_id": 3}),
            ("duplicate_rows", at("duplicate_rows"), 1),
            ("centre_gap", centre_at("centre_gap"), 1.0),
            ("centre_near", centre_at("centre_near"), True),
            ("profile", centre_at("profile"), NF3_PROFILE),
            ("counts", centre_at("counts"), NF3_COUNTS),
            (
                "share_near_best",
                centre_at("share_near_best"),
                {"numerator": 1, "denominator": 2, "value": 0.5},
            ),
            ("coverage", centre_at("coverage"), {"numerator": 3, "denominator": 3, "value": 1.0}),
            ("missing_vectors", at("missing_vectors"), []),
            (
                "sources",
                at("sources"),
                [
                    {"index": 0, "rows": 4, "space_hash": None},
                    {"index": 1, "rows": 2, "space_hash": None},
                ],
            ),
        ],
        pair_k=0,
    )

    def nf3_unscorable_duplicate():
        sources = [fixture("nf3_source0.json"), fixture("nf3_source1.json")]
        sources[0]["trials"][2].update(status="engine_error", feasible=False, objective=None)
        return sources

    add(
        "NF3 with the duplicate row an engine error",
        "test_neighbourhood.py NF3Tests.test_unscorable_duplicate_is_ignored_for_the_outcome",
        nf3_unscorable_duplicate,
        [(0, 0)],
        ABSOLUTE_1,
        [
            ("duplicate_rows", at("duplicate_rows"), 1),
            ("profile", centre_at("profile"), NF3_PROFILE),
            ("counts", centre_at("counts"), NF3_COUNTS),
        ],
        pair_k=0,
    )
    add(
        "NF7 unscorable neighbours and scored vectors with unscorable duplicates",
        "derived by hand in this file (contract clauses 4 and 6, N9, N11)",
        nf7_source,
        [(0, 0)],
        ABSOLUTE_1,
        [
            ("reference", at("reference"), {"objective": 10.5, "source": 0, "trial_id": 10}),
            ("duplicate_rows", at("duplicate_rows"), 4),
            ("centre_gap", centre_at("centre_gap"), 0.5),
            ("centre_near", centre_at("centre_near"), True),
            ("profile", centre_at("profile"), NF7_PROFILE),
            ("counts", centre_at("counts"), NF7_COUNTS),
            ("dropped", centre_at("dropped"), {"bounds": 0, "same_as_centre": 0}),
            (
                "share_near_best",
                centre_at("share_near_best"),
                {"numerator": 1, "denominator": 3, "value": 0.3333333333333333},
            ),
            ("coverage", centre_at("coverage"), {"numerator": 5, "denominator": 5, "value": 1.0}),
            ("missing_vectors", at("missing_vectors"), []),
            (
                "admission",
                at("admission"),
                {"rows": 11, "lookups": 5, "work": 16, "max_work": None},
            ),
        ],
        pair_k=0,
    )
    add(
        "NF6 minimize",
        "test_neighbourhood.py NF6Tests.test_minimize",
        lambda: [fixture("nf6_result.json")],
        [(0, 0)],
        ABSOLUTE_1,
        [
            ("direction", at("direction"), "minimize"),
            ("reference", at("reference"), {"objective": 2.5, "source": 0, "trial_id": 1}),
            ("centre_gap", centre_at("centre_gap"), 0.5),
            ("centre_near", centre_at("centre_near"), True),
            (
                "profile",
                centre_at("profile"),
                {
                    "Fast": [
                        cell(2, "scored_ok", 2.5, 0.0, True, [[0, 1]]),
                        cell(6, "scored_ok", 5.0, 2.5, False, [[0, 2]]),
                    ]
                },
            ),
            ("better_neighbours", centre_at("counts", "better_neighbours"), 1),
            (
                "share_near_best",
                centre_at("share_near_best"),
                {"numerator": 1, "denominator": 2, "value": 0.5},
            ),
            ("coverage", centre_at("coverage"), {"numerator": 2, "denominator": 2, "value": 1.0}),
        ],
        pair_k=0,
    )
    add(
        "NF6 with pair_k 2 and one varying dimension",
        "test_neighbourhood.py NF6Tests.test_pair_k_with_one_varying_dimension_builds_no_map",
        lambda: [fixture("nf6_result.json")],
        [(0, 0)],
        ABSOLUTE_1,
        [
            ("pair_maps", centre_at("pair_maps"), []),
            ("admission lookups", at("admission", "lookups"), 2),
        ],
        pair_k=2,
    )
    nf5 = lambda: [fixture("nf5_result.json")]  # noqa: E731
    add(
        "NF5 axis part, pair_k 0",
        "test_neighbourhood.py CentreTests.test_nf5_axis_part_without_pair_maps",
        nf5,
        [(0, 0)],
        ABSOLUTE_1,
        [
            ("reference", at("reference"), {"objective": 8.0, "source": 0, "trial_id": 0}),
            (
                "profile",
                centre_at("profile"),
                {
                    "Fast": [
                        cell(2, "scored_ok", 7.0, 1.0, True, [[0, 1]]),
                        cell(6, "scored_ok", 6.0, 2.0, False, [[0, 2]]),
                    ],
                    "Slow": [
                        cell(10, "scored_ok", 7.5, 0.5, True, [[0, 3]]),
                        cell(30, "scored_ok", 4.0, 4.0, False, [[0, 4]]),
                    ],
                },
            ),
            (
                "share_near_best",
                centre_at("share_near_best"),
                {"numerator": 2, "denominator": 4, "value": 0.5},
            ),
            ("coverage", centre_at("coverage"), {"numerator": 4, "denominator": 4, "value": 1.0}),
            ("pair_maps", centre_at("pair_maps"), []),
            ("missing_vectors", at("missing_vectors"), []),
            ("admission", at("admission"), {"rows": 8, "lookups": 4, "work": 12, "max_work": None}),
        ],
        pair_k=0,
    )
    add(
        "NF5 two centres given in reverse order",
        "test_neighbourhood.py CentreTests.test_two_centres_from_top_ids",
        nf5,
        [(0, 5), (0, 0)],
        ABSOLUTE_1,
        [
            (
                "centre order, ascending identity",
                lambda result: [item["trial_id"] for item in result["centres"]],
                [0, 5],
            ),
            ("centre 0:5 gap", at("centres", 1, "centre_gap"), 0.0),
            ("centre 0:5 near", at("centres", 1, "centre_near"), True),
            ("centre 0:5 dropped", at("centres", 1, "dropped"), {"bounds": 2, "same_as_centre": 0}),
            (
                "centre 0:5 profile",
                at("centres", 1, "profile"),
                {
                    "Fast": [cell(4, "scored_ok", 7.5, 0.5, True, [[0, 3]])],
                    "Slow": [cell(20, "scored_ok", 7.0, 1.0, True, [[0, 1]])],
                },
            ),
            (
                "centre 0:5 share_near_best",
                at("centres", 1, "share_near_best"),
                {"numerator": 2, "denominator": 2, "value": 1.0},
            ),
            ("centre 0:5 better_neighbours", at("centres", 1, "counts", "better_neighbours"), 0),
        ],
        pair_k=0,
    )
    add(
        "NF5 pair_k 1",
        "test_sensitivity.py NF5Tests.test_pair_map_cells_and_diagonal_totals",
        nf5,
        [(0, 0)],
        ABSOLUTE_1,
        [
            ("pair dimensions", centre_at("pair_maps", 0, "dimensions"), ["Fast", "Slow"]),
            ("pair score", centre_at("pair_maps", 0, "score"), 2),
            ("pair cells", lambda result: project(result["centres"][0]["pair_maps"][0]), NF5_CELLS),
            (
                "diagonal totals",
                lambda result: diagonal_totals(result["centres"][0]["pair_maps"][0]),
                {
                    "diagonal_total": 4,
                    "diagonal_evaluated": 3,
                    "diagonal_scored": 2,
                    "diagonal_near": 1,
                },
            ),
            (
                "roles: centre, axis, diagonal cells",
                lambda result: [
                    sum(
                        1
                        for c in result["centres"][0]["pair_maps"][0]["cells"]
                        if c["role"] == role
                    )
                    for role in ROLES
                ],
                [1, 4, 4],
            ),
            ("missing_vectors", at("missing_vectors"), [{"Fast": 2, "Slow": 30}]),
            (
                "admission",
                at("admission"),
                {"rows": 8, "lookups": 12, "work": 20, "max_work": None},
            ),
            ("pair_k", at("rules", "pair_k"), 1),
        ],
        pair_k=1,
    )
    nf5b = lambda: [fixture("nf5b_result.json")]  # noqa: E731
    add(
        "NF5b pair_k 3",
        "test_sensitivity.py NF5bTests.test_the_three_maps_cell_by_cell, "
        "test_missing_vectors_collect_the_diagonals_in_vector_order, "
        "test_plan_and_maps_through_the_public_functions and "
        "test_admission_counts_the_pair_cells",
        nf5b,
        [(0, 0)],
        ABSOLUTE_1,
        [
            (
                "pair dimensions",
                lambda result: [m["dimensions"] for m in result["centres"][0]["pair_maps"]],
                NF5B_PAIR_NAMES,
            ),
            (
                "pair scores",
                lambda result: [m["score"] for m in result["centres"][0]["pair_maps"]],
                [2, 1, 1],
            ),
            (
                "cells of map 0",
                lambda result: project(result["centres"][0]["pair_maps"][0]),
                NF5B_MAPS[0],
            ),
            (
                "cells of map 1",
                lambda result: project(result["centres"][0]["pair_maps"][1]),
                NF5B_MAPS[1],
            ),
            (
                "cells of map 2",
                lambda result: project(result["centres"][0]["pair_maps"][2]),
                NF5B_MAPS[2],
            ),
            (
                "diagonal totals of the three maps",
                lambda result: [
                    tuple(diagonal_totals(m).values()) for m in result["centres"][0]["pair_maps"]
                ],
                [(4, 0, 0, 0), (2, 0, 0, 0), (2, 0, 0, 0)],
            ),
            ("missing_vectors", at("missing_vectors"), NF5B_MISSING),
            (
                "admission",
                at("admission"),
                {"rows": 6, "lookups": 23, "work": 29, "max_work": None},
            ),
        ],
        pair_k=3,
    )
    for pair_k, count in ((0, 0), (1, 1), (2, 2), (10, 3)):
        expects = [
            (
                "pair dimensions",
                lambda result: [m["dimensions"] for m in result["centres"][0]["pair_maps"]],
                NF5B_PAIR_NAMES[:count],
            )
        ]
        if count:
            expects.append(
                (
                    "pair scores",
                    lambda result: [m["score"] for m in result["centres"][0]["pair_maps"]],
                    [2, 1, 1][:count],
                )
            )
        add(
            f"NF5b pair_k {pair_k}",
            "test_sensitivity.py NF5bTests.test_ranking_for_pair_k_one_two_and_three",
            nf5b,
            [(0, 0)],
            ABSOLUTE_1,
            expects,
            pair_k=pair_k,
        )

    def mode_order_source():
        space = {
            "parameters": {
                "Fast": {"kind": "integer", "low": 2, "high": 6, "step": 2, "log": False},
                "Mode": {"kind": "categorical", "choices": ["wma", "ema", "sma"]},
            },
            "objective": OBJECTIVE,
        }
        return [{"space": space, "trials": [row_of(0, {"Fast": 4, "Mode": "ema"})]}]

    add(
        "categorical axis keeps the declared order (Mode declared wma, ema, sma)",
        "test_sensitivity.py AxisOrderTests.test_categorical_axis_keeps_the_declared_order",
        mode_order_source,
        [(0, 0)],
        ABSOLUTE_1,
        [
            ("pair dimensions", centre_at("pair_maps", 0, "dimensions"), ["Fast", "Mode"]),
            (
                "cell order",
                lambda result: [
                    (c["values"]["Fast"], c["values"]["Mode"])
                    for c in result["centres"][0]["pair_maps"][0]["cells"]
                ],
                [(f, m) for f in (2, 4, 6) for m in ("wma", "ema", "sma")],
            ),
            (
                "roles",
                lambda result: [c["role"] for c in result["centres"][0]["pair_maps"][0]["cells"]],
                [
                    "diagonal",
                    "axis",
                    "diagonal",
                    "axis",
                    "centre",
                    "axis",
                    "diagonal",
                    "axis",
                    "diagonal",
                ],
            ),
        ],
        pair_k=1,
    )

    def whole_source():
        return [{"space": copy.deepcopy(WHOLE_SPACE), "trials": [row_of(0, dict(WHOLE_CENTRE))]}]

    whole_values = (
        ("A", [4, 8]),
        ("B", [4, 14]),
        ("C", [0.5, 1.5]),
        ("D", [0.25, 0.75]),
        ("E", [0.5, 2.0]),
        ("F", [True]),
        ("G", ["ema", "wma"]),
    )
    whole = [
        (
            "profile dimensions",
            lambda result: list(result["centres"][0]["profile"]),
            [n for n, _ in whole_values],
        ),
    ]
    whole += [(f"values of {name}", profile_values(name), values) for name, values in whole_values]
    whole += [
        ("constant_dimensions", at("constant_dimensions"), ["H", "I"]),
        (
            "(geometric, never_evaluated)",
            lambda result: (
                result["centres"][0]["counts"]["geometric"],
                result["centres"][0]["counts"]["never_evaluated"],
            ),
            (13, 13),
        ),
        ("dropped", centre_at("dropped"), {"bounds": 0, "same_as_centre": 0}),
        ("share_near_best", centre_at("share_near_best"), None),
        ("coverage", centre_at("coverage"), {"numerator": 0, "denominator": 13, "value": 0.0}),
        ("number of missing vectors", lambda result: len(result["missing_vectors"]), 13),
        (
            "first missing vector",
            lambda result: result["missing_vectors"][0],
            dict(WHOLE_CENTRE, A=4),
        ),
        (
            "last missing vector",
            lambda result: result["missing_vectors"][-1],
            dict(WHOLE_CENTRE, A=8),
        ),
    ]
    add(
        "generation table through the whole analysis (nine dimensions)",
        "test_neighbourhood.py NF4Tests.test_whole_table_through_the_public_function",
        whole_source,
        [(0, 0)],
        ABSOLUTE_1,
        whole,
        fraction=0.25,
        ratio=2.0,
        pair_k=0,
    )
    return cases


def spec(kind: str, **fields) -> dict:
    result = {"kind": kind}
    result.update(fields)
    return result


def lattice_spec(low, high, step, log=False) -> dict:
    return spec("integer", low=low, high=high, step=step, log=log)


def real_spec(low, high, step=None, log=False) -> dict:
    return spec("real", low=low, high=high, step=step, log=log)


# (label, spec, centre value, expected, source); expected keys: values, bounds, same, constant,
# lattice
GENERATION_TABLE = [
    (
        "integer 2..10 step 2 at 2",
        lattice_spec(2, 10, 2),
        2,
        {"values": (4,), "bounds": 1, "same": 0},
        "test_neighbourhood.py NF4Tests.test_integer_step_two",
    ),
    (
        "integer 2..10 step 2 at 6",
        lattice_spec(2, 10, 2),
        6,
        {"values": (4, 8), "bounds": 0, "same": 0},
        "test_neighbourhood.py NF4Tests.test_integer_step_two",
    ),
    (
        "integer 2..10 step 2 at 10",
        lattice_spec(2, 10, 2),
        10,
        {"values": (8,), "bounds": 1, "same": 0},
        "test_neighbourhood.py NF4Tests.test_integer_step_two",
    ),
    (
        "integer 1..3 step 1 at 2",
        lattice_spec(1, 3, 1),
        2,
        {"values": (1, 3)},
        "test_neighbourhood.py NF4Tests.test_integer_step_one",
    ),
    (
        "integer 1..3 step 1 at 1",
        lattice_spec(1, 3, 1),
        1,
        {"values": (2,)},
        "test_neighbourhood.py NF4Tests.test_integer_step_one",
    ),
    (
        "integer 5..5 is constant",
        lattice_spec(5, 5, 1),
        5,
        {"constant": True, "values": (), "bounds": 0, "same": 0},
        "test_neighbourhood.py NF4Tests.test_integer_constant",
    ),
    (
        "integer 2..3 step 2 is constant",
        lattice_spec(2, 3, 2),
        2,
        {"constant": True},
        "test_neighbourhood.py NF4Tests.test_integer_lattice_with_one_ordinal_is_constant",
    ),
    (
        "real 2.0..2.0 step 0.1 is constant",
        real_spec(2.0, 2.0, 0.1),
        2.0,
        {"constant": True, "values": (), "bounds": 0, "same": 0},
        "test_neighbourhood.py "
        "NF4Tests.test_real_with_one_legal_value_is_constant_even_with_a_step",
    ),
    (
        "real 0.0..0.05 step 0.1 is constant",
        real_spec(0.0, 0.05, 0.1),
        0.0,
        {"constant": True, "lattice": [0.0]},
        "test_neighbourhood.py "
        "NF4Tests.test_real_with_one_legal_value_is_constant_even_with_a_step",
    ),
    (
        "stepped real 0.5..2.0 step 0.5 at 1.0",
        real_spec(0.5, 2.0, 0.5),
        1.0,
        {"values": (0.5, 1.5), "bounds": 0, "same": 0},
        "test_neighbourhood.py NF4Tests.test_stepped_real",
    ),
    (
        "stepped real 0.5..2.0 step 0.5 at 2.0",
        real_spec(0.5, 2.0, 0.5),
        2.0,
        {"values": (1.5,), "bounds": 1, "same": 0},
        "test_neighbourhood.py NF4Tests.test_stepped_real",
    ),
    (
        "stepped real 0.5..2.0 step 0.5 at 0.5",
        real_spec(0.5, 2.0, 0.5),
        0.5,
        {"values": (1.0,), "bounds": 1, "same": 0},
        "test_neighbourhood.py NF4Tests.test_stepped_real",
    ),
    (
        "continuous real 0..1, f 0.25, at 0.5",
        real_spec(0.0, 1.0),
        0.5,
        {"values": (0.25, 0.75)},
        "test_neighbourhood.py NF4Tests.test_continuous_real",
    ),
    (
        "continuous real 0..1, f 0.25, at 0.875",
        real_spec(0.0, 1.0),
        0.875,
        {"values": (0.625,)},
        "test_neighbourhood.py NF4Tests.test_continuous_real",
    ),
    (
        "continuous real 0..1, f 0.25, at 0.0",
        real_spec(0.0, 1.0),
        0.0,
        {"values": (0.25,)},
        "test_neighbourhood.py NF4Tests.test_continuous_real",
    ),
    (
        "continuous real 0..1, f 0.25, at 1.0",
        real_spec(0.0, 1.0),
        1.0,
        {"values": (0.75,)},
        "test_neighbourhood.py NF4Tests.test_continuous_real",
    ),
    (
        "log real 0.125..8, ratio 2, at 1.0",
        real_spec(0.125, 8.0, None, True),
        1.0,
        {"values": (0.5, 2.0)},
        "test_neighbourhood.py NF4Tests.test_log_real",
    ),
    (
        "log real 0.125..8, ratio 2, at 0.125",
        real_spec(0.125, 8.0, None, True),
        0.125,
        {"values": (0.25,), "bounds": 1, "same": 0},
        "test_neighbourhood.py NF4Tests.test_log_real",
    ),
    (
        "log real 0.125..8, ratio 2, at 8.0",
        real_spec(0.125, 8.0, None, True),
        8.0,
        {"values": (4.0,), "bounds": 1, "same": 0},
        "test_neighbourhood.py NF4Tests.test_log_real",
    ),
    (
        "log integer 1..1000, ratio 2, at 1",
        lattice_spec(1, 1000, 1, True),
        1,
        {"values": (2,), "bounds": 0, "same": 1},
        "test_neighbourhood.py NF4Tests.test_log_integer",
    ),
    (
        "log integer 1..1000, ratio 2, at 3",
        lattice_spec(1, 1000, 1, True),
        3,
        {"values": (2, 6)},
        "test_neighbourhood.py NF4Tests.test_log_integer",
    ),
    (
        "log integer 1..1000, ratio 2, at 5",
        lattice_spec(1, 1000, 1, True),
        5,
        {"values": (3, 10)},
        "test_neighbourhood.py NF4Tests.test_log_integer",
    ),
    (
        "log integer 1..1000, ratio 2, at 7",
        lattice_spec(1, 1000, 1, True),
        7,
        {"values": (4, 14)},
        "test_neighbourhood.py NF4Tests.test_log_integer",
    ),
    (
        "log integer 1..1000, ratio 2, at 600",
        lattice_spec(1, 1000, 1, True),
        600,
        {"values": (300,), "bounds": 1, "same": 0},
        "test_neighbourhood.py NF4Tests.test_log_integer",
    ),
    (
        "log integer 1..1000, ratio 2, at 1000",
        lattice_spec(1, 1000, 1, True),
        1000,
        {"values": (500,), "bounds": 1, "same": 0},
        "test_neighbourhood.py NF4Tests.test_log_integer",
    ),
    (
        "boolean at False",
        spec("boolean"),
        False,
        {"values": (True,)},
        "test_neighbourhood.py NF4Tests.test_boolean",
    ),
    (
        "boolean at True",
        spec("boolean"),
        True,
        {"values": (False,)},
        "test_neighbourhood.py NF4Tests.test_boolean",
    ),
    (
        "categorical ema/sma/wma at sma",
        spec("categorical", choices=["ema", "sma", "wma"]),
        "sma",
        {"values": ("ema", "wma")},
        "test_neighbourhood.py NF4Tests.test_categorical",
    ),
    (
        "categorical ema/sma/wma at ema",
        spec("categorical", choices=["ema", "sma", "wma"]),
        "ema",
        {"values": ("sma", "wma")},
        "test_neighbourhood.py NF4Tests.test_categorical",
    ),
    (
        "categorical [only] is constant",
        spec("categorical", choices=["only"]),
        "only",
        {"constant": True, "values": (), "bounds": 0, "same": 0},
        "test_neighbourhood.py NF4Tests.test_categorical_single_choice_is_constant",
    ),
    (
        "categorical [a, a, b] at a",
        spec("categorical", choices=["a", "a", "b"]),
        "a",
        {"values": ("b",)},
        "test_neighbourhood.py NF4Tests.test_equal_categorical_choices_collapse",
    ),
    (
        "categorical [a, a] is constant",
        spec("categorical", choices=["a", "a"]),
        "a",
        {"constant": True},
        "test_neighbourhood.py NF4Tests.test_equal_categorical_choices_collapse",
    ),
    (
        "categorical [1, 1.0, True] at 1.0",
        spec("categorical", choices=[1, 1.0, True]),
        1.0,
        {"values": (1, True)},
        "test_neighbourhood.py NF4Tests.test_categorical_choice_types_stay_apart",
    ),
    (
        "stepped real 0.0..0.3 step 0.1: lattice with the endpoint snap",
        real_spec(0.0, 0.3, 0.1),
        0.2,
        {"lattice": [0.0, 0.1, 0.2, 0.3], "values": (0.1, 0.3), "bounds": 0, "same": 0},
        "test_neighbourhood.py NF4Tests.test_stepped_real_endpoint_snap",
    ),
    (
        "stepped real 0.0..0.3 step 0.1 at 0.3",
        real_spec(0.0, 0.3, 0.1),
        0.3,
        {"values": (0.2,), "bounds": 1, "same": 0},
        "test_neighbourhood.py NF4Tests.test_stepped_real_endpoint_snap",
    ),
    (
        "stepped real 0.0..0.3 step 0.1 at 0.1",
        real_spec(0.0, 0.3, 0.1),
        0.1,
        {"values": (0.0, 0.2), "bounds": 0, "same": 0},
        "test_neighbourhood.py NF4Tests.test_stepped_real_endpoint_snap",
    ),
    (
        "stepped real 0.0..0.3 step 0.1 at 0.0",
        real_spec(0.0, 0.3, 0.1),
        0.0,
        {"values": (0.1,), "bounds": 1, "same": 0},
        "test_neighbourhood.py NF4Tests.test_stepped_real_endpoint_snap",
    ),
]
TABLE_FRACTION = 0.25
TABLE_RATIO = 2.0

# (counts, pair_k, expected [(first, second, score)], source)
RANKING_CASES = [
    (
        {"Slow": 1, "Fast": 1, "Mode": 0},
        0,
        [],
        "test_sensitivity.py RankPairsTests.test_nf5b_ranking_by_the_sum_of_not_near_counts",
    ),
    (
        {"Slow": 1, "Fast": 1, "Mode": 0},
        1,
        [("Fast", "Slow", 2)],
        "test_sensitivity.py RankPairsTests.test_nf5b_ranking_by_the_sum_of_not_near_counts",
    ),
    (
        {"Slow": 1, "Fast": 1, "Mode": 0},
        2,
        [("Fast", "Slow", 2), ("Fast", "Mode", 1)],
        "test_sensitivity.py RankPairsTests.test_nf5b_ranking_by_the_sum_of_not_near_counts",
    ),
    (
        {"Slow": 1, "Fast": 1, "Mode": 0},
        3,
        [("Fast", "Slow", 2), ("Fast", "Mode", 1), ("Mode", "Slow", 1)],
        "test_sensitivity.py RankPairsTests.test_nf5b_ranking_by_the_sum_of_not_near_counts",
    ),
    (
        {"Slow": 1, "Fast": 1, "Mode": 0},
        4,
        [("Fast", "Slow", 2), ("Fast", "Mode", 1), ("Mode", "Slow", 1)],
        "test_sensitivity.py RankPairsTests.test_nf5b_ranking_by_the_sum_of_not_near_counts",
    ),
    (
        {"b": 1, "a": 1, "c": 1, "B": 1},
        6,
        [("B", "a", 2), ("B", "b", 2), ("B", "c", 2), ("a", "b", 2), ("a", "c", 2), ("b", "c", 2)],
        "test_sensitivity.py RankPairsTests.test_ties_go_by_ascending_byte_wise_names",
    ),
    (
        {"A": 3, "B": 0, "C": 2},
        3,
        [("A", "C", 5), ("A", "B", 3), ("B", "C", 2)],
        "test_sensitivity.py RankPairsTests.test_a_higher_sum_beats_the_name_order",
    ),
    ({"A": 4}, 3, [], "test_sensitivity.py RankPairsTests.test_one_name_has_no_pair"),
    ({}, 3, [], "test_sensitivity.py RankPairsTests.test_one_name_has_no_pair"),
]
NOT_NEAR_PROFILE = {
    "A": [
        {"class": "scored_ok", "near": False},
        {"class": "scored_ok", "near": True},
        {"class": "scored_infeasible", "near": False},
        {"class": "unscorable", "near": False},
        {"class": "never_evaluated", "near": False},
    ],
    "B": [],
}

# (sources builder, k, expected, source)
TOP_ID_CASES = [
    (
        lambda: [fixture("nf3_source0.json"), fixture("nf3_source1.json")],
        1,
        [(0, 3)],
        "test_neighbourhood.py "
        "TopIdsTests.test_best_distinct_vectors_first_with_lower_identity_on_ties",
    ),
    (
        lambda: [fixture("nf3_source0.json"), fixture("nf3_source1.json")],
        3,
        [(0, 3), (1, 0), (0, 0)],
        "test_neighbourhood.py "
        "TopIdsTests.test_best_distinct_vectors_first_with_lower_identity_on_ties",
    ),
    (
        lambda: [fixture("nf3_source0.json"), fixture("nf3_source1.json")],
        10,
        [(0, 3), (1, 0), (0, 0), (0, 1)],
        "test_neighbourhood.py "
        "TopIdsTests.test_best_distinct_vectors_first_with_lower_identity_on_ties",
    ),
    (
        lambda: [fixture("nf5_result.json")],
        2,
        [(0, 0), (0, 5)],
        "test_neighbourhood.py TopIdsTests.test_tie_goes_to_the_lower_identity",
    ),
    (
        lambda: [fixture("nf5_result.json")],
        1,
        [(0, 0)],
        "test_neighbourhood.py TopIdsTests.test_tie_goes_to_the_lower_identity",
    ),
    (
        lambda: [fixture("nf6_result.json")],
        2,
        [(0, 1), (0, 0)],
        "test_neighbourhood.py TopIdsTests.test_minimize_takes_the_lowest",
    ),
]

# (label, centres on NF1, expected offenders, source)
CENTRE_CASES = [
    (
        "absent row",
        [(0, 99)],
        [(0, 99)],
        "test_neighbourhood.py RefusalTests.test_centre_invalid_variants",
    ),
    (
        "absent source",
        [(1, 0)],
        [(1, 0)],
        "test_neighbourhood.py RefusalTests.test_centre_invalid_variants",
    ),
    (
        "constraint violation",
        [(0, 3)],
        [(0, 3)],
        "test_neighbourhood.py RefusalTests.test_centre_invalid_variants",
    ),
    (
        "engine error",
        [(0, 4)],
        [(0, 4)],
        "test_neighbourhood.py RefusalTests.test_centre_invalid_variants",
    ),
    (
        "listed twice",
        [(0, 0), (0, 0)],
        [(0, 0)],
        "test_neighbourhood.py RefusalTests.test_centre_invalid_variants",
    ),
    (
        "one good, two bad",
        [(0, 0), (0, 3), (0, 99)],
        [(0, 3), (0, 99)],
        "test_neighbourhood.py RefusalTests.test_centre_invalid_variants",
    ),
]
LOG_INTEGER_SWEEPS = [(1, 1000, range(1, 65)), (3, 50, range(3, 51))]
LOG_INTEGER_RATIOS = (1.5, 2.0, 2.5, 3.0, 1.1)
CONTINUOUS_SWEEPS = [(0.125, "continuous"), (0.5, "continuous"), (1.0, "continuous")]
CONTINUOUS_CENTRES = (0.0, 0.25, 0.5, 1.0, 1.75, 3.5, 3.75, 4.0)
LOG_REAL_RATIOS = (2.0, 4.0, 8.0)
LOG_REAL_CENTRES = (0.0625, 0.125, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)


def run_case(case: dict) -> dict:
    return analyse(case["build"](), case["centres"], case["band"], **case["params"])


# ---------------------------------------------------------------------------------------------
# Phase 1 sections (no module).
# ---------------------------------------------------------------------------------------------


def section_cases(ledger: Ledger, cases: list) -> None:
    ledger.begin("worked fixtures NF1-NF6, NF5, NF5b recomputed from the fixture rows")
    for case in cases:
        result = run_case(case)
        for label, getter, literal in case["expects"]:
            ledger.compare(
                "fixture", f"{case['name']}: {label}", getter(result), literal, case["source"]
            )
    ledger.end(sum(len(case["expects"]) for case in cases))


def table_checks(label: str, table_spec: dict, value, expected: dict, fraction, ratio) -> list:
    """(key, oracle value, expected) for every key the table row pins."""
    domain = Domain("X", table_spec)
    kept, bounds, same_count = neighbours(domain, value, fraction, ratio)
    found = {
        "values": tuple(kept),
        "bounds": bounds,
        "same": same_count,
        "constant": domain.rule == "constant",
        "lattice": domain.lattice,
    }
    return [(key, found[key], literal) for key, literal in expected.items()]


def section_generation_table(ledger: Ledger) -> None:
    ledger.begin("generation table: the neighbours of one value, by dimension kind")
    for label, table_spec, value, expected, source in GENERATION_TABLE:
        for key, found, literal in table_checks(
            label, table_spec, value, expected, TABLE_FRACTION, TABLE_RATIO
        ):
            ledger.compare("fixture", f"{label}: {key}", found, literal, source)
    ledger.end(sum(len(row[3]) for row in GENERATION_TABLE))


def section_ranking(ledger: Ledger) -> None:
    ledger.begin("pair ranking by the sum of the not-near counts, ties by byte-wise names")
    for counts, pair_k, expected, source in RANKING_CASES:
        ledger.compare(
            "fixture",
            f"counts {counts}, pair_k {pair_k}",
            rank_pairs(counts, pair_k),
            expected,
            source,
        )
    ledger.compare(
        "fixture",
        "not-near counts: scored far ones and infeasible ones count, unscorable and missing do not",
        not_near_counts(NOT_NEAR_PROFILE),
        {"A": 2, "B": 0},
        "test_sensitivity.py "
        "RankPairsTests.test_not_near_counts_include_infeasible_but_not_unscored",
    )
    ledger.end(len(RANKING_CASES) + 1)


def section_top_ids(ledger: Ledger) -> None:
    ledger.begin("top_ids: best distinct vectors first, lower identity on ties")
    for build, k, expected, source in TOP_ID_CASES:
        ledger.compare(
            "fixture",
            f"top_ids(k={k}) over {len(build())} source(s)",
            oracle_top_ids(build(), k),
            expected,
            source,
        )
    ledger.end(len(TOP_ID_CASES))


def section_refusal_rules(ledger: Ledger) -> None:
    ledger.begin("refusal rules decided from the rows (duplicates, centres, relative band)")
    base = [fixture("nf3_source0.json"), fixture("nf3_source1.json")]
    clash = copy.deepcopy(base)
    clash[0]["trials"][2]["objective"] = 6.5
    ledger.compare(
        "fixture",
        "NF3 with row 0:2 scoring 6.5 while 0:0 scores 6.0: the conflicting scored rows",
        duplicate_conflict_ids(clash),
        [(0, 0), (0, 2)],
        "test_neighbourhood.py NF3Tests.test_disagreeing_duplicates_are_refused",
    )
    status = copy.deepcopy(base)
    status[0]["trials"][2].update(status="constraint_violation", feasible=False)
    ledger.compare(
        "fixture",
        "NF3 with row 0:2 a constraint violation next to the ok row 0:0: the conflicting rows",
        duplicate_conflict_ids(status),
        [(0, 0), (0, 2)],
        "test_neighbourhood.py NF3Tests.test_disagreeing_status_is_refused",
    )
    ledger.compare(
        "fixture",
        "NF3 unchanged: no conflicting rows",
        duplicate_conflict_ids(base),
        [],
        "test_neighbourhood.py NF3Tests.test_base (the base fixture is accepted)",
    )
    for label, build, named in NF7_CONFLICTS:
        ledger.compare(
            "fixture",
            f"{label}: the conflicting scored rows",
            duplicate_conflict_ids(build()),
            named,
            "derived by hand in this file (N11, the rule of test_neighbourhood.py "
            "NF3Tests.test_disagreeing_duplicates_are_refused and "
            "test_disagreeing_status_is_refused, in both id orders)",
        )
    for label, centres, expected, source in CENTRE_CASES:
        ledger.compare(
            "fixture",
            f"centres {centres} on NF1 ({label})",
            centre_offenders([fixture("nf1_result.json")], centres),
            expected,
            source,
        )
    zero = fixture("nf1_result.json")
    for trial in zero["trials"]:
        if trial["status"] == "ok":
            trial["objective"] = 0.0
    study = Study([zero])
    result = analyse([zero], [(0, 0)], ABSOLUTE_1, pair_k=0)
    ledger.compare(
        "fixture",
        "all ok objectives 0.0: a relative band has no scale (reference is 0), "
        "an absolute band runs",
        (study.reference == 0.0, result["reference"]["objective"]),
        (True, 0.0),
        "test_neighbourhood.py RefusalTests.test_relative_band_at_a_zero_reference_is_degenerate",
    )
    ledger.end(3 + len(NF7_CONFLICTS) + len(CENTRE_CASES) + 1)


def sweep_count() -> int:
    """Comparisons one sweep section makes: log integers by ratio, continuous by fraction, log
    reals."""
    return (
        len(LOG_INTEGER_SWEEPS) * len(LOG_INTEGER_RATIOS)
        + len(CONTINUOUS_SWEEPS)
        + len(LOG_REAL_RATIOS)
    )


def section_sweeps_internal(ledger: Ledger) -> None:
    ledger.begin("rule sweeps: each rule computed two ways inside the oracle")
    for low, high, centres in LOG_INTEGER_SWEEPS:
        domain = Domain("X", lattice_spec(low, high, 1, True))
        for ratio in LOG_INTEGER_RATIOS:
            mismatches = [
                value
                for value in centres
                if not same(
                    neighbours(domain, value, None, ratio),
                    log_integer_by_decimal(domain, value, ratio),
                    True,
                )
            ]
            ledger.compare(
                "internal",
                f"log integer {low}..{high}, ratio {ratio}: integer arithmetic against Decimal, "
                f"values {centres.start}..{centres.stop - 1}",
                (len(centres), mismatches),
                (len(centres), []),
            )
    for fraction, _ in CONTINUOUS_SWEEPS:
        domain = Domain("X", real_spec(0.0, 4.0))
        mismatches = [
            value
            for value in CONTINUOUS_CENTRES
            if not same(
                neighbours(domain, value, fraction, None),
                real_rule_by_floats(domain, value, fraction, None),
                True,
            )
        ]
        ledger.compare(
            "internal",
            f"continuous real 0..4, fraction {fraction}: exact rationals against binary64",
            (len(CONTINUOUS_CENTRES), mismatches),
            (len(CONTINUOUS_CENTRES), []),
        )
    for ratio in LOG_REAL_RATIOS:
        domain = Domain("X", real_spec(0.0625, 16.0, None, True))
        mismatches = [
            value
            for value in LOG_REAL_CENTRES
            if not same(
                neighbours(domain, value, None, ratio),
                real_rule_by_floats(domain, value, None, ratio),
                True,
            )
        ]
        ledger.compare(
            "internal",
            f"log real 0.0625..16, ratio {ratio}: exact rationals against binary64",
            (len(LOG_REAL_CENTRES), mismatches),
            (len(LOG_REAL_CENTRES), []),
        )
    ledger.end(sweep_count())


def random_specs(rng: random.Random) -> dict:
    while True:
        specs: dict = {}
        for index in range(rng.randint(2, 4)):
            name = "PQRS"[index]
            kind = rng.choice(("integer", "integer", "real", "boolean", "categorical", "constant"))
            if kind == "integer":
                low = rng.randint(1, 4)
                step = rng.choice((1, 2, 3))
                count = rng.randint(2, 4)
                high = low + step * (count - 1) + rng.randint(0, step - 1)
                specs[name] = lattice_spec(low, high, step)
            elif kind == "real":
                low = rng.choice((0.0, 0.5, 1.0))
                step = rng.choice((0.25, 0.5, 1.0))
                count = rng.randint(2, 4)
                high = low + step * (count - 1) + rng.choice((0.0, step / 4))
                specs[name] = real_spec(low, high, step)
            elif kind == "boolean":
                specs[name] = spec("boolean")
            elif kind == "categorical":
                specs[name] = spec(
                    "categorical",
                    choices=rng.sample(["ema", "sma", "wma", "hma"], rng.randint(2, 3)),
                )
            else:
                specs[name] = lattice_spec(5, 5, 1)
        if any(Domain(name, one).rule != "constant" for name, one in specs.items()):
            return specs


def single_row_source(specs: dict, centre: dict) -> list:
    return [
        {
            "space": {"parameters": specs, "objective": OBJECTIVE},
            "trials": [row_of(0, dict(centre))],
        }
    ]


def random_spaces(rng: random.Random, count: int) -> list:
    """(specs, centres) pairs; at most 24 centres per space, drawn from the whole product."""
    spaces = []
    for _ in range(count):
        specs = random_specs(rng)
        names = sorted(specs, key=lambda name: name.encode("utf-8"))
        legal = [finite_values(Domain(name, specs[name])) for name in names]
        product = [dict(zip(names, combo)) for combo in itertools.product(*legal)]
        spaces.append((specs, rng.sample(product, min(len(product), 24))))
    return spaces


def section_product_internal(ledger: Ledger, spaces: list, seed: int) -> None:
    ledger.begin(
        f"random small spaces (seed {seed}): the rule-based one-move sets against the "
        "enumerated product"
    )
    for number, (specs, centres) in enumerate(spaces):
        mismatches = []
        for centre in centres:
            rule_based = analyse(
                single_row_source(specs, centre), [(0, 0)], ABSOLUTE_1, pair_k=0
            )["missing_vectors"]
            if vectors_as_tags(specs, rule_based) != one_move_from_product(specs, centre):
                mismatches.append(centre)
        ledger.compare(
            "internal",
            f"space {number} {sorted(specs)}: {len(centres)} centres",
            (len(centres), mismatches),
            (len(centres), []),
            quiet=True,
        )
    ledger.end(len(spaces))


# Statuses of the rows of a random study that no scored class holds; engine errors and pruned rows
# are drawn more often because they are the common ones in a real study.
UNSCORED = (
    "engine_error",
    "engine_error",
    "pruned",
    "pruned",
    "trial_timeout",
    "objective_error",
    "partial",
)
# Dimensions a random study may add to its finite ones (finding P3-9: log and continuous reals
# appeared only in the sweeps): (spec, the values its rows may hold). With the fraction 0.25 and
# the ratio 2.0 that every random study passes, each value and each neighbour is exactly a
# binary64 number, so the oracle judges them exactly; neighbours that fall on no row are
# never-evaluated cells.
WIDE_DIMENSIONS = [
    (real_spec(0.0, 1.0), [0.0, 0.25, 0.5, 0.75, 1.0]),
    (real_spec(0.25, 4.0, None, True), [0.25, 0.5, 1.0, 2.0, 4.0]),
    (lattice_spec(1, 16, 1, True), [1, 2, 3, 4, 6, 8, 12, 16]),
]
DOCUMENT_KEYS = (
    "direction",
    "reference",
    "band",
    "rules",
    "sources",
    "duplicate_rows",
    "constant_dimensions",
    "missing_vectors",
    "admission",
)
CENTRE_KEYS = (
    "source",
    "trial_id",
    "objective",
    "centre_gap",
    "centre_near",
    "counts",
    "dropped",
    "share_near_best",
    "coverage",
    "profile",
    "pair_maps",
)


def random_study(rng: random.Random) -> dict:
    """One random small study over a finite space (finding P1-1: the random sections before this one
    held a single row and so never classified anything). Rows sit on a random subset of the product;
    some vectors hold several rows, scored duplicates always agree and unscorable ones differ
    freely; the ids are given after a shuffle, so a duplicate can have the lower or the higher
    identity; statuses and objectives are random, the first vector is always an ok row; one to
    three ok rows are the centres; the band, the direction and pair_k are random."""
    specs = random_specs(rng)
    legal_by_name = {name: finite_values(Domain(name, specs[name])) for name in specs}
    if rng.random() < 0.5:
        wide_spec, wide_values = rng.choice(WIDE_DIMENSIONS)
        specs = {**specs, "W": wide_spec}
        legal_by_name["W"] = wide_values
    names = sorted(specs, key=lambda name: name.encode("utf-8"))
    legal = [legal_by_name[name] for name in names]
    product = [dict(zip(names, combo)) for combo in itertools.product(*legal)]
    held = rng.sample(product, min(len(product), rng.randint(3, 14)))
    rows = []
    for index, vector in enumerate(held):
        roll = rng.random()
        if index == 0 or roll < 0.55:
            outcome = ("ok", rng.randint(1, 48) * 0.25)
        elif roll < 0.7:
            outcome = ("constraint_violation", rng.randint(1, 48) * 0.25)
        else:
            outcome = None
        for copy_number in range(rng.choice((1, 1, 1, 2, 3))):
            if outcome is not None and (copy_number == 0 or rng.random() < 0.7):
                rows.append((vector, outcome[0], outcome[1]))
            else:
                status = rng.choice(UNSCORED)
                objective = (
                    rng.randint(1, 48) * 0.25
                    if status == "pruned" and rng.random() < 0.7
                    else None
                )
                rows.append((vector, status, objective))
    rng.shuffle(rows)
    trials = [
        row_of(number, dict(vector), objective, status)
        for number, (vector, status, objective) in enumerate(rows)
    ]
    ok_ids = [trial["trial_id"] for trial in trials if trial["status"] == "ok"]
    centres = [
        (0, trial_id) for trial_id in rng.sample(ok_ids, min(len(ok_ids), rng.randint(1, 3)))
    ]
    band = rng.choice(
        [{"mode": "absolute", "value": value} for value in (0.0, 0.5, 1.0, 2.0, 4.0)]
        + [{"mode": "relative", "value": value} for value in (0.125, 0.25)]
    )
    objective = {**OBJECTIVE, "direction": rng.choice(("maximize", "minimize"))}
    return {
        "sources": [{"space": {"parameters": specs, "objective": objective}, "trials": trials}],
        "centres": centres,
        "band": band,
        "params": {"fraction": 0.25, "ratio": 2.0, "pair_k": rng.choice((0, 0, 1, 2, 3))},
    }


def random_studies(rng: random.Random, count: int) -> list:
    return [random_study(rng) for _ in range(count)]


def section_studies_internal(ledger: Ledger, studies: list, seed: int) -> None:
    ledger.begin(
        f"random studies (seed {seed}): the oracle classifies and aggregates the rows of "
        "random studies"
    )
    tally = dict.fromkeys(CLASSES, 0)
    tally.update(
        near=0, better=0, pair_maps=0, duplicate_rows=0, prefix_objectives=0, shared_cells=0
    )
    for number, study in enumerate(studies):
        document = analyse(study["sources"], study["centres"], study["band"], **study["params"])
        tally["duplicate_rows"] += document["duplicate_rows"]
        for centre in document["centres"]:
            counts = centre["counts"]
            ledger.compare(
                "internal",
                f"study {number}, centre {centre['source']}:{centre['trial_id']}: "
                "the class counts add up to geometric",
                sum(counts[klass] for klass in CLASSES),
                counts["geometric"],
                quiet=True,
            )
            for klass in CLASSES:
                tally[klass] += counts[klass]
            tally["near"] += counts["near"]
            tally["better"] += counts["better_neighbours"]
            tally["pair_maps"] += len(centre["pair_maps"])
            for cells in centre["profile"].values():
                for cell_found in cells:
                    tally["prefix_objectives"] += (
                        cell_found["class"] == "unscorable" and cell_found["objective"] is not None
                    )
                    tally["shared_cells"] += len(cell_found["rows"]) > 1
    ledger.note(f"what the random studies produced, summed over all centres: {tally}")
    if len(studies) >= 20:
        for name, total in tally.items():
            if not total:
                ledger.fail(f"the random studies never produced any of: {name}")
    ledger.end(sum(len(study["centres"]) for study in studies))


def section_controls(ledger: Ledger) -> None:
    ledger.begin("controls: the comparison can fail, and the oracle follows a changed input")
    must_differ = [
        (3, 4),
        (0.5, 0.25),
        (Fraction(1, 2), Fraction(1, 3)),
        (True, 1),
        ([1, 2], [1, 3]),
        ({"a": 1}, {"a": 2}),
        (None, 0),
        ("a", "b"),
        ((1,), (1, 2)),
        (float("nan"), float("nan")),
    ]
    must_match = [
        (1, 1.0),
        (Fraction(1, 2), 0.5),
        ([1, (2, 3)], (1, [2, 3])),
        ({"a": [1]}, {"a": (1,)}),
        (None, None),
        (True, True),
    ]
    for left, right in must_differ:
        ledger.check(
            "control",
            f"same({show(left)}, {show(right)}) is False",
            same(left, right) is False,
        )
    for left, right in must_match:
        ledger.check(
            "control",
            f"same({show(left)}, {show(right)}) is True",
            same(left, right) is True,
        )
    # NF1 with row 0:2 (Fast 6) changed from 7.0 to 10.25. By hand: R stays 10.5 (row 0:5); the
    # neighbour Fast 6 then has gap 10.5 - 10.25 = 0.25, near, and 10.25 is above the centre's 10.0.
    # Scored ok 2 (9.5, 10.25), infeasible 1, unscorable 1, never evaluated 1, near 2, better 1;
    # share 2 of 3 scored.
    document = fixture("nf1_result.json")
    document["trials"][2]["objective"] = 10.25
    result = analyse([document], [(0, 0)], ABSOLUTE_1, pair_k=0)
    note = "derived by hand in this file"
    ledger.compare(
        "control",
        "NF1 with Fast 6 scoring 10.25: counts",
        result["centres"][0]["counts"],
        {
            "geometric": 5,
            "scored_ok": 2,
            "scored_infeasible": 1,
            "unscorable": 1,
            "never_evaluated": 1,
            "near": 2,
            "better_neighbours": 1,
        },
        note,
    )
    ledger.compare(
        "control",
        "NF1 with Fast 6 scoring 10.25: share_near_best",
        result["centres"][0]["share_near_best"],
        {"numerator": 2, "denominator": 3, "value": 0.6666666666666666},
        note,
    )
    ledger.compare(
        "control",
        "NF1 with Fast 6 scoring 10.25: the Fast 6 cell",
        result["centres"][0]["profile"]["Fast"][1],
        cell(6, "scored_ok", 10.25, 0.25, True, [[0, 2]]),
        note,
    )
    ledger.end(len(must_differ) + len(must_match) + 3)


# The study of the set-aside controls, also used by the verdict self-test of
# neighbourhood_real_rows.py. Fast is the integer lattice 2, 4, ..., 10 and Level a continuous real
# in [0, 1]. The fraction 0.1 is not a binary64 number, so the oracle can judge the Level moves of a
# centre only when value +- 0.1 (the binary64 nearest 0.1, exactly) is itself a binary64 number: for
# the value 0.0 it is, for 0.5 it is not (0.5 + 0.1 needs 55 bits). Band absolute 1.0, maximize,
# reference 10.0 at row 0:0. By hand:
#   centre 0:0 (Fast 4, Level 0.0), 10.0: Fast 2 is row 0:2, ok 9.5, gap 0.5, near; Fast 6 is row
#     0:3, engine_error, unscorable; Level 0.1 has no row, and Level -0.1 is below the lower bound
#     and is dropped by the bounds. Counts: 3 neighbours, scored ok 1, unscorable 1, never
#     evaluated 1, near 1, better 0; share 1 of 1 scored; coverage 2 of 3.
#   centre 0:1 (Fast 4, Level 0.5), 9.0 (gap 1.0, near): Fast 2 is row 0:4, ok 8.0, gap 2.0, not
#     near; Fast 6 has no row; its Level is declined.
SET_ASIDE_SPACE = {
    "parameters": {
        "Fast": {"kind": "integer", "low": 2, "high": 10, "step": 2, "log": False},
        "Level": {"kind": "real", "low": 0.0, "high": 1.0, "step": None, "log": False},
    },
    "objective": OBJECTIVE,
}
SET_ASIDE_CENTRES = [(0, 0), (0, 1)]
SET_ASIDE_FRACTION = 0.1


def set_aside_sources() -> list:
    """The rows of the set-aside study as one parsed result."""
    rows = [
        row_of(0, {"Fast": 4, "Level": 0.0}, 10.0),
        row_of(1, {"Fast": 4, "Level": 0.5}, 9.0),
        row_of(2, {"Fast": 2, "Level": 0.0}, 9.5),
        row_of(3, {"Fast": 6, "Level": 0.0}, None, "engine_error"),
        row_of(4, {"Fast": 2, "Level": 0.5}, 8.0),
    ]
    return [{"space": copy.deepcopy(SET_ASIDE_SPACE), "trials": rows}]


def section_set_aside(ledger: Ledger) -> None:
    """Controls for ``analyse(set_aside=...)``, the route the real-rows script takes when the oracle
    declines a continuous or log real dimension of a centre. Mutation: let a decline mark the other
    dimensions of its centre DECLINED too, and the comparison of the Fast profile of centre 0:1
    fails."""
    ledger.begin("controls: a declined dimension sets aside only what it touches (set_aside)")
    sources = set_aside_sources()
    note = "derived by hand in this file"
    declined: list = []
    result = analyse(
        sources,
        SET_ASIDE_CENTRES,
        ABSOLUTE_1,
        fraction=SET_ASIDE_FRACTION,
        pair_k=0,
        set_aside=declined,
    )
    first, second = result["centres"]
    ledger.compare(
        "control",
        "the declined (centre, dimension) pairs",
        [(source, trial, name) for source, trial, name, _reason in declined],
        [(0, 1, "Level")],
        note,
    )
    ledger.check(
        "control",
        "the reason says that a value is not a binary64 number",
        len(declined) == 1 and declined[0][3].endswith("is not a binary64 value"),
    )
    ledger.compare(
        "control",
        "centre 0:0, which is not declined: counts, dropped, share_near_best, coverage",
        (first["counts"], first["dropped"], first["share_near_best"], first["coverage"]),
        (
            {
                "geometric": 3,
                "scored_ok": 1,
                "scored_infeasible": 0,
                "unscorable": 1,
                "never_evaluated": 1,
                "near": 1,
                "better_neighbours": 0,
            },
            {"bounds": 1, "same_as_centre": 0},
            {"numerator": 1, "denominator": 1, "value": 1.0},
            {"numerator": 2, "denominator": 3, "value": 0.6666666666666666},
        ),
        note,
    )
    ledger.compare(
        "control",
        "centre 0:0: the profile",
        first["profile"],
        {
            "Fast": [
                cell(2, "scored_ok", 9.5, 0.5, True, [[0, 2]]),
                cell(6, "unscorable", None, None, False, [[0, 3]]),
            ],
            "Level": [cell(0.1, "never_evaluated", None, None, False, [])],
        },
        note,
    )
    ledger.compare(
        "control",
        "centre 0:1, whose Level is declined: the Fast profile is still computed",
        second["profile"]["Fast"],
        [
            cell(2, "scored_ok", 8.0, 2.0, False, [[0, 4]]),
            cell(6, "never_evaluated", None, None, False, []),
        ],
        note,
    )
    for key in ("counts", "dropped", "share_near_best", "coverage"):
        ledger.check("control", f"centre 0:1: {key} is DECLINED", second[key] is DECLINED)
    ledger.check(
        "control",
        "centre 0:1: the Level profile is DECLINED",
        second["profile"]["Level"] is DECLINED,
    )
    ledger.compare(
        "control",
        "centre 0:1: the figures the decline does not touch",
        (
            second["source"],
            second["trial_id"],
            second["objective"],
            second["centre_gap"],
            second["centre_near"],
            second["pair_maps"],
        ),
        (0, 1, 9.0, 1.0, True, []),
        note,
    )
    ledger.compare(
        "control",
        "the figures of the document the decline does not touch",
        (
            result["direction"],
            result["reference"],
            result["band"],
            result["duplicate_rows"],
            result["constant_dimensions"],
            result["admission"]["rows"],
            result["admission"]["max_work"],
        ),
        (
            "maximize",
            {"objective": 10.0, "source": 0, "trial_id": 0},
            {"mode": "absolute", "value": 1.0, "delta": 1.0},
            0,
            [],
            5,
            None,
        ),
        note,
    )
    ledger.check(
        "control",
        "the document's missing_vectors, lookups and work are DECLINED",
        result["missing_vectors"] is DECLINED
        and result["admission"]["lookups"] is DECLINED
        and result["admission"]["work"] is DECLINED,
    )
    paired = analyse(
        sources, SET_ASIDE_CENTRES, ABSOLUTE_1, fraction=SET_ASIDE_FRACTION, pair_k=1, set_aside=[]
    )
    ledger.check(
        "control",
        "with pair_k 1: centre 0:1 has DECLINED pair maps, centre 0:0 one map of 3 x 2 cells",
        paired["centres"][1]["pair_maps"] is DECLINED
        and isinstance(paired["centres"][0]["pair_maps"], list)
        and len(paired["centres"][0]["pair_maps"]) == 1
        and len(paired["centres"][0]["pair_maps"][0]["cells"]) == 6,
    )
    try:
        analyse(sources, SET_ASIDE_CENTRES, ABSOLUTE_1, fraction=SET_ASIDE_FRACTION, pair_k=0)
        ended = False
    except InexactOracle:
        ended = True
    ledger.check("control", "without a set_aside list the decline still ends the call", ended)
    nothing: list = []
    judged = analyse(
        sources, SET_ASIDE_CENTRES, ABSOLUTE_1, fraction=0.25, pair_k=0, set_aside=nothing
    )
    plain = analyse(sources, SET_ASIDE_CENTRES, ABSOLUTE_1, fraction=0.25, pair_k=0)
    ledger.compare("control", "the fraction 0.25 is judged: no pair set aside", nothing, [], note)
    ledger.compare("control", "a set_aside list changes nothing if none is declined", judged, plain)
    ledger.end(17)


# ---------------------------------------------------------------------------------------------
# Phase 2: the module, imported only now.
# ---------------------------------------------------------------------------------------------


def load_modules():
    sys.path.insert(0, str(ROOT / "python"))
    sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))
    from pineforge_hpo import neighbourhood, sensitivity

    return neighbourhood, sensitivity


def module_run(module, case: dict) -> dict:
    """The module's document, or its refusal; only the module's own refusal type is caught."""
    try:
        return module.neighbourhood(
            case["build"](), centres=case["centres"], band=case["band"], **case["params"]
        )
    except module.NeighbourhoodError as error:
        return {"status": "refused", "code": error.code, "ids": tuple(error.ids)}


def module_cases(ledger: Ledger, module, cases: list) -> None:
    ledger.begin("module: the worked fixtures against the oracle")
    for case in cases:
        oracle = run_case(case)
        found = module_run(module, case)
        if found["status"] != "ok":
            ledger.fail(f"{case['name']}: the module refused: {found}")
        for label, getter, _literal in case["expects"]:
            other = None if found["status"] != "ok" else getter(found)
            ledger.compare(
                "module", f"{case['name']}: {label}", getter(oracle), other, case["source"]
            )
    ledger.end(sum(len(case["expects"]) for case in cases))


def module_generation(ledger: Ledger, module) -> None:
    ledger.begin("module: the generation table, one dimension at a time")
    for label, table_spec, value, expected, source in GENERATION_TABLE:
        domain = Domain("X", table_spec)
        kept, bounds, same_count = neighbours(domain, value, TABLE_FRACTION, TABLE_RATIO)
        oracle = {
            "values": tuple(kept),
            "bounds": bounds,
            "same": same_count,
            "constant": domain.rule == "constant",
            "lattice": domain.lattice,
        }
        dimension = module.dimension("X", table_spec)
        moves = dimension.neighbours(value, TABLE_FRACTION, TABLE_RATIO)
        found = {
            "values": tuple(moves.values),
            "bounds": moves.bounds,
            "same": moves.same_as_centre,
            "constant": dimension.constant,
            "lattice": None,
        }
        for key in expected:
            if key == "lattice":
                continue
            ledger.compare("module", f"{label}: {key}", oracle[key], found[key], source)
    ledger.end(sum(len([key for key in row[3] if key != "lattice"]) for row in GENERATION_TABLE))


def module_sweeps(ledger: Ledger, module) -> None:
    ledger.begin("module: rule sweeps against the oracle")
    for low, high, centres in LOG_INTEGER_SWEEPS:
        domain = Domain("X", lattice_spec(low, high, 1, True))
        dimension = module.dimension("X", lattice_spec(low, high, 1, True))
        for ratio in LOG_INTEGER_RATIOS:
            mismatches = []
            for value in centres:
                moves = dimension.neighbours(value, None, ratio)
                found = (list(moves.values), moves.bounds, moves.same_as_centre)
                if not same(found, neighbours(domain, value, None, ratio), True):
                    mismatches.append(value)
            ledger.compare(
                "module",
                f"log integer {low}..{high}, ratio {ratio}: "
                f"values {centres.start}..{centres.stop - 1}",
                (len(centres), []),
                (len(centres), mismatches),
            )
    for fraction, _ in CONTINUOUS_SWEEPS:
        domain = Domain("X", real_spec(0.0, 4.0))
        dimension = module.dimension("X", real_spec(0.0, 4.0))
        mismatches = []
        for value in CONTINUOUS_CENTRES:
            moves = dimension.neighbours(value, fraction, None)
            found = (list(moves.values), moves.bounds, moves.same_as_centre)
            if not same(found, neighbours(domain, value, fraction, None), True):
                mismatches.append(value)
        ledger.compare(
            "module",
            f"continuous real 0..4, fraction {fraction}",
            (len(CONTINUOUS_CENTRES), []),
            (len(CONTINUOUS_CENTRES), mismatches),
        )
    for ratio in LOG_REAL_RATIOS:
        domain = Domain("X", real_spec(0.0625, 16.0, None, True))
        dimension = module.dimension("X", real_spec(0.0625, 16.0, None, True))
        mismatches = []
        for value in LOG_REAL_CENTRES:
            moves = dimension.neighbours(value, None, ratio)
            found = (list(moves.values), moves.bounds, moves.same_as_centre)
            if not same(found, neighbours(domain, value, None, ratio), True):
                mismatches.append(value)
        ledger.compare(
            "module",
            f"log real 0.0625..16, ratio {ratio}",
            (len(LOG_REAL_CENTRES), []),
            (len(LOG_REAL_CENTRES), mismatches),
        )
    ledger.end(sweep_count())


def module_ranking(ledger: Ledger, sensitivity) -> None:
    ledger.begin("module: pair ranking functions")
    for counts, pair_k, _expected, source in RANKING_CASES:
        ledger.compare(
            "module",
            f"rank_pairs({counts}, {pair_k})",
            rank_pairs(counts, pair_k),
            sensitivity.rank_pairs(dict(counts), pair_k),
            source,
        )
    ledger.compare(
        "module",
        "not_near_counts on the mixed profile",
        not_near_counts(NOT_NEAR_PROFILE),
        sensitivity.not_near_counts(copy.deepcopy(NOT_NEAR_PROFILE)),
        "test_sensitivity.py "
        "RankPairsTests.test_not_near_counts_include_infeasible_but_not_unscored",
    )
    ledger.end(len(RANKING_CASES) + 1)


def module_top_ids(ledger: Ledger, module) -> None:
    ledger.begin("module: top_ids")
    for build, k, _expected, source in TOP_ID_CASES:
        ledger.compare(
            "module",
            f"top_ids(k={k}) over {len(build())} source(s)",
            oracle_top_ids(build(), k),
            module.top_ids(build(), k),
            source,
        )
    ledger.end(len(TOP_ID_CASES))


def module_refusals(ledger: Ledger, module) -> None:
    ledger.begin(
        "module: refusals the oracle can decide from the rows, and the admission boundaries"
    )

    def refusal(call) -> tuple:
        try:
            call()
        except module.NeighbourhoodError as error:
            return (error.code, tuple(error.ids))
        return ("returned", ())

    base = [fixture("nf3_source0.json"), fixture("nf3_source1.json")]
    clash = copy.deepcopy(base)
    clash[0]["trials"][2]["objective"] = 6.5
    status = copy.deepcopy(base)
    status[0]["trials"][2].update(status="constraint_violation", feasible=False)
    for label, sources, source in (
        (
            "objectives disagree",
            clash,
            "test_neighbourhood.py NF3Tests.test_disagreeing_duplicates_are_refused",
        ),
        (
            "status disagrees",
            status,
            "test_neighbourhood.py NF3Tests.test_disagreeing_status_is_refused",
        ),
    ):
        ledger.compare(
            "module",
            f"duplicate vectors, {label}: (code, rows named)",
            ("neighbourhood_duplicate_conflict", tuple(duplicate_conflict_ids(sources))),
            refusal(
                lambda sources=sources: module.neighbourhood(
                    copy.deepcopy(sources), centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0
                )
            ),
            source,
        )
    for label, build, named in NF7_CONFLICTS:
        ledger.compare(
            "module",
            f"{label}: (code, rows named)",
            ("neighbourhood_duplicate_conflict", tuple(named)),
            refusal(
                lambda build=build: module.neighbourhood(
                    build(), centres=[(0, 0)], band=ABSOLUTE_1, pair_k=0
                )
            ),
            "derived by hand in this file (N11)",
        )
    for label, centres, _expected, source in CENTRE_CASES:
        sources = [fixture("nf1_result.json")]
        ledger.compare(
            "module",
            f"centres {centres} on NF1 ({label}): (code, rows named)",
            ("neighbourhood_centre_invalid", tuple(centre_offenders(sources, centres))),
            refusal(
                lambda centres=centres: module.neighbourhood(
                    [fixture("nf1_result.json")], centres=centres, band=ABSOLUTE_1, pair_k=0
                )
            ),
            source,
        )
    zero = fixture("nf1_result.json")
    for trial in zero["trials"]:
        if trial["status"] == "ok":
            trial["objective"] = 0.0
    ledger.compare(
        "module",
        "all ok objectives 0.0 with a relative band: (code, rows named)",
        ("neighbourhood_band_degenerate", ()),
        refusal(
            lambda: module.neighbourhood(
                copy.deepcopy([zero]),
                centres=[(0, 0)],
                band={"mode": "relative", "value": 0.5},
                pair_k=0,
            )
        ),
        "test_neighbourhood.py RefusalTests.test_relative_band_at_a_zero_reference_is_degenerate",
    )
    for label, build, params, source in (
        (
            "NF1",
            lambda: [fixture("nf1_result.json")],
            {"pair_k": 0},
            "test_neighbourhood.py "
            "RefusalTests.test_work_above_max_work_is_refused_at_the_boundary",
        ),
        (
            "NF5b with pair_k 3",
            lambda: [fixture("nf5b_result.json")],
            {"pair_k": 3},
            "test_neighbourhood.py RefusalTests.test_pair_cells_are_admitted_after_the_ranking",
        ),
    ):
        figure = analyse(build(), [(0, 0)], ABSOLUTE_1, **params)["admission"]["work"]
        at_figure = refusal(
            lambda: module.neighbourhood(
                build(), centres=[(0, 0)], band=ABSOLUTE_1, max_work=figure, **params
            )
        )
        below = refusal(
            lambda: module.neighbourhood(
                build(), centres=[(0, 0)], band=ABSOLUTE_1, max_work=figure - 1, **params
            )
        )
        ledger.compare(
            "module",
            f"{label}: max_work {figure} runs and {figure - 1} refuses",
            (("returned", ()), ("neighbourhood_resource_exceeded", ())),
            (at_figure, below),
            source,
        )
    ledger.end(2 + len(NF7_CONFLICTS) + len(CENTRE_CASES) + 1 + 2)


def module_spaces(ledger: Ledger, module, spaces: list, seed: int) -> None:
    ledger.begin(f"module: the random small spaces (seed {seed}), every drawn centre")
    for number, (specs, centres) in enumerate(spaces):
        mismatches = []
        for centre in centres:
            case = {
                "build": lambda specs=specs, centre=centre: single_row_source(specs, centre),
                "centres": [(0, 0)],
                "band": ABSOLUTE_1,
                "params": {"pair_k": 0},
            }
            found = module_run(module, case)
            if found["status"] != "ok" or (
                vectors_as_tags(specs, found["missing_vectors"])
                != one_move_from_product(specs, centre)
            ):
                mismatches.append(centre)
        ledger.compare(
            "module",
            f"space {number} {sorted(specs)}: {len(centres)} centres",
            (len(centres), []),
            (len(centres), mismatches),
            quiet=True,
        )
    ledger.end(len(spaces))


def module_studies(ledger: Ledger, module, studies: list, seed: int) -> None:
    ledger.begin(f"module: the random studies (seed {seed}), the whole document against the oracle")
    expected_total = 0
    for number, study in enumerate(studies):
        oracle = analyse(study["sources"], study["centres"], study["band"], **study["params"])
        case = {
            "build": lambda study=study: copy.deepcopy(study["sources"]),
            "centres": study["centres"],
            "band": study["band"],
            "params": study["params"],
        }
        found = module_run(module, case)
        if found["status"] != "ok":
            ledger.fail(f"study {number}: the module refused: {found}")
        for key in DOCUMENT_KEYS:
            ledger.compare(
                "module", f"study {number}: {key}", oracle[key], found.get(key), quiet=True
            )
        for index, wanted in enumerate(oracle["centres"]):
            seen = (
                found["centres"][index]
                if found["status"] == "ok" and index < len(found["centres"])
                else {}
            )
            for key in CENTRE_KEYS:
                ledger.compare(
                    "module",
                    f"study {number}, centre {index}: {key}",
                    wanted[key],
                    seen.get(key),
                    quiet=True,
                )
        expected_total += len(DOCUMENT_KEYS) + len(CENTRE_KEYS) * len(oracle["centres"])
    ledger.end(expected_total)


def phase_two(ledger: Ledger, cases: list, spaces: list, studies: list, seed: int) -> None:
    print("\n##### phase 2: the module under test")
    ledger.begin("independence: nothing of the package was imported before phase 2")
    ledger.check(
        "internal",
        "'pineforge_hpo' is not in sys.modules before phase 2",
        "pineforge_hpo" not in sys.modules,
    )
    ledger.end(1)
    module, sensitivity = load_modules()
    module_cases(ledger, module, cases)
    module_generation(ledger, module)
    module_sweeps(ledger, module)
    module_ranking(ledger, sensitivity)
    module_top_ids(ledger, module)
    module_refusals(ledger, module)
    module_spaces(ledger, module, spaces, seed)
    module_studies(ledger, module, studies, seed)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Independent oracle for pineforge_hpo.neighbourhood"
    )
    parser.add_argument(
        "--no-module",
        action="store_true",
        help="run phase 1 only (the checker against the pinned fixture values); the module is "
        "not imported, the verdict says so and the exit code is 3, never 0",
    )
    parser.add_argument("--cases", type=int, default=40, help="random small spaces (default 40)")
    parser.add_argument("--seed", type=int, default=20261011, help="random seed (default 20261011)")
    args = parser.parse_args(argv)
    if args.cases < 5:
        parser.error("--cases must be at least 5")
    ledger = Ledger(strict=True)
    print(
        f"neighbourhood oracle: root {ROOT}, python {sys.version.split()[0]}, "
        f"spaces {args.cases}, seed {args.seed}"
    )
    harness_self_test()
    print("##### phase 1: the checker alone (no module imported)")
    cases = build_cases()
    section_cases(ledger, cases)
    section_generation_table(ledger)
    section_ranking(ledger)
    section_top_ids(ledger)
    section_refusal_rules(ledger)
    section_sweeps_internal(ledger)
    spaces = random_spaces(random.Random(args.seed), args.cases)
    section_product_internal(ledger, spaces, args.seed)
    studies = random_studies(random.Random(args.seed + 2), args.cases)
    section_studies_internal(ledger, studies, args.seed)
    section_controls(ledger)
    section_set_aside(ledger)
    if args.no_module:
        print("\n##### phase 2 NOT RUN (--no-module)")
    else:
        phase_two(ledger, cases, spaces, studies, args.seed)
    print("\nSUMMARY")
    for kind in ("fixture", "internal", "control", "module"):
        print(f"  {kind:8s} comparisons: {ledger.counts.get(kind, 0)}")
    print(f"  failed comparisons and checks: {len(ledger.failures)}")
    for item in ledger.failures:
        print(f"   - {item}")
    if ledger.failures:
        print("RESULT: FAIL")
        return 1
    if args.no_module:
        print(
            f"RESULT: PHASE 1 ONLY (exit {NO_MODULE_EXIT}): the checker agrees with the pinned "
            "values, but the module was NOT run, so no comparison with the module was made and "
            "nothing here supports calling a G2 result accepted"
        )
        return NO_MODULE_EXIT
    print("RESULT: PASS (checker agrees with the pinned values and with the module)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
