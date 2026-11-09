#!/usr/bin/env python3
"""A missing v0.12.0 baseline fails when required and skips when optional.

Usage: test_v012_required_baseline.py NATIVE PLUGIN [unittest arguments]
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


if len(sys.argv) < 3:
    sys.exit("Usage: test_v012_required_baseline.py NATIVE PLUGIN [unittest arguments]")

NATIVE = Path(sys.argv.pop(1)).resolve()
PLUGIN = Path(sys.argv.pop(1)).resolve()
COMPARATOR = Path(__file__).resolve().with_name("test_rolling_default_v012.py")

for label, path in (("NATIVE", NATIVE), ("PLUGIN", PLUGIN), ("comparator", COMPARATOR)):
    if not path.is_file():
        sys.exit(f"ERROR: {label} must be an existing regular file: {path}")


class MissingBaselinePolicyTests(unittest.TestCase):
    def test_missing_baseline_required_and_optional(self) -> None:
        cases = (
            (True, 1, "ERROR: required v0.12.0 native binary was not provided"),
            (False, 77, "SKIP: no v0.12.0 native binary was provided"),
        )
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "missing-v012-native"
            for required, expected_returncode, expected_prefix in cases:
                with self.subTest(required=required):
                    self.assertFalse(baseline.exists(), f"baseline must not exist: {baseline}")
                    command = [
                        sys.executable,
                        "-I",
                        str(COMPARATOR),
                        str(NATIVE),
                        str(baseline),
                        str(PLUGIN),
                    ]
                    if required:
                        command.append("--require-baseline")
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                    )
                    details = (
                        f"command: {completed.args!r}\n"
                        f"exit: {completed.returncode}\n"
                        f"stderr:\n{completed.stderr}"
                    )
                    self.assertEqual(completed.returncode, expected_returncode, details)
                    self.assertIn(expected_prefix, completed.stderr, details)


if __name__ == "__main__":
    unittest.main()
