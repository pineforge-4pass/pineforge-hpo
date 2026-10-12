#!/usr/bin/env python3
"""Independent exact oracle for post hoc walk-forward analysis of finished studies.

Phase 1 embeds worked-example rows and hand-derived pins. Selection uses the
unbeaten-row predicate, not a signed sort. Ranks use Fraction, vector lookup uses
kind-normalised JSON, and stability retains each recorded scalar. Digest evidence
comes from literal entry texts. No package code is imported before phase 2.

Usage: python3 tests/g3_oracles/walkforward_oracle.py [--no-module]
       [--cases N] [--seed S]
Exit 0: all comparisons made and agreed, including the module. Exit 1: failure.
Exit 2: usage error or unavailable module. Exit 3: phase 1 only, module not compared.

v1 analyses finished studies; native in-loop walk-forward comes later.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, field
from fractions import Fraction
import hashlib
import importlib
import json
import math
from pathlib import Path
import random
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
METRIC = "metrics.all.net_profit_pct"
SPACE = {
    "parameters": {
        "A": {"kind": "integer", "low": 1, "high": 5, "step": 1, "log": False},
        "B": {"kind": "real", "low": 0.0, "high": 1.0, "step": 0.25, "log": False},
        "C": {"kind": "boolean"},
    },
    "objective": {"expression": METRIC, "direction": "maximize", "constraints": []},
}
STATUSES = (
    "ok",
    "constraint_violation",
    "engine_error",
    "pruned",
    "trial_timeout",
    "objective_error",
    "partial",
    "constraint_error",
    "trial_error",
)
COUNTERS = (
    "input_bars_processed",
    "script_bars_processed",
    "input_tf_seconds",
    "script_tf_seconds",
)
ABSENT = object()
MISSING = object()
FIXTURE_NAMES = (
    "three finished windows",
    "carried vector absent",
    "carrying row pruned",
    "equal carried duplicates",
    "supplied w1,w2",
    "supplied w1,w3",
    "supplied w1",
    "middle supplied window without selection",
    "excluded ids reverse input order",
    "target without eligible rows",
    "unrelated conflicting duplicates",
    "recorded metric maximize",
    "recorded metric minimize",
    "recorded types and signed zero",
    "admission bound 27",
    "admission bound 30",
    "different contexts across windows",
    "equal recorded hashes",
    "one hash without recorded space",
    "one-window plan",
    "integer extrema above binary64 precision",
    "real extrema with shared binary64 identity",
)
REFUSAL_CODES = (
    "parameter_missing",
    "parameter_invalid",
    "window_unknown",
    "no_supplied_sources",
    "input_invalid",
    "rows_incomplete",
    "space_missing",
    "space_mismatch",
    "context_mismatch",
    "direction_mismatch",
    "metric_unknown",
    "duplicate_vector",
    "no_eligible_rows",
    "resource_exceeded",
)
REFUSAL_MULTIPLICITIES = (4, 9, 1, 1, 8, 1, 1, 3, 1, 2, 1, 1, 1, 1)
assert len(FIXTURE_NAMES) == len(set(FIXTURE_NAMES))
assert len(REFUSAL_CODES) == len(REFUSAL_MULTIPLICITIES) == 14
FIXTURE_CASE_COUNT = len(FIXTURE_NAMES)
REFUSAL_CASE_COUNT = sum(REFUSAL_MULTIPLICITIES)

# The status/cell table is resolved before selection, lookup, or accounting.
# A status exclusion applies regardless of that row's cell category.
ELIGIBILITY = {
    (status, kind): f"status_{status}"
    for status in STATUSES
    if status != "ok"
    for kind in ("absent", "null", "number", "other")
}
ELIGIBILITY.update(
    {
        ("ok", "absent"): "metric_not_recorded",
        ("ok", "null"): "metric_unavailable",
        ("ok", "number"): None,
        ("ok", "other"): "invalid",
    }
)

# Literal compact ASCII preimages derived from the documented digest recipe.
# No expected digest is taken from a module invocation or fixture serialisation.
D_TEXTS = (
    '["w1",0,"ok",1.5]',
    '["w1",1,"ok",3.0]',
    '["w1",2,"pruned",9.0]',
    '["w1",3,"ok",3.0]',
    '["w2",0,"ok",4.0]',
    '["w2",1,"ok",4.0]',
    '["w2",2,"engine_error",null]',
    '["w2",3,"ok",5.0]',
    '["w3",0,"ok",3.0]',
    '["w3",1,"ok",1.0]',
    '["w3",2,"constraint_violation",6.0]',
    '["w3",3,"ok",4.0]',
)
E_TEXTS = (
    '["w1",0,"ok",2.0]',
    '["w1",1,"ok",3.0]',
    '["w1",2,"pruned",9.0]',
    '["w1",3,"ok",null]',
    '["w2",0,"ok",null]',
    '["w2",1,"ok",4.0]',
    '["w2",2,"engine_error",null]',
    '["w2",3,"ok",5.0]',
    '["w3",0,"ok",3.0]',
    '["w3",1,"ok",1.0]',
    '["w3",2,"constraint_violation",6.0]',
    '["w3",3,"ok",null]',
)

NOT_COMPARED = (
    "Exact wording of interpretation; only a nonempty string's presence is checked, "
    "never equality with the module's constant.",
    "Refusal precedence when several defects coexist; each refusal case has "
    "one defect.",
    "Values inside refusal detail; only code, ascending ids, and first names "
    "entry count, except that equal-text unknown labels compare all names.",
    "Reader faults absent from these inputs: files, bad UTF-8, duplicate JSON keys, "
    "JSON tokens NaN/Infinity, overflow tokens, and complete reader validation.",
    "Digest for random cases; fixture digests alone have independent literal "
    "preimages.",
    "Non-ok cells that are strings, arrays, objects, or booleans, and metrics "
    "containers other than objects; the fixtures do not cover their classification "
    "or digests.",
    "Other recorded metrics: only the worked recorded-profit fixture is compared.",
    "Direction naming and selection convention independently of the pins: both routes "
    "read the same names; hand-derived maximize/minimize and tie pins anchor them.",
    "Full parameter-domain validation or StudySpec conversion; random inputs use legal "
    "small spaces. Vector-key wire strings are not compared; lookup identity is "
    "independently computed from kind-normalised numeric JSON.",
    "Native execution, dataset identity, out-of-sample claims, or robustness evidence.",
)


def compact(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def same(left, right) -> bool:
    """Compare exact ranks, typed counts, containers, and float zero signs."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, Fraction) or isinstance(right, Fraction):
        if not isinstance(left, (int, float, Fraction)) or not isinstance(
            right, (int, float, Fraction)
        ):
            return False
        try:
            return Fraction(left) == Fraction(right)
        except (ValueError, OverflowError):
            return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            same(left[key], right[key]) for key in left
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
    if type(left) is float and type(right) is float and left == right == 0.0:
        return math.copysign(1.0, left) == math.copysign(1.0, right)
    return type(left) is type(right) and left == right


class Ledger:
    """Every executed comparison counts; a missing section is a failure."""

    def __init__(self, echo: bool = True):
        self.echo = echo
        self.name = ""
        self.made = 0
        self.counts: dict[str, int] = {}
        self.failures: list[str] = []

    def begin(self, name: str):
        self.name = name
        self.made = 0
        if self.echo:
            print(f"\n== {name}")

    def check(self, kind: str, label: str, condition: bool):
        self.made += 1
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if not condition:
            self.failures.append(f"{self.name}: {label}")
        if self.echo:
            print(f"{'AGREE' if condition else 'DISAGREE'} {kind}: {label}")

    def compare(self, kind: str, label: str, expected, found):
        condition = same(expected, found)
        self.check(kind, label, condition)
        if not condition and self.echo:
            print(f"  oracle/pin={expected!r}\n  found={found!r}")

    def end(self, expected: int):
        if self.echo:
            print(f"-- {self.name}: {self.made} comparisons made, {expected} expected")
        if self.made != expected:
            self.failures.append(
                f"{self.name}: {self.made} comparisons made, {expected} expected"
            )


def harness_self_test():
    """Catch numeric, container, type, signed-zero, and accounting blindness."""
    matching = (
        (Fraction(5, 2), 2.5),
        ({"x": [1, True, None]}, {"x": [1, True, None]}),
        (-0.0, -0.0),
    )
    differing = (
        (Fraction(5, 2), 1.0),
        (Fraction(5, 2), math.nextafter(2.5, math.inf)),
        (1, 1.0),
        (True, 1),
        ({"x": 1}, {"x": 2}),
        ([1], []),
        (None, 0),
        (float("nan"), float("nan")),
        (-0.0, 0.0),
        ({"parameters": {"B": -0.0}}, {"parameters": {"B": 0.0}}),
        ({"value": -0.0}, {"value": 0.0}),
    )
    if any(same(a, b) is not True for a, b in matching):
        raise AssertionError("harness rejects an exact match")
    if any(same(a, b) is not False for a, b in differing):
        raise AssertionError("harness misses a planted difference")
    if compact([1, -0.0]) == compact([1.0, 0.0]):
        raise AssertionError("byte comparison lost type or signed zero")
    ledger = Ledger(echo=False)
    ledger.begin("planted disagreement")
    ledger.compare("control", "different rank", Fraction(5, 2), 1.0)
    ledger.end(1)
    if ledger.made != 1 or len(ledger.failures) != 1:
        raise AssertionError("ledger swallowed a disagreement")
    ledger.begin("missing comparison")
    ledger.end(1)
    if len(ledger.failures) != 2:
        raise AssertionError("ledger swallowed a missing comparison")
    print("harness self-test passed")


@dataclass
class Case:
    name: str
    windows: list | tuple | None
    sources: dict | list | None
    metric: str | int | None = "objective"
    direction: str | None = "maximize"
    space: dict | int | None = None
    max_work: int | None = None
    texts: tuple[str, ...] = ()
    pins: dict = field(default_factory=dict)


class OracleRefusal(Exception):
    def __init__(self, code: str, ids=(), names=()):
        self.code = f"walkforward_{code}"
        self.ids = tuple(sorted(ids))
        self.names = tuple(names)
        super().__init__(self.code)

    def view(self):
        return self.code, self.ids, self.names[:1]


def row(identifier, status, objective, a, b, c, *, space=None):
    return {
        "trial_id": identifier,
        "status": status,
        "feasible": status == "ok",
        "objective": objective,
        "parameters": {"A": a, "B": b, "C": c},
        "space": copy.deepcopy(SPACE if space is None else space),
    }


def base_rows():
    """Worked rows: failed/pruned objectives do not compete, ids tie exactly."""
    return {
        "w1": [
            row(0, "ok", 1.5, 2, 0.25, True),
            row(1, "ok", 3.0, 3, 0.5, False),
            row(2, "pruned", 9.0, 1, 0.0, True),
            row(3, "ok", 3.0, 4, 0.0, False),
        ],
        "w2": [
            row(0, "ok", 4.0, 3, 0.5, False),
            row(1, "ok", 4.0, 2, 0.25, True),
            row(2, "engine_error", None, 5, 1.0, True),
            row(3, "ok", 5.0, 5, 0.75, True),
        ],
        "w3": [
            row(0, "ok", 3.0, 3, 0.5, True),
            row(1, "ok", 1.0, 1, 0.25, False),
            row(2, "constraint_violation", 6.0, 2, 0.5, False),
            row(3, "ok", 4.0, 5, 0.75, True),
        ],
    }


def recorded_rows():
    """Recorded-metric table: absent and null are separate exclusion families."""
    rows = base_rows()
    cells = {
        "w1": (2.0, 3.0, 9.0, ABSENT),
        "w2": (None, 4.0, ABSENT, 5.0),
        "w3": (3.0, 1.0, 6.0, ABSENT),
    }
    for label, entries in rows.items():
        for entry, cell in zip(entries, cells[label]):
            if cell is not ABSENT:
                entry["metrics"] = {METRIC: cell}
    return rows


def cell_of(entry, metric):
    if metric == "objective":
        return entry["objective"]
    metrics = entry.get("metrics", {})
    return metrics.get(metric, ABSENT) if isinstance(metrics, dict) else ABSENT


def cell_kind(cell):
    if cell is ABSENT:
        return "absent"
    if cell is None:
        return "null"
    if type(cell) in (int, float):
        return "number"
    return "other"


def exact_value(cell):
    """Compare binary64 inputs exactly, including rounded integer cells."""
    value = float(cell)
    if not math.isfinite(value):
        raise ValueError("not finite binary64")
    return Fraction(value)


def normal_scalar(value, kind):
    if kind == "real":
        number = float(value)
        return 0.0 if number == 0 else number
    if kind == "integer":
        return int(value)
    if kind == "boolean":
        return bool(value)
    return value


def lookup_key(parameters, space):
    """Numeric JSON keyed by dimension kind; no typed-scalar wire key is used."""
    return compact(
        {
            name: normal_scalar(parameters[name], dimension["kind"])
            for name, dimension in space["parameters"].items()
        }
    )


def distinct_key(value, kind):
    if kind == "categorical":
        return kind, type(value).__name__, value
    return kind, normal_scalar(value, kind)


def digest_texts(texts):
    """Hex sorting is a separate construction of the raw-byte ordering recipe."""
    hexes = sorted(hashlib.sha256(text.encode("ascii")).hexdigest() for text in texts)
    return hashlib.sha256(bytes.fromhex("".join(hexes))).hexdigest()


def read_inline(case):
    """A bounded independent reader for the inline inputs this script declares.

    This covers only the declared inline cases of the shared reader's validation.
    The malformed-row and incomplete-result cases each carry exactly one defect.
    """
    rows = {}
    holders = []
    directions = {}
    for label in case.windows:
        if label not in case.sources:
            continue
        source = case.sources[label]
        if isinstance(source, dict):
            if source.get("trials_out", "all") != "all":
                raise OracleRefusal("rows_incomplete", names=(label,))
            entries = source.get("trials")
            if not isinstance(entries, list):
                raise OracleRefusal("input_invalid", names=(label,))
            entries = source.get("warm_start_trials", []) + entries
            if "space" in source:
                holders.append((label, -1, source["space"]))
            directions[label] = source.get("direction")
        elif isinstance(source, list):
            entries = source
        else:
            raise OracleRefusal("input_invalid", names=(label,))
        rows[label] = entries
        seen = set()
        for entry in entries:
            identifier = entry.get("trial_id") if isinstance(entry, dict) else None
            ids = (identifier,) if type(identifier) is int else ()
            if not isinstance(entry, dict) or type(identifier) is not int:
                raise OracleRefusal("input_invalid", ids, (label,))
            if "metrics" in entry and not isinstance(entry["metrics"], dict):
                raise OracleRefusal("input_invalid", ids, (label,))
            status = entry.get("status")
            if (
                status not in STATUSES
                or identifier in seen
                or type(entry.get("feasible")) is not bool
                or entry["feasible"] != (status == "ok")
                or "objective" not in entry
            ):
                raise OracleRefusal("input_invalid", ids, (label,))
            seen.add(identifier)
            objective = entry["objective"]
            try:
                if objective is not None:
                    if type(objective) not in (int, float):
                        raise ValueError("objective type")
                    exact_value(objective)
                elif status in ("ok", "constraint_violation"):
                    raise ValueError("objective required")
            except (ValueError, OverflowError):
                raise OracleRefusal("input_invalid", ids, (label,)) from None
            if "space" in entry:
                holders.append((label, identifier, entry["space"]))
        for field_name in (*COUNTERS, "space_hash"):
            observed = [
                (entry["trial_id"], entry[field_name])
                for entry in entries
                if field_name == "space_hash" and field_name in entry
            ]
            if field_name != "space_hash":
                observed = [
                    (entry["trial_id"], entry["backtest"].get(field_name))
                    for entry in entries
                    if entry["status"] == "ok" and "backtest" in entry
                ]
            if observed and len({value for _, value in observed}) > 1:
                reference_id, reference = min(observed)
                apart = [
                    identifier for identifier, value in observed if value != reference
                ]
                raise OracleRefusal(
                    "context_mismatch", (reference_id, *apart), (label, field_name)
                )
    return rows, holders, directions


def check_inputs(case):
    required = {
        "windows": case.windows,
        "sources": case.sources,
        "metric": case.metric,
        "direction": case.direction,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise OracleRefusal("parameter_missing", names=missing)
    invalid = []
    if not isinstance(case.windows, (list, tuple)) or not case.windows:
        invalid.append("windows")
    elif any(type(label) is not str or not label for label in case.windows):
        invalid.append("windows")
    elif len(set(case.windows)) != len(case.windows):
        invalid.append("windows")
    if not isinstance(case.sources, dict):
        invalid.append("sources")
    if not isinstance(case.metric, str):
        invalid.append("metric")
    if case.direction not in ("maximize", "minimize"):
        invalid.append("direction")
    if case.space is not None and not isinstance(case.space, dict):
        invalid.append("space")
    if case.max_work is not None and (
        type(case.max_work) is not int or case.max_work <= 0
    ):
        invalid.append("max_work")
    if invalid:
        raise OracleRefusal("parameter_invalid", names=invalid)
    unknown = set(case.sources) - set(case.windows)
    if unknown:
        raise OracleRefusal(
            "window_unknown",
            names=sorted(unknown, key=lambda label: (str(label), type(label).__name__)),
        )
    if not case.sources:
        raise OracleRefusal("no_supplied_sources")


def resolve_space(case, rows, holders, directions):
    if not holders and case.space is None:
        raise OracleRefusal("space_missing")
    if holders:
        holders.sort(key=lambda item: (case.windows.index(item[0]), item[1]))
        reference_label, _, space = holders[0]
        apart = [holder for holder in holders if compact(holder[2]) != compact(space)]
        if apart:
            labels = {reference_label, *(label for label, _, _ in apart)}
            names = [label for label in case.windows if label in labels]
            raise OracleRefusal(
                "space_mismatch",
                [identifier for _, identifier, _ in apart if identifier >= 0],
                names,
            )
    else:
        space = case.space
    hashed = [
        (case.windows.index(label), entry["trial_id"], label, entry["space_hash"])
        for label, entries in rows.items()
        for entry in entries
        if "space_hash" in entry
    ]
    if hashed:
        _, _, reference_label, reference = min(hashed)
        apart = [item for item in hashed if item[3] != reference]
        if apart:
            involved = {reference_label, *(item[2] for item in apart)}
            raise OracleRefusal(
                "space_mismatch",
                [item[1] for item in apart],
                [label for label in case.windows if label in involved],
            )
    if case.metric == "objective":
        if holders and space.get("objective", {}).get("direction") not in (
            None,
            case.direction,
        ):
            raise OracleRefusal("direction_mismatch", names=("direction",))
        for label, entries in rows.items():
            recorded = {directions.get(label)}
            recorded.update(
                entry.get("space", {}).get("objective", {}).get("direction")
                for entry in entries
            )
            if any(value not in (None, case.direction) for value in recorded):
                raise OracleRefusal("direction_mismatch", names=(label, "direction"))
    return space


def eligibility_table(case, rows):
    if case.metric != "objective" and any(rows.values()):
        if all(
            cell_of(entry, case.metric) is ABSENT
            for entries in rows.values()
            for entry in entries
        ):
            raise OracleRefusal("metric_unknown", names=(case.metric,))
    table = {}
    for label, entries in rows.items():
        for entry in entries:
            cell = cell_of(entry, case.metric)
            kind = cell_kind(cell)
            reason = ELIGIBILITY[entry["status"], kind]
            try:
                if kind == "number":
                    value = exact_value(cell)
                else:
                    value = None
                if cell is not ABSENT:
                    compact(cell)
                if reason == "invalid":
                    raise ValueError("ok cell type")
            except (TypeError, ValueError, OverflowError):
                raise OracleRefusal(
                    "input_invalid", (entry["trial_id"],), (label, case.metric)
                ) from None
            table[label, entry["trial_id"]] = reason, value
    return table


def parameter_check(rows, space):
    dimensions = space["parameters"]
    for label, entries in rows.items():
        for entry in entries:
            parameters = entry.get("parameters")
            if (
                not isinstance(parameters, dict)
                or parameters.keys() != dimensions.keys()
            ):
                raise OracleRefusal("input_invalid", (entry["trial_id"],), (label,))
            for name, dimension in dimensions.items():
                value = parameters[name]
                kind = dimension["kind"]
                valid = {
                    "integer": type(value) is int,
                    "real": type(value) in (int, float),
                    "boolean": type(value) is bool,
                    "categorical": any(
                        type(value) is type(choice) and value == choice
                        for choice in dimension.get("choices", [])
                    ),
                }[kind]
                if not valid:
                    raise OracleRefusal(
                        "input_invalid", (entry["trial_id"],), (label, name)
                    )


def better(left, right, direction):
    return left > right if direction == "maximize" else left < right


def selected_row(entries, values, direction):
    """No row is strictly better, and no equal row has a lower id: no sort."""
    unbeaten = [
        entry
        for entry in entries
        if not any(
            better(values[other["trial_id"]], values[entry["trial_id"]], direction)
            or (
                values[other["trial_id"]] == values[entry["trial_id"]]
                and other["trial_id"] < entry["trial_id"]
            )
            for other in entries
        )
    ]
    if len(unbeaten) != (1 if entries else 0):
        raise AssertionError("unbeaten predicate did not produce a unique selection")
    return unbeaten[0] if unbeaten else None


def oracle(case):
    """Recompute a document without any package function or package data type."""
    check_inputs(case)
    rows, holders, directions = read_inline(case)
    space = resolve_space(case, rows, holders, directions)
    table = eligibility_table(case, rows)
    parameter_check(rows, space)
    supplied = [label for label in case.windows if label in case.sources]
    never_run = [label for label in case.windows if label not in case.sources]
    eligible = {
        label: [
            entry for entry in entries if table[label, entry["trial_id"]][0] is None
        ]
        for label, entries in rows.items()
    }
    selected = {}
    per_window = []
    indices = {}
    for label in supplied:
        values = {
            entry["trial_id"]: table[label, entry["trial_id"]][1]
            for entry in eligible[label]
        }
        selected[label] = selected_row(eligible[label], values, case.direction)
        families = {}
        indices[label] = {}
        for entry in rows[label]:
            reason, _ = table[label, entry["trial_id"]]
            if reason is not None:
                families.setdefault(reason, []).append(entry["trial_id"])
            key = lookup_key(entry["parameters"], space)
            indices[label].setdefault(key, []).append(entry)
        order = sorted(name for name in families if name.startswith("status_"))
        order += [
            name
            for name in ("metric_not_recorded", "metric_unavailable")
            if name in families
        ]
        exclusions = {
            name: {"count": len(families[name]), "ids": sorted(families[name])}
            for name in order
        }
        chosen = selected[label]
        per_window.append(
            {
                "window": label,
                "rows_read": len(rows[label]),
                "rows_eligible": len(eligible[label]),
                "rows_excluded": sum(len(ids) for ids in families.values()),
                "excluded": exclusions,
                "selected": (
                    {
                        "trial_id": chosen["trial_id"],
                        "value": float(cell_of(chosen, case.metric)),
                        "parameters": copy.deepcopy(chosen["parameters"]),
                    }
                    if chosen is not None
                    else None
                ),
                "no_eligible_rows": chosen is None,
            }
        )
    positions = {label: position for position, label in enumerate(case.windows)}
    successive = list(zip(supplied, supplied[1:]))
    breaks = [
        {
            "from": left,
            "to": right,
            "skipped": list(case.windows[positions[left] + 1 : positions[right]]),
        }
        for left, right in successive
        if positions[right] - positions[left] > 1
    ]
    pairs = [
        (left, right)
        for left, right in successive
        if positions[right] - positions[left] == 1 and selected[left] is not None
    ]
    carry = []
    for left, right in pairs:
        parameters = selected[left]["parameters"]
        candidates = indices[right].get(lookup_key(parameters, space), [])
        scored = [
            entry for entry in candidates if table[right, entry["trial_id"]][0] is None
        ]
        if len({table[right, entry["trial_id"]][1] for entry in scored}) > 1:
            raise OracleRefusal(
                "duplicate_vector", [entry["trial_id"] for entry in scored], (right,)
            )
        entry = {
            "from": left,
            "to": right,
            "parameters": copy.deepcopy(parameters),
            "row_found": bool(scored),
            "trial_id": None,
            "value": None,
            "better": None,
            "equal": None,
            "rank_mid": None,
            "reason": None,
        }
        if scored:
            chosen = min(scored, key=lambda item: item["trial_id"])
            value = table[right, chosen["trial_id"]][1]
            ranks = [table[right, item["trial_id"]][1] for item in eligible[right]]
            better_count = sum(better(other, value, case.direction) for other in ranks)
            equal_count = sum(other == value for other in ranks)
            entry.update(
                trial_id=chosen["trial_id"],
                value=float(cell_of(chosen, case.metric)),
                parameters=copy.deepcopy(chosen["parameters"]),
                better=better_count,
                equal=equal_count,
                rank_mid=Fraction(better_count) + Fraction(equal_count + 1, 2),
            )
        elif candidates:
            lowest = min(candidates, key=lambda item: item["trial_id"])
            entry["reason"] = table[right, lowest["trial_id"]][0]
        else:
            entry["reason"] = "no_row"
        carry.append(entry)
    selected_windows = [label for label in supplied if selected[label] is not None]
    if not selected_windows:
        raise OracleRefusal("no_eligible_rows")
    dimensions = {}
    for name in sorted(space["parameters"]):
        kind = space["parameters"][name]["kind"]
        values = [selected[label]["parameters"][name] for label in selected_windows]
        numeric = kind in ("integer", "real")
        dimensions[name] = {
            "kind": kind,
            "values": values,
            "distinct": len({distinct_key(value, kind) for value in values}),
            "minimum": next(value for value in values if value == min(values))
            if numeric
            else None,
            "maximum": next(value for value in values if value == max(values))
            if numeric
            else None,
        }
    rows_read = sum(len(entries) for entries in rows.values())
    rows_eligible = sum(len(entries) for entries in eligible.values())
    # Count the triangular allowance by its rows, rather than a product/division.
    pair_allowance = sum(sum(range(len(entries))) for entries in eligible.values())
    lookup_allowance = sum(len(eligible[right]) for _, right in pairs)
    work = rows_read + pair_allowance + lookup_allowance
    if case.max_work is not None and work > case.max_work:
        raise OracleRefusal("resource_exceeded")
    document = {
        "contract": "pineforge-hpo-walkforward/v1",
        "status": "ok",
        "metric": case.metric,
        "direction": case.direction,
        "windows": list(case.windows),
        "supplied": supplied,
        "never_run": never_run,
        "rows_read": rows_read,
        "rows_eligible": rows_eligible,
        "rows_excluded": rows_read - rows_eligible,
        "per_window": per_window,
        "carry_over": carry,
        "chain_breaks": breaks,
        "stability": {
            "windows": selected_windows,
            "windows_without_selection": [
                label for label in supplied if selected[label] is None
            ],
            "dimensions": dimensions,
        },
        "work": work,
        "algorithm": "exact_per_window_v1",
    }
    if case.texts:
        document["input_sha256"] = digest_texts(case.texts)
    return document


def parameters(a, b, c):
    return {"A": a, "B": b, "C": c}


def selection(identifier, value, a, b, c):
    return {"trial_id": identifier, "value": value, "parameters": parameters(a, b, c)}


def exclusion(reason, ids):
    return {reason: {"count": len(ids), "ids": ids}}


def dimension(kind, values, distinct, minimum=None, maximum=None):
    """Format literal pins; distinct and extremes are supplied, never inferred."""
    return {
        "kind": kind,
        "values": values,
        "distinct": distinct,
        "minimum": minimum,
        "maximum": maximum,
    }


def carry_pin(left, right, a, b, c, identifier, value, counts, reason=None):
    """Format hand-derived carry pins; no lookup or rank computation occurs."""
    return {
        "from": left,
        "to": right,
        "parameters": parameters(a, b, c),
        "row_found": identifier is not None,
        "trial_id": identifier,
        "value": value,
        "better": counts[0],
        "equal": counts[1],
        "rank_mid": counts[2],
        "reason": reason,
    }


def pin_view(document):
    """Expose computed fields; dimension text preserves scalar types and zeros."""
    return {
        "windows": document["windows"],
        "supplied": document["supplied"],
        "never_run": document["never_run"],
        "totals": tuple(
            document[name] for name in ("rows_read", "rows_eligible", "rows_excluded")
        ),
        "selected": [block["selected"] for block in document["per_window"]],
        "accounting": [
            (
                block["window"],
                block["rows_read"],
                block["rows_eligible"],
                block["rows_excluded"],
                block["excluded"],
                list(block["excluded"]),
                block["no_eligible_rows"],
            )
            for block in document["per_window"]
        ],
        "carry": document["carry_over"],
        "breaks": document["chain_breaks"],
        "stability_windows": document["stability"]["windows"],
        "without_selection": document["stability"]["windows_without_selection"],
        "dimensions": {
            name: compact(value)
            for name, value in document["stability"]["dimensions"].items()
        },
        "dimension_order": list(document["stability"]["dimensions"]),
        "work": document["work"],
    }


def expected_view(
    windows,
    supplied,
    never,
    totals,
    selected,
    accounting,
    carry,
    breaks,
    contributing,
    without,
    dimensions,
    work,
):
    """Assemble pins only; every numerical argument comes from a hand derivation."""
    return {
        "windows": windows,
        "supplied": supplied,
        "never_run": never,
        "totals": totals,
        "selected": selected,
        "accounting": [
            (label, read, eligible, excluded, reasons, list(reasons), empty)
            for label, read, eligible, excluded, reasons, empty in accounting
        ],
        "carry": carry,
        "breaks": breaks,
        "stability_windows": contributing,
        "without_selection": without,
        "dimensions": {name: compact(value) for name, value in dimensions.items()},
        "dimension_order": list(dimensions),
        "work": work,
    }


def fixture_cases():
    """Hand-derived pins catch reversed direction, last-id ties, lost entries,
    status leakage, ordinal ranks, unexplained gaps, and normalised stability.

    Base: eligible values (1.5,3,3), (4,4,5), (3,1,4) select ids 1,3,3.
    Carried values 4 and 4 occupy tied positions 2/3 and unique position 1.
    Work: 12 rows + 3+3+3 triangular allowances + 3+3 lookups = 27.
    Variant pins below are derived from their changed rows and the analysis rules.
    """
    plan = ["w1", "w2", "w3"]
    base_pin = expected_view(
        plan,
        plan,
        [],
        (12, 9, 3),
        [
            selection(1, 3.0, 3, 0.5, False),
            selection(3, 5.0, 5, 0.75, True),
            selection(3, 4.0, 5, 0.75, True),
        ],
        [
            ("w1", 4, 3, 1, exclusion("status_pruned", [2]), False),
            ("w2", 4, 3, 1, exclusion("status_engine_error", [2]), False),
            ("w3", 4, 3, 1, exclusion("status_constraint_violation", [2]), False),
        ],
        [
            carry_pin("w1", "w2", 3, 0.5, False, 0, 4.0, (1, 2, Fraction(5, 2))),
            carry_pin("w2", "w3", 5, 0.75, True, 3, 4.0, (0, 1, Fraction(1))),
        ],
        [],
        plan,
        [],
        {
            "A": dimension("integer", [3, 5, 5], 2, 3, 5),
            "B": dimension("real", [0.5, 0.75, 0.75], 2, 0.5, 0.75),
            "C": dimension("boolean", [False, True, True], 2),
        },
        27,
    )
    cases = [
        Case("three finished windows", plan, base_rows(), texts=D_TEXTS, pins=base_pin)
    ]

    absent = base_rows()
    absent["w3"][3]["parameters"]["C"] = False
    pins = copy.deepcopy(base_pin)
    pins["selected"][2]["parameters"]["C"] = False
    pins["carry"][1] = carry_pin(
        "w2", "w3", 5, 0.75, True, None, None, (None, None, None), "no_row"
    )
    pins["dimensions"]["C"] = compact(dimension("boolean", [False, True, False], 2))
    cases.append(Case("carried vector absent", plan, absent, texts=D_TEXTS, pins=pins))

    pruned = base_rows()
    pruned["w3"][3].update(status="pruned", feasible=False)
    pins = copy.deepcopy(base_pin)
    pins.update(totals=(12, 8, 4), work=24)
    pins["selected"][2] = selection(0, 3.0, 3, 0.5, True)
    reasons = {
        **exclusion("status_constraint_violation", [2]),
        **exclusion("status_pruned", [3]),
    }
    pins["accounting"][2] = ("w3", 4, 2, 2, reasons, list(reasons), False)
    pins["carry"][1] = carry_pin(
        "w2", "w3", 5, 0.75, True, None, None, (None, None, None), "status_pruned"
    )
    pins["dimensions"].update(
        A=compact(dimension("integer", [3, 5, 3], 2, 3, 5)),
        B=compact(dimension("real", [0.5, 0.75, 0.5], 2, 0.5, 0.75)),
    )
    texts = (*D_TEXTS[:11], '["w3",3,"pruned",4.0]')
    cases.append(Case("carrying row pruned", plan, pruned, texts=texts, pins=pins))

    equal = base_rows()
    equal["w2"][1]["parameters"] = parameters(3, 0.5, False)
    cases.append(
        Case("equal carried duplicates", plan, equal, texts=D_TEXTS, pins=base_pin)
    )

    for labels, work, contributing in (
        (["w1", "w2"], 17, ["w1", "w2"]),
        (["w1", "w3"], 14, ["w1", "w3"]),
        (["w1"], 7, ["w1"]),
    ):
        pins = copy.deepcopy(base_pin)
        positions = [plan.index(label) for label in labels]
        pins.update(
            supplied=labels,
            never_run=[label for label in plan if label not in labels],
            totals=(4 * len(labels), 3 * len(labels), len(labels)),
            selected=[base_pin["selected"][position] for position in positions],
            accounting=[base_pin["accounting"][position] for position in positions],
            stability_windows=contributing,
            work=work,
        )
        if labels == ["w1", "w2"]:
            pins["carry"] = base_pin["carry"][:1]
        else:
            pins["carry"] = []
        if labels == ["w1", "w3"]:
            pins["breaks"] = [{"from": "w1", "to": "w3", "skipped": ["w2"]}]
        if len(labels) == 2:
            pins["dimensions"] = {
                "A": compact(dimension("integer", [3, 5], 2, 3, 5)),
                "B": compact(dimension("real", [0.5, 0.75], 2, 0.5, 0.75)),
                "C": compact(dimension("boolean", [False, True], 2)),
            }
        else:
            pins["dimensions"] = {
                "A": compact(dimension("integer", [3], 1, 3, 3)),
                "B": compact(dimension("real", [0.5], 1, 0.5, 0.5)),
                "C": compact(dimension("boolean", [False], 1)),
            }
        rows = base_rows()
        sources = {label: rows[label] for label in labels}
        texts = tuple(text for text in D_TEXTS if json.loads(text)[0] in labels)
        cases.append(
            Case("supplied " + ",".join(labels), plan, sources, texts=texts, pins=pins)
        )

    empty = base_rows()
    for identifier in (0, 1, 3):
        empty["w2"][identifier].update(status="pruned", feasible=False)
    pins = copy.deepcopy(base_pin)
    reasons = {
        **exclusion("status_engine_error", [2]),
        **exclusion("status_pruned", [0, 1, 3]),
    }
    pins.update(
        totals=(12, 6, 6),
        work=18,
        selected=[base_pin["selected"][0], None, base_pin["selected"][2]],
        stability_windows=["w1", "w3"],
        without_selection=["w2"],
    )
    pins["accounting"][1] = ("w2", 4, 0, 4, reasons, list(reasons), True)
    pins["carry"] = [
        carry_pin(
            "w1", "w2", 3, 0.5, False, None, None, (None, None, None), "status_pruned"
        )
    ]
    pins["dimensions"] = {
        "A": compact(dimension("integer", [3, 5], 2, 3, 5)),
        "B": compact(dimension("real", [0.5, 0.75], 2, 0.5, 0.75)),
        "C": compact(dimension("boolean", [False, True], 2)),
    }
    texts = (
        *D_TEXTS[:4],
        '["w2",0,"pruned",4.0]',
        '["w2",1,"pruned",4.0]',
        D_TEXTS[6],
        '["w2",3,"pruned",5.0]',
        *D_TEXTS[8:],
    )
    cases.append(
        Case(
            "middle supplied window without selection",
            plan,
            empty,
            texts=texts,
            pins=pins,
        )
    )
    reversed_empty = copy.deepcopy(empty)
    reversed_empty["w2"].reverse()
    cases.append(
        Case(
            "excluded ids reverse input order",
            plan,
            reversed_empty,
            texts=texts,
            pins=pins,
        )
    )

    small = {
        "w1": [row(0, "ok", 1.0, 1, 0.0, True)],
        "w2": [
            row(0, "engine_error", None, 1, 0.0, True),
            row(1, "pruned", 2.0, 4, 0.25, True),
        ],
    }
    reasons = {
        **exclusion("status_engine_error", [0]),
        **exclusion("status_pruned", [1]),
    }
    pins = expected_view(
        ["w1", "w2"],
        ["w1", "w2"],
        [],
        (3, 1, 2),
        [selection(0, 1.0, 1, 0.0, True), None],
        [("w1", 1, 1, 0, {}, False), ("w2", 2, 0, 2, reasons, True)],
        [
            carry_pin(
                "w1",
                "w2",
                1,
                0.0,
                True,
                None,
                None,
                (None, None, None),
                "status_engine_error",
            )
        ],
        [],
        ["w1"],
        ["w2"],
        {
            "A": dimension("integer", [1], 1, 1, 1),
            "B": dimension("real", [0.0], 1, 0.0, 0.0),
            "C": dimension("boolean", [True], 1),
        },
        3,
    )
    texts = (
        '["w1",0,"ok",1.0]',
        '["w2",0,"engine_error",null]',
        '["w2",1,"pruned",2.0]',
    )
    cases.append(
        Case(
            "target without eligible rows", ["w1", "w2"], small, texts=texts, pins=pins
        )
    )

    unrelated = {
        "w1": [row(0, "ok", 1.0, 1, 0.0, True)],
        "w2": [
            row(0, "ok", 2.0, 1, 0.0, True),
            row(1, "ok", 3.0, 2, 0.25, False),
            row(2, "ok", 3.5, 2, 0.25, False),
        ],
    }
    pins = expected_view(
        ["w1", "w2"],
        ["w1", "w2"],
        [],
        (4, 4, 0),
        [selection(0, 1.0, 1, 0.0, True), selection(2, 3.5, 2, 0.25, False)],
        [("w1", 1, 1, 0, {}, False), ("w2", 3, 3, 0, {}, False)],
        [carry_pin("w1", "w2", 1, 0.0, True, 0, 2.0, (2, 1, Fraction(3)))],
        [],
        ["w1", "w2"],
        [],
        {
            "A": dimension("integer", [1, 2], 2, 1, 2),
            "B": dimension("real", [0.0, 0.25], 2, 0.0, 0.25),
            "C": dimension("boolean", [True, False], 2),
        },
        10,
    )
    texts = (
        '["w1",0,"ok",1.0]',
        '["w2",0,"ok",2.0]',
        '["w2",1,"ok",3.0]',
        '["w2",2,"ok",3.5]',
    )
    cases.append(
        Case(
            "unrelated conflicting duplicates",
            ["w1", "w2"],
            unrelated,
            texts=texts,
            pins=pins,
        )
    )

    recorded_pin = copy.deepcopy(base_pin)
    recorded_pin.update(totals=(12, 6, 6), work=19)
    recorded_pin["selected"][2] = selection(0, 3.0, 3, 0.5, True)
    reasons_by_window = [
        {**exclusion("status_pruned", [2]), **exclusion("metric_not_recorded", [3])},
        {
            **exclusion("status_engine_error", [2]),
            **exclusion("metric_unavailable", [0]),
        },
        {
            **exclusion("status_constraint_violation", [2]),
            **exclusion("metric_not_recorded", [3]),
        },
    ]
    recorded_pin["accounting"] = [
        (label, 4, 2, 2, reasons, list(reasons), False)
        for label, reasons in zip(plan, reasons_by_window)
    ]
    recorded_pin["carry"] = [
        carry_pin(
            "w1",
            "w2",
            3,
            0.5,
            False,
            None,
            None,
            (None, None, None),
            "metric_unavailable",
        ),
        carry_pin(
            "w2",
            "w3",
            5,
            0.75,
            True,
            None,
            None,
            (None, None, None),
            "metric_not_recorded",
        ),
    ]
    recorded_pin["dimensions"].update(
        A=compact(dimension("integer", [3, 5, 3], 2, 3, 5)),
        B=compact(dimension("real", [0.5, 0.75, 0.5], 2, 0.5, 0.75)),
    )
    cases.append(
        Case(
            "recorded metric maximize",
            plan,
            recorded_rows(),
            metric=METRIC,
            texts=E_TEXTS,
            pins=recorded_pin,
        )
    )
    pins = copy.deepcopy(recorded_pin)
    pins["selected"] = [
        selection(0, 2.0, 2, 0.25, True),
        selection(1, 4.0, 2, 0.25, True),
        selection(1, 1.0, 1, 0.25, False),
    ]
    pins["carry"] = [
        carry_pin("w1", "w2", 2, 0.25, True, 1, 4.0, (0, 1, Fraction(1))),
        carry_pin("w2", "w3", 2, 0.25, True, None, None, (None, None, None), "no_row"),
    ]
    pins["dimensions"] = {
        "A": compact(dimension("integer", [2, 2, 1], 2, 1, 2)),
        "B": compact(dimension("real", [0.25, 0.25, 0.25], 1, 0.25, 0.25)),
        "C": compact(dimension("boolean", [True, True, False], 2)),
    }
    cases.append(
        Case(
            "recorded metric minimize",
            plan,
            recorded_rows(),
            metric=METRIC,
            direction="minimize",
            texts=E_TEXTS,
            pins=pins,
        )
    )

    real_plan = ["w1", "w2", "w3", "w4"]
    real_rows = {
        label: [row(0, "ok", value, 1, b, True)]
        for label, value, b in zip(real_plan, (1.0, 2.0, 3.0, 4.0), (1, 1.0, -0.0, 0.0))
    }
    pins = expected_view(
        real_plan,
        real_plan,
        [],
        (4, 4, 0),
        [
            selection(0, 1.0, 1, 1, True),
            selection(0, 2.0, 1, 1.0, True),
            selection(0, 3.0, 1, -0.0, True),
            selection(0, 4.0, 1, 0.0, True),
        ],
        [(label, 1, 1, 0, {}, False) for label in real_plan],
        [
            carry_pin("w1", "w2", 1, 1.0, True, 0, 2.0, (0, 1, Fraction(1))),
            carry_pin(
                "w2", "w3", 1, 1.0, True, None, None, (None, None, None), "no_row"
            ),
            carry_pin("w3", "w4", 1, 0.0, True, 0, 4.0, (0, 1, Fraction(1))),
        ],
        [],
        real_plan,
        [],
        {
            "A": dimension("integer", [1, 1, 1, 1], 1, 1, 1),
            "B": dimension("real", [1, 1.0, -0.0, 0.0], 2, -0.0, 1),
            "C": dimension("boolean", [True, True, True, True], 1),
        },
        7,
    )
    texts = (
        '["w1",0,"ok",1.0]',
        '["w2",0,"ok",2.0]',
        '["w3",0,"ok",3.0]',
        '["w4",0,"ok",4.0]',
    )
    cases.append(
        Case(
            "recorded types and signed zero",
            real_plan,
            real_rows,
            texts=texts,
            pins=pins,
        )
    )

    for bound in (27, 30):
        cases.append(
            Case(
                f"admission bound {bound}",
                plan,
                base_rows(),
                max_work=bound,
                texts=D_TEXTS,
                pins=base_pin,
            )
        )
    counters = base_rows()
    for window_number, entries in enumerate(counters.values(), 1):
        for entry in entries:
            if entry["status"] == "ok":
                entry["backtest"] = dict.fromkeys(COUNTERS, 100 * window_number)
    cases.append(
        Case(
            "different contexts across windows",
            plan,
            counters,
            texts=D_TEXTS,
            pins=base_pin,
        )
    )
    hashed = base_rows()
    for entries in hashed.values():
        for entry in entries:
            entry["space_hash"] = "aa"
    cases.append(
        Case("equal recorded hashes", plan, hashed, texts=D_TEXTS, pins=base_pin)
    )
    hash_only = base_rows()
    for entry in hash_only["w1"]:
        entry.pop("space")
        entry["space_hash"] = "aa"
    cases.append(
        Case(
            "one hash without recorded space",
            plan,
            hash_only,
            texts=D_TEXTS,
            pins=base_pin,
        )
    )
    single_pin = copy.deepcopy(cases[6].pins)
    single_pin.update(windows=["w1"], never_run=[])
    cases.append(
        Case(
            "one-window plan",
            ["w1"],
            {"w1": base_rows()["w1"]},
            texts=D_TEXTS[:4],
            pins=single_pin,
        )
    )

    # Adjacent integers 2**62 + 1 and 2**62 have distinct integer identities.
    # Exact comparison selects the second as minimum and the first as maximum.
    # A float comparison ties them and wrongly keeps the first as minimum.
    # Each window selects its only row; the changed A prevents carry-over.
    # Work is 2 rows + 0 triangular allowance + 1 target lookup = 3.
    large_plan = ["w1", "w2"]
    large_texts = ('["w1",0,"ok",1.0]', '["w2",0,"ok",2.0]')
    integer_space = copy.deepcopy(SPACE)
    integer_space["parameters"]["A"]["high"] = 4611686018427387905
    integer_rows = {
        "w1": [row(0, "ok", 1.0, 4611686018427387905, 0.0, True, space=integer_space)],
        "w2": [row(0, "ok", 2.0, 4611686018427387904, 0.0, True, space=integer_space)],
    }
    pins = expected_view(
        large_plan,
        large_plan,
        [],
        (2, 2, 0),
        [
            selection(0, 1.0, 4611686018427387905, 0.0, True),
            selection(0, 2.0, 4611686018427387904, 0.0, True),
        ],
        [("w1", 1, 1, 0, {}, False), ("w2", 1, 1, 0, {}, False)],
        [
            carry_pin(
                "w1",
                "w2",
                4611686018427387905,
                0.0,
                True,
                None,
                None,
                (None, None, None),
                "no_row",
            )
        ],
        [],
        large_plan,
        [],
        {
            "A": dimension(
                "integer",
                [4611686018427387905, 4611686018427387904],
                2,
                4611686018427387904,
                4611686018427387905,
            ),
            "B": dimension("real", [0.0, 0.0], 1, 0.0, 0.0),
            "C": dimension("boolean", [True, True], 1),
        },
        3,
    )
    cases.append(
        Case(
            "integer extrema above binary64 precision",
            large_plan,
            integer_rows,
            texts=large_texts,
            pins=pins,
        )
    )

    # The int 2**53 + 1 rounds ties-to-even to 2**53 for real typed identity.
    # Thus distinct is 1 and the carried row is found with rank 1, but exact
    # comparison gives the float 2**53 as minimum and the int as maximum.
    # Float-key extrema wrongly keep the first int as minimum; converting the
    # recorded values to floats loses the maximum. Work is again 2 + 0 + 1 = 3.
    real_space = copy.deepcopy(SPACE)
    real_space["parameters"]["B"]["high"] = 9007199254740994.0
    large_real_rows = {
        "w1": [row(0, "ok", 1.0, 1, 9007199254740993, True, space=real_space)],
        "w2": [row(0, "ok", 2.0, 1, 9007199254740992.0, True, space=real_space)],
    }
    pins = expected_view(
        large_plan,
        large_plan,
        [],
        (2, 2, 0),
        [
            selection(0, 1.0, 1, 9007199254740993, True),
            selection(0, 2.0, 1, 9007199254740992.0, True),
        ],
        [("w1", 1, 1, 0, {}, False), ("w2", 1, 1, 0, {}, False)],
        [
            carry_pin(
                "w1", "w2", 1, 9007199254740992.0, True, 0, 2.0, (0, 1, Fraction(1))
            )
        ],
        [],
        large_plan,
        [],
        {
            "A": dimension("integer", [1, 1], 1, 1, 1),
            "B": dimension(
                "real",
                [9007199254740993, 9007199254740992.0],
                1,
                9007199254740992.0,
                9007199254740993,
            ),
            "C": dimension("boolean", [True, True], 1),
        },
        3,
    )
    cases.append(
        Case(
            "real extrema with shared binary64 identity",
            large_plan,
            large_real_rows,
            texts=large_texts,
            pins=pins,
        )
    )
    assert tuple(case.name for case in cases) == FIXTURE_NAMES
    return cases


@dataclass
class RefusalCase:
    case: Case
    code: str
    ids: tuple = ()
    first_names: tuple = ()

    def expected(self):
        return f"walkforward_{self.code}", self.ids, self.first_names


def refusal_cases():
    """One-defect inputs with pinned codes, ids, and first name.

    These catch missing checks, accepting carried conflicting duplicates, applying
    a direction check to a recorded metric, keying only selected rows, ignoring
    cross-window spaces/hashes, and treating the admission bound as inclusive.
    They make no assertion about precedence between simultaneous defects.
    """
    plan = ["w1", "w2", "w3"]
    cases = []
    for name in ("windows", "sources", "metric", "direction"):
        case = Case(f"None {name}", plan, base_rows())
        setattr(case, name, None)
        cases.append(RefusalCase(case, "parameter_missing", first_names=(name,)))
    for name, value in (
        ("windows", []),
        ("windows", "w1"),
        ("windows", ["w1", ""]),
        ("windows", ["w1", "w1"]),
        ("sources", [("w1", "r.json")]),
        ("metric", 7),
        ("max_work", 0),
        ("space", 42),
        ("direction", "best"),
    ):
        case = Case(f"invalid {name} {value!r}", plan, base_rows())
        setattr(case, name, value)
        cases.append(RefusalCase(case, "parameter_invalid", first_names=(name,)))
    cases.extend(
        [
            RefusalCase(
                Case("unknown window", ["w1"], {"w2": []}),
                "window_unknown",
                first_names=("w2",),
            ),
            RefusalCase(Case("no supplied source", ["w1"], {}), "no_supplied_sources"),
            RefusalCase(
                Case(
                    "incomplete result",
                    ["w1"],
                    {"w1": {"trials_out": "best"}},
                    space=SPACE,
                ),
                "rows_incomplete",
                first_names=("w1",),
            ),
            RefusalCase(
                Case("malformed row", ["w1"], {"w1": {"trials": [{}]}}),
                "input_invalid",
                first_names=("w1",),
            ),
            RefusalCase(
                Case(
                    "objective direction differs",
                    plan,
                    base_rows(),
                    direction="minimize",
                ),
                "direction_mismatch",
                first_names=("direction",),
            ),
            RefusalCase(
                Case("unknown metric", plan, base_rows(), metric="metrics.nope.nope"),
                "metric_unknown",
                first_names=("metrics.nope.nope",),
            ),
            RefusalCase(
                Case("admission bound below 27", plan, base_rows(), max_work=25),
                "resource_exceeded",
            ),
        ]
    )
    no_space = base_rows()
    for entries in no_space.values():
        for entry in entries:
            entry.pop("space")
    cases.append(
        RefusalCase(
            Case("no recorded or passed space", plan, no_space), "space_missing"
        )
    )
    own_direction = row(0, "ok", 1.0, 1, 0.0, True)
    own_direction.pop("space")
    source = {"trials": [own_direction], "direction": "maximize"}
    cases.append(
        RefusalCase(
            Case(
                "result direction differs with passed space",
                ["w1"],
                {"w1": source},
                direction="minimize",
                space=SPACE,
            ),
            "direction_mismatch",
            first_names=("w1",),
        )
    )
    changed_space = base_rows()
    for entry in changed_space["w2"]:
        entry["space"]["parameters"]["A"]["high"] = 6
    cases.append(
        RefusalCase(
            Case("different recorded space", plan, changed_space),
            "space_mismatch",
            (0, 1, 2, 3),
            ("w1",),
        )
    )
    changed_context = base_rows()
    changed_context["w1"][0]["backtest"] = dict.fromkeys(COUNTERS, 100)
    changed_context["w1"][1]["backtest"] = {
        **dict.fromkeys(COUNTERS, 100),
        "input_bars_processed": 101,
    }
    cases.append(
        RefusalCase(
            Case("different counters inside a window", plan, changed_context),
            "context_mismatch",
            (0, 1),
            ("w1",),
        )
    )
    for changed_labels in (("w2",), ("w2", "w3")):
        changed_hash = base_rows()
        for label, entries in changed_hash.items():
            for entry in entries:
                entry["space_hash"] = "bb" if label in changed_labels else "aa"
        # Differing ids are reduced from row identities, preserving multiplicity.
        ids = (0, 1, 2, 3) if len(changed_labels) == 1 else (0, 0, 1, 1, 2, 2, 3, 3)
        cases.append(
            RefusalCase(
                Case(
                    "different hashes " + ",".join(changed_labels), plan, changed_hash
                ),
                "space_mismatch",
                ids,
                ("w1",),
            )
        )
    for label, identifier, value in (
        ("w1", 0, True),
        ("w2", 1, "x"),
        ("w2", 2, math.inf),
        ("w2", 2, {1}),
    ):
        bad_cell = recorded_rows()
        bad_cell[label][identifier]["metrics"] = {METRIC: value}
        cases.append(
            RefusalCase(
                Case(
                    f"invalid metric cell {label}/{identifier} {value!r}",
                    plan,
                    bad_cell,
                    metric=METRIC,
                ),
                "input_invalid",
                (identifier,),
                (label,),
            )
        )
    for identifier in (1, 2):
        absent_parameters = base_rows()
        absent_parameters["w1"][identifier].pop("parameters")
        cases.append(
            RefusalCase(
                Case(f"missing parameters row {identifier}", plan, absent_parameters),
                "input_invalid",
                (identifier,),
                ("w1",),
            )
        )
    wrong_names = base_rows()
    wrong_names["w1"][2]["parameters"].pop("C")
    cases.append(
        RefusalCase(
            Case("incomplete non-selected vector", plan, wrong_names),
            "input_invalid",
            (2,),
            ("w1",),
        )
    )
    duplicates = base_rows()
    duplicates["w2"][1].update(objective=4.5, parameters=parameters(3, 0.5, False))
    cases.append(
        RefusalCase(
            Case("conflicting carried duplicates", plan, duplicates),
            "duplicate_vector",
            (0, 1),
            ("w2",),
        )
    )
    all_excluded = {
        "w1": [row(0, "pruned", 1.0, 1, 0.0, True)],
        "w2": [
            row(0, "engine_error", None, 1, 0.0, True),
            row(1, "pruned", 2.0, 4, 0.25, True),
        ],
    }
    cases.append(
        RefusalCase(
            Case("no eligible row in any window", ["w1", "w2"], all_excluded),
            "no_eligible_rows",
        )
    )
    assert {refusal.code for refusal in cases} == set(REFUSAL_CODES)
    counts = tuple(
        sum(refusal.code == code for refusal in cases) for code in REFUSAL_CODES
    )
    assert counts == REFUSAL_MULTIPLICITIES
    return cases


def unknown_label_cases():
    """Equal text sorts by type name: int precedes str in both mapping orders.

    Full names reject reversed or missing unknown labels.
    """
    return (
        (
            Case("unknown labels, integer first", ["w1"], {1: 42, "1": 42}),
            ("walkforward_window_unknown", (), (1, "1")),
        ),
        (
            Case("unknown labels, string first", ["w1"], {"1": 42, 1: 42}),
            ("walkforward_window_unknown", (), (1, "1")),
        ),
    )


def oracle_outcome(case, *, all_names=False):
    try:
        return oracle(case)
    except OracleRefusal as error:
        if all_names:
            return error.code, error.ids, error.names
        return error.view()


def phase_one_fixtures(ledger, cases):
    ledger.begin("phase 1: hand-derived fixture values")
    documents = []
    for case in cases:
        document = oracle(case)
        documents.append(document)
        ledger.compare("fixture", case.name, case.pins, pin_view(document))
    ledger.end(FIXTURE_CASE_COUNT)
    return documents


def phase_one_literals(ledger, cases):
    """Check the literal preimages against independently read inline row columns.

    A SHA-256 has no hand-derived numeric pin. The literal preimage text is the
    evidence; hashing it prepares the expected phase-2 digest, not a self-pass.
    """
    ledger.begin("phase 1: literal digest preimages")
    for case in cases:
        rows, _, _ = read_inline(case)
        from_literals = sorted(
            (json.loads(text) for text in case.texts),
            key=lambda item: (item[0], item[1]),
        )
        columns = []
        for label, entries in rows.items():
            for entry in entries:
                cell = cell_of(entry, case.metric)
                value = None if cell is ABSENT or cell is None else float(cell)
                columns.append([label, entry["trial_id"], entry["status"], value])
        columns.sort(key=lambda item: (item[0], item[1]))
        ledger.compare(
            "fixture", f"{case.name}: preimage columns", columns, from_literals
        )
    raw_digests = [hashlib.sha256(text.encode("ascii")).digest() for text in D_TEXTS]
    ledger.check(
        "internal",
        "literal digest read order differs from raw-byte sort",
        raw_digests != sorted(raw_digests),
    )
    ledger.end(FIXTURE_CASE_COUNT + 1)


def phase_one_refusals(ledger, refusals):
    ledger.begin("phase 1: one-defect refusal table")
    for refusal in refusals:
        ledger.compare(
            "fixture",
            refusal.case.name,
            refusal.expected(),
            oracle_outcome(refusal.case),
        )
    ledger.end(REFUSAL_CASE_COUNT)

    ledger.begin("phase 1: unknown labels with equal text")
    unknown_labels = unknown_label_cases()
    for case, expected in unknown_labels:
        ledger.compare(
            "fixture", case.name, expected, oracle_outcome(case, all_names=True)
        )
    ledger.end(len(unknown_labels))


def one_sided_controls(ledger, cases, documents, reference=None, never_reference=None):
    """Five analysis controls mutate only an oracle input/output copy.

    Reference is unmodified pinned/oracle data in phase 1 and an unmodified module
    document in phase 2. No control edits the reference or module input.
    """
    ledger.begin(
        "one-sided controls"
        + (" against module" if reference is not None else " against pins")
    )
    base_case = cases[0]
    base = documents[0]
    baseline_id = (
        3 if reference is None else reference["per_window"][1]["selected"]["trial_id"]
    )
    baseline_rank = (
        Fraction(5, 2) if reference is None else reference["carry_over"][0]["rank_mid"]
    )
    baseline_digest = (
        base["input_sha256"] if reference is None else reference["input_sha256"]
    )
    baseline_work = 27 if reference is None else reference["work"]

    changed = copy.deepcopy(base_case)
    changed.sources["w2"][3]["objective"] = 4.0
    changed.texts = (*D_TEXTS[:7], '["w2",3,"ok",4.0]', *D_TEXTS[8:])
    new_id = oracle(changed)["per_window"][1]["selected"]["trial_id"]
    ledger.compare("control", "objective 5 to 4 selects lowest tied id", 0, new_id)
    ledger.check(
        "control",
        "changed selection disagrees with unchanged reference",
        not same(new_id, baseline_id),
    )

    changed_rank = copy.deepcopy(base)
    changed_rank["carry_over"][0]["rank_mid"] = Fraction(1)
    ordinal = changed_rank["carry_over"][0]["rank_mid"]
    ledger.check(
        "control",
        "ordinal rank disagrees with unchanged mid-rank",
        not same(ordinal, baseline_rank),
    )
    ledger.compare(
        "control",
        "ordinal rank differs by exactly 1.5",
        Fraction(3, 2),
        Fraction(baseline_rank) - ordinal,
    )

    changed_texts = list(base_case.texts)
    changed_texts[0] = '[0,"ok",1.5]'
    changed_digest = digest_texts(changed_texts)
    ledger.check(
        "control",
        "one missing literal label changes the digest",
        changed_digest != baseline_digest,
    )

    one_source = next(case for case in cases if case.name == "supplied w1")
    changed_never = copy.deepcopy(oracle(one_source))
    changed_never["never_run"].pop()
    ledger.check(
        "control",
        "shortened never-run list disagrees with its pin",
        not same(
            changed_never["never_run"],
            one_source.pins["never_run"]
            if never_reference is None
            else never_reference,
        ),
    )

    counts = [block["rows_eligible"] for block in base["per_window"]]
    changed_work = base["rows_read"] + sum(count * (count - 1) for count in counts)
    changed_work += sum(
        counts[base_case.windows.index(entry["to"])] for entry in base["carry_over"]
    )
    ledger.compare("control", "ordered pair allowance produces 36", 36, changed_work)
    ledger.check(
        "control",
        "ordered allowance disagrees with unchanged work",
        not same(changed_work, baseline_work),
    )
    ledger.end(8)


def moved_case(case, rng=None, *, reverse_plan=False):
    moved = copy.deepcopy(case)
    moved.sources = dict(reversed(list(moved.sources.items())))
    for entries in moved.sources.values():
        if rng is None:
            entries.reverse()
        else:
            rng.shuffle(entries)
    if reverse_plan:
        moved.windows = list(reversed(moved.windows))
    return moved


def phase_one_signed_zero_control(ledger, cases, documents):
    """A target's positive zero must not be replaced by the source's negative zero."""
    ledger.begin("phase 1: one-sided signed-zero control")
    index = next(
        index
        for index, case in enumerate(cases)
        if case.name == "recorded types and signed zero"
    )
    reference = documents[index]
    changed = copy.deepcopy(reference)
    changed["carry_over"][2]["parameters"]["B"] = -0.0
    ledger.check(
        "control",
        "changed carry parameter zero sign disagrees with unchanged reference",
        not same(reference, changed),
    )
    ledger.end(1)


def phase_one_determinism(ledger, case, document):
    """Reordering must keep the digest and row choice, but plan order changes pairs."""
    ledger.begin("phase 1: ordering and repeated calls")
    ledger.compare(
        "internal", "rows and mapping reversed", document, oracle(moved_case(case))
    )
    ledger.compare(
        "internal", "repeated independent computation", document, oracle(case)
    )
    reversed_case = moved_case(case, reverse_plan=True)
    reversed_document = oracle(reversed_case)
    ledger.compare(
        "fixture",
        "reversed plan per-window order",
        ["w3", "w2", "w1"],
        [block["window"] for block in reversed_document["per_window"]],
    )
    ledger.compare(
        "fixture",
        "reversed plan ranks and missing carry",
        [
            carry_pin("w3", "w2", 5, 0.75, True, 3, 5.0, (0, 1, Fraction(1))),
            carry_pin(
                "w2", "w1", 5, 0.75, True, None, None, (None, None, None), "no_row"
            ),
        ],
        pin_view(reversed_document)["carry"],
    )
    ledger.check(
        "internal",
        "plan reversal changes the computed document",
        not same(document, reversed_document),
    )
    ledger.end(5)


def random_cases(seed, count):
    """Legal small random plans with gaps, empty studies, ties and both directions.

    Only objective cells are randomised. Within one window a vector has one value,
    so duplicate conflicts cannot accidentally turn an accepted random case into
    a refusal. Refusal cases have their separate hand-derived table.
    """
    rng = random.Random(seed)
    cases = []
    numeric = (-0.0, 0.0, -2.0, 1.0, 1.0 + 2.0**-52, 2.5, 4.0)
    for number in range(count):
        direction = "maximize" if number % 2 == 0 else "minimize"
        space = copy.deepcopy(SPACE)
        space["objective"]["direction"] = direction
        space["parameters"]["A"]["high"] = rng.randint(2, 5)
        if number % 2:
            space["parameters"]["D"] = {"kind": "categorical", "choices": ["red", "青"]}
        plan = [f"period-{number}-{position}" for position in range(rng.randint(3, 8))]
        rng.shuffle(plan)
        supplied = [label for label in plan if rng.random() < 0.7]
        supplied = list(dict.fromkeys([plan[0], *supplied]))
        if number % 4 == 0:
            supplied = [label for label in supplied if label != plan[1]]
        if number % 5 == 0:
            supplied.append(plan[-1])
            supplied = list(dict.fromkeys(supplied))
        supplied = [label for label in plan if label in supplied]
        pool = []
        for _ in range(rng.randint(2, 6)):
            vector = parameters(
                rng.randint(1, space["parameters"]["A"]["high"]),
                rng.choice((0, -0.0, 0.0, 0.25, 0.5, 0.75, 1, 1.0)),
                rng.choice((False, True)),
            )
            if "D" in space["parameters"]:
                vector["D"] = rng.choice(("red", "青"))
            pool.append(vector)
        sources = {}
        for position, label in enumerate(supplied):
            values = {lookup_key(vector, space): rng.choice(numeric) for vector in pool}
            # Two guaranteed equal eligible cells, possibly on duplicate vectors.
            tie = rng.choice(numeric)
            values[lookup_key(pool[0], space)] = tie
            values[lookup_key(pool[1], space)] = tie
            entries = []
            total = rng.randint(0, 12)
            if position == 0:
                total = max(total, 3)
            if position == 1 and number % 3 == 0:
                total = 0
            for identifier in range(total):
                vector = copy.deepcopy(
                    pool[identifier % len(pool)] if identifier < 2 else rng.choice(pool)
                )
                status = rng.choice(STATUSES)
                if position == 0 and identifier < 2:
                    status = "ok"
                elif position == 0 and identifier == 2:
                    status = STATUSES[number % len(STATUSES)]
                objective = values[lookup_key(vector, space)]
                if (
                    status not in ("ok", "constraint_violation", "pruned")
                    and rng.random() < 0.5
                ):
                    objective = None
                entry = {
                    "trial_id": identifier,
                    "status": status,
                    "feasible": status == "ok",
                    "objective": objective,
                    "parameters": vector,
                    "space": copy.deepcopy(space),
                }
                entries.append(entry)
            rng.shuffle(entries)
            sources[label] = entries
        case = Case(f"random {number}", plan, sources, direction=direction)
        cases.append(case)
    return cases


def phase_one_random(ledger, cases, seed, expected_count):
    ledger.begin(f"phase 1: seeded random invariance (seed {seed})")
    rng = random.Random(seed + 1)
    documents = []
    for case in cases:
        document = oracle(case)
        documents.append(document)
        ledger.compare(
            "internal",
            f"{case.name}: shuffled rows and mapping",
            document,
            oracle(moved_case(case, rng)),
        )
    ledger.end(expected_count)
    ledger.begin("random coverage, checked from inputs")
    all_rows = [
        entry
        for case in cases
        for entries in case.sources.values()
        for entry in entries
    ]
    ledger.compare(
        "internal",
        "both requested directions present",
        {"maximize", "minimize"},
        {case.direction for case in cases},
    )
    ledger.check(
        "internal",
        "never-run labels present",
        any(set(case.windows) - set(case.sources) for case in cases),
    )
    ledger.check(
        "internal",
        "supplied empty windows present",
        any(not entries for case in cases for entries in case.sources.values()),
    )
    ledger.compare(
        "internal",
        "all nine statuses present",
        set(STATUSES),
        {entry["status"] for entry in all_rows},
    )
    ledger.check(
        "internal",
        "exact eligible ties present",
        any(
            len([entry for entry in entries if entry["status"] == "ok"])
            > len(
                {
                    exact_value(entry["objective"])
                    for entry in entries
                    if entry["status"] == "ok"
                }
            )
            for case in cases
            for entries in case.sources.values()
        ),
    )
    ledger.end(5)
    return documents


def module_call(module, case, *, all_names=False):
    """Only the actual side calls the public function, on a private input copy."""
    supplied_case = copy.deepcopy(case)
    try:
        return module.walkforward(
            supplied_case.windows,
            supplied_case.sources,
            metric=supplied_case.metric,
            direction=supplied_case.direction,
            space=supplied_case.space,
            max_work=supplied_case.max_work,
        )
    except module.WalkforwardError as error:
        if all_names:
            return error.code, tuple(error.ids), tuple(error.names)
        return error.code, tuple(error.ids), tuple(error.names[:1])


def comparison_view(document, *, digest):
    """Keep every compared field, remove only the expressly unclaimed fields.

    Dimension objects become compact text; ranks remain numbers so same() converts
    a module float to Fraction and checks the exact half-integer. Unknown or missing
    document keys remain observable rather than silently filtered out.
    """
    if not isinstance(document, dict):
        return document
    result = copy.deepcopy(document)
    result.pop("interpretation", None)
    carry = result.get("carry_over")
    if isinstance(carry, list):
        for entry in carry:
            if isinstance(entry, dict):
                entry.pop("vector_key", None)
    if not digest:
        result.pop("input_sha256", None)
    stability = result.get("stability")
    if isinstance(stability, dict) and isinstance(stability.get("dimensions"), dict):
        stability["dimensions"] = {
            name: compact(value) for name, value in stability["dimensions"].items()
        }
    return result


def ordering_view(document):
    if not isinstance(document, dict):
        return MISSING
    try:
        return (
            [list(block["excluded"]) for block in document["per_window"]],
            list(document["stability"]["dimensions"]),
        )
    except (KeyError, TypeError):
        return MISSING


def compare_module_document(ledger, case, expected, found):
    ledger.compare(
        "module",
        case.name,
        comparison_view(expected, digest=bool(case.texts)),
        comparison_view(found, digest=bool(case.texts)),
    )
    ledger.compare(
        "module",
        f"{case.name}: exclusion and dimension key order",
        ordering_view(expected),
        ordering_view(found),
    )
    ledger.check(
        "module",
        f"{case.name}: interpretation present as nonempty text",
        isinstance(found, dict)
        and isinstance(found.get("interpretation"), str)
        and bool(found["interpretation"]),
    )


def phase_two_signature(ledger, module):
    """Missing arguments must be TypeError, not module-local missing-value errors."""
    ledger.begin("module: required arguments")
    options = {"metric": "objective", "direction": "maximize"}
    calls = (
        ((), options),
        ((["w1"],), options),
        ((["w1"], {"w1": []}), {"direction": "maximize"}),
        ((["w1"], {"w1": []}), {"metric": "objective"}),
    )
    for index, (positional, keywords) in enumerate(calls):
        try:
            module.walkforward(*positional, **keywords)
        except TypeError:
            found = "TypeError"
        except module.WalkforwardError as error:
            found = error.code
        else:
            found = "returned"
        ledger.compare(
            "module", f"required argument omission {index}", "TypeError", found
        )
    ledger.end(4)


def planted_module_document_control(ledger, case, expected, changed, label):
    """Require the complete module-document comparison to record the planted fault."""
    probe = Ledger(echo=False)
    probe.begin(label)
    compare_module_document(probe, case, expected, changed)
    probe.end(3)
    ledger.check(
        "control",
        label,
        probe.made == 3 and probe.failures == [f"{label}: {case.name}"],
    )


def phase_two(
    ledger,
    module,
    cases,
    documents,
    refusals,
    random_inputs,
    random_documents,
    seed,
    expected_count,
):
    print("\n##### phase 2: compare the public module with the independent oracle")
    ledger.begin("module: worked fixtures")
    base_actual = None
    for case, expected in zip(cases, documents):
        found = module_call(module, case)
        compare_module_document(ledger, case, expected, found)
        if case is cases[0] and isinstance(found, dict):
            base_actual = found
    ledger.end(3 * FIXTURE_CASE_COUNT)
    ledger.begin("module: one-defect refusals")
    for refusal in refusals:
        ledger.compare(
            "module",
            refusal.case.name,
            refusal.expected(),
            module_call(module, refusal.case),
        )
    ledger.end(REFUSAL_CASE_COUNT)

    ledger.begin("module: unknown labels with equal text")
    unknown_labels = unknown_label_cases()
    for case, expected in unknown_labels:
        ledger.compare(
            "module", case.name, expected, module_call(module, case, all_names=True)
        )
    ledger.end(len(unknown_labels))
    phase_two_signature(ledger, module)

    ledger.begin("module: determinism and plan reversal")
    base_case = cases[0]
    for variant in (
        moved_case(base_case),
        copy.deepcopy(base_case),
        moved_case(base_case, reverse_plan=True),
    ):
        compare_module_document(
            ledger, variant, oracle(variant), module_call(module, variant)
        )
    ledger.end(9)

    ledger.begin("module: changed three-way tie")
    changed = copy.deepcopy(base_case)
    changed.sources["w2"][3]["objective"] = 4.0
    changed.texts = (*D_TEXTS[:7], '["w2",3,"ok",4.0]', *D_TEXTS[8:])
    compare_module_document(
        ledger,
        changed,
        oracle(changed),
        module_call(module, changed),
    )
    ledger.end(3)

    if base_actual is not None:
        try:
            one_source = next(case for case in cases if case.name == "supplied w1")
            never_actual = module_call(module, one_source)
            one_sided_controls(
                ledger, cases, documents, base_actual, never_actual["never_run"]
            )
        except (KeyError, TypeError, ValueError) as error:
            ledger.failures.append(
                f"module controls could not inspect document: {error}"
            )
            ledger.end(8)
    else:
        ledger.begin("one-sided controls against module")
        ledger.end(8)

    ledger.begin("module-document one-sided controls")
    if base_actual is not None:
        changed = copy.deepcopy(base_actual)
        changed["carry_over"][0]["rank_mid"] = 1.0
        planted_module_document_control(
            ledger,
            base_case,
            documents[0],
            changed,
            "planted module ordinal rank records DISAGREE",
        )
    zero_index = next(
        index
        for index, case in enumerate(cases)
        if case.name == "recorded types and signed zero"
    )
    zero_case = cases[zero_index]
    zero_actual = module_call(module, zero_case)
    if isinstance(zero_actual, dict):
        changed = copy.deepcopy(zero_actual)
        changed["carry_over"][2]["parameters"]["B"] = -0.0
        planted_module_document_control(
            ledger,
            zero_case,
            documents[zero_index],
            changed,
            "planted module carry zero sign records DISAGREE",
        )
    ledger.end(2)

    ledger.begin(f"module: seeded random plans (seed {seed}), original and shuffled")
    rng = random.Random(seed + 1)
    for case, expected in zip(random_inputs, random_documents):
        compare_module_document(ledger, case, expected, module_call(module, case))
        moved = moved_case(case, rng)
        compare_module_document(ledger, moved, expected, module_call(module, moved))
    ledger.end(6 * expected_count)


def summary(ledger, *, no_module=False, unavailable=False):
    print("\nNOT COMPARED")
    for item in NOT_COMPARED:
        print(f"  - {item}")
    if no_module or unavailable:
        print("  - Every module field: phase 2 was not compared.")
    print("\nSUMMARY")
    for kind in ("fixture", "internal", "control", "module"):
        print(f"  {kind} comparisons: {ledger.counts.get(kind, 0)}")
    print(f"  failed comparisons/checks: {len(ledger.failures)}")
    for failure in ledger.failures:
        print(f"  - {failure}")
    if ledger.failures:
        print("RESULT: FAIL (exit 1)")
        return 1
    if unavailable:
        print("RESULT: MODULE UNAVAILABLE (exit 2): phase 2 was not compared")
        return 2
    if no_module:
        print("RESULT: PHASE 1 ONLY (exit 3): module NOT compared; no full pass")
        return 3
    if ledger.counts.get("module", 0) == 0:
        print("RESULT: FAIL (exit 1): no module comparisons were made")
        return 1
    print("RESULT: PASS (exit 0): checker agrees with pins and compared module fields")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-module",
        action="store_true",
        help="phase 1 only, exit 3; no package import",
    )
    parser.add_argument(
        "--cases", type=int, default=100, help="random plans (at least 30; default 100)"
    )
    parser.add_argument(
        "--seed", type=int, default=20261012, help="seed (default 20261012)"
    )
    args = parser.parse_args(argv)
    if args.cases < 30:
        parser.error("--cases must be at least 30")
    ledger = Ledger()
    print(
        f"walk-forward oracle: cases {args.cases}, seed {args.seed}, "
        f"Python {sys.version.split()[0]}"
    )
    try:
        harness_self_test()
        print("##### phase 1: independent checker, no module imported")
        ledger.begin("phase 1: import independence")
        ledger.check(
            "internal",
            "no pineforge_hpo import",
            not any(
                name == "pineforge_hpo" or name.startswith("pineforge_hpo.")
                for name in sys.modules
            ),
        )
        ledger.end(1)
        cases = fixture_cases()
        refusals = refusal_cases()
        documents = phase_one_fixtures(ledger, cases)
        phase_one_literals(ledger, cases)
        phase_one_refusals(ledger, refusals)
        phase_one_determinism(ledger, cases[0], documents[0])
        one_sided_controls(ledger, cases, documents)
        phase_one_signed_zero_control(ledger, cases, documents)
        random_inputs = random_cases(args.seed, args.cases)
        random_documents = phase_one_random(
            ledger, random_inputs, args.seed, args.cases
        )
    except Exception as error:
        ledger.failures.append(f"oracle/harness error: {type(error).__name__}: {error}")
        return summary(ledger, no_module=True)
    if args.no_module:
        return summary(ledger, no_module=True)
    if ledger.failures:
        return summary(ledger, no_module=True)
    ledger.begin("phase 2: import boundary")
    ledger.check(
        "internal",
        "package still absent after all phase-1 comparisons",
        not any(
            name == "pineforge_hpo" or name.startswith("pineforge_hpo.")
            for name in sys.modules
        ),
    )
    ledger.end(1)
    if ledger.failures:
        return summary(ledger, no_module=True)
    sys.path.insert(0, str(ROOT / "python"))
    try:
        module = importlib.import_module("pineforge_hpo.walkforward")
    except ImportError as error:
        if (
            isinstance(error, ModuleNotFoundError)
            and error.name == "pineforge_hpo.walkforward"
        ):
            print(f"MODULE UNAVAILABLE: {error}")
            return summary(ledger, unavailable=True)
        ledger.failures.append(f"module import error: {type(error).__name__}: {error}")
        return summary(ledger)
    try:
        phase_two(
            ledger,
            module,
            cases,
            documents,
            refusals,
            random_inputs,
            random_documents,
            args.seed,
            args.cases,
        )
    except Exception as error:
        ledger.failures.append(
            f"module/comparison error: {type(error).__name__}: {error}"
        )
    return summary(ledger)


if __name__ == "__main__":
    sys.exit(main())
