#!/usr/bin/env python3
"""INSTRUMENT CONTROLS for observe_variance.py.

Every fixture written here is synthetic and labelled INSTRUMENT CONTROL. None of
it is native memory evidence, and the observer's command-line summary refuses
it. Real processes run only in the deliberate crash and exit rows and in the
real-gate control, which drives the unchanged gate with a native stand-in that
kills itself (so the gate must exit 2 and the sample must stay INVALID).

The same-binary contract has its own controls: native, plugin and manifest
SHA256 are HARD identity. A foreign native, plugin or manifest digest in any one
sample, ten independently rebuilt digests, a malformed digest and a missing
digest must each leave the collection INCOMPLETE (nonzero); only ten equal
digests are accepted as the same bytes. A second group derives the small-span
crossing count from the raw peaks, including a boundary pair on each side of the
original 256 bytes/trial limit.

Run in the admitted CI controls job, or on a separate test host, before any
measurement:

  python3 -I test_variance_observer.py --checkout <clean HPO checkout> \
      --work <fresh control directory, not the system temp directory> -v

The run is failfast: the first control failure stops it, and no measurement may
start after a failed control. Exit 0 only if every control ran and passed.
"""

from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import unittest

KIT = Path(__file__).resolve().parent
OBSERVER_PATH = KIT / "observe_variance.py"
print(f"FILES observer module read: {OBSERVER_PATH}", file=sys.stderr, flush=True)
SPEC = importlib.util.spec_from_file_location("observe_variance", OBSERVER_PATH)
OBS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OBS)

CHECKOUT = None
WORK = None
LABEL = "INSTRUMENT CONTROL: synthetic fixture, never native memory evidence"
KIB = 1024
MIB = 1024 * KIB


def put(path: Path, text: str, omit=()) -> None:
    if path.name in omit:
        return
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)


def digest(label: str) -> str:
    """A 64-hex fixture digest derived from a label; never the digest of a real binary."""
    return hashlib.sha256(f"INSTRUMENT CONTROL {label}".encode("ascii")).hexdigest()


def peaks_for(index: int, extra_top: int = 0, extra_small: int = 0) -> tuple:
    """Distinct per-sample peaks (multiples of 1 KiB) so statistics depend on input."""
    p10 = 16 * MIB + index * 4 * KIB
    p1000 = p10 + (32 + index) * KIB + extra_small
    return (p10, p1000, p1000 + (512 + 8 * index) * KIB + extra_top)


def fixture_slopes(peaks: tuple) -> list:
    """Independent restatement of the gate's integer edge rule for fixtures."""
    edges = []
    for index, limit in enumerate((256, 32)):
        left, right = OBS.COUNTS[index], OBS.COUNTS[index + 1]
        delta = peaks[index + 1] - peaks[index]
        span = right - left
        edges.append(
            {
                "from_trials": left,
                "to_trials": right,
                "delta_rss_bytes": delta,
                "bytes_per_trial": delta / span,
                "limit_bytes_per_trial": limit,
                "verdict": "PASS" if delta <= limit * span else "FAIL",
            }
        )
    return edges


def child_row(run_dir: Path, label: str, native: list, output: str, **fields) -> dict:
    row = {
        "command": [OBS.GNU_TIME, "-f", "%M", "-o", str(run_dir / f"{label}.time"), "--", *native],
        "native_command": native,
        "peak_source": "gnu_time_wait4",
        "gnu_time_file": str(run_dir / f"{label}.time"),
        "gnu_time_output": output,
        "launcher_floor_note": LABEL,
        "stdout": str(run_dir / f"{label}.stdout"),
        "stderr": str(run_dir / f"{label}.stderr"),
        "exit_file": str(run_dir / f"{label}.exit"),
        "result_file": str(run_dir / f"{label}.result.json"),
    }
    row.update(fields)
    return row


def write_row(run_dir, label, *, axis, trials, bars, peak, pid, omit=(), recorded=None):
    output = f"{peak // KIB}\n"
    native = ["/instrument-control/native", "run", "--max-trials", str(trials)]
    row = child_row(
        run_dir,
        label,
        native,
        output,
        pid=pid,
        waited_pid=pid,
        exit_code=0,
        peak_rss_bytes=peak if recorded is None else recorded,
        verdict="PASS",
        type="measurement",
        axis=axis,
        trials=trials,
        bars=bars,
        trial_rows=trials,
    )
    put(run_dir / f"{label}.time", output, omit)
    put(run_dir / f"{label}.exit", "0\n", omit)
    put(run_dir / f"{label}.stdout", LABEL + "\n", omit)
    put(run_dir / f"{label}.stderr", "", omit)
    put(run_dir / f"{label}.result.json", json.dumps(row, sort_keys=True) + "\n", omit)


def write_crash(run_dir: Path, ok: bool) -> None:
    label = "deliberate-crash"
    output = f"Command terminated by signal {OBS.SIGKILL}\n1024\n"
    row = child_row(
        run_dir,
        label,
        [sys.executable, *OBS.CRASH_CHILD],
        output,
        pid=2999,
        waited_pid=2999,
        exit_code=128 + OBS.SIGKILL,
        peak_rss_bytes=MIB,
        verdict="PASS" if ok else "ERROR",
        reason=OBS.CRASH_REASON,
    )
    put(run_dir / f"{label}.time", output)
    put(run_dir / f"{label}.exit", f"{128 + OBS.SIGKILL}\n")
    put(run_dir / f"{label}.stdout", "")
    put(run_dir / f"{label}.stderr", "")
    put(run_dir / f"{label}.result.json", json.dumps(row, sort_keys=True) + "\n")


def write_gate_run(run_dir, peaks, *, crash_ok=True, override=None, omit=(), peak_offset=0):
    """Synthetic stand-in for one gate run directory (INSTRUMENT CONTROL)."""
    run_dir.mkdir(parents=True)
    write_crash(run_dir, crash_ok)
    for index, (trials, peak) in enumerate(zip(OBS.COUNTS, peaks)):
        write_row(
            run_dir,
            f"trials-{trials}",
            axis="trials",
            trials=trials,
            bars=2,
            peak=peak,
            pid=3000 + index,
            omit=omit,
            recorded=peak + peak_offset if index == 0 else None,
        )
    edges = fixture_slopes(peaks)
    passed = all(edge["verdict"] == "PASS" for edge in edges)
    complete = {
        "type": "verdict",
        "verdict": "PASS" if passed else "FAIL",
        "exit_code": 0 if passed else 1,
        "measurements": 3,
        "trial_slopes": edges,
        "peak_sources": ["gnu_time_wait4"] * 3,
        "measurement_resolution": LABEL,
        "artifact_directory": str(run_dir),
    }
    if passed:
        bar_peaks = [peaks[0] + (count // 1000) * KIB for count in OBS.BAR_COUNTS]
        for index, (count, peak) in enumerate(zip(OBS.BAR_COUNTS, bar_peaks)):
            write_row(
                run_dir,
                f"bars-{count}",
                axis="bars",
                trials=10,
                bars=count,
                peak=peak,
                pid=4000 + index,
                omit=omit,
            )
        pairs = list(zip(OBS.BAR_COUNTS, bar_peaks))
        complete["bar_slopes"] = [
            {
                "from_bars": left,
                "to_bars": right,
                "bytes_per_bar": (right_peak - left_peak) / (right - left),
                "gated": False,
            }
            for (left, left_peak), (right, right_peak) in zip(pairs, pairs[1:])
        ]
    else:
        complete["reason"] = f"{OBS.FAIL_REASON_PREFIX}; {LABEL}"
    complete.update(override or {})
    put(run_dir / "RUN.COMPLETE", json.dumps(complete, sort_keys=True) + "\n", omit)
    put(run_dir / "gate.exit", f"{complete['exit_code']}\n", omit)
    return complete


def synthetic_identity(changes=None) -> dict:
    identity = {key: f"instrument-control-{key}" for key in OBS.HARD_IDENTITY_KEYS}
    identity.update(
        source_head="c" * 40,
        expected_head="c" * 40,
        gitlinks=[["external/pineforge-engine", "e" * 40]],
        submodule_heads=[["external/pineforge-engine", "e" * 40]],
        tracked_changes="",
        gate_sha256=OBS.ORIGINAL_GATE_SHA256,
        runner_label=OBS.ADMITTED_RUNNER_LABEL,
        gate_flags=["-I", "--native", "--plugin", "--ohlcv", "--artifacts", "--bars-axis"],
        native_sha256=digest("native"),
        plugin_sha256=digest("plugin"),
        manifest_sha256=digest("manifest"),
        kernel_release="instrument-control",
    )
    identity.update(changes or {})
    return identity


def rewrite_identity(root: Path, drop=()) -> None:
    """Delete identity keys from one control observation (the manifest excludes it)."""
    (path,) = OBS.list_files("control observation rewrite", [root / OBS.OBSERVATION], within=root)
    record = OBS.read_json(path)
    for key in drop:
        del record["identity"][key]
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, indent=1) + "\n")


def make_sample(base, repeat_id, peaks, *, identity_changes=None, returncode=None, name=None, **gate_options):
    """Build one labelled control sample through the observer's own code paths."""
    root = OBS.approved(base / (name or f"sample-{repeat_id}"))
    root.mkdir(parents=True)
    record = OBS.new_record(repeat_id)
    record[OBS.CONTROL_LABEL_KEY] = LABEL
    record["ci_context"] = {key: None for key in OBS.CI_CONTEXT_NAMES}
    record["ci_context"].update(
        hostname=f"control-host-{repeat_id}",
        RUNNER_NAME=f"control-runner-{repeat_id}",
        GITHUB_RUN_ID="instrument-control",
    )
    record["identity"] = synthetic_identity(identity_changes)
    record["observer_crash_control"] = OBS.observer_crash_control(root)
    record["gate_argv"] = [LABEL]
    run_dir = root / OBS.ARTIFACTS / f"run-control-{repeat_id}"
    complete = write_gate_run(run_dir, peaks, **gate_options)
    code = complete["exit_code"] if returncode is None else returncode
    put(root / "gate.stdout", f"{LABEL}\n{json.dumps(complete, sort_keys=True)}\n")
    put(root / "gate.stderr", LABEL + "\n")
    put(root / "gate.returncode", f"{code}\n")
    record["observer_exit_code"] = OBS.observe_gate_run(root, record, code)
    OBS.finalize_observation(root, record)
    return root, record


def collection(base, fail_ids=(), options=None, small_extra=None) -> list:
    roots = []
    for index, repeat_id in enumerate(OBS.REPEAT_IDS, start=1):
        peaks = peaks_for(
            index,
            4 * MIB if repeat_id in fail_ids else 0,
            (small_extra or {}).get(repeat_id, 0),
        )
        root, _record = make_sample(base, repeat_id, peaks, **(options or {}).get(repeat_id, {}))
        roots.append(root)
    return roots


def copy_as(source: Path, target: Path, repeat_id: str) -> Path:
    """Byte copy of a sample under a new repeat id: the copied-data control."""
    target = OBS.approved(target)
    for directory, _dirs, files in os.walk(source):
        base = Path(directory)
        destination = target / base.relative_to(source)
        destination.mkdir(parents=True, exist_ok=True)
        for name in files:
            if base == source and name in (OBS.OBSERVATION, OBS.OBSERVER_EXIT):
                continue
            shutil.copyfile(base / name, destination / name)
    record = OBS.read_json(source / OBS.OBSERVATION)
    record["repeat_id"] = repeat_id
    record.pop("manifest")
    OBS.finalize_observation(target, record)
    return target


class InstrumentControls(unittest.TestCase):
    def setUp(self):
        self.base = OBS.approved(Path(tempfile.mkdtemp(prefix="control-", dir=WORK)))
        OBS.list_files("control case directory", [self.base])

    def summarize(self, roots):
        return OBS.summarize_roots([str(root) for root in roots], allow_instrument_controls=True)

    def control_inputs(self):
        native = self.base / "native-crash"
        put(native, "#!/bin/sh\nkill -9 $$\n")
        native.chmod(0o755)
        plugin_dir = self.base / "plugin"
        plugin_dir.mkdir()
        put(plugin_dir / "fake_strategy.so", LABEL + "\n")
        put(plugin_dir / "manifest.json", "{}\n")
        bars = self.base / "memory-gate-bars.csv"
        with bars.open("xb") as handle:
            handle.write(OBS.EXPECTED_BARS_BYTES)
        return native, plugin_dir / "fake_strategy.so", bars

    def sample_arguments(self, root, gate, inputs, head=None):
        native, plugin, bars = inputs
        head = head or OBS.git(CHECKOUT, "rev-parse", "HEAD").strip()
        return [
            "sample",
            "--repeat-id",
            "r01",
            "--sample-root",
            str(root),
            "--source-root",
            str(CHECKOUT),
            "--expected-head",
            head,
            "--runner-label",
            OBS.ADMITTED_RUNNER_LABEL,
            "--gate",
            str(gate),
            "--native",
            str(native),
            "--plugin",
            str(plugin),
            "--ohlcv",
            str(bars),
        ]

    def test_real_crash_and_exit_markers_are_the_childs_own(self):
        cases = (
            ("crash", OBS.CRASH_CHILD, -OBS.SIGKILL),
            ("exit-0", ("-I", "-c", "raise SystemExit(0)"), 0),
            ("exit-1", ("-I", "-c", "raise SystemExit(1)"), 1),
            ("exit-7", ("-I", "-c", "raise SystemExit(7)"), 7),
        )
        for name, child, expected in cases:
            with self.subTest(child=name):
                paths = OBS.list_files(
                    "control child files",
                    [self.base / f"{name}.stdout", self.base / f"{name}.stderr", self.base / f"{name}.exit"],
                    within=self.base,
                )
                self.assertEqual(OBS.run_and_mark([sys.executable, *child], *paths), expected)
                self.assertEqual(OBS.read_marker(paths[2]), expected)
        self.assertEqual(OBS.shell_status(-OBS.SIGKILL), 128 + OBS.SIGKILL)
        self.assertEqual(
            [OBS.invalid_exit(code) for code in (None, 0, 1, 2, -OBS.SIGKILL)],
            [3, 3, 3, 2, 128 + OBS.SIGKILL],
        )
        self.assertEqual(
            OBS.observer_crash_control(self.base),
            {"returncode": -OBS.SIGKILL, "marker": -OBS.SIGKILL, "status": "PASS"},
        )

    def test_real_gate_with_self_killing_native_stays_invalid_with_its_own_exit(self):
        inputs = self.control_inputs()
        root = self.base / "sample-r01"
        gate = CHECKOUT / OBS.GATE_RELATIVE_PATH
        code = OBS.main(self.sample_arguments(root, gate, inputs))
        record = OBS.read_json(root / OBS.OBSERVATION)
        self.assertEqual(code, 2, record["invalid_reasons"])
        self.assertEqual(
            (record["status"], record["gate_returncode"], record["observer_exit_code"]),
            ("INVALID", 2, 2),
        )
        self.assertEqual(OBS.read_marker(root / "gate.returncode"), 2)
        self.assertEqual(OBS.read_marker(root / OBS.OBSERVER_EXIT), 2)
        self.assertEqual(record["identity"]["gate_sha256"], OBS.ORIGINAL_GATE_SHA256)
        self.assertEqual(record["gate_sha256_after"], OBS.ORIGINAL_GATE_SHA256)
        native, plugin, _bars = inputs
        for key, path in (
            ("native_sha256", native),
            ("plugin_sha256", plugin),
            ("manifest_sha256", plugin.parent / "manifest.json"),
        ):
            (checked,) = OBS.list_files("control input digest", [path], within=self.base)
            self.assertEqual(record["identity"][key], hashlib.sha256(checked.read_bytes()).hexdigest())
        self.assertEqual(record["observer_crash_control"]["status"], "PASS")
        run_dir = OBS.locate_run_directory(OBS.approved(root))
        complete = OBS.read_json(run_dir / "RUN.COMPLETE")
        self.assertEqual((complete["verdict"], complete["exit_code"]), ("ERROR", 2))
        self.assertIn("native child exited", complete["reason"])
        self.assertEqual(OBS.read_json(run_dir / "deliberate-crash.result.json")["verdict"], "PASS")
        self.assertTrue(any("gate exit 2" in reason for reason in record["invalid_reasons"]))
        summary, summary_code = OBS.summarize_roots([str(root)])
        self.assertEqual((summary_code, summary["status"]), (2, "INCOMPLETE"))
        self.assertEqual(summary["gate_exit_vector"][0], {"repeat_id": "r01", "gate_returncode": 2, "sample": "INVALID"})

    def test_foreign_gate_or_head_is_refused_before_any_gate_run(self):
        inputs = self.control_inputs()
        foreign = self.base / "scripts" / "ci_memory_gate.py"
        foreign.parent.mkdir()
        original = OBS.approved(CHECKOUT / OBS.GATE_RELATIVE_PATH).read_bytes()
        with foreign.open("xb") as handle:
            handle.write(original + b"# INSTRUMENT CONTROL: one extra line\n")
        real_gate = CHECKOUT / OBS.GATE_RELATIVE_PATH
        for name, gate, head, needle in (
            ("foreign-gate", foreign, None, "is not " + OBS.ORIGINAL_GATE_SHA256),
            ("foreign-head", real_gate, "0" * 40, "is not --expected-head"),
        ):
            with self.subTest(case=name):
                root = self.base / f"sample-{name}"
                code = OBS.main(self.sample_arguments(root, gate, inputs, head))
                record = OBS.read_json(root / OBS.OBSERVATION)
                self.assertEqual((code, record["status"]), (3, "INVALID"))
                self.assertIsNone(record["gate_returncode"])
                self.assertFalse((root / "gate.returncode").exists())
                self.assertFalse((root / OBS.ARTIFACTS).exists())
                reasons = " ".join(record["invalid_reasons"])
                self.assertIn("the gate was not run", reasons)
                self.assertIn(needle, reasons)

    def test_complete_control_collection_statistics_follow_the_inputs(self):
        roots = collection(self.base)
        summary, code = self.summarize(roots)
        self.assertEqual((code, summary["status"]), (0, "INSTRUMENT_CONTROL"), summary["errors"])
        self.assertNotIn(summary["status"], ("MEASURED", "PASS"))
        self.assertEqual(summary["evidence"], "NONE: synthetic INSTRUMENT CONTROL fixtures")
        self.assertEqual((summary["n_valid"], summary["original_gate_fail_count"]), (10, 0))
        self.assertEqual([row["repeat_id"] for row in summary["raw_rows"]], list(OBS.REPEAT_IDS))
        self.assertEqual([entry["sample"] for entry in summary["gate_exit_vector"]], ["VALID"] * 10)
        expected = [peaks_for(index) for index in range(1, 11)]
        for position, point in enumerate(summary["points"]):
            values = [Fraction(peaks[position]) for peaks in expected]
            self.assertEqual(point["values"], [int(value) for value in values])
            self.assertEqual(Fraction(point["mean_exact"]), statistics.mean(values))
            self.assertEqual(Fraction(point["sample_variance_exact"]), statistics.variance(values))
            self.assertEqual(Fraction(point["median_exact"]), statistics.median(values))
            self.assertEqual((point["min"], point["max"]), (min(values), max(values)))
        for index, edge in enumerate(summary["edges"]):
            span = OBS.COUNTS[index + 1] - OBS.COUNTS[index]
            deltas = [peaks[index + 1] - peaks[index] for peaks in expected]
            self.assertEqual(edge["delta_rss_bytes"]["values"], deltas)
            self.assertEqual(
                Fraction(edge["bytes_per_trial"]["mean_exact"]),
                Fraction(sum(deltas), span * len(deltas)),
            )
        bound = summary["empirical_false_fail_fraction"]["one_sided_95_upper_bound_exact_binomial"]
        self.assertAlmostEqual(bound, 1 - 0.05**0.1, places=6)

    def test_cli_summary_refuses_instrument_control_fixtures(self):
        roots = collection(self.base)
        output = self.base / "summary.json"
        arguments = ["summarize"]
        for root in roots:
            arguments += ["--sample", str(root)]
        self.assertEqual(OBS.main([*arguments, "--output", str(output)]), 2)
        summary = OBS.read_json(output)
        self.assertEqual(summary["status"], "INCOMPLETE")
        self.assertEqual(sum("INSTRUMENT CONTROL" in error for error in summary["errors"]), 10)
        before = output.read_bytes()
        self.assertEqual(OBS.main([*arguments, "--output", str(output)]), 2)
        self.assertEqual(output.read_bytes(), before)

    def test_observed_gate_red_is_counted_and_never_relabelled(self):
        roots = collection(self.base, fail_ids=("r05",))
        record = OBS.read_json(roots[4] / OBS.OBSERVATION)
        self.assertEqual(
            (record["status"], record["gate_verdict"], record["gate_returncode"], record["observer_exit_code"]),
            ("VALID", "FAIL", 1, 1),
        )
        summary, code = self.summarize(roots)
        self.assertEqual(code, 1, summary["errors"])
        self.assertEqual(summary["original_gate_fail_count"], 1)
        self.assertEqual(
            [entry["gate_returncode"] for entry in summary["gate_exit_vector"]],
            [0, 0, 0, 0, 1, 0, 0, 0, 0, 0],
        )
        self.assertEqual([edge["original_edge_fail_count"] for edge in summary["edges"]], [0, 1])
        self.assertEqual(summary["empirical_false_fail_fraction"]["fraction_exact"], "1/10")
        self.assertEqual(summary["raw_rows"][4]["gate_verdict"], "FAIL")

    def test_missing_samples_make_the_collection_incomplete(self):
        roots = collection(self.base, options={"r03": {"omit": ("trials-100000.result.json",)}})
        record = OBS.read_json(roots[2] / OBS.OBSERVATION)
        self.assertEqual((record["status"], record["observer_exit_code"]), ("INVALID", 3))
        self.assertIn("missing file: trials-100000.result.json", " ".join(record["invalid_reasons"]))
        summary, code = self.summarize(roots)
        self.assertEqual((code, summary["status"]), (2, "INCOMPLETE"))
        self.assertEqual(summary["gate_exit_vector"][2]["sample"], "INVALID")
        focus = summary["small_span_focus"]
        self.assertEqual((focus["valid_samples"], focus["crossing_fraction_exact"]), (9, "0/9"))
        self.assertIn("never counted as a non-crossing one", focus["not_counted_as_flat"])
        nine, code = self.summarize(roots[:2] + roots[3:])
        self.assertEqual(code, 2)
        self.assertIn("exactly 10 named sample roots are required; got 9", nine["errors"])
        self.assertEqual(nine["gate_exit_vector"][2]["sample"], "MISSING")
        empty = self.base / "empty-root"
        empty.mkdir()
        summary, code = self.summarize(roots[:2] + [empty] + roots[3:])
        self.assertEqual(code, 2)
        self.assertTrue(any("missing file: OBSERVATION.json" in error for error in summary["errors"]))

    def test_duplicate_roots_ids_and_copied_data_are_refused(self):
        roots = collection(self.base)
        twice, code = self.summarize(roots[:9] + [roots[0]])
        self.assertEqual(code, 2)
        self.assertTrue(any("named twice" in error for error in twice["errors"]))
        other = self.base / "other"
        other.mkdir()
        duplicate, _record = make_sample(other, "r02", peaks_for(2))
        same_id, code = self.summarize(roots[:9] + [duplicate])
        self.assertEqual(code, 2)
        self.assertTrue(any("duplicate repeat id r02" in error for error in same_id["errors"]))
        copied = copy_as(roots[0], self.base / "copied-r10", "r10")
        copy_summary, code = self.summarize(roots[:9] + [copied])
        self.assertEqual(code, 2)
        self.assertTrue(any("copied data" in error for error in copy_summary["errors"]))

    def test_foreign_identity_is_refused(self):
        foreign = {"source_head": "f" * 40, "expected_head": "f" * 40}
        roots = collection(self.base, options={"r04": {"identity_changes": foreign}})
        summary, code = self.summarize(roots)
        self.assertEqual(code, 2)
        self.assertIn("r04: foreign identity: source_head differs from r01", summary["errors"])
        other = self.base / "foreign-gate"
        other.mkdir()
        changed = {"identity_changes": {"gate_sha256": "0" * 64}}
        roots = collection(other, options={repeat_id: changed for repeat_id in OBS.REPEAT_IDS})
        summary, code = self.summarize(roots)
        self.assertEqual(code, 2)
        self.assertIn("the collection's gate SHA256 is not the original gate", summary["errors"])

    def test_same_binary_bytes_are_hard_identity(self):
        for key in ("native_sha256", "plugin_sha256", "manifest_sha256"):
            self.assertIn(key, OBS.HARD_IDENTITY_KEYS)
        self.assertEqual(OBS.BINARY_IDENTITY_KEYS, ("native_sha256", "plugin_sha256", "manifest_sha256"))
        self.assertFalse(hasattr(OBS, "REPORTED_IDENTITY_KEYS"))
        self.assertEqual(
            set(inspect.signature(OBS.summarize_roots).parameters),
            {"roots", "allow_instrument_controls"},
        )
        roots = collection(self.base)
        summary, code = self.summarize(roots)
        self.assertEqual((code, summary["status"]), (0, "INSTRUMENT_CONTROL"), summary["errors"])
        binary = summary["binary_identity"]
        self.assertEqual(binary["status"], "SAME_BYTES")
        self.assertEqual(
            binary["digests"],
            {
                "native_sha256": digest("native"),
                "plugin_sha256": digest("plugin"),
                "manifest_sha256": digest("manifest"),
            },
        )
        self.assertTrue(all(len(values) == 1 for values in binary["distinct"].values()))
        self.assertEqual(summary["identity_reference"]["native_sha256"], digest("native"))
        self.assertEqual({row["native_sha256"] for row in summary["raw_rows"]}, {digest("native")})

    def test_foreign_native_plugin_or_manifest_makes_the_collection_incomplete(self):
        for key in OBS.BINARY_IDENTITY_KEYS:
            with self.subTest(foreign=key):
                other = self.base / f"foreign-{key}"
                other.mkdir()
                foreign = {"identity_changes": {key: digest(f"foreign {key}")}}
                roots = collection(other, options={"r04": foreign})
                summary, code = self.summarize(roots)
                self.assertEqual((code, summary["status"]), (2, "INCOMPLETE"))
                self.assertIn(f"r04: foreign identity: {key} differs from r01", summary["errors"])
                self.assertEqual(sum("foreign identity" in error for error in summary["errors"]), 1)
                self.assertEqual(
                    [entry["sample"] for entry in summary["gate_exit_vector"]], ["VALID"] * 10
                )
                binary = summary["binary_identity"]
                self.assertEqual(binary["status"], "DIFFERS_OR_UNVERIFIED")
                self.assertEqual(
                    {name: len(values) for name, values in binary["distinct"].items()},
                    {name: 2 if name == key else 1 for name in OBS.BINARY_IDENTITY_KEYS},
                )
        other = self.base / "foreign-reference"
        other.mkdir()
        first = {"identity_changes": {"native_sha256": digest("foreign reference native")}}
        summary, code = self.summarize(collection(other, options={"r01": first}))
        self.assertEqual((code, summary["status"]), (2, "INCOMPLETE"))
        self.assertEqual(
            sum("foreign identity: native_sha256 differs from r01" in e for e in summary["errors"]), 9
        )

    def test_ten_independently_rebuilt_binaries_are_not_the_same_bytes(self):
        other = self.base / "ten-rebuilds"
        other.mkdir()
        options = {
            repeat_id: {
                "identity_changes": {
                    key: digest(f"rebuild {key} {repeat_id}") for key in OBS.BINARY_IDENTITY_KEYS
                }
            }
            for repeat_id in OBS.REPEAT_IDS
        }
        summary, code = self.summarize(collection(other, options=options))
        self.assertEqual((code, summary["status"]), (2, "INCOMPLETE"))
        self.assertEqual(sum("foreign identity" in error for error in summary["errors"]), 27)
        binary = summary["binary_identity"]
        self.assertEqual(binary["status"], "DIFFERS_OR_UNVERIFIED")
        self.assertEqual(
            {name: len(values) for name, values in binary["distinct"].items()},
            {name: 10 for name in OBS.BINARY_IDENTITY_KEYS},
        )

    def test_malformed_or_missing_binary_digest_is_refused(self):
        cases = (
            ("not-hex", {"native_sha256": "not-a-digest"}),
            ("upper-case", {"plugin_sha256": digest("plugin").upper()}),
            ("short", {"manifest_sha256": digest("manifest")[:63]}),
            ("not-a-string", {"native_sha256": 7}),
        )
        for name, changes in cases:
            with self.subTest(case=name):
                other = self.base / f"malformed-{name}"
                other.mkdir()
                roots = collection(other, options={"r06": {"identity_changes": changes}})
                (key,) = changes
                summary, code = self.summarize(roots)
                self.assertEqual((code, summary["status"]), (2, "INCOMPLETE"))
                needle = f"observation {key} is not a SHA256 hex digest"
                self.assertTrue(any(needle in error for error in summary["errors"]), summary["errors"])
                self.assertEqual(summary["gate_exit_vector"][5]["sample"], "INVALID")
        with self.subTest(case="missing-key"):
            other = self.base / "malformed-missing"
            other.mkdir()
            roots = collection(other)
            rewrite_identity(roots[7], drop=("manifest_sha256",))
            summary, code = self.summarize(roots)
            self.assertEqual((code, summary["status"]), (2, "INCOMPLETE"))
            self.assertTrue(any("observation identity is incomplete" in e for e in summary["errors"]))
            self.assertEqual(summary["gate_exit_vector"][7]["sample"], "INVALID")

    def test_small_span_crossing_count_follows_the_raw_peaks(self):
        # With the fixture's (32 + index) KiB base delta, +212 KiB gives exactly 247 KiB at r03
        # (252928 B, below the 253440 B limit) and 248 KiB at r04 (253952 B, above it): a crossing
        # is delta > 256 * 990, never >=. KiB-granular peaks cannot sit exactly on the limit.
        small_extra = {"r03": 212 * KIB, "r04": 212 * KIB, "r09": 256 * KIB}
        roots = collection(self.base, small_extra=small_extra)
        summary, code = self.summarize(roots)
        self.assertEqual((code, summary["status"]), (1, "INSTRUMENT_CONTROL"), summary["errors"])
        expected = {
            repeat_id: peaks_for(index, 0, small_extra.get(repeat_id, 0))
            for index, repeat_id in enumerate(OBS.REPEAT_IDS, start=1)
        }
        margins = [peaks[1] - peaks[0] - 256 * 990 for peaks in expected.values()]
        focus = summary["small_span_focus"]
        self.assertEqual(focus["limit_delta_bytes"], 253440)
        self.assertEqual((focus["from_trials"], focus["to_trials"], focus["trial_span"]), (10, 1000, 990))
        self.assertEqual(focus["margin_bytes"]["values"], margins)
        self.assertEqual(margins[2], -512)
        self.assertEqual(margins[3], 512)
        self.assertEqual(focus["crossings"], sum(1 for margin in margins if margin > 0))
        self.assertEqual(focus["crossing_repeat_ids"], ["r04", "r09"])
        self.assertEqual(focus["crossing_fraction_exact"], "2/10")
        self.assertEqual(focus["upper_edge_original_fail_count"], 0)
        self.assertEqual([edge["original_edge_fail_count"] for edge in summary["edges"]], [2, 0])
        self.assertEqual(summary["original_gate_fail_count"], 2)
        self.assertEqual(
            [entry["gate_returncode"] for entry in summary["gate_exit_vector"]],
            [0, 0, 0, 1, 0, 0, 0, 0, 1, 0],
        )
        self.assertEqual([point["trials"] for point in summary["points"]], [10, 1000, 100000])
        self.assertEqual(
            summary["edges"][1]["delta_rss_bytes"]["values"],
            [peaks[2] - peaks[1] for peaks in expected.values()],
        )
        self.assertEqual(
            [edge["original_limit_bytes_per_trial"] for edge in summary["edges"]], [256, 32]
        )

    def test_changed_retained_file_is_refused(self):
        roots = collection(self.base)
        run_dir = OBS.locate_run_directory(roots[5])
        with (run_dir / "trials-1000.time").open("a", encoding="ascii") as handle:
            handle.write("1\n")
        summary, code = self.summarize(roots)
        self.assertEqual(code, 2)
        self.assertTrue(
            any("changed after observation" in e and "trials-1000.time" in e for e in summary["errors"])
        )

    def test_gate_errors_failed_controls_and_unraw_peaks_never_count(self):
        cases = (
            ("error-exit-1", {"override": {"verdict": "ERROR", "exit_code": 1}}, 3),
            ("exit-2", {"override": {"verdict": "ERROR", "exit_code": 2, "reason": LABEL}}, 2),
            ("crash-control-failed", {"crash_ok": False}, 3),
            ("peak-not-raw-gnu-time", {"peak_offset": KIB}, 3),
            ("exit-differs-from-marker", {"returncode": 1}, 3),
        )
        for name, options, expected in cases:
            with self.subTest(case=name):
                root, record = make_sample(self.base, "r01", peaks_for(1), name=f"sample-{name}", **options)
                self.assertEqual((record["status"], record["observer_exit_code"]), ("INVALID", expected))
                self.assertEqual(OBS.read_marker(root / OBS.OBSERVER_EXIT), expected)
                summary, code = self.summarize([root])
                self.assertEqual((code, summary["status"]), (2, "INCOMPLETE"))

    def test_protected_names_and_escaping_symlinks_are_refused(self):
        for name in (
            ".config/zsh/keys/private",
            ".config/pineforge/settings",
            ".ssh/id_ed25519",
            ".aws/credentials",
            ".codex/auth.json",
            "secret.env",
            "secret.env/../otherwise-safe",
        ):
            with self.subTest(name=name), self.assertRaisesRegex(OBS.Refused, "refused protected path by name"):
                OBS.approved(self.base / name)
        root = self.base / "root"
        root.mkdir()
        outside = self.base / "outside.txt"
        put(outside, LABEL + "\n")
        (root / "link.json").symlink_to(outside)
        with self.assertRaisesRegex(OBS.Refused, "refused path escaping"):
            OBS.approved(root / "link.json", within=root)
        with self.assertRaisesRegex(OBS.SampleInvalid, "non-regular file inside sample root"):
            OBS.build_manifest(OBS.approved(root))
        (root / "alias").symlink_to(self.base / "secret.env")
        with self.assertRaisesRegex(OBS.Refused, "refused protected path by name"):
            OBS.approved(root / "alias")


def main() -> int:
    global CHECKOUT, WORK
    parser = argparse.ArgumentParser(description="Run the INSTRUMENT CONTROLS (failfast).")
    parser.add_argument("--checkout", type=Path, required=True, help="clean HPO checkout with submodules")
    parser.add_argument("--work", type=Path, required=True, help="control directory, not system temp")
    known, rest = parser.parse_known_args()
    CHECKOUT, WORK = OBS.list_files("control checkout and work directory", [known.checkout, known.work])
    WORK.mkdir(parents=True, exist_ok=True)
    program = unittest.main(argv=[sys.argv[0], *rest], failfast=True, exit=False)
    result = program.result
    return 0 if result.wasSuccessful() and result.testsRun > 0 and not result.skipped else 1


if __name__ == "__main__":
    sys.exit(main())
