#!/usr/bin/env python3
"""Row-space serialization contracts against the real native runner."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_native_cli import invoke

ROOT = Path(__file__).resolve().parents[1]
NATIVE = Path(sys.argv.pop(1)).resolve()
PLUGIN = Path(sys.argv.pop(1)).resolve()


class RowSpaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.csv = self.root / "bars.csv"
        self.csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            "1700000000000,100,102,99,101,10\n"
            "1700000060000,101,103,100,102,11\n",
            encoding="utf-8",
        )

    def run_study(self, name, *extra, trials=3, high=16, expected_exit=0):
        final = self.root / f"{name}.json"
        records = self.root / f"{name}.jsonl"
        progress = self.root / f"{name}.progress.jsonl"
        with progress.open("wb") as stream:
            completed = subprocess.run(
                [
                    str(NATIVE), "run", "--strategy", str(PLUGIN),
                    "--ohlcv", str(self.csv), "--objective", "metrics.all.net_profit",
                    "--sampler", "grid", "--max-trials", str(trials), "--workers", "1",
                    "--input-tf", "1", "--script-tf", "5",
                    "--chart-timezone", "Asia/Taipei",
                    "--bar-magnifier", "true", "--magnifier-samples", "6",
                    "--magnifier-distribution", "triangle",
                    "--int-dim", "Length", "14", str(high), "1",
                    "--output", str(final), "--trials-file", str(records),
                    "--progress-fd", str(stream.fileno()), *extra,
                ],
                capture_output=True,
                pass_fds=(stream.fileno(),),
                check=False,
            )
        self.assertEqual(completed.returncode, expected_exit, completed.stderr)
        self.assertEqual(final.read_bytes(), completed.stdout)
        self.assertEqual(records.read_bytes(), progress.read_bytes())
        content = records.read_bytes()
        self.assertTrue(content.endswith(b"\n"))
        rows = [json.loads(line) for line in content.splitlines()]
        result = json.loads(completed.stdout)
        self.assertEqual(len(rows), result["trials_completed"])
        self.assertEqual(
            [row["trial_id"] for row in rows],
            sorted({row["trial_id"] for row in rows}),
        )
        return completed.stdout, result, rows, content

    def assert_row_space(self, result, rows, mode):
        self.assertIn("space", result)
        self.assertEqual(result["space_hash_version"], 1)
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn("space", row)
            if mode == "hash":
                self.assertEqual(row["space_hash_version"], result["space_hash_version"])
                self.assertEqual(row["space_hash"], result["space_hash"])
            else:
                self.assertNotIn("space_hash_version", row)
                self.assertNotIn("space_hash", row)

    def test_default_and_explicit_full_match_legacy_bytes(self):
        # Transcribed from the renderer and fake strategy before the row-space option
        # was introduced. Only the package version and SHA-256
        # of this literal canonical v1 space are substituted; no runner output
        # is used to construct the expected bytes.
        canonical = (
            b'{"objective":{"constraints":[],"direction":"maximize",'
            b'"expression":"metrics.all.net_profit"},"parameters":{"Length":'
            b'{"high":"14","kind":"integer","log":false,"low":"14","step":"1"}},'
            b'"space_hash_version":1}'
        )
        expected = (ROOT / "tests/fixtures/row-space-full.json.in").read_bytes()
        expected = expected.replace(
            b"@SPACE_HASH@", hashlib.sha256(canonical).hexdigest().encode("ascii")
        ).replace(b"@VERSION@", (ROOT / "VERSION").read_bytes().strip())
        baseline, _, _, baseline_lines = self.run_study("default", trials=1, high=14)
        self.assertEqual(baseline, expected)
        expected_lines = next(
            line[4:] + b"\n" for line in expected.splitlines() if line.startswith(b"    {")
        )
        self.assertEqual(baseline_lines, expected_lines)
        explicit, _, _, explicit_lines = self.run_study(
            "full", "--row-space", "full", trials=1, high=14
        )
        self.assertEqual(explicit, expected)
        self.assertEqual(explicit, baseline)
        self.assertEqual(explicit_lines, baseline_lines)

    def check_retention_modes(self, mode):
        for retention in ("all", "best-k", "none"):
            with self.subTest(retention=retention):
                flags = ("--trials-out", retention, "--best-k", "2")
                _, baseline, baseline_rows, _ = self.run_study(
                    f"baseline-{retention}", *flags
                )
                _, result, rows, _ = self.run_study(
                    f"{mode}-{retention}", *flags, "--row-space", mode
                )
                self.assertEqual(len(rows), 3)
                retained = result["trials"] + result.get("summary", {}).get("best_k", [])
                expected_count = {"all": 3, "best-k": 2, "none": 0}[retention]
                self.assertEqual(len(result["trials"]), expected_count)
                self.assert_row_space(result, rows + retained, mode)
                # Pin every other field, including top-level space, metrics,
                # best selection and retention, to the same deterministic run.
                for row in (
                    baseline_rows + baseline["trials"] +
                    baseline.get("summary", {}).get("best_k", [])
                ):
                    row.pop("space", None)
                    if mode == "none":
                        row.pop("space_hash_version", None)
                        row.pop("space_hash", None)
                self.assertEqual(rows, baseline_rows)
                self.assertEqual(result, baseline)
                self.assertEqual(result["best_trial_id"], 2)

    def test_hash_rows_and_retained_winners(self):
        self.check_retention_modes("hash")

    def test_none_rows_and_retained_winners(self):
        self.check_retention_modes("none")

    def test_hash_rows_join_top_level_space(self):
        _, result, rows, _ = self.run_study("join", "--row-space", "hash")
        # Independently apply the documented v1 integer-space encoding to the
        # actual top-level space, without calling either production hash helper.
        space = result["space"]
        length = space["parameters"]["Length"]
        self.assertEqual(length["kind"], "integer")
        canonical = {
            "space_hash_version": 1,
            "parameters": {
                "Length": {
                    "kind": "integer",
                    "log": length["log"],
                    **{key: str(length[key]) for key in ("low", "high", "step")},
                }
            },
            "objective": {
                **space["objective"],
                "constraints": sorted(space["objective"]["constraints"]),
            },
        }
        digest = hashlib.sha256(
            json.dumps(
                canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(result["space_hash"], digest)
        for row in rows + result["trials"]:
            self.assertEqual(row["space_hash"], digest)
            self.assertNotIn("space", row)

    def test_unknown_row_space_has_typed_usage_failure(self):
        for value in ("unknown", "FULL", "", "best-k"):
            with self.subTest(value=value):
                rejected = invoke(NATIVE, PLUGIN, self.csv, "--row-space", value)
                self.assertEqual(rejected.returncode, 1)
                self.assertEqual(
                    json.loads(rejected.stdout)["failure"],
                    {
                        "origin": "hpo",
                        "code": "hpo_cli_usage",
                        "args": {},
                        "exit_code": 1,
                    },
                )
                self.assertIn("--row-space must be full, hash, or none", rejected.stderr)

    def test_compact_ancestor_rows_still_support_continuation(self):
        self.run_study("parent", trials=1, high=17)
        for mode in ("hash", "none"):
            with self.subTest(mode=mode):
                _, result, rows, _ = self.run_study(
                    mode, "--warm-start", str(self.root / "parent.json"),
                    "--row-space", mode, trials=1, high=17
                )
                self.assertEqual(len(result["warm_start_trials"]), 1)
                self.assert_row_space(
                    result, rows + result["trials"] + result["warm_start_trials"], mode
                )
                _, child, _, _ = self.run_study(
                    f"{mode}-child", "--warm-start", str(self.root / f"{mode}.json"),
                    trials=1, high=17
                )
                self.assertEqual(child["warm_start"]["trials"], 2)
                self.assertEqual(child["trials"][0]["trial_id"], 2)

    def test_compact_error_and_timeout_rows(self):
        for mode in ("hash", "none"):
            for status, flags, exit_code in (
                ("engine_error", ("--fixed-input", "fail", "true"), 2),
                ("trial_timeout", ("--fixed-input", "HangAtLength", "14",
                                   "--trial-timeout-seconds", "0.2"), 3),
            ):
                with self.subTest(mode=mode, status=status):
                    _, result, rows, _ = self.run_study(
                        f"{mode}-{status}", "--row-space", mode, *flags,
                        trials=1, high=14, expected_exit=exit_code
                    )
                    self.assertEqual([row["status"] for row in rows], [status])
                    self.assert_row_space(result, rows + result["trials"], mode)


if __name__ == "__main__":
    unittest.main()
