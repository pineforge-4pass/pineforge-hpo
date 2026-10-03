#!/usr/bin/env python3
"""Bounded results, ordered billing records and output-mode replay."""

import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

NATIVE = Path(sys.argv.pop(1)).resolve()
PLUGIN = Path(sys.argv.pop(1)).resolve()


class TrialOutputTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.csv = self.root / "bars.csv"
        self.csv.write_text("timestamp,open,high,low,close,volume\n"
                            "1700000000000,100,102,99,101,10\n"
                            "1700000060000,100,102,99,101,10\n")

    def command(self, *extra, workers=8, trials=1200):
        return [str(NATIVE), "run", "--strategy", str(PLUGIN), "--ohlcv", str(self.csv),
                "--objective", "metrics.all.net_profit", "--sampler", "tpe",
                "--seed", "17", "--workers", str(workers), "--batch-size", "8",
                "--max-trials", str(trials), "--input-tf", "1", "--script-tf", "5",
                "--bar-magnifier", "true", "--magnifier-samples", "6",
                "--magnifier-distribution", "triangle",
                "--real-dim", "Length", "1", "100", "continuous",
                *extra]

    def run_mode(self, mode, workers=8, *extra):
        records = self.root / f"{mode}-{workers}.ndjson"
        billing = self.root / f"{mode}-{workers}.billing"
        with billing.open("wb") as stream:
            completed = subprocess.run(self.command("--trials-out", mode, "--best-k", "3",
                "--trials-file", str(records), "--progress-fd", str(stream.fileno()),
                *extra, workers=workers), capture_output=True, text=True,
                pass_fds=(stream.fileno(),), check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(records.read_bytes(), billing.read_bytes())
        self.assertTrue(records.read_bytes().endswith(b"\n"))
        lines = [json.loads(line) for line in records.read_text().splitlines()]
        self.assertEqual([line["trial_id"] for line in lines], list(range(1200)))
        for line in lines:
            self.assertTrue({"trial_id", "status", "objective", "feasible", "parameters",
                             "backtest"}.issubset(line))
            self.assertTrue({"input_bars_processed", "script_bars_processed",
                             "magnifier_sample_ticks_total"}.issubset(line["backtest"]))
        return json.loads(completed.stdout), lines

    def test_retention_and_worker_replay(self):
        complete, reference = self.run_mode("all", 1)
        self.assertEqual(complete["trials"], reference)
        for mode in ("best-k", "none"):
            result, lines = self.run_mode(mode, 8)
            self.assertEqual(reference, lines)
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["trials_completed"], 1200)
            self.assertEqual(result["summary"]["counts_by_status"], {"ok": 1200})
            self.assertEqual(len(result["summary"]["best_k"]), 3)
            self.assertEqual(len(result["trials"]), 3 if mode == "best-k" else 0)
            self.assertEqual(complete["best_trial_id"], result["best_trial_id"])
            self.assertEqual(complete["best_value"], result["best_value"])
        _, lag_one = self.run_mode("none", 1, "--batch-lag", "1")
        _, lag_eight = self.run_mode("none", 8, "--batch-lag", "1")
        self.assertEqual(lag_one, lag_eight)

    def test_bounded_pruner_worker_replay(self):
        self.csv.write_text("timestamp,open,high,low,close,volume\n" + "".join(
            f"{1700000000000 + index * 60000},100,102,99,101,10\n"
            for index in range(64)))
        flags = ("--chart-timezone", "Asia/Taipei", "--fixed-input", "BatchPrefixTest", "1")
        for pruner in ("median", "halving"):
            _, sequential = self.run_mode("none", 1, "--pruner", pruner, *flags)
            _, parallel = self.run_mode("none", 8, "--pruner", pruner, *flags)
            self.assertEqual(sequential, parallel)

    def test_huge_finite_space(self):
        flags = [value for index in range(63)
                 for value in ("--int-dim", f"Huge{index}", "1", "1000000000", "1")]
        command = self.command("--trials-out", "none", *flags, trials=16)
        position = command.index("--real-dim")
        command[position:position + 5] = ["--int-dim", "Length", "1", "100", "1"]
        completed = subprocess.run(command,
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["search_space_cardinality_overflow"])
        self.assertIsNone(result["search_space_cardinality"])

    def test_stop_flushes_whole_lines(self):
        for stop in ("deadline", "signal"):
            records = self.root / f"{stop}.ndjson"
            flags = ("--max-wall-seconds", "0.05") if stop == "deadline" else ()
            process = subprocess.Popen(self.command("--trials-out", "none", "--trials-file",
                str(records), "--fixed-input", "DelayMs", "3", *flags,
                trials=0 if stop == "deadline" else 100000),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if stop == "signal":
                time.sleep(0.05)
                process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=20)
            self.assertIn(process.returncode, (0, 2), errors)
            result = json.loads(output)
            content = records.read_bytes()
            self.assertTrue(not content or content.endswith(b"\n"))
            lines = [json.loads(line) for line in content.splitlines()]
            ids = [line["trial_id"] for line in lines]
            self.assertEqual(ids, sorted(set(ids)))
            self.assertEqual(result["trials_completed"], len(lines))


if __name__ == "__main__":
    unittest.main()
