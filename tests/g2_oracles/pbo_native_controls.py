#!/usr/bin/env python3
"""Mutations of passing native rows, each required to reach its own named check."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pbo_real_rows as checker

CONTROL_NAMES = (
    "count_mismatch", "sum_one_ulp", "self_consistent_count", "boundary_time",
    "non_positive_equity", "m2_one_ulp", "null_payload",
)


def module_table(result: dict, size: int, roster: list, dataset_identity: str):
    sys.path.insert(0, str(checker.pbo_oracle.ROOT / "python"))
    from pineforge_hpo import pbo

    declared = {**result["return_blocks"], "dataset_identity": dataset_identity}
    return pbo.load_moment_table([(declared, result["trials"])], S=size, roster=roster)


def next_finite(value: float) -> float:
    changed = math.nextafter(float(value), math.inf)
    if not math.isfinite(changed):
        changed = math.nextafter(float(value), -math.inf)
    if not math.isfinite(changed) or changed == value:
        raise AssertionError("the one-ulp control needs a different finite neighbour")
    return changed


def make_controls(result: dict, derived: dict) -> list:
    """Make separate copies. One declared block count changes all linked row counts."""
    rows = result["trials"]
    first = min(range(len(rows)), key=lambda index: rows[index]["trial_id"])
    trial_id = rows[first]["trial_id"]
    layout = result["return_blocks"]
    blocks = derived[trial_id]["blocks"]
    count_block = min(range(len(blocks)), key=lambda index: blocks[index]["count"])
    flat_blocks = [index for index, block in enumerate(blocks) if block["A"] == 0]
    if not flat_blocks:
        raise AssertionError("the one-ulp sum-bound control needs a zero-return curve block")
    sum_block = flat_blocks[0]
    m2_blocks = [index for index, block in enumerate(blocks)
                 if block["count"] > 1 and block["M"] > 0]
    if not m2_blocks:
        raise AssertionError("the m2 control needs a block with count > 1 and positive exact M")
    m2_block = m2_blocks[0]
    controls = []

    def add(name, stage, code, changed, ids=()):
        controls.append({"name": name, "stage": stage, "code": code,
                         "result": changed, "ids": tuple(ids)})

    changed = copy.deepcopy(result)
    changed["trials"][first]["block_returns"]["count"][count_block] += 1
    add("count_mismatch", "module", "pbo_block_mismatch", changed, (trial_id,))

    changed = copy.deepcopy(result)
    changed["trials"][first]["block_returns"]["sum"][sum_block] = next_finite(
        rows[first]["block_returns"]["sum"][sum_block]
    )
    add("sum_one_ulp", "curve", "curve_sum_bound", changed)

    changed = copy.deepcopy(result)
    changed["return_blocks"]["n_b"][count_block] += 1
    changed["return_blocks"]["K"] += 1
    for row in changed["trials"]:
        row["block_returns"]["count"][count_block] += 1
    add("self_consistent_count", "curve", "curve_count", changed)

    changed = copy.deepcopy(result)
    times = layout["boundary_time_ms"]
    # Changing the final boundary by +1 always preserves non-decreasing order.
    changed["return_blocks"]["boundary_time_ms"][-1] = times[-1] + 1
    add("boundary_time", "curve", "curve_boundary", changed)

    changed = copy.deepcopy(result)
    changed["trials"][first]["block_returns"]["status"] = "non_positive_equity"
    add("non_positive_equity", "module", "pbo_non_positive_equity", changed, (trial_id,))

    changed = copy.deepcopy(result)
    changed["trials"][first]["block_returns"]["m2"][m2_block] = next_finite(
        rows[first]["block_returns"]["m2"][m2_block]
    )
    add("m2_one_ulp", "curve", "curve_m2_bits", changed)

    changed = copy.deepcopy(result)
    changed["trials"][first]["block_returns"] = None
    add("null_payload", "module", "pbo_rows_incomplete", changed, (trial_id,))
    return controls


def check_control(control: dict, curves: dict, size: int, roster: list,
                  dataset_identity: str) -> dict:
    """Return evidence only from the required exception; acceptance always fails."""
    from pineforge_hpo import pbo

    changed = control["result"]
    name, stage, intended = (control[key] for key in ("name", "stage", "code"))
    if stage == "module":
        try:
            module_table(changed, size, roster, dataset_identity)
        except pbo.PboError as error:
            if (error.code != intended or tuple(error.ids) != control["ids"]
                    or [item["code"] for item in error.refusals] != [intended]):
                raise AssertionError(f"{name}: wrong module refusal {error.code}, ids {error.ids}")
            observed = error.code
        else:
            raise AssertionError(f"{name}: the module accepted the mutation")
    elif stage == "curve":
        # A generic module refusal cannot count as this control's curve disagreement.
        module_table(changed, size, roster, dataset_identity)
        try:
            checker.check_curves(changed, changed["trials"], curves, size, roster=roster)
        except checker.CurveCheckError as error:
            if error.code != intended:
                raise AssertionError(f"{name}: wrong curve check {error.code}, expected {intended}")
            observed = error.code
        else:
            raise AssertionError(f"{name}: the curve checker accepted the mutation")
    else:
        raise AssertionError(f"{name}: unknown control stage {stage}")
    return {"name": name, "stage": stage, "code": observed}


def run_controls(result: dict, curves: dict, size: int, roster: list,
                 dataset_identity: str, *, output: Path | None = None) -> dict:
    """Validate the original, then require the observed named refusal of all seven controls."""
    original = json.dumps(result, sort_keys=True, allow_nan=False)
    module_table(result, size, roster, dataset_identity)
    baseline = checker.check_curves(result, result["trials"], curves, size, roster=roster)
    if baseline["blocks"] != len(roster) * size or baseline["blocks"] == 0:
        raise AssertionError("controls require every baseline row and block to have passed")
    controls = make_controls(result, baseline["derived"])
    if tuple(control["name"] for control in controls) != CONTROL_NAMES:
        raise AssertionError("the complete seven-control roster is required")
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
    verdicts = []
    for control in controls:
        if output is not None:
            (output / f"{control['name']}.json").write_text(
                json.dumps(control["result"], indent=2, sort_keys=True, allow_nan=False) + "\n",
                encoding="utf-8",
            )
        verdict = check_control(control, curves, size, roster, dataset_identity)
        if verdict != {key: control[key] for key in ("name", "stage", "code")}:
            raise AssertionError(f"{control['name']}: missing evidence of the intended check")
        verdicts.append(verdict)
        print(f"CONTROL: {verdict['name']} {verdict['stage']} {verdict['code']}")
    if json.dumps(result, sort_keys=True, allow_nan=False) != original:
        raise AssertionError("the controls changed the original result")
    return {"controls": verdicts}
