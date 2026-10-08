#!/usr/bin/env python3
"""Black-box patience contracts, with an oracle derived from ordinary CLI output.

Set PFH_NO_IMPROVEMENT_EVIDENCE to retain every command, stdout, stderr, final
document, progress stream and trial stream. No runner internals are imported.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest


NATIVE: Path
PLUGIN: Path
ROOT = Path(__file__).resolve().parents[1]
UINT64_MAX = (1 << 64) - 1


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
                native=None, warm=None, direction="maximize", upper=4095):
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
        with progress_path.open("wb") as progress:
            command += ["--progress-fd", str(progress.fileno())]
            (directory / "command.json").write_text(json.dumps(command, indent=2) + "\n")
            started = time.monotonic()
            completed = subprocess.run(command, capture_output=True, timeout=180,
                                       pass_fds=(progress.fileno(),))
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
                    "--fixed-input", "SequenceDelayMs", "0,0,800,0",
                    "--progress-fd", str(write_fd), "--trials-file", str(trials),
                    "--output", "/dev/full" if case == "final-error" else str(final),
                ]
                if watchdog:
                    command += ["--fixed-input", "HangAtLength", "2",
                                "--trial-timeout-seconds", "0.3"]
                if case == "deadline":
                    command += ["--max-wall-seconds", "0.2"]
                (directory / "command.json").write_text(json.dumps(command) + "\n")
                child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         pass_fds=(write_fd,))
                if broken_progress:
                    os.close(write_fd)
                stdout = stderr = None
                started = time.monotonic()
                try:
                    if case.startswith("cancel") or broken_progress:
                        observed = b""
                        while observed.count(b"\n") < 2:
                            self.assertIsNone(child.poll(), "runner exited before live progress")
                            self.assertLess(time.monotonic() - started, 5,
                                            "first complete batch did not emit progress")
                            if read_fd is None:
                                observed = progress_path.read_bytes()
                                time.sleep(0.005)
                            elif select.select([read_fd], [], [], 0.05)[0]:
                                chunk = os.read(read_fd, 65536)
                                self.assertTrue(chunk, "progress closed before first batch")
                                observed += chunk
                        # The next batch is still active; allow ordered feedback to latch.
                        time.sleep(0.08)
                        if broken_progress:
                            progress.write(observed)
                            progress.flush()
                            os.close(read_fd)
                            read_fd = None
                        else:
                            child.send_signal(signal.SIGINT if case == "cancel-int"
                                              else signal.SIGTERM)
                    stdout, stderr = child.communicate(timeout=5)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
                    if read_fd is not None:
                        os.close(read_fd)
                    progress.close()
                    if stdout is None:
                        stdout, stderr = child.communicate(timeout=2)
                    (directory / "stdout").write_bytes(stdout)
                    (directory / "stderr").write_bytes(stderr)
                    (directory / "process.json").write_text(json.dumps({
                        "returncode": child.returncode, "wall_seconds": time.monotonic() - started,
                    }) + "\n")
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
                    self.assertLess(time.monotonic() - started, 3)


if __name__ == "__main__":
    NATIVE, PLUGIN = (Path(sys.argv.pop(1)).resolve() for _ in range(2))
    unittest.main()
