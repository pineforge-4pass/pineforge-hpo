#!/usr/bin/env python3
"""External native-CLI contracts for full-window PBO block moments.

Usage: test_pbo_blocks_cli.py NATIVE PLUGIN [unittest arguments].
Numeric expectations are hand-derived literals from equities supplied to the fake plugin.
These fixtures check synthetic mechanics; they do not prove PBO on real data.
The early unbound-identity refusal is covered by test_return_stats_integration.py.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


NATIVE = Path(sys.argv.pop(1)).resolve()
PLUGIN = Path(sys.argv.pop(1)).resolve()
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from pineforge_hpo import pbo as pbo_module  # noqa: E402


CURVE = "1,2,1.5,1.5,1.875"
SPACE = ("--int-dim", "Length", "10", "13", "1")
IDENTITY_PREFIX = "pineforge-hpo-return-stats-build/v2:sha256:"
LAYOUT_KEYS = ("contract", "series", "S", "K", "n_b", "boundary_time_ms",
               "numeric_build_identity")

# Adjacent returns: 2/1-1 = 1; 1.5/2-1 = -0.25; 1.5/1.5-1 = 0;
# 1.875/1.5-1 = 0.25. Block 0: sum = 0.75, mean = 0.375,
# m2 = (1-0.375)^2 + (-0.25-0.375)^2 = 2*0.390625 = 0.78125.
# Block 1: sum = 0.25, mean = 0.125,
# m2 = (0-0.125)^2 + (0.25-0.125)^2 = 2*0.015625 = 0.03125.
MOMENTS = {"status": "ok", "count": [2, 2], "sum": [0.75, 0.25],
           "m2": [0.78125, 0.03125]}
# Curve times are 1700000000000 + 60000*i; the S=2 boundaries open at i=0,2,4.
BOUNDARIES = [1700000000000, 1700000120000, 1700000240000]

# Ordered key goldens of an ordinary grid result, written independently of native output.
RESULT_KEYS = (
    "schema_version", "space_hash_version", "space_hash", "space", "pineforge_hpo_version",
    "sampler_implementation", "ok", "artifact_key", "sampler", "candidate_policy",
    "candidate_policy_implementation", "seed", "tpe_history_switch", "sampler_config",
    "direction", "batch_size", "batch_lag", "replay_contract", "continuation_contract",
    "pruner", "pruner_eta", "pruner_rungs", "search_space_finite",
    "search_space_cardinality_overflow", "search_space_cardinality", "trials_requested",
    "trials_completed", "unique_candidates_attempted", "duplicate_proposals_skipped",
    "remaining_candidates", "search_space_exhausted", "stop_reason", "full_parameter_coverage",
    "exhaustive_equivalent", "best_trial_id", "best_value", "trials", "trials_out", "best_k",
)
ROW_KEYS = (
    "trial_id", "status", "feasible", "objective", "total_trades", "net_profit", "backtest",
    "parameters", "constraint_values", "metrics", "error", "failure_code", "failure_args",
    "failure_origin", "space_hash_version", "space_hash", "space",
)


def native_argv(csv, *extra, curve=CURVE, native=NATIVE, plugin=PLUGIN, space=SPACE):
    return [
        str(native), "run", "--strategy", str(plugin), "--ohlcv", str(csv),
        "--objective", "metrics.all.net_profit", "--input-tf", "1", "--script-tf", "5",
        "--chart-timezone", "UTC", "--bar-magnifier", "true", "--magnifier-samples", "6",
        "--magnifier-distribution", "triangle", "--fixed-input", "BatchPrefixTest", "1",
        "--fixed-input", "BatchPrefixJitter", "0", "--fixed-input", "CurveEquity", curve,
        *space, "--sampler", "grid", "--max-trials", "4", "--batch-size", "2",
        *[str(item) for item in extra],
    ]


def write_bars(csv):
    csv.write_text(
        "timestamp,open,high,low,close,volume\n"
        + "".join(f"{1700000000000 + index * 60000},100,102,99,101,10\n"
                  for index in range(64)), encoding="utf-8")


def setUpModule():
    with tempfile.TemporaryDirectory(prefix="pf_pbo_probe_") as directory:
        csv = Path(directory) / "bars.csv"
        write_bars(csv)
        probe = subprocess.run(native_argv(csv, "--pbo-blocks", "2", "--max-trials", "1"),
                               text=True, capture_output=True, check=False)
    document = json.loads(probe.stdout)
    failure = document.get("failure")
    if failure and failure.get("code") == "hpo_toolchain_unavailable":
        required = (os.environ.get("PFH_REQUIRE_RETURN_STATS_IDENTITY") == "1"
                    or os.environ.get("PFH_PBO_PROOF") == "1")
        message = "the native return-statistics identity is unbound: " + probe.stderr.strip()
        if required:
            raise AssertionError(message)
        raise unittest.SkipTest(message + " (early refusal: test_return_stats_integration.py)")
    if probe.returncode != 0:
        raise AssertionError(f"the block-request probe failed: {probe.stderr}")


class PboBlocksCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pf_pbo_cli_")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.csv = self.directory / "bars.csv"
        write_bars(self.csv)

    def run_cli(self, *extra, expected=0, pass_fds=(), **options):
        completed = subprocess.run(native_argv(self.csv, *extra, **options), text=True,
                                   capture_output=True, check=False, pass_fds=pass_fds)
        self.assertEqual(completed.returncode, expected, completed.stderr)
        return completed

    def result(self, *extra, **options):
        return json.loads(self.run_cli(*extra, **options).stdout)

    def assert_refusal(self, completed, code, args):
        self.assertEqual(completed.returncode, 1, completed.stderr)
        document = json.loads(completed.stdout)
        self.assertFalse(document["ok"])
        self.assertNotIn("trials", document)
        self.assertEqual(document["failure"],
                         {"origin": "hpo", "code": code, "args": args, "exit_code": 1})

    def assert_layout(self, document, *, S=2, K=4, counts=None, boundaries=None):
        layout = document["return_blocks"]
        self.assertEqual(tuple(layout), LAYOUT_KEYS)
        self.assertEqual(layout["contract"], "pineforge-hpo-return-blocks/v1")
        self.assertEqual(layout["series"], "bar")
        self.assertIs(type(layout["S"]), int)
        self.assertIs(type(layout["K"]), int)
        self.assertEqual(layout["S"], S)
        self.assertEqual(layout["K"], K)
        self.assertEqual(layout["n_b"], [2, 2] if counts is None else counts)
        self.assertEqual(layout["boundary_time_ms"],
                         BOUNDARIES if boundaries is None else boundaries)
        for value in (*layout["n_b"], *layout["boundary_time_ms"]):
            self.assertIs(type(value), int)
        identity = layout["numeric_build_identity"]
        self.assertRegex(identity, "^" + IDENTITY_PREFIX + "[0-9a-f]{64}$")
        return layout

    def assert_moments(self, row, expected=MOMENTS):
        self.assertEqual(row["block_returns"], expected)
        self.assertEqual(tuple(row["block_returns"]), ("status", "count", "sum", "m2"))
        for count in row["block_returns"]["count"]:
            self.assertIs(type(count), int)

    def parts(self, document):
        declared = dict(document["return_blocks"])
        declared["dataset_identity"] = "synthetic curve fixture"
        return [(declared, document["trials"])]

    def test_no_flag_preserves_ordered_key_goldens(self):
        document = self.result()
        self.assertEqual(tuple(document), RESULT_KEYS)
        self.assertNotIn("return_blocks", document)
        self.assertNotIn("return_stats", document)
        self.assertEqual([row["trial_id"] for row in document["trials"]], [0, 1, 2, 3])
        # UTC fixture profit is Length + 100000/1000, independently of its equity curve.
        self.assertEqual([row["objective"] for row in document["trials"]],
                         [110, 111, 112, 113])
        for row in document["trials"]:
            self.assertEqual(tuple(row), ROW_KEYS)
            self.assertNotIn("block_returns", row)

    def test_help_names_the_native_option(self):
        completed = subprocess.run([str(NATIVE), "--help"], text=True, capture_output=True,
                                   check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--pbo-blocks S", completed.stdout)

    def test_two_blocks_publish_hand_derived_moments_and_integer_layout(self):
        document = self.result("--pbo-blocks", "2")
        self.assert_layout(document)
        self.assertNotIn("return_stats", document)
        for row in document["trials"]:
            self.assertEqual(row["status"], "ok")
            self.assert_moments(row)

    def test_uneven_blocks_use_the_curve_boundaries(self):
        # Returns (1,-0.25,0 | 0.25,0.25): K=5,S=2 opens at points 0,3,5.
        # First mean=0.25, m2=0.75^2+(-0.5)^2+(-0.25)^2=0.875;
        # second mean=0.25, m2=0. Both sums are dyadic and exact.
        document = self.result("--pbo-blocks", "2", curve="1,2,1.5,1.5,1.875,2.34375")
        self.assert_layout(document, K=5, counts=[3, 2],
                           boundaries=[1700000000000, 1700000180000, 1700000300000])
        for row in document["trials"]:
            self.assert_moments(row, {"status": "ok", "count": [3, 2], "sum": [0.75, 0.5],
                                      "m2": [0.875, 0]})

    def test_four_blocks_and_single_interval_moments(self):
        # K=S=4 assigns one of (1,-0.25,0,0.25) to each block; each m2 is zero.
        document = self.result("--pbo-blocks", "4")
        self.assert_layout(document, S=4, counts=[1, 1, 1, 1],
                           boundaries=[1700000000000, 1700000060000, 1700000120000,
                                       1700000180000, 1700000240000])
        for row in document["trials"]:
            self.assert_moments(row, {"status": "ok", "count": [1, 1, 1, 1],
                                      "sum": [1, -0.25, 0, 0.25], "m2": [0, 0, 0, 0]})

    def test_invalid_counts_are_refused_before_plugin_dataset_or_output(self):
        never = self.directory / "never.jsonl"
        absent_csv = self.directory / "absent.csv"
        absent_plugin = self.directory / "absent-plugin"
        for value in ("3", "0", "1", "-2", "2.0", "", "abc", "4294967295",
                      "4294967296", "18446744073709551616"):
            with self.subTest(S=value):
                completed = subprocess.run(
                    native_argv(absent_csv, "--trials-file", never, "--pbo-blocks", value,
                                plugin=absent_plugin), text=True, capture_output=True,
                    check=False)
                self.assert_refusal(completed, "hpo_study_spec_invalid", {"reason": "study"})
                self.assertFalse(never.exists())
        completed = subprocess.run(native_argv(absent_csv, "--pbo-blocks", plugin=absent_plugin),
                                   text=True, capture_output=True, check=False)
        self.assert_refusal(completed, "hpo_study_spec_invalid", {"reason": "study"})

    def test_impossible_curve_layout_is_a_study_refusal(self):
        # Three points give K=2<S=4. S=2,K=2 also cannot give two intervals per half.
        for curve, blocks in (("1,2,1.5", "4"), ("1,2,1.5", "2"), (CURVE, "4294967294")):
            with self.subTest(curve=curve, S=blocks):
                completed = self.run_cli("--pbo-blocks", blocks, curve=curve, expected=1)
                self.assert_refusal(completed, "hpo_study_spec_invalid", {"reason": "study"})

    def test_bad_equities_keep_trials_ok_and_publish_only_the_status(self):
        for curve, status, code in (
            ("1,0,1.5,1.5,1.875", "non_positive_equity", "pbo_non_positive_equity"),
            ("1,nan,1.5,1.5,1.875", "non_finite", "pbo_rows_incomplete"),
            ("1,0,nan,1.5,1.875", "non_finite", "pbo_rows_incomplete"),
            ("1,2,1.5,1.5,inf", "non_finite", "pbo_rows_incomplete"),
            ("1e-308,1e308,1,1,1", "non_finite", "pbo_rows_incomplete"),
        ):
            with self.subTest(curve=curve):
                document = self.result("--pbo-blocks", "2", curve=curve)
                self.assert_layout(document)
                self.assertEqual([row["objective"] for row in document["trials"]],
                                 [110, 111, 112, 113])
                self.assertEqual(document["best_trial_id"], 3)
                for row in document["trials"]:
                    self.assertEqual(row["status"], "ok")
                    self.assertEqual(row["block_returns"], {"status": status})
                    self.assertIsNone(row["failure_code"])
                with self.assertRaises(pbo_module.PboError) as refused:
                    pbo_module.load_moment_table(self.parts(document), S=2, roster=[0, 1, 2, 3])
                self.assertEqual(refused.exception.code, code)
                self.assertEqual(refused.exception.ids, (0, 1, 2, 3))

    def test_layout_requires_a_real_usable_completed_snapshot(self):
        # Empty comma-separated entries supply no equity points; no moments were reduced.
        empty = self.result("--pbo-blocks", "2", curve=",,")
        self.assertNotIn("return_blocks", empty)
        for row in empty["trials"]:
            self.assertEqual(row["status"], "ok")
            self.assertNotIn("block_returns", row)
        # Trial 0 has no curve; trial 1 supplies the first usable full-window snapshot.
        mixed = self.result("--pbo-blocks", "2", "--max-trials", "2", "--fixed-input",
                            "SequenceCurveEquity", ",,;" + CURVE)
        self.assert_layout(mixed)
        self.assertEqual([row["status"] for row in mixed["trials"]], ["ok", "ok"])
        self.assertNotIn("block_returns", mixed["trials"][0])
        self.assert_moments(mixed["trials"][1])
        with self.assertRaises(pbo_module.PboError) as refused:
            pbo_module.load_moment_table(self.parts(mixed), S=2, roster=[0, 1])
        self.assertEqual(refused.exception.code, "pbo_rows_incomplete")
        self.assertEqual(refused.exception.ids, (0,))

    def test_constraint_violations_supply_layout_even_without_retained_rows(self):
        for retention in ("all", "none"):
            with self.subTest(retention=retention):
                document = self.result("--pbo-blocks", "2", "--trials-out", retention,
                                       "--constraint", "metrics.all.net_profit < 0", expected=2)
                self.assert_layout(document)
                self.assertIsNone(document["best_trial_id"])
                if retention == "all":
                    for row in document["trials"]:
                        self.assertEqual(row["status"], "constraint_violation")
                        self.assert_moments(row)
                    table = pbo_module.load_moment_table(self.parts(document), S=2,
                                                        roster=[0, 1, 2, 3])
                    self.assertEqual(table.trial_ids, (0, 1, 2, 3))
                else:
                    self.assertEqual(document["trials"], [])
                    self.assertEqual(document["summary"]["best_k"], [])

    def test_engine_and_objective_errors_carry_no_blocks(self):
        for extra, status in ((["--fixed-input", "SequenceScore", "error"], "engine_error"),
                              (["--fixed-input", "SequenceScore", "nan"], "objective_error"),
                              (["--pruner", "median", "--fixed-input", "FailAfterBars", "32"],
                               "engine_error")):
            with self.subTest(extra=extra):
                document = self.result("--pbo-blocks", "2", *extra, expected=2)
                self.assertNotIn("return_blocks", document)
                for row in document["trials"]:
                    self.assertEqual(row["status"], status)
                    self.assertNotIn("block_returns", row)

    def test_pruning_never_publishes_prefix_moments(self):
        document = self.result("--pbo-blocks", "2", "--pruner", "median", "--workers", "2",
                               "--fixed-input", "SequenceScore", "1,2,4,3", "--fixed-input",
                               "PrefixCurveEquity", "1,2", "--fixed-input", "FullWindowBars", "64")
        self.assert_layout(document)
        self.assertEqual([row["status"] for row in document["trials"]],
                         ["ok", "ok", "pruned", "pruned"])
        for row in document["trials"][:2]:
            self.assert_moments(row)
            self.assertEqual(row["backtest"]["input_bars_processed"], 64)
            # The two prefixes process 16 and 32 bars, then the full window processes 64.
            self.assertEqual(row["pruning"]["bars_processed_total"], 112)
        for row in document["trials"][2:]:
            self.assertNotIn("block_returns", row)
            self.assertEqual(row["pruning"]["bars_processed_total"], 16)
        with self.assertRaises(pbo_module.PboError) as refused:
            pbo_module.load_moment_table(self.parts(document), S=2, roster=[0, 1, 2, 3])
        self.assertEqual(refused.exception.code, "pbo_status_pruned")
        self.assertEqual(refused.exception.ids, (2, 3))

    def test_layout_disagreement_refuses_even_discarded_trials(self):
        for retention in ("all", "best-k", "none"):
            for extra in (
                ["--fixed-input", "SequenceCurveEquity", CURVE + ";1,2,1.5,1.5,1.875,2.34375"],
                ["--fixed-input", "SequenceCurveTimeOffsetMs", "0,1"],
                ["--fixed-input", "SequenceCurveEquity", CURVE + ";1,0,1.5,1.5,1.875,2.34375"],
            ):
                with self.subTest(retention=retention, extra=extra):
                    completed = self.run_cli("--pbo-blocks", "2", "--max-trials", "2",
                                             "--trials-out", retention, "--best-k", "1", *extra,
                                             expected=1)
                    self.assert_refusal(completed, "hpo_report_invalid", {})

    def test_layout_refusal_releases_parallel_stream_workers(self):
        stream = self.directory / "refused.jsonl"
        completed = self.run_cli("--pbo-blocks", "2", "--workers", "4", "--batch-size", "4",
                                 "--trials-file", stream, "--fixed-input",
                                 "SequenceCurveTimeOffsetMs", "0,1", expected=1)
        self.assert_refusal(completed, "hpo_report_invalid", {})

    def test_payloads_survive_retention_streams_and_parallel_completion(self):
        for workers in (1, 4):
            with self.subTest(workers=workers):
                trials_file = self.directory / f"trials-{workers}.jsonl"
                progress_file = self.directory / f"progress-{workers}.jsonl"
                with progress_file.open("wb") as progress:
                    document = self.result("--pbo-blocks", "2", "--trials-out", "none",
                                           "--best-k", "1", "--workers", workers,
                                           "--trials-file", trials_file, "--progress-fd",
                                           progress.fileno(), pass_fds=(progress.fileno(),))
                self.assert_layout(document)
                self.assertEqual(document["trials"], [])
                self.assertEqual(document["best_trial_id"], 3)
                best = document["summary"]["best_k"]
                self.assertEqual([row["trial_id"] for row in best], [3])
                self.assert_moments(best[0])
                for file in (trials_file, progress_file):
                    rows = [json.loads(line) for line in file.read_text().splitlines()]
                    self.assertEqual([row["trial_id"] for row in rows], [0, 1, 2, 3])
                    for row in rows:
                        self.assert_moments(row)

    def test_timeout_writer_uses_only_completed_full_window_snapshots(self):
        for hang, ids, statuses in (("11", [0, 1], ["ok", "trial_timeout"]),
                                    ("10", [0], ["trial_timeout"])):
            with self.subTest(hang=hang):
                document = self.result("--pbo-blocks", "2", "--workers", "1", "--fixed-input",
                                       "HangAtLength", hang, "--trial-timeout-seconds", "0.5",
                                       expected=3)
                self.assertEqual(document["stop_reason"], "trial_timeout")
                self.assertEqual([row["trial_id"] for row in document["trials"]], ids)
                self.assertEqual([row["status"] for row in document["trials"]], statuses)
                if hang == "11":
                    self.assert_layout(document)
                    self.assert_moments(document["trials"][0])
                else:
                    self.assertNotIn("return_blocks", document)
                self.assertNotIn("block_returns", document["trials"][-1])

    def test_statistics_and_blocks_share_the_reducing_identity(self):
        document = self.result("--pbo-blocks", "2", "--record-metric", "returns.bar.count")
        layout = self.assert_layout(document)
        self.assertEqual(layout["numeric_build_identity"],
                         document["return_stats"]["numeric_build_identity"])
        for row in document["trials"]:
            self.assert_moments(row)
            self.assertEqual(row["metrics"]["returns.bar.count"], 4)

    def test_native_rows_are_accepted_and_bad_count_is_refused_by_id(self):
        document = self.result("--pbo-blocks", "2")
        parts = self.parts(document)
        table = pbo_module.load_moment_table(parts, S=2, roster=[0, 1, 2, 3])
        self.assertEqual(table.trial_ids, (0, 1, 2, 3))
        self.assertEqual(table.counts, (2, 2))
        self.assertEqual(table.sums, ((0.75, 0.25), (0.75, 0.25), (0.75, 0.25), (0.75, 0.25)))
        self.assertEqual(table.m2, ((0.78125, 0.03125), (0.78125, 0.03125),
                                   (0.78125, 0.03125), (0.78125, 0.03125)))
        # Four identical trials tie on both halves: mid-rank 2.5/(N+1)=0.5, logit=0.
        # Both ordered splits therefore have all their weight at zero and PBO=1.
        analysis = pbo_module.pbo(parts, S=2, roster=[0, 1, 2, 3], sampler="grid", early_stop=None)
        self.assertEqual(analysis["n_splits"], 2)
        self.assertEqual((analysis["n_lt0"], analysis["n_eq0"], analysis["n_gt0"]), (0, 2, 0))
        self.assertEqual(analysis["pbo"], 1)
        self.assertEqual(analysis["coverage"], {"declared": 4, "supplied": 4, "usable": 4})
        broken = copy.deepcopy(document)
        broken["trials"][1]["block_returns"]["count"][0] = 3
        with self.assertRaises(pbo_module.PboError) as refused:
            pbo_module.load_moment_table(self.parts(broken), S=2, roster=[0, 1, 2, 3])
        self.assertEqual(refused.exception.code, "pbo_block_mismatch")
        self.assertEqual(refused.exception.ids, (1,))

    def test_supplied_reference_preserves_default_result_bytes(self):
        path = os.environ.get("PFH_PBO_REFERENCE")
        digest = os.environ.get("PFH_PBO_REFERENCE_SHA256")
        if not path or not digest:
            if os.environ.get("PFH_PBO_PROOF") == "1":
                self.fail("proof mode needs PFH_PBO_REFERENCE and PFH_PBO_REFERENCE_SHA256")
            self.skipTest("reference binary not supplied: no default byte comparison was made")
        reference = Path(path).resolve()
        self.assertEqual(hashlib.sha256(reference.read_bytes()).hexdigest(), digest.lower())
        for extra in ([], ["--workers", "4"], ["--pruner", "median"]):
            with self.subTest(extra=extra):
                old = self.run_cli(*extra, native=reference)
                new = self.run_cli(*extra)
                self.assertEqual(new.stdout, old.stdout)


if __name__ == "__main__":
    unittest.main()
