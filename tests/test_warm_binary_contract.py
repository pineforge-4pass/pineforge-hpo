"""Bitwise warm-v2 equivalence; optionally compare the actual v0.5.0 executable."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile

from test_warm_start_contract import ROOT, require, run
from pineforge_hpo.study_spec import load_study_spec
from pineforge_hpo.warm_binary import encode_warm_block
from pineforge_hpo.continuation import space_info, WarmStartError


def fingerprint(result):
    payload = bytearray()
    for trial in result["trials"]:
        payload.extend(struct.pack("<Q", trial["trial_id"]))
        for name, value in sorted(trial["parameters"].items()):
            payload.extend(name.encode() + b"\0")
            if type(value) is float:
                payload.extend(b"d" + struct.pack("<d", value))
            else:
                payload.extend(json.dumps(value, separators=(",", ":")).encode() + b"\0")
        payload.extend(trial["status"].encode() + b"\0")
        payload.extend(b"null" if trial["objective"] is None else
                       struct.pack("<d", trial["objective"]))
    return hashlib.sha256(payload).hexdigest()


def check(directory, native, baseline, plugin):
    csv = directory / "bars.csv"
    csv.write_text("timestamp,open,high,low,close,volume\n"
                   "1700000000000,100,102,99,101,10\n"
                   "1700000060000,101,103,100,102,11\n")
    cases = [
        ("finite", ("--int-dim", "Length", "0", "399", "1"),
         {"Length": {"kind": "integer", "low": 0, "high": 399}},
         ("grid", "random", "tpe")),
        ("mixed", ("--int-dim", "Length", "0", "399", "1",
                   "--real-dim", "Real", "-1", "1", "continuous",
                   "--bool-dim", "Flag", "--categorical-choice", "Choice", "a",
                   "--categorical-real-choice", "Choice", "3.5",
                   "--real-dim", "Stepped", "-2", "2", "0.25"),
         {"Length": {"kind": "integer", "low": 0, "high": 399},
          "Real": {"kind": "real", "low": -1.0, "high": 1.0},
          "Flag": {"kind": "boolean"},
          "Choice": {"kind": "categorical", "choices": ["a", 3.5]},
          "Stepped": {"kind": "real", "low": -2.0, "high": 2.0, "step": 0.25}},
         ("random", "tpe")),
        ("log", ("--int-dim", "Length", "0", "399", "1",
                 "--log-real-dim", "Log", "0.01", "100"),
         {"Length": {"kind": "integer", "low": 0, "high": 399},
          "Log": {"kind": "real", "low": 0.01, "high": 100.0, "log": True}},
         ("random", "tpe")),
    ]
    evidence = []
    for name, dimensions, parameters, samplers in cases:
        for constrained in (False, True):
            constraints = ["metrics.all.num_trades >= 50",
                           "metrics.all.num_trades <= 399"] if constrained else []
            extra = tuple(item for expression in constraints
                          for item in ("--constraint", expression))
            spec = json.loads((ROOT / "examples/single_strategy/study.json").read_text())
            spec["strategies"][0].update(source="missing.pine", search_space=parameters)
            spec["objective"].update(expression="metrics.all.net_profit",
                                     constraints=constraints)
            spec_path = directory / "study.json"
            spec_path.write_text(json.dumps(spec))
            study = load_study_spec(spec_path)
            for sampler in samplers:
                for batch in (1, 4, 8, 32):
                    label = f"{name}-{int(constrained)}-{sampler}-b{batch}"
                    _, parent, parent_path, _ = run(
                        baseline, plugin, csv, directory, sampler, batch, trials=128,
                        dimensions=dimensions, extra=extra, label=label + "-parent")
                    if sampler == "tpe":
                        _, checkpoint_parent, checkpoint_path, _ = run(
                            native, plugin, csv, directory, sampler, batch, trials=128,
                            dimensions=dimensions, extra=extra, label=label + "-checkpoint")
                        require(fingerprint(parent) == fingerprint(checkpoint_parent),
                                f"{label}: fresh suggestions differ from baseline")
                        parent = checkpoint_parent
                        parent_path = checkpoint_path
                    binary_path = directory / (label + ".bin")
                    multi_path = directory / (label + "-multi.bin")
                    state = parent.get("tpe_sampler_state")
                    single = encode_warm_block(study, parent["trials"], sampler_state=state)
                    binary_path.write_bytes(single)
                    chunks = [parent["trials"][begin:begin + 17]
                              for begin in range(0, 128, 17)]
                    multi_path.write_bytes(b"".join(
                        encode_warm_block(study, chunk,
                                          sampler_state=state if index == 0 else None)
                        for index, chunk in enumerate(reversed(chunks))))
                    encoded_path = directory / (label + "-native.bin")
                    process = subprocess.run(
                        [str(native), "warm-encode", "--spec", str(spec_path),
                         "--input", str(parent_path), "--output", str(encoded_path)],
                        capture_output=True, text=True, timeout=30)
                    require(process.returncode == 0, process.stderr)
                    require(encoded_path.read_bytes() == single,
                            f"{label}: native/reference writer bytes differ")
                    _, reference, _, _ = run(
                        baseline, plugin, csv, directory, sampler, batch, trials=48,
                        warm=parent_path, dimensions=dimensions, extra=extra,
                        label=label + "-baseline")
                    expected = fingerprint(reference)
                    for mode, warm in (("json", parent_path), ("binary", binary_path),
                                       ("multi", multi_path)):
                        _, actual, child_path, _ = run(
                            native, plugin, csv, directory, sampler, batch, trials=48,
                            warm=warm, dimensions=dimensions, extra=extra,
                            label=label + "-" + mode)
                        require(fingerprint(actual) == expected,
                                f"{label}: {mode} differs bitwise from v0.5 JSON")
                        require(actual["trials"][0]["trial_id"] == 128,
                                "child IDs do not follow highest parent ID")
                        if mode == "binary" and batch == 4:
                            _, grandchild, _, _ = run(
                                native, plugin, csv, directory, sampler, batch, trials=16,
                                warm=child_path, dimensions=dimensions, extra=extra,
                                label=label + "-grandchild")
                            require(grandchild["warm_start"]["trials"] == 176,
                                    "binary ancestor was lost from JSON child")
                    evidence.append({"case": label, "suggestions": 48,
                                     "suggestion_sha256": expected,
                                     "warm_sha256": hashlib.sha256(single).hexdigest(),
                                     "comparisons": ["baseline-replay", "checkpoint-json",
                                                     "checkpoint-binary", "reversed-blocks"]})
    evidence.extend(check_fallback(directory, native, baseline, plugin, csv))
    return evidence


def check_fallback(directory, native, baseline, plugin, csv):
    dimensions = ("--int-dim", "Length", "0", "19999", "1")
    constraints = ["metrics.all.num_trades >= 1", "metrics.all.num_trades <= 20000"]
    extra = tuple(item for expression in constraints for item in ("--constraint", expression))
    spec = json.loads((ROOT / "examples/single_strategy/study.json").read_text())
    spec["strategies"][0].update(
        source="missing.pine", search_space={"Length": {"kind": "integer", "low": 0,
                                                        "high": 19999}})
    spec["objective"].update(expression="metrics.all.net_profit", constraints=constraints)
    spec_path = directory / "fallback.spec.json"
    spec_path.write_text(json.dumps(spec))
    study = load_study_spec(spec_path)
    _, parent, _, _ = run(baseline, plugin, csv, directory, "random", 8, trials=2048,
                         dimensions=dimensions, extra=extra, label="fallback-parent")
    states = ("constraint_violation", "engine_error", "objective_error", "constraint_error",
              "trial_error", "trial_timeout", "pruned", "partial")
    for row, trial in enumerate(parent["trials"]):
        trial["trial_id"] = (1 << 33) + row * 3
        trial["status"] = "ok" if row % 2 == 0 else states[(row // 2) % len(states)]
        trial["feasible"] = trial["status"] == "ok"
        trial["objective"] = float(row % 13) if trial["feasible"] else None
        trial["constraint_values"] = [None, None]
        if row % 7 == 0:
            trial["parameters"] = copy.deepcopy(parent["trials"][0]["parameters"])
    legacy = directory / "fallback.json"
    legacy.write_text(json.dumps(parent))
    binary = directory / "fallback.bin"
    block = encode_warm_block(study, parent["trials"])
    binary.write_bytes(block)
    multiple = directory / "fallback-multi.bin"
    multiple.write_bytes(b"".join(encode_warm_block(study, parent["trials"][begin:begin + 101])
                                  for begin in range(0, 2048, 101)))
    require(space_info(study, legacy) == space_info(study, binary, native=native),
            "Python JSON/binary preflight coverage differs")
    evidence = []
    for switch, reservoir in ((32, 448), (32, 0), (32, 1), (1_000_000_000, 448)):
        config = extra + ("--tpe-history-switch", str(switch),
                          "--tpe-bad-reservoir-size", str(reservoir))
        for policy in ("sampler_default", "without_replacement"):
            for batch in (1, 4, 8, 32):
                label = f"fallback-{switch}-{reservoir}-{policy}-b{batch}"
                _, reference, _, _ = run(
                    baseline, plugin, csv, directory, "tpe", batch, trials=32, warm=legacy,
                    dimensions=dimensions, extra=config, policy=policy, label=label + "-v05")
                expected = fingerprint(reference)
                for mode, warm in (("json", legacy), ("binary", binary), ("multi", multiple)):
                    _, actual, _, _ = run(
                        native, plugin, csv, directory, "tpe", batch, trials=32, warm=warm,
                        dimensions=dimensions, extra=config, policy=policy,
                        label=label + "-" + mode)
                    require(actual["warm_start_model"] == "rebuilt_history",
                            "sparse-ID history unexpectedly replayed")
                    require(fingerprint(actual) == expected, f"{label}: {mode} bits differ")
                    require(actual["trials"][0]["trial_id"] == (1 << 33) + 2047 * 3 + 1,
                            "sparse/wide maximum parent ID was lost")
                evidence.append({"case": label, "suggestions": 32,
                                 "suggestion_sha256": expected,
                                 "warm_sha256": hashlib.sha256(block).hexdigest(),
                                 "comparisons": ["v0.5-json", "v0.6-json",
                                                 "v0.6-binary", "multi-block"]})
    for label, offset in (("version", 8), ("hash", 48), ("counts", 40)):
        malformed = directory / (label + ".bin")
        damaged = bytearray(block)
        damaged[offset] ^= 255
        malformed.write_bytes(damaged)
        run(native, plugin, csv, directory, "tpe", 4, trials=16, warm=malformed,
            dimensions=dimensions, extra=extra, expected=4, label="refuse-" + label)
        try:
            space_info(study, malformed, native=native)
        except WarmStartError:
            pass
        else:
            raise AssertionError("Python binary preflight accepted malformed history")
    truncated = directory / "truncated.bin"
    truncated.write_bytes(block[:-1])
    run(native, plugin, csv, directory, "tpe", 4, trials=16, warm=truncated,
        dimensions=dimensions, extra=extra, expected=4, label="refuse-truncated")
    return evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("native", type=Path)
    parser.add_argument("plugin", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    native = args.native.resolve()
    baseline = (args.baseline or native).resolve()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        evidence = check(args.output.resolve(), native, baseline, args.plugin.resolve())
        (args.output / "equivalence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    else:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = check(Path(temporary), native, baseline, args.plugin.resolve())
    print(f"{len(evidence)} cases: bit-identical JSON/binary/multi-block suggestions")


if __name__ == "__main__":
    main()
