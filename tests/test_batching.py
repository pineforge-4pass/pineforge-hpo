#!/usr/bin/env python3
"""Native scheduling and prefix-pruning replay gates."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


NATIVE = Path(sys.argv.pop(1)).resolve()
PLUGIN = Path(sys.argv.pop(1)).resolve()


class BatchReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.csv = Path(self.directory.name) / "bars.csv"
        self.csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            + "".join(
                f"{1700000000000 + index * 60000},100,102,99,101,10\n"
                for index in range(64)
            ),
            encoding="utf-8",
        )

    def invoke(self, *extra: str, workers: int = 4, sampler: str = "tpe", progress=None):
        return subprocess.run(
            [
                str(NATIVE), "run", "--strategy", str(PLUGIN),
                "--ohlcv", str(self.csv), "--objective", "metrics.all.net_profit",
                "--sampler", sampler, "--max-trials", "32", "--seed", "90210",
                "--workers", str(workers), "--input-tf", "1", "--script-tf", "5",
                "--chart-timezone", "Asia/Taipei", "--bar-magnifier", "true",
                "--magnifier-samples", "6", "--magnifier-distribution", "triangle",
                "--fixed-input", "BatchPrefixTest", "1",
                "--int-dim", "Length", "1", "100", "1", *extra,
                *(["--progress-fd", str(progress)] if progress is not None else []),
            ],
            text=True, capture_output=True, check=False,
            pass_fds=(progress,) if progress is not None else (),
        )

    def result(self, *extra: str, workers: int = 4, sampler: str = "tpe"):
        completed = self.invoke(*extra, workers=workers, sampler=sampler)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def trial_bytes(self, completed) -> bytes:
        self.assertEqual(completed.returncode, 0, completed.stderr)
        start = completed.stdout.index('  "trials": [') + len('  "trials": ')
        return completed.stdout[start:completed.stdout.rfind("\n}")].encode()

    def replay(self, *extra: str, sampler: str = "tpe") -> None:
        reference = None
        for workers in (1, 2, 4, 8):
            completed = self.invoke("--batch-size", "4", *extra,
                                    workers=workers, sampler=sampler)
            encoded = self.trial_bytes(completed)
            if reference is None:
                reference = encoded
            self.assertEqual(encoded, reference, f"replay changed at workers={workers}")

    def test_worker_independent_batches(self) -> None:
        for sampler in ("tpe", "dlib_global", "random", "grid"):
            self.replay(sampler=sampler)

    def test_fixed_lag_replay(self) -> None:
        for sampler in ("tpe", "dlib_global", "random", "grid"):
            self.replay("--batch-lag", "1", sampler=sampler)

    def test_fixed_lag_uses_one_previous_pending_batch(self) -> None:
        random_start = self.result("--tpe-startup-trials", "100", "--batch-size", "4")
        lagged = self.result("--tpe-startup-trials", "4", "--batch-size", "4",
                             "--batch-lag", "1")
        barrier = self.result("--tpe-startup-trials", "4", "--batch-size", "4")
        parameters = lambda result: [trial["parameters"] for trial in result["trials"]]
        self.assertEqual(parameters(lagged)[:8], parameters(random_start)[:8])
        self.assertNotEqual(parameters(barrier)[4:8], parameters(random_start)[4:8])

    def test_default_compatibility(self) -> None:
        for sampler in ("tpe", "dlib_global", "random", "grid"):
            before = self.invoke(sampler=sampler)
            explicit = self.invoke("--batch-size", "4", "--batch-lag", "0",
                                   "--pruner", "none", sampler=sampler)
            self.assertEqual(self.trial_bytes(before), self.trial_bytes(explicit))

    def test_pruning_replay_and_partial_metrics(self) -> None:
        for kind in ("median", "halving"):
            for lag in ("0", "1"):
                flags = ("--pruner", kind, "--pruner-rungs", "0.25,0.5",
                         "--pruner-eta", "2", "--batch-lag", lag)
                self.replay(*flags)
                result = self.result("--batch-size", "4", *flags)
                pruned = [trial for trial in result["trials"] if trial["status"] == "pruned"]
                self.assertTrue(pruned)
                self.assertEqual(len(result["trials"]), 32)
                self.assertTrue(all(trial["status"] == "ok"
                                    for trial in result["trials"][:4]))
                for trial in pruned:
                    self.assertFalse(trial["feasible"])
                    self.assertIsNotNone(trial["objective"])
                    self.assertIn("metrics.all.net_profit", trial["metrics"])
                    self.assertLess(trial["backtest"]["input_bars_processed"], 64)
                    self.assertGreaterEqual(trial["pruning"]["bars_processed_total"],
                                            trial["backtest"]["input_bars_processed"])
                self.assertEqual(result["trials"][result["best_trial_id"]]["status"], "ok")

    def test_prefix_constraints_are_full_window_only(self) -> None:
        result = self.result("--pruner", "median", "--batch-size", "4",
                             "--constraint", "input_bars_processed >= 64")
        self.assertTrue(any(trial["status"] == "pruned" for trial in result["trials"]))
        self.assertTrue(all(trial["status"] in ("ok", "pruned")
                            for trial in result["trials"]))

    def test_pruned_progress_matches_terminal_records(self) -> None:
        path = Path(self.directory.name) / "progress.jsonl"
        with path.open("w") as progress:
            completed = self.invoke("--batch-size", "4", "--batch-lag", "1",
                                    "--pruner", "median", progress=progress.fileno())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        final = json.loads(completed.stdout)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(sorted(lines, key=lambda trial: trial["trial_id"]), final["trials"])
        self.assertTrue(any(trial["status"] == "pruned" for trial in lines))

    def test_stop_skips_unstarted_pipelined_pruned_trials(self) -> None:
        completed = self.invoke("--batch-size", "4", "--batch-lag", "1",
                                "--pruner", "median", "--max-wall-seconds", "0.03",
                                "--fixed-input", "DelayMs", "10")
        self.assertIn(completed.returncode, (0, 2), completed.stderr)
        final = json.loads(completed.stdout)
        self.assertEqual(final["stop_reason"], "deadline")
        self.assertLess(len(final["trials"]), 32)
        self.assertTrue(all(trial["status"] != "pending" for trial in final["trials"]))

    def test_bad_options(self) -> None:
        for flags in (("--batch-size", "0"), ("--batch-lag", "2"),
                      ("--pruner", "unknown"), ("--pruner-eta", "1"),
                      ("--pruner-rungs", "0.5,0.25"),
                      ("--pruner-rungs", "0"), ("--pruner-rungs", "1.1")):
            self.assertEqual(self.invoke(*flags).returncode, 1, flags)


if __name__ == "__main__":
    unittest.main()
