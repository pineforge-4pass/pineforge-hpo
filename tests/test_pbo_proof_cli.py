#!/usr/bin/env python3
"""Proof-only CLI contracts, including original execution rather than a curve rerun."""

from __future__ import annotations

import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
PRODUCTION, PROOF, PLUGIN = (Path(value).resolve() for value in sys.argv[1:4])
del sys.argv[1:4]
EQUITIES = (1, 2, 6, 24, 24, 48)


def bits(value):
    return struct.pack(">d", float(value)).hex()


class ProofCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.csv = self.directory / "bars.csv"
        self.csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            + "".join(f"{1700000000000 + index * 60000},100,102,99,101,10\n"
                      for index in range(32)), encoding="utf-8",
        )

    def command(self, executable, trace, *extra):
        return [
            str(executable), "run", "--strategy", str(PLUGIN), "--ohlcv", str(self.csv),
            "--input-tf", "1", "--script-tf", "5", "--chart-timezone", "UTC",
            "--bar-magnifier", "true", "--magnifier-samples", "6",
            "--magnifier-distribution", "triangle",
            "--objective", "metrics.all.net_profit", "--sampler", "grid",
            "--int-dim", "Length", "10", "11", "1", "--max-trials", "2", "--seed", "17",
            "--workers", "1", "--batch-size", "1",
            "--fixed-input", "BatchPrefixTest", "1",
            "--fixed-input", "BatchPrefixJitter", "0",
            "--fixed-input", "CurveEquity", ",".join(map(str, EQUITIES)),
            "--fixed-input", "ProofRunTrace", str(trace), *extra,
        ]

    def run_native(self, executable, trace, *extra):
        completed = subprocess.run(self.command(executable, trace, *extra),
                                   capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def test_production_help_and_option_surface_are_isolated(self):
        production = subprocess.run([str(PRODUCTION), "--help"], capture_output=True,
                                    text=True, check=False)
        proof = subprocess.run([str(PROOF), "--help"], capture_output=True,
                               text=True, check=False)
        self.assertEqual((production.returncode, proof.returncode), (0, 0))
        self.assertNotIn("--proof-curves", production.stdout)
        self.assertEqual(sum("--proof-curves" in line for line in proof.stdout.splitlines()), 1)
        without_proof_line = "".join(
            line for line in proof.stdout.splitlines(keepends=True) if "--proof-curves" not in line
        )
        self.assertEqual(without_proof_line, production.stdout)
        trace = self.directory / "refused-trace.csv"
        completed = subprocess.run(
            self.command(PRODUCTION, trace, "--proof-curves", str(self.directory / "never.jsonl")),
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("unknown option `--proof-curves`", completed.stderr)
        self.assertFalse(trace.exists())

    def test_without_curve_request_the_result_bytes_match_production(self):
        trace = self.directory / "trace.csv"
        production = self.run_native(PRODUCTION, trace)
        proof = self.run_native(PROOF, trace)
        self.assertEqual(proof.stdout, production.stdout)

    def verify_original(self, pruning):
        curves = self.directory / "curves.jsonl"
        trace = self.directory / "trace.csv"
        extra = ["--pbo-blocks", "2", "--proof-curves", str(curves)]
        if pruning:
            extra += ["--pruner", "median"]
        completed = self.run_native(PROOF, trace, *extra)
        result = json.loads(completed.stdout)
        self.assertEqual([row["status"] for row in result["trials"]], ["ok", "ok"])
        self.assertEqual(result["return_blocks"]["n_b"], [3, 2])
        records = [json.loads(line) for line in curves.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["trial_id"] for record in records], [0, 1])
        actual_calls = [tuple(map(int, line.split(",")))
                        for line in trace.read_text(encoding="utf-8").splitlines()]
        # The 32 bars and default rungs give ceil(32*0.25)=8, ceil(32*0.5)=16, then 32.
        # The median needs two prior observations per rung, so both trials finish all rungs.
        # One worker runs Length 10 before 11: full-window invocations are 3 and 6,
        # or 1 and 2 without prefixes. Only those full-window curves may be dumped.
        expected_calls = (
            [(10, 8, 1), (10, 16, 2), (10, 32, 3),
             (11, 8, 4), (11, 16, 5), (11, 32, 6)]
            if pruning else [(10, 32, 1), (11, 32, 2)]
        )
        self.assertEqual(actual_calls, expected_calls)
        for record, invocation in zip(records, (3, 6) if pruning else (1, 2)):
            self.assertEqual(
                record["curve"],
                [[1700000000000 + index * 60000, bits(equity * invocation)]
                 for index, equity in enumerate(EQUITIES)],
            )
        for row in result["trials"]:
            self.assertEqual(row["block_returns"],
                             {"status": "ok", "count": [3, 2], "sum": [6.0, 1.0], "m2": [2.0, 0.5]})

    def test_dump_uses_the_original_full_window_execution(self):
        self.verify_original(False)

    def test_prefix_curves_are_not_dumped(self):
        self.verify_original(True)

    def test_missing_output_directory_fails_before_execution(self):
        trace = self.directory / "trace.csv"
        completed = subprocess.run(
            self.command(PROOF, trace, "--proof-curves",
                         str(self.directory / "missing" / "curves.jsonl")),
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("cannot open proof curves file", completed.stderr)
        self.assertFalse(trace.exists())


if __name__ == "__main__":
    unittest.main()
