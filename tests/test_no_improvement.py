#!/usr/bin/env python3
"""Black-box patience contracts, with an oracle derived from ordinary CLI output.

Set PFH_NO_IMPROVEMENT_EVIDENCE to retain every command, stdout, stderr, final
document, progress stream and trial stream. No runner internals are imported.
For literal reference comparisons before release version stamping, supply both
PFH_NO_IMPROVEMENT_REFERENCE and PFH_NO_IMPROVEMENT_REFERENCE_SHA256. The tests
never fetch/build a reference or normalize version/build fields.
"""

from __future__ import annotations

import array
import errno
import fcntl
import functools
import hashlib
import json
import math
import os
import re
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest


NATIVE: Path
PLUGIN: Path
ROOT = Path(__file__).resolve().parents[1]
UINT64_MAX = (1 << 64) - 1
VALUE_DIAGNOSTIC = (
    r"--no-improvement-trials requires (?:an unsigned 64-bit|a non-negative) integer"
)


class AdmissionError(AssertionError):
    """The intended process ordering was not established; never a product RED."""


def process_threads(pid):
    tasks = Path(f"/proc/{pid}/task")
    try:
        return {int(path.name): (path / "wchan").read_text().strip()
                for path in tasks.iterdir()}
    except (FileNotFoundError, ProcessLookupError):
        return {}


@functools.lru_cache(maxsize=1)
def late_reader_capability():
    """Probe OS facilities with a helper, never the product's admission outcome."""
    def report(status, reason, **details):
        result = dict(status=status, available=status == "available", reason=reason,
                      platform=sys.platform, **details)
        print("NO_IMPROVEMENT_LATE_READER_CAPABILITY " + json.dumps(result), flush=True)
        return result

    if not sys.platform.startswith("linux"):
        return report("unavailable", "Linux /proc task wait channels required")
    missing = [name for module, name in ((fcntl, "F_SETPIPE_SZ"),
               (fcntl, "F_GETPIPE_SZ"), (termios, "FIONREAD")) if not hasattr(module, name)]
    if missing:
        return report("unavailable", "pipe controls missing", missing=missing)
    if not Path("/proc/self/task").is_dir():
        return report("unavailable", "/proc task directory unavailable")

    read_fd, write_fd = os.pipe()
    child = None
    try:
        try:
            fcntl.fcntl(read_fd, fcntl.F_SETPIPE_SZ, 4096)
            capacity = fcntl.fcntl(read_fd, fcntl.F_GETPIPE_SZ)
            queued = array.array("i", [0])
            fcntl.ioctl(read_fd, termios.FIONREAD, queued, True)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EPERM, errno.EINVAL, errno.ENOSYS,
                                  errno.ENOTTY, errno.ENOTSUP}:
                raise
            return report("unavailable", "pipe controls unavailable", errno=error.errno)
        if capacity != 4096:
            return report("unavailable", "4096-byte pipe capacity unavailable", capacity=capacity)

        # A full pipe blocks the helper's writer while its main thread joins it.
        # This checks cross-process wchan visibility without a native behavior hook.
        helper = ("import os,sys,threading; "
                  "writer=threading.Thread(target=os.write,args=(int(sys.argv[1]),b'x'*8192)); "
                  "writer.start(); writer.join()")
        child = subprocess.Popen([sys.executable, "-S", "-c", helper, str(write_fd)],
                                 pass_fds=(write_fd,), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        deadline = time.monotonic() + 5
        tasks = {}
        while time.monotonic() < deadline:
            if child.poll() is not None:
                _, diagnostic = child.communicate()
                report("error", "capability helper exited", returncode=child.returncode)
                raise AdmissionError(f"capability helper failed: {diagnostic!r}")
            fcntl.ioctl(read_fd, termios.FIONREAD, queued, True)
            try:
                tasks = {int(path.name): (path / "wchan").read_text().strip()
                         for path in Path(f"/proc/{child.pid}/task").iterdir()}
            except (FileNotFoundError, PermissionError) as error:
                if child.poll() is not None:
                    raise AdmissionError("capability helper exited before /proc read") from error
                return report("unavailable", "child task wait channels unreadable",
                              errno=error.errno)
            if (len(tasks) == 2 and queued[0] == capacity
                    and "futex" in tasks.get(child.pid, "")
                    and any("pipe" in state for tid, state in tasks.items() if tid != child.pid)):
                return report("available", "pipe controls and child wait channels observed",
                              capacity=capacity, tasks=tasks)
            time.sleep(0.01)
        hidden = all(state in {"", "0"} for state in tasks.values())
        named = all(state and not state.isdecimal() for state in tasks.values())
        if len(tasks) == 2 and queued[0] == capacity and (hidden or named):
            return report("unavailable", "required futex/pipe wait-channel symbols unavailable",
                          tasks=tasks)
        report("error", "capability helper ordering not established", tasks=tasks,
               queued_bytes=queued[0])
        raise AdmissionError("capability helper ordering not established; not a capability skip")
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        os.close(read_fd)
        os.close(write_fd)


def supplied_reference():
    path = os.environ.get("PFH_NO_IMPROVEMENT_REFERENCE")
    expected = os.environ.get("PFH_NO_IMPROVEMENT_REFERENCE_SHA256")
    if path is None and expected is None:
        return None
    if not path or not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise AdmissionError("reference requires a caller-supplied path and SHA256")
    reference = Path(path).resolve(strict=True)
    if hashlib.sha256(reference.read_bytes()).hexdigest() != expected:
        raise AdmissionError("caller-supplied reference SHA256 mismatch")
    return reference


def expected_stop(rows, patience, batch, lag, direction="maximize"):
    """Find gaps between strict record scores in an independently completed run.

    This uses record positions and gap lengths, not the product's counter/state
    transition. The proposal frontier follows the documented batch schedule.
    The caller can pass a new-part suffix of an uninterrupted genuine result.
    """
    eligible = [
        (index, row["objective"])
        for index, row in enumerate(rows)
        if row["status"] == "ok" and row["feasible"]
        and row["objective"] is not None and math.isfinite(row["objective"])
    ]
    records = []
    for position, (index, score) in enumerate(eligible):
        earlier = [value for _, value in eligible[:position]]
        if not earlier or (score > max(earlier) if direction == "maximize"
                           else score < min(earlier)):
            records.append(index)
    if patience:
        for previous, following in zip(records, [*records[1:], len(rows)]):
            trigger = previous + patience
            if trigger < following:
                frontier = min(len(rows), (trigger // batch + lag + 1) * batch) - 1
                return rows[trigger]["trial_id"], rows[frontier]["trial_id"]
    return None, rows[-1]["trial_id"] if rows else None


def raw_trial_array(document: bytes) -> bytes:
    # Retain the original spelling and whitespace, not a JSON reserialization.
    start = document.index(b'  "trials": [') + len(b'  "trials": ')
    end = document.index(b"\n  ]", start) + len(b"\n  ]")
    return document[start:end]


def checkpoint_mode(result):
    envelope, digest, payload = result["tpe_sampler_state"].split("\n", 2)
    if envelope != "PFHTPE2" or hashlib.sha256(payload.encode()).hexdigest() != digest:
        raise AssertionError("invalid product checkpoint envelope")
    # Existing v2 wire: signature, numerical identity, then eight integer fields.
    counters = payload.splitlines()[2].split()
    if len(counters) != 8 or counters[4] not in {"0", "1"}:
        raise AssertionError("unexpected product checkpoint counters")
    return "bounded" if counters[4] == "1" else "full"


class NoImprovementTests(unittest.TestCase):
    def setUp(self):
        retained = os.environ.get("PFH_NO_IMPROVEMENT_EVIDENCE")
        if retained:
            self.directory = Path(retained) / self._testMethodName
            self.directory.mkdir(parents=True, exist_ok=False)
        else:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            self.directory = Path(temporary.name)
        self.serial = 0
        self.csv = self.directory / "bars.csv"
        self.csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            + "".join(f"{1700000000000 + i * 60000},100,102,99,101,10\n"
                      for i in range(64)), encoding="utf-8")

    def run_cli(self, *extra, sampler="grid", workers=4, batch=4, lag=0,
                budget=32, score="10", delay="0", patience=None, expected=0,
                native=None, warm=None, direction="maximize", upper=4095, trace=False):
        self.serial += 1
        directory = self.directory / f"{self.serial:03d}"
        directory.mkdir()
        final = directory / "final.json"
        progress_path = directory / "progress.jsonl"
        trials_path = directory / "trials.jsonl"
        command = [
            str(native or NATIVE), "run", "--strategy", str(PLUGIN),
            "--ohlcv", str(self.csv), "--objective", "metrics.all.net_profit",
            "--sampler", sampler, "--seed", "73", "--max-trials", str(budget),
            "--workers", str(workers), "--batch-size", str(batch),
            "--batch-lag", str(lag), "--direction", direction,
            "--int-dim", "Length", "0", str(upper), "1",
            "--input-tf", "1", "--script-tf", "5",
            "--chart-timezone", "Asia/Taipei", "--bar-magnifier", "true",
            "--magnifier-samples", "6", "--magnifier-distribution", "triangle",
            "--fixed-input", "BatchPrefixTest", "1",
            "--fixed-input", "BatchPrefixJitter", "0",
            "--fixed-input", "SequenceScore", score,
            "--fixed-input", "SequenceDelayMs", delay,
            "--output", str(final), "--trials-file", str(trials_path),
        ]
        if patience is not None:
            command += ["--no-improvement-trials", str(patience)]
        if warm is not None:
            command += ["--warm-start", str(warm)]
        command += list(extra)
        with progress_path.open("wb") as progress, (directory / "arrivals").open("ab") as arrivals:
            command += ["--progress-fd", str(progress.fileno())]
            descriptors = [progress.fileno()]
            if trace:
                command += ["--fixed-input", "CompletionTraceFd", str(arrivals.fileno())]
                descriptors.append(arrivals.fileno())
            (directory / "command.json").write_text(json.dumps(command, indent=2) + "\n")
            started = time.monotonic()
            completed = subprocess.run(command, capture_output=True, timeout=180,
                                       pass_fds=tuple(descriptors))
        (directory / "stdout").write_bytes(completed.stdout)
        (directory / "stderr").write_bytes(completed.stderr)
        (directory / "process.json").write_text(json.dumps({
            "returncode": completed.returncode,
            "wall_seconds": time.monotonic() - started,
        }) + "\n")
        self.assertEqual(completed.returncode, expected,
                         f"{directory}: {completed.stderr.decode(errors='replace')}")
        result = json.loads(completed.stdout)
        if "trials" not in result:
            return result, directory
        self.assertEqual(final.read_bytes(), completed.stdout)
        self.assertEqual(progress_path.read_bytes(), trials_path.read_bytes())
        rows = [json.loads(line) for line in progress_path.read_bytes().splitlines()]
        if result["trials_out"] == "all":
            self.assertEqual(rows, result["trials"])
        else:
            self.assertTrue(all(row in rows for row in result["summary"]["best_k"]))
        self.assertEqual(result["trials_completed"], len(rows))
        self.assertEqual(len({row["trial_id"] for row in rows}), len(rows))
        self.assertTrue(all(row["status"] != "pending" for row in rows))
        if "tpe_sampler_state" in result:
            (directory / "checkpoint").write_bytes(result["tpe_sampler_state"].encode())
        (directory / "trial-array").write_bytes(raw_trial_array(completed.stdout))
        return result, directory

    def assert_stopped_prefix(self, reference, actual, patience, batch, lag,
                              direction="maximize"):
        reference_result, reference_path = reference
        result, directory = actual
        rows = reference_result["trials"]
        trigger, frontier = expected_stop(rows, patience, batch, lag, direction)
        self.assertEqual(result["early_stop"], {
            "patience_trials": patience,
            "trigger_trial_id": trigger,
            "drained_through_trial_id": frontier,
            "reference_scope": "part",
        })
        count = next((index + 1 for index, row in enumerate(rows)
                      if row["trial_id"] == frontier), 0)
        self.assertEqual(result["trials"], rows[:count])
        self.assertEqual((directory / "progress.jsonl").read_bytes(), b"".join(
            (reference_path / "progress.jsonl").read_bytes().splitlines(keepends=True)[:count]))
        self.assertEqual(result["stop_reason"], "no_improvement" if trigger is not None
                         else reference_result["stop_reason"])
        if trigger is not None:
            self.assertLessEqual(frontier - trigger, (lag + 1) * batch - 1)
            self.assertEqual([row["trial_id"] for row in result["trials"]],
                             list(range(rows[0]["trial_id"], frontier + 1)))
        if result["sampler"] == "tpe":
            # The shipped CLI only exports a checkpoint after reservations drain.
            self.assertTrue(result.get("tpe_sampler_state", "").startswith("PFHTPE2\n"))
        feasible = [row for row in result["trials"] if row["feasible"]
                    and row["objective"] is not None and math.isfinite(row["objective"])]
        if feasible:
            best = (max if direction == "maximize" else min)(
                feasible, key=lambda row: row["objective"])
            self.assertEqual((result["best_trial_id"], result["best_value"]),
                             (best["trial_id"], best["objective"]))

    def test_00_genuine_cli_fixture_controls(self):
        result, _ = self.run_cli("--constraint", "metrics.all.net_profit >= 0",
                                 score="error,10,nan,-1,10,50", budget=6)
        self.assertEqual([row["status"] for row in result["trials"]],
                         ["engine_error", "ok", "objective_error", "constraint_violation",
                          "ok", "ok"])
        self.assertEqual(result["best_value"], 50)
        self.assertNotIn("early_stop", result)
        control, _ = self.run_cli("--pruner", "median", score="10,10,9,8,7,6",
                                  budget=6, batch=2)
        self.assertTrue(any(row["status"] == "pruned" for row in control["trials"]))

    def test_semantics_and_batch_boundaries(self):
        cases = [
            ("plateau-inside", "10", 1, 4, 0, 24, "maximize", (), 0),
            ("plateau-final", "10", 7, 4, 0, 24, "maximize", (), 0),
            ("budget-tie", "10", 7, 4, 0, 8, "maximize", (), 0),
            ("partial-batch", "10", 5, 4, 1, 7, "maximize", (), 0),
            ("improvement-tie", "10,10,20,20,20,20,30,30", 3, 4, 1, 24,
             "maximize", (), 0),
            ("minimize", "30,30,20,20,20,20,10,10", 3, 4, 1, 24,
             "minimize", (), 0),
            ("first-feasible-late", "10", 2, 4, 1, 24, "maximize",
             ("--constraint", "metrics.all.num_trades >= 5"), 0),
            ("no-feasible", "10", 1, 4, 1, 24, "maximize",
             ("--constraint", "metrics.all.net_profit < 0"), 2),
            ("all-errors", "error,nan", 1, 4, 1, 24, "maximize", (), 2),
            ("mixed", "error,10,error,nan,-1,10,50,50", 4, 4, 1, 24,
             "maximize", ("--constraint", "metrics.all.net_profit >= 0"), 0),
            ("pruned", "10,10,9,8,7,6,5,4", 5, 2, 1, 24, "maximize",
             ("--pruner", "median"), 0),
        ]
        for label, score, patience, batch, lag, budget, direction, flags, expected in cases:
            with self.subTest(case=label):
                settings = dict(score=score, batch=batch, lag=lag, budget=budget,
                                direction=direction, expected=expected)
                reference = self.run_cli(*flags, **settings)
                actual = self.run_cli(*flags, patience=patience, **settings)
                self.assert_stopped_prefix(reference, actual, patience, batch, lag, direction)
                if label == "pruned":
                    self.assertTrue(any(row["status"] == "pruned"
                                        for row in actual[0]["trials"]))
                if label in {"mixed", "improvement-tie", "minimize"}:
                    self.assertGreater(actual[0]["best_trial_id"],
                                       actual[0]["early_stop"]["trigger_trial_id"])

    def test_uint64_and_invalid_inputs(self):
        for patience in (UINT64_MAX, UINT64_MAX - 1, 1 << 63, 32):
            reference = self.run_cli(budget=8)
            actual = self.run_cli(budget=8, patience=patience)
            self.assert_stopped_prefix(reference, actual, patience, 4, 0)
        for value in ("-1", " -1", "1.5", "1e2", str(UINT64_MAX + 1), "", "NaN"):
            with self.subTest(value=value):
                result, directory = self.run_cli(patience=value, expected=1)
                self.assertEqual(result["failure"]["code"], "hpo_cli_usage")
                diagnostic = (directory / "stderr").read_text()
                self.assertRegex(diagnostic, VALUE_DIAGNOSTIC)
                self.assertNotIn("unknown option", diagnostic)
                self.assertFalse((directory / "trials.jsonl").exists())
                self.assertEqual((directory / "progress.jsonl").read_bytes(), b"")

    def test_exhaustion_priority(self):
        for patience in (7, UINT64_MAX):
            reference = self.run_cli(upper=7, budget=0)
            actual = self.run_cli(upper=7, budget=0, patience=patience)
            self.assert_stopped_prefix(reference, actual, patience, 4, 0)
            self.assertTrue(actual[0]["search_space_exhausted"])

    def test_metadata_without_completions(self):
        result, _ = self.run_cli("--max-wall-seconds", "0.000000001", patience=1, expected=2)
        self.assertEqual(result["trials_completed"], 0)
        self.assertEqual(result["stop_reason"], "deadline")
        self.assertEqual(result["early_stop"], {"patience_trials": 1, "trigger_trial_id": None,
                         "drained_through_trial_id": None, "reference_scope": "part"})

    def test_tail_best_with_limited_output(self):
        settings = dict(score="10,10,10,50,10,10,10,10", patience=1, budget=24, lag=1)
        ordinary, original = self.run_cli(**settings)
        self.assertGreater(ordinary["best_trial_id"], ordinary["early_stop"]["trigger_trial_id"])
        for mode in ("none", "best-k"):
            result, directory = self.run_cli("--trials-out", mode, "--best-k", "1", **settings)
            for field in ("early_stop", "stop_reason", "trials_completed", "best_value", "best_trial_id"):
                self.assertEqual(result[field], ordinary[field])
            self.assertEqual((original / "progress.jsonl").read_bytes(),
                             (directory / "progress.jsonl").read_bytes())

    def test_disabled_raw_bytes(self):
        for sampler in ("grid", "random", "tpe", "dlib_global"):
            for lag in (0, 1):
                with self.subTest(sampler=sampler, lag=lag):
                    absent, first = self.run_cli(sampler=sampler, lag=lag)
                    explicit, second = self.run_cli(sampler=sampler, lag=lag, patience=0)
                    self.assertNotIn("early_stop", absent)
                    self.assertNotIn("early_stop", explicit)
                    for name in ("stdout", "final.json", "progress.jsonl", "trials.jsonl",
                                 "trial-array", *(["checkpoint"] if sampler == "tpe" else [])):
                        self.assertEqual((first / name).read_bytes(),
                                         (second / name).read_bytes(), name)

    def test_supplied_reference_off_bytes(self):
        reference = supplied_reference()
        if reference is None:
            self.skipTest("supplied reference unavailable: no reference comparison performed")
        (self.directory / "reference.json").write_text(json.dumps({
            "path": str(reference), "sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
            "contract": "literal bytes before release version stamping; no normalization",
        }) + "\n")
        for sampler in ("grid", "random", "tpe", "dlib_global"):
            for lag in (0, 1):
                with self.subTest(sampler=sampler, lag=lag):
                    _, old = self.run_cli(sampler=sampler, lag=lag, native=reference)
                    _, current = self.run_cli(sampler=sampler, lag=lag)
                    for name in ("stdout", "final.json", "progress.jsonl", "trials.jsonl",
                                 "trial-array", *(["checkpoint"] if sampler == "tpe" else [])):
                        self.assertEqual((old / name).read_bytes(), (current / name).read_bytes(),
                                         f"{sampler}/lag{lag}/{name}")

    def test_pre_feature_unknown_option_is_not_a_value_diagnostic(self):
        reference = supplied_reference()
        if reference is None:
            self.skipTest("supplied reference unavailable: pre-feature negative control not run")
        old, path = self.run_cli(native=reference, patience="-1", expected=1)
        self.assertEqual(old["failure"]["code"], "hpo_cli_usage")
        diagnostic = (path / "stderr").read_text()
        self.assertIn("unknown option", diagnostic)
        self.assertNotRegex(diagnostic, VALUE_DIAGNOSTIC)
        _, current = self.run_cli(patience="-1", expected=1)
        self.assertRegex((current / "stderr").read_text(), VALUE_DIAGNOSTIC)

    def test_improvement_at_expiration_boundary(self):
        scores = "10,10,10,20,20,20,20,20"
        ordinary = self.run_cli(score=scores, batch=4, budget=8)
        actual = self.run_cli(score=scores, batch=4, budget=8, patience=3)
        self.assert_stopped_prefix(ordinary, actual, 3, 4, 0)
        self.assertEqual(actual[0]["early_stop"]["trigger_trial_id"], 6)
        self.assertEqual(actual[0]["early_stop"]["drained_through_trial_id"], 7)
        control = self.run_cli(score="10", batch=4, budget=8, patience=3)
        self.assertEqual(control[0]["early_stop"]["trigger_trial_id"], 3)

    def test_nonconstant_reference_ignores_arrival_order(self):
        scores = "10,10,20,20,20,20,30,30"
        for lag in (0, 1):
            reference = self.run_cli(score=scores, budget=24, lag=lag)
            identity = None
            for workers in (1, 16):
                for delays in ("90,80,10,0,70,60,20,5", "80,90,0,10,60,70,5,20"):
                    with self.subTest(lag=lag, workers=workers, delays=delays):
                        actual = self.run_cli(score=scores, delay=delays, budget=24,
                                              lag=lag, workers=workers, patience=3, trace=True)
                        self.assert_stopped_prefix(reference, actual, 3, 4, lag)
                        arrivals = [int(line) for line in (actual[1] / "arrivals").read_text().splitlines()]
                        self.assertEqual(sorted(arrivals), list(range(len(actual[0]["trials"]))))
                        if workers == 16:
                            self.assertLess(min(arrivals.index(2), arrivals.index(3)), arrivals.index(0),
                                            "fixture did not deliver a higher score before ID0")
                        encoded = (actual[1] / "progress.jsonl").read_bytes()
                        if identity is None:
                            identity = encoded
                        self.assertEqual(encoded, identity)

    def test_workers_repeats_and_delay_permutations(self):
        for sampler in ("grid", "random", "tpe", "dlib_global"):
            for lag in (0, 1):
                reference = self.run_cli(sampler=sampler, lag=lag, budget=48)
                identity = None
                for workers in (1, 4, 16):
                    for delay in ("6,0,4,1,5,2,3", "0,5,1,6,2,4,3"):
                        with self.subTest(sampler=sampler, lag=lag, workers=workers, delay=delay):
                            actual = self.run_cli(sampler=sampler, lag=lag, workers=workers,
                                                  budget=48, patience=9, delay=delay)
                            self.assert_stopped_prefix(reference, actual, 9, 4, lag)
                            current = tuple((actual[1] / name).read_bytes() for name in
                                            ("stdout", "progress.jsonl", "trial-array"))
                            if identity is None:
                                identity = current
                            self.assertEqual(current, identity)

    def test_tpe_full_and_bounded_beyond_1000(self):
        for history in ((), ("--tpe-history-switch", "1000")):
            for lag in (0, 1):
                reference = self.run_cli(*history, sampler="tpe", budget=1040, batch=8, lag=lag)
                identity = None
                for workers in (1, 4, 16):
                    for delay in ("1,0,0,0", "0,0,0,1"):
                        with self.subTest(history=history, lag=lag, workers=workers, delay=delay):
                            actual = self.run_cli(*history, sampler="tpe", budget=1040,
                                                  batch=8, lag=lag, workers=workers,
                                                  patience=1010, delay=delay)
                            self.assert_stopped_prefix(reference, actual, 1010, 8, lag)
                            self.assertGreater(actual[0]["trials_completed"], 1000)
                            self.assertEqual(checkpoint_mode(actual[0]),
                                             "bounded" if history else "full")
                            current = tuple((actual[1] / name).read_bytes() for name in
                                            ("stdout", "progress.jsonl", "checkpoint"))
                            if identity is None:
                                identity = current
                            self.assertEqual(current, identity)

    def test_checkpoint_frontier_and_future_lag_zero(self):
        for history in ((), ("--tpe-history-switch", "8")):
            for lag in (0, 1):
                early, early_path = self.run_cli(*history, sampler="tpe", lag=lag,
                                                 patience=9, budget=48)
                count = early["trials_completed"]
                capped, capped_path = self.run_cli(*history, sampler="tpe", lag=lag,
                                                   budget=count)
                self.assertEqual(early["trials"], capped["trials"])
                self.assertEqual((early_path / "checkpoint").read_bytes(),
                                 (capped_path / "checkpoint").read_bytes())
                for future_lag in (0, 1):
                    outputs = []
                    for parent in (early_path, capped_path):
                        child, path = self.run_cli(*history, sampler="tpe", lag=future_lag,
                                                   patience=5, budget=16,
                                                   warm=parent / "final.json")
                        self.assertEqual(child["warm_start_model"],
                                         "restored_sampler_state" if future_lag == 0
                                         else "rebuilt_history")
                        self.assertEqual(child["early_stop"]["trigger_trial_id"], count + 5)
                        outputs.append((path / "trial-array").read_bytes())
                    self.assertEqual(*outputs)
                if lag == 0:
                    uninterrupted = self.run_cli(*history, sampler="tpe", budget=count + 16)
                    child = self.run_cli(*history, sampler="tpe", budget=16, patience=5,
                                         warm=early_path / "final.json")
                    suffix = dict(uninterrupted[0], trials=uninterrupted[0]["trials"][count:])
                    trigger, frontier = expected_stop(suffix["trials"], 5, 4, 0)
                    self.assertEqual(child[0]["early_stop"]["trigger_trial_id"], trigger)
                    self.assertEqual(child[0]["trials"],
                                     [row for row in suffix["trials"]
                                      if row["trial_id"] <= frontier])

    def test_continuation_resets_reference_and_count(self):
        scores = "1000,1000,1000,1000,1000,1000,1000,1000,error,error,10,10,20,20,20,20"
        parent, parent_path = self.run_cli(score=scores, budget=8)
        reference = self.run_cli(score=scores, budget=16, warm=parent_path / "final.json")
        actual = self.run_cli(score=scores, budget=16, patience=3,
                              warm=parent_path / "final.json")
        self.assert_stopped_prefix(reference, actual, 3, 4, 0)
        self.assertLess(actual[0]["best_value"], parent["best_value"])
        self.assertEqual(actual[0]["trials"][:2], reference[0]["trials"][:2])
        for sampler in ("random", "tpe"):
            parent, parent_path = self.run_cli(sampler=sampler, budget=16)
            for lag in (0, 1):
                reference = self.run_cli(sampler=sampler, lag=lag, budget=32,
                                         warm=parent_path / "final.json")
                actual = self.run_cli(sampler=sampler, lag=lag, budget=32, patience=5,
                                      warm=parent_path / "final.json")
                replay = self.run_cli(sampler=sampler, lag=lag, budget=32, patience=5,
                                      warm=parent_path / "final.json", workers=1)
                self.assert_stopped_prefix(reference, actual, 5, 4, lag)
                self.assertEqual((actual[1] / "stdout").read_bytes(),
                                 (replay[1] / "stdout").read_bytes())
                if sampler == "random":
                    points = {row["parameters"]["Length"] for row in parent["trials"]}
                    self.assertTrue(all(row["parameters"]["Length"] not in points
                                        for row in actual[0]["trials"]))

    def test_binary_parent_preserves_part_reset(self):
        spec = json.loads((ROOT / "examples/single_strategy/study.json").read_text())
        spec["strategies"][0]["search_space"] = {
            "Length": {"kind": "integer", "low": 0, "high": 4095, "step": 1}}
        spec["objective"].update(expression="metrics.all.net_profit", constraints=[])
        spec["sampler"].update(kind="tpe", trials=16)
        spec_path = self.directory / "spec.json"
        spec_path.write_text(json.dumps(spec))
        parent, parent_path = self.run_cli("--tpe-history-switch", "8", sampler="tpe",
                                           patience=9, budget=48)
        binary = self.directory / "parent.pfhw"
        command = [str(NATIVE), "warm-encode", "--spec", str(spec_path),
                   "--input", str(parent_path / "final.json"), "--output", str(binary)]
        completed = subprocess.run(command, capture_output=True, timeout=30)
        (self.directory / "encode-command.json").write_text(json.dumps(command) + "\n")
        (self.directory / "encode-stdout").write_bytes(completed.stdout)
        (self.directory / "encode-stderr").write_bytes(completed.stderr)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        outputs = []
        for warm in (parent_path / "final.json", binary):
            child, path = self.run_cli("--tpe-history-switch", "8", sampler="tpe",
                                       patience=5, budget=16, warm=warm)
            self.assertEqual(child["warm_start_model"], "restored_sampler_state")
            self.assertEqual(child["early_stop"]["trigger_trial_id"],
                             parent["trials_completed"] + 5)
            outputs.append((path / "trial-array").read_bytes())
        self.assertEqual(*outputs)

    def slow_terminal_reader(self, patience, intervention):
        """Gate the actual blocking JSONL sink after evaluation, without a mock runner.

        --trials-file is a FIFO consumed by a reader thread held behind an Event.
        A regular --progress-fd supplies the first genuine terminal row. A full
        FIFO, both engine completions and only main+writer tasks prove workers.close
        has finished while the real writer is blocked. No timed reader trickle or
        replacement clock is used. An admission failure is never product RED.
        """
        label = f"{intervention}-p{patience}"
        directory = self.directory / label
        directory.mkdir()
        fifo = directory / "terminal.fifo"
        os.mkfifo(fifo)
        reader_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        fcntl.fcntl(reader_fd, fcntl.F_SETPIPE_SZ, 4096)
        capacity = fcntl.fcntl(reader_fd, fcntl.F_GETPIPE_SZ)
        release_reader = threading.Event()
        reader_errors = []

        def read_terminal_stream():
            try:
                release_reader.wait()
                os.set_blocking(reader_fd, True)
                with (directory / "trials.jsonl").open("wb") as output:
                    while chunk := os.read(reader_fd, 65536):
                        output.write(chunk)
            except Exception as error:
                reader_errors.append(repr(error))
            finally:
                os.close(reader_fd)

        reader = threading.Thread(target=read_terminal_stream)
        reader.start()
        deadline_seconds = 3.0
        receipt = {"case": label, "fifo_capacity": capacity, "admitted": False}
        progress_path = directory / "progress.jsonl"
        arrivals_path = directory / "arrivals"
        final = directory / "final.json"
        started = time.monotonic()
        child = None
        stdout = stderr = b""
        with progress_path.open("wb") as progress, arrivals_path.open("ab") as arrivals:
            command = [
                str(NATIVE), "run", "--strategy", str(PLUGIN), "--ohlcv", str(self.csv),
                "--objective", "metrics.all.net_profit", "--sampler", "grid",
                "--max-trials", "2", "--workers", "1", "--batch-size", "2",
                "--batch-lag", "0", "--no-improvement-trials", str(patience),
                "--int-dim", "Length", "0", "31", "1", "--input-tf", "1",
                "--script-tf", "5", "--bar-magnifier", "true",
                "--magnifier-samples", "6", "--magnifier-distribution", "triangle",
                "--fixed-input", "BatchPrefixTest", "1",
                "--fixed-input", "BatchPrefixJitter", "0",
                "--fixed-input", "SequenceScore", "10",
                "--fixed-input", "CompletionTraceFd", str(arrivals.fileno()),
                "--categorical-choice", "Payload", "m" * 16384,
                "--progress-fd", str(progress.fileno()), "--trials-file", str(fifo),
                "--output", str(final),
            ]
            if intervention == "deadline":
                command += ["--max-wall-seconds", str(deadline_seconds)]
            (directory / "command.json").write_text(json.dumps(command) + "\n")
            try:
                started = time.monotonic()
                child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         pass_fds=(progress.fileno(), arrivals.fileno()))
                while True:
                    queued = array.array("i", [0])
                    fcntl.ioctl(reader_fd, termios.FIONREAD, queued, True)
                    tasks = process_threads(child.pid)
                    emitted = progress_path.read_bytes()
                    completed = arrivals_path.read_text().splitlines()
                    elapsed = time.monotonic() - started
                    ready = (child.poll() is None and len(tasks) == 2
                             and "futex" in tasks.get(child.pid, "")
                             and any("pipe" in state for tid, state in tasks.items()
                                     if tid != child.pid)
                             and queued[0] == capacity and completed == ["0", "1"]
                             and emitted.endswith(b"\n") and len(emitted) > capacity)
                    if ready:
                        first = json.loads(emitted)
                        if first["trial_id"] != 0 or first["status"] != "ok":
                            raise AdmissionError("slow-reader control has no genuine first terminal")
                        admitted_at = time.monotonic()
                        receipt.update(admitted=True, admitted_seconds=elapsed, tasks=tasks,
                                       queued_bytes=queued[0], engine_completions=completed,
                                       first_terminal_id=first["trial_id"])
                        break
                    if child.poll() is not None or elapsed >= deadline_seconds / 2:
                        receipt.update(tasks=tasks, queued_bytes=queued[0],
                                       engine_completions=completed, elapsed_seconds=elapsed)
                        raise AdmissionError("evaluation/closed-workers/blocked-writer ordering not admitted")
                    time.sleep(0.005)
                if intervention == "deadline":
                    # run() necessarily started before the admitted first terminal;
                    # this crosses its deadline even if process startup was delayed.
                    remaining = admitted_at + deadline_seconds + 0.2 - time.monotonic()
                    if remaining > 0:
                        release_reader.wait(remaining)
                else:
                    child.send_signal(signal.SIGTERM)
                    receipt["signal_sent_seconds"] = time.monotonic() - started
                receipt["release_seconds"] = time.monotonic() - started
                release_reader.set()
                stdout, stderr = child.communicate(timeout=10)
            finally:
                if child is not None and child.poll() is None:
                    child.kill()
                    child.wait()
                release_reader.set()
                reader.join(timeout=5)
                if child is not None and not stdout:
                    stdout, stderr = child.communicate(timeout=2)
                receipt.update(returncode=child.returncode if child else None,
                               reader_errors=reader_errors, reader_finished=not reader.is_alive())
                (directory / "stdout").write_bytes(stdout)
                (directory / "stderr").write_bytes(stderr)
                (directory / "admission.json").write_text(json.dumps(receipt, indent=2) + "\n")
                fifo.unlink()
        self.assertFalse(reader.is_alive(), "gated reader did not finish")
        self.assertEqual(reader_errors, [])
        self.assertEqual(child.returncode, 0, stderr)
        result = json.loads(stdout)
        self.assertEqual(final.read_bytes(), stdout)
        self.assertEqual(progress_path.read_bytes(), (directory / "trials.jsonl").read_bytes())
        self.assertEqual([json.loads(line) for line in progress_path.read_bytes().splitlines()],
                         result["trials"])
        self.assertEqual(result["trials_completed"], 2)
        self.assertEqual(result["stop_reason"],
                         "no_improvement" if patience == 1 else "trial_budget_reached",
                         "a stop first observed during output drain relabelled completed evaluation")
        if patience:
            self.assertEqual(result["early_stop"]["trigger_trial_id"], 1 if patience == 1 else None)

    def require_late_reader_capability(self):
        capability = late_reader_capability()
        if capability["status"] == "unavailable":
            self.skipTest("late-reader capability unavailable: " + capability["reason"])
        self.assertTrue(capability["available"], capability)

    def test_late_deadline_during_trials_file_drain(self):
        self.require_late_reader_capability()
        for patience in (0, 1, 8):
            with self.subTest(patience=patience):
                self.slow_terminal_reader(patience, "deadline")

    def test_late_signal_during_trials_file_drain(self):
        self.require_late_reader_capability()
        for patience in (0, 1, 8):
            with self.subTest(patience=patience):
                self.slow_terminal_reader(patience, "signal")

    def test_real_stop_and_failure_precedence(self):
        for case in ("cancel-int", "cancel-term", "deadline", "watchdog",
                     "progress-error", "final-error", "watchdog-progress-error"):
            with self.subTest(case=case):
                directory = self.directory / case
                directory.mkdir()
                final = directory / "final.json"
                trials = directory / "trials.jsonl"
                progress_path = directory / "progress.jsonl"
                broken_progress = "progress-error" in case
                watchdog = "watchdog" in case
                read_fd = None
                progress = progress_path.open("wb")
                if broken_progress:
                    read_fd, write_fd = os.pipe()
                else:
                    write_fd = progress.fileno()
                entered_read, entered_write = os.pipe()
                release_read, release_write = os.pipe()
                command = [
                    str(NATIVE), "run", "--strategy", str(PLUGIN), "--ohlcv", str(self.csv),
                    "--objective", "metrics.all.net_profit", "--sampler", "grid",
                    "--max-trials", "32", "--workers", "1", "--batch-size", "2",
                    "--batch-lag", "1", "--no-improvement-trials", "1",
                    "--int-dim", "Length", "0", "31", "1", "--input-tf", "1",
                    "--script-tf", "5", "--bar-magnifier", "true",
                    "--magnifier-samples", "6", "--magnifier-distribution", "triangle",
                    "--fixed-input", "BatchPrefixTest", "1",
                    "--fixed-input", "BatchPrefixJitter", "0",
                    "--fixed-input", "SequenceScore", "10",
                    "--fixed-input", "GateAtLength", "2",
                    "--fixed-input", "GateEnteredFd", str(entered_write),
                    "--fixed-input", "GateReleaseFd", str(release_read),
                    "--progress-fd", str(write_fd), "--trials-file", str(trials),
                    "--output", "/dev/full" if case == "final-error" else str(final),
                ]
                if watchdog:
                    command += ["--trial-timeout-seconds", "3"]
                if case == "deadline":
                    command += ["--max-wall-seconds", "3"]
                (directory / "command.json").write_text(json.dumps(command) + "\n")
                started = time.monotonic()
                child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         pass_fds=(write_fd, entered_write, release_read))
                os.close(entered_write)
                os.close(release_read)
                if broken_progress:
                    os.close(write_fd)
                stdout = stderr = None
                admission = {"admitted": False}
                try:
                    observed = b""
                    entered = False
                    while not entered or observed.count(b"\n") < 2:
                        if child.poll() is not None or time.monotonic() - started >= 1.5:
                            raise AdmissionError("active trial gate and first batch not admitted")
                        if not entered and select.select([entered_read], [], [], 0.005)[0]:
                            entered = os.read(entered_read, 1) == b"G"
                        if read_fd is None:
                            observed = progress_path.read_bytes()
                            time.sleep(0.005)
                        elif select.select([read_fd], [], [], 0.005)[0]:
                            observed += os.read(read_fd, 65536)
                    rows = [json.loads(line) for line in observed.splitlines()]
                    if [row["trial_id"] for row in rows] != [0, 1] or any(
                            row["status"] != "ok" for row in rows):
                        raise AdmissionError("gate control did not produce two genuine terminals")
                    admitted_at = time.monotonic()
                    admission.update(admitted=True, admitted_seconds=admitted_at - started,
                                     gated_candidate=2, terminal_ids=[0, 1])
                    if broken_progress:
                        progress.write(observed)
                        progress.flush()
                        os.close(read_fd)
                        read_fd = None
                    elif case.startswith("cancel"):
                        child.send_signal(signal.SIGINT if case == "cancel-int"
                                          else signal.SIGTERM)
                    elif case == "deadline":
                        # The first batch predates admission; keep trial 2 active
                        # across the actual deadline, then allow it to complete.
                        time.sleep(max(0, admitted_at + 3.2 - time.monotonic()))
                    if not watchdog:
                        os.write(release_write, b"R")
                    admission["intervention_seconds"] = time.monotonic() - started
                    stdout, stderr = child.communicate(timeout=8)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
                    if read_fd is not None:
                        os.close(read_fd)
                    os.close(entered_read)
                    os.close(release_write)
                    progress.close()
                    if stdout is None:
                        stdout, stderr = child.communicate(timeout=2)
                    (directory / "stdout").write_bytes(stdout)
                    (directory / "stderr").write_bytes(stderr)
                    (directory / "process.json").write_text(json.dumps({
                        "returncode": child.returncode, "wall_seconds": time.monotonic() - started,
                    }) + "\n")
                    (directory / "admission.json").write_text(json.dumps(admission) + "\n")
                result = json.loads(stdout)
                expected_exit = 3 if watchdog else 1 if "error" in case else 0
                self.assertEqual(child.returncode, expected_exit, stderr)
                if "error" in case:
                    self.assertEqual(result["failure"]["code"], "hpo_output_io_failed")
                    self.assertEqual(result["failure"]["exit_code"], expected_exit)
                if case == "final-error":
                    # The final sink failed only after all reserved trials drained.
                    self.assertEqual(len(trials.read_bytes().splitlines()), 4)
                    continue
                self.assertEqual(final.read_bytes(), stdout)
                self.assertEqual(result["early_stop"]["trigger_trial_id"], 1)
                self.assertEqual(result["early_stop"]["reference_scope"], "part")
                self.assertEqual(result["stop_reason"], "trial_timeout" if watchdog else
                                 "deadline" if case == "deadline" else "cancelled")
                if watchdog:
                    self.assertEqual(sum(row["status"] == "trial_timeout"
                                         for row in result["trials"]), 1)
                    self.assertLess(time.monotonic() - started, 8)


if __name__ == "__main__":
    NATIVE, PLUGIN = (Path(sys.argv.pop(1)).resolve() for _ in range(2))
    program = unittest.main(verbosity=2, exit=False)
    skips = [{"test": test.id(), "reason": reason} for test, reason in program.result.skipped]
    print("NO_IMPROVEMENT_SKIP_SUMMARY " + json.dumps({
        "total": len(skips),
        "late_reader_capability": sum(item["reason"].startswith("late-reader capability unavailable:")
                                      for item in skips),
        "supplied_reference": sum(item["reason"].startswith("supplied reference unavailable:")
                                  for item in skips),
        "skips": skips,
    }), flush=True)
    sys.exit(0 if program.result.wasSuccessful() else 1)
