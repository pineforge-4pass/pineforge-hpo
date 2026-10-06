#!/usr/bin/env python3
"""Failure channels, hostile diagnostics and checked/legacy execution parity."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


NATIVE, LEGACY, CODED, STATUS = (Path(sys.argv.pop(1)).resolve() for _ in range(4))


class FailureCodesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.csv = self.root / "bars.csv"
        self.csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            "1700000000000,100,102,99,101,10\n"
            "1700000060000,100,102,99,101,10\n"
        )

    def command(self, *extra, plugin=CODED):
        return [
            str(NATIVE),
            "run",
            "--strategy",
            str(plugin),
            "--ohlcv",
            str(self.csv),
            "--objective",
            "metrics.all.net_profit",
            "--sampler",
            "grid",
            "--seed",
            "17",
            "--workers",
            "1",
            "--max-trials",
            "3",
            "--input-tf",
            "1",
            "--script-tf",
            "5",
            "--bar-magnifier",
            "true",
            "--magnifier-samples",
            "6",
            "--magnifier-distribution",
            "triangle",
            "--int-dim",
            "Length",
            "1",
            "3",
            "1",
            *extra,
        ]

    def run_study(self, *extra, plugin=CODED, env=None, exit_code=2):
        billing = self.root / "billing.ndjson"
        records = self.root / "trials.ndjson"
        with billing.open("wb") as stream:
            completed = subprocess.run(
                self.command(
                    "--trials-file",
                    str(records),
                    "--progress-fd",
                    str(stream.fileno()),
                    *extra,
                    plugin=plugin,
                ),
                capture_output=True,
                text=True,
                pass_fds=(stream.fileno(),),
                env=env,
            )
        self.assertEqual(completed.returncode, exit_code, completed.stderr)
        self.assertEqual(records.read_bytes(), billing.read_bytes())
        lines = [json.loads(line) for line in records.read_text().splitlines()]
        result = json.loads(completed.stdout)
        self.assertEqual(result["trials"], lines)
        self.assertEqual(len(lines), 3)
        for line in lines:
            keys = list(line)
            position = keys.index("error")
            self.assertEqual(
                keys[position + 1 : position + 4],
                ["failure_code", "failure_args", "failure_origin"],
            )
        return result, lines

    def test_silent_runtime_failure_is_not_scored(self):
        result, lines = self.run_study("--fixed-input", "FailureMode", "silent")
        self.assertIsNone(result["best_trial_id"])
        for trial in lines:
            self.assertEqual(trial["status"], "engine_error")
            self.assertEqual(trial["failure_code"], "strategy_runtime_error")
            self.assertEqual(trial["failure_args"], {})
            self.assertEqual(trial["failure_origin"], "engine")
            self.assertEqual(trial["error"], "")
            self.assertIsNone(trial["objective"])
            self.assertIsNone(trial["net_profit"])

    def test_independent_failure_channels_and_optional_getters(self):
        for mode in ("text_only", "code_only", "silent"):
            with self.subTest(mode=mode):
                _, lines = self.run_study("--fixed-input", "FailureMode", mode)
                self.assertTrue(
                    all(trial["status"] == "engine_error" for trial in lines)
                )
        for plugin, mode in ((LEGACY, "text_only"), (STATUS, "silent")):
            with self.subTest(plugin=plugin, mode=mode):
                _, lines = self.run_study(
                    "--fixed-input", "FailureMode", mode, plugin=plugin
                )
                for trial in lines:
                    self.assertEqual(trial["status"], "engine_error")
                    self.assertIsNone(trial["failure_code"])
                    self.assertIsNone(trial["failure_args"])
                    self.assertEqual(trial["failure_origin"], "engine")

    def test_unknown_engine_code_and_scalar_canonicalization(self):
        _, lines = self.run_study("--fixed-input", "FailureMode", "unknown_code")
        for trial in lines:
            self.assertEqual(trial["failure_code"], "future_engine_failure")
            self.assertEqual(list(trial["failure_args"]), sorted(trial["failure_args"]))
            self.assertEqual(trial["failure_origin"], "engine")
        raw = (self.root / "trials.ndjson").read_text()
        self.assertNotIn("1.00", raw)

    def test_status_without_code_and_invalid_report(self):
        _, lines = self.run_study("--fixed-input", "FailureMode", "status_only")
        for trial in lines:
            self.assertEqual(trial["status"], "engine_error")
            self.assertIsNone(trial["failure_code"])
            self.assertIsNone(trial["failure_args"])
            self.assertEqual(trial["failure_origin"], "engine")
        _, lines = self.run_study("--fixed-input", "FailureMode", "invalid_report")
        for trial in lines:
            self.assertEqual(trial["status"], "trial_error")
            self.assertEqual(trial["failure_code"], "hpo_report_invalid")
            self.assertEqual(trial["failure_origin"], "hpo")

    def test_invalid_engine_args_are_dropped(self):
        for mode in (
            "nested_args",
            "array_args",
            "invalid_args",
            "nonfinite_args",
            "duplicate_args",
            "large_args",
        ):
            with self.subTest(mode=mode):
                _, lines = self.run_study("--fixed-input", "FailureMode", mode)
                for trial in lines:
                    self.assertEqual(trial["failure_code"], "strategy_runtime_error")
                    self.assertIsNone(trial["failure_args"])

    def test_utf8_text_cap_and_forged_diagnostics(self):
        _, lines = self.run_study("--fixed-input", "FailureMode", "unicode")
        for trial in lines:
            self.assertEqual(len(trial["error"].encode("utf-8")), 4094)
            self.assertEqual(trial["failure_code"], "strategy_runtime_error")
        _, lines = self.run_study("--fixed-input", "FailureMode", "forged")
        for trial in lines:
            self.assertEqual(trial["failure_code"], "strategy_runtime_error")
            self.assertEqual(trial["failure_origin"], "engine")

    def test_checked_settings_refuse_bad_titles_values_keys_and_enums(self):
        cases = (
            ("--fixed-input", "Unknown", "1", "strategy_set_input", "unknown_key"),
            ("--fixed-input", "length", "1", "strategy_set_input", "unknown_key"),
            (
                "--fixed-input",
                "FixedNumber",
                "not-a-number",
                "strategy_set_input",
                "unparseable_value",
            ),
            (
                "--strategy-override",
                "unknown_override",
                "1",
                "strategy_set_override",
                "unknown_key",
            ),
            (
                "--strategy-override",
                "commission_type",
                "not-an-enum",
                "strategy_set_override",
                "unparseable_value",
            ),
        )
        for flag, key, value, entrypoint, reason in cases:
            with self.subTest(key=key):
                _, lines = self.run_study(flag, key, value)
                for trial in lines:
                    self.assertEqual(trial["status"], "trial_error")
                    self.assertEqual(trial["failure_code"], "setting_rejected")
                    self.assertEqual(
                        trial["failure_args"],
                        {"entrypoint": entrypoint, "reason": reason},
                    )
                    self.assertEqual(trial["failure_origin"], "engine")

    def test_checked_and_legacy_success_are_identical(self):
        results = []
        for plugin in (LEGACY, CODED):
            _, lines = self.run_study(plugin=plugin, exit_code=0)
            for trial in lines:
                self.assertIsNone(trial["failure_code"])
                self.assertIsNone(trial["failure_args"])
                self.assertIsNone(trial["failure_origin"])
            results.append(json.dumps(lines, separators=(",", ":")))
        self.assertEqual(*results)

    def test_checked_factory_and_metric_failures(self):
        _, lines = self.run_study(env={**os.environ, "PFH_TEST_CREATE_REFUSED": "1"})
        for trial in lines:
            self.assertEqual(trial["status"], "trial_error")
            self.assertEqual(trial["failure_code"], "hpo_strategy_create_failed")
            self.assertEqual(trial["failure_origin"], "hpo")
            self.assertEqual(trial["failure_args"], {})
        command = self.command()
        command[command.index("--objective") + 1] = "metrics.all.net_profit / 0"
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        for trial in json.loads(completed.stdout)["trials"]:
            self.assertEqual(trial["status"], "objective_error")
            self.assertEqual(trial["failure_code"], "hpo_metric_expression_failed")
            self.assertEqual(trial["failure_origin"], "hpo")

    def test_init_failure_and_output_failure_are_single_documents(self):
        commands = (
            ([str(NATIVE), "run"], "hpo_cli_usage"),
            (self.command(plugin=self.root / "missing.so"), "hpo_plugin_invalid"),
            (
                self.command("--output", str(self.root / "missing" / "result.json")),
                "hpo_output_io_failed",
            ),
            (
                self.command(
                    "--scheduler-stats", str(self.root / "missing" / "stats.json")
                ),
                "hpo_output_io_failed",
            ),
        )
        for command, code in commands:
            with self.subTest(code=code, command=command):
                completed = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(completed.returncode, 1)
                self.assertTrue(completed.stderr.startswith("pineforge-hpo-native:"))
                document = json.loads(completed.stdout)
                self.assertEqual(list(document), ["schema_version", "ok", "failure"])
                self.assertFalse(document["ok"])
                self.assertEqual(document["failure"]["code"], code)
                self.assertEqual(document["failure"]["origin"], "hpo")
                self.assertEqual(document["failure"]["exit_code"], 1)
        self.csv.write_text("bad header\n")
        completed = subprocess.run(self.command(), capture_output=True, text=True)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            json.loads(completed.stdout)["failure"]["code"], "hpo_dataset_invalid"
        )


if __name__ == "__main__":
    unittest.main()
