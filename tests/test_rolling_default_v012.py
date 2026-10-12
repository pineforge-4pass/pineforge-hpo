#!/usr/bin/env python3
"""The default schedule gives the bytes of v0.12.0.

A native binary built from the v0.12.0 tag (the baseline) and the build under test run the same
studies at 1, 2, 4 and 8 workers: the baseline without any schedule option, the build under test
once without it and once with `--schedule batches`. For grid, random, tpe and dlib_global the
`trials[]` bytes and the whole result must match byte for byte; the only difference allowed is the
version string that names the running release. (The sobol and candidates samplers do not exist in
v0.12.0, so they have no baseline.)

The test needs two binaries, so it takes the baseline path as an argument. When the path is not a
file the test reports itself skipped (exit status 77, which ctest shows as skipped); configure with
-DPINEFORGE_HPO_V012_NATIVE=<path> to run it.

Usage: test_rolling_default_v012.py NATIVE BASELINE PLUGIN [unittest arguments]
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


NATIVE = Path(sys.argv.pop(1)).resolve()
BASELINE_PATH = Path(sys.argv.pop(1))
PLUGIN = Path(sys.argv.pop(1)).resolve()

if not BASELINE_PATH.is_file():
    sys.stderr.write(
        "SKIP: no v0.12.0 native binary was provided (configure with "
        "-DPINEFORGE_HPO_V012_NATIVE=<path to a pineforge-hpo-native built from the v0.12.0 tag>)\n"
    )
    sys.exit(77)
BASELINE = BASELINE_PATH.resolve()

VERSION_FIELD = re.compile(rb'"pineforge_hpo_version": "[^"]*"')
VERSION_PLACEHOLDER = b'"pineforge_hpo_version": "<version>"'


class DefaultUnchangedTests(unittest.TestCase):
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

    def arguments(self, *extra: str, workers: int = 4, sampler: str = "tpe"):
        return [
            "run", "--strategy", str(PLUGIN),
            "--ohlcv", str(self.csv), "--objective", "metrics.all.net_profit",
            "--sampler", sampler, "--max-trials", "32", "--seed", "90210",
            "--workers", str(workers), "--input-tf", "1", "--script-tf", "5",
            "--chart-timezone", "Asia/Taipei", "--bar-magnifier", "true",
            "--magnifier-samples", "6", "--magnifier-distribution", "triangle",
            "--fixed-input", "BatchPrefixTest", "1",
            "--int-dim", "Length", "1", "100", "1", *extra,
        ]

    def invoke(self, binary: Path, *extra: str, workers: int = 4, sampler: str = "tpe"):
        return subprocess.run(
            [str(binary), *self.arguments(*extra, workers=workers, sampler=sampler)],
            capture_output=True, check=False,
        )

    def stdout_of(self, completed) -> bytes:
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        return completed.stdout

    def trial_bytes(self, completed) -> bytes:
        stdout = self.stdout_of(completed)
        start = stdout.index(b'  "trials": [') + len(b'  "trials": ')
        return stdout[start:stdout.rfind(b"\n}")]

    def whole_result(self, completed) -> bytes:
        """The whole result as raw bytes with only the version string replaced."""
        replaced, count = VERSION_FIELD.subn(VERSION_PLACEHOLDER, self.stdout_of(completed))
        self.assertEqual(count, 1, "the result must record its version exactly once")
        return replaced

    def test_the_two_binaries_are_different_builds(self) -> None:
        """Fails if the baseline is a copy of the build under test, which would make every
        comparison below prove nothing: the two files must differ, and only the build under test
        may accept --schedule."""
        self.assertNotEqual(hashlib.sha256(NATIVE.read_bytes()).hexdigest(),
                            hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
                            "the baseline binary is the build under test")
        baseline = self.invoke(BASELINE, "--schedule", "rolling", sampler="grid")
        self.assertNotEqual(baseline.returncode, 0,
                            "the baseline accepted --schedule: it is not a v0.12.0 build")
        current = self.invoke(NATIVE, "--schedule", "rolling", sampler="grid")
        self.assertEqual(current.returncode, 0, current.stderr.decode(errors="replace"))

    def test_default_matches_v012(self) -> None:
        """Fails on any byte of difference in trials[] or anywhere else in the whole result,
        except the version string, between the baseline and the build under test with the
        schedule unset or set to batches, for each sampler at each worker count."""
        for sampler in ("grid", "random", "tpe", "dlib_global"):
            for workers in (1, 2, 4, 8):
                with self.subTest(sampler=sampler, workers=workers):
                    baseline = self.invoke(BASELINE, sampler=sampler, workers=workers)
                    omitted = self.invoke(NATIVE, sampler=sampler, workers=workers)
                    explicit = self.invoke(NATIVE, "--schedule", "batches",
                                           sampler=sampler, workers=workers)
                    reference = self.trial_bytes(baseline)
                    self.assertEqual(self.trial_bytes(omitted), reference,
                                     "trials[] differs with the schedule unset")
                    self.assertEqual(self.trial_bytes(explicit), reference,
                                     "trials[] differs with --schedule batches")
                    whole = self.whole_result(baseline)
                    self.assertEqual(self.whole_result(omitted), whole,
                                     "the result differs with the schedule unset")
                    self.assertEqual(self.whole_result(explicit), whole,
                                     "the result differs with --schedule batches")


if __name__ == "__main__":
    unittest.main()
