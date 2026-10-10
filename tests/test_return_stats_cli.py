#!/usr/bin/env python3
"""Shipped-CLI contracts for the return-statistics metrics (legacy mode, contract
`pineforge-hpo-return-stats/v1`), driven only through the native binary and the fake plugin.
Usage: test_return_stats_cli.py NATIVE PLUGIN [unittest arguments].

UNEXECUTED until the spot phase. The fake plugin reports a two-point curve (equity `E` then
`E + 1`, one minute apart, `E` = 100000 unless overridden), so every expectation below is closed
form: one bar interval gives T = 1, no period (fewer than three points, status 6); the monthly
series of the same curve is one UTC bucket, hence T = 0 and P = 12 (status 1), and any chart
timezone other than UTC makes it undefined (status 7, every field null). Real curves, the
engine's own Sharpe ratios, high-precision references and timing are in
test_return_stats_e2e.py.

The literal off-byte comparison needs a binary built at the pinned release. It is mandatory
when PFH_RETURN_STATS_PROOF=1 (the actual proof run) and is skipped, saying so, otherwise:
PFH_RETURN_STATS_REFERENCE and PFH_RETURN_STATS_REFERENCE_SHA256.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest


NATIVE = Path(sys.argv.pop(1)).resolve()
PLUGIN = Path(sys.argv.pop(1)).resolve()

FIELDS = ("count", "skipped", "periods_per_year", "mean", "std", "sharpe_per_period", "skew",
          "kurt_raw", "status")
SERIES = ("bar", "monthly")
NAMES = tuple(f"returns.{series}.{field}" for series in SERIES for field in FIELDS)
CONTRACT = "pineforge-hpo-return-stats/v1"
IDENTITY_PREFIX = "pineforge-hpo-return-stats-build/v2:sha256:"
SPACE = ("--int-dim", "Length", "10", "13", "1")
# A parent that covers a whole finite space leaves a warm child nothing to try (exit 5), so the
# continuation case uses a space the parent list does not exhaust.
WIDE = ("--int-dim", "Length", "10", "60", "1")
BASE_EQUITY = 100000.0
ONE_RETURN = (BASE_EQUITY + 1.0) / BASE_EQUITY - 1.0  # binary64, as the reducer computes it


def nulls(**values):
    row = {field: None for field in FIELDS}
    row.update(values)
    return row


# Bar series of the two-point curve: one valid return, no period basis.
BAR_FAKE = nulls(count=1, skipped=0, mean=ONE_RETURN, status=6)
# Monthly series of the same curve for a UTC chart: a single bucket, hence no return.
MONTHLY_UTC_FAKE = nulls(count=0, skipped=0, periods_per_year=12, status=1)
# Any other chart timezone: undefined, every field null including the counts.
MONTHLY_OTHER_FAKE = nulls(status=7)


def wire(series, expected):
    return {f"returns.{series}.{field}": value for field, value in expected.items()}


def supplied_reference(required: bool):
    path = os.environ.get("PFH_RETURN_STATS_REFERENCE")
    expected = os.environ.get("PFH_RETURN_STATS_REFERENCE_SHA256")
    if not path or not expected:
        if required:
            raise AssertionError("proof mode needs PFH_RETURN_STATS_REFERENCE and "
                                 "PFH_RETURN_STATS_REFERENCE_SHA256 (the pinned release binary)")
        return None
    reference = Path(path).resolve()
    actual = hashlib.sha256(reference.read_bytes()).hexdigest()
    if actual != expected.lower():
        raise AssertionError(f"reference binary digest {actual} is not the supplied {expected}")
    return reference


def setUpModule() -> None:
    """These contracts need a build whose statistics identity is bound.

    An unbound build (explicit compile-database OFF, a multi-configuration generator, a compiler
    launcher, ...) is a supported configuration that refuses requested statistics and runs
    everything else; that behaviour is proven by test_return_stats_integration.py. In a proof run
    (PFH_RETURN_STATS_PROOF=1 or PFH_REQUIRE_RETURN_STATS_IDENTITY=1) an unbound binary is a
    failure here, never a silent skip.
    """
    with tempfile.TemporaryDirectory(prefix="pf_rs_probe_") as directory:
        csv = Path(directory) / "bars.csv"
        csv.write_text("timestamp,open,high,low,close,volume\n"
                       + "".join(f"{1700000000000 + index * 60000},100,102,99,101,10\n"
                                 for index in range(4)), encoding="utf-8")
        probe = subprocess.run(
            [str(NATIVE), "run", "--strategy", str(PLUGIN), "--ohlcv", str(csv), "--objective",
             "metrics.all.net_profit", "--input-tf", "1", "--script-tf", "5", "--chart-timezone",
             "UTC", "--fixed-input", "BatchPrefixTest", "1", *SPACE, "--sampler", "grid",
             "--bar-magnifier", "true", "--magnifier-samples", "6",
             "--magnifier-distribution", "triangle",
             "--max-trials", "1", "--record-metric", "returns.bar.count"],
            text=True, capture_output=True, check=False, timeout=120)
    try:
        failure = json.loads(probe.stdout).get("failure")
    except ValueError:
        failure = None
    if failure and failure.get("code") == "hpo_toolchain_unavailable":
        proof = (os.environ.get("PFH_RETURN_STATS_PROOF") == "1"
                 or os.environ.get("PFH_REQUIRE_RETURN_STATS_IDENTITY") == "1")
        message = ("the native binary's statistics identity is unbound: " + probe.stderr.strip())
        if proof:
            raise AssertionError(message)
        raise unittest.SkipTest(message + " (unbound behaviour: test_return_stats_integration.py)")
    if probe.returncode != 0:
        raise AssertionError(f"the bound-build probe failed: {probe.stderr}")


class ReturnStatsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pf_rs_cli_")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.csv = self.directory / "bars.csv"
        self.csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            + "".join(f"{1700000000000 + index * 60000},100,102,99,101,10\n"
                      for index in range(64)),
            encoding="utf-8",
        )

    # ---- helpers ---------------------------------------------------------------------

    def argv(self, *extra, timezone="Asia/Taipei", sampler="grid", native=None, space=SPACE):
        command = [
            str(native or NATIVE), "run", "--strategy", str(PLUGIN), "--ohlcv", str(self.csv),
            "--objective", "metrics.all.net_profit", "--input-tf", "1", "--script-tf", "5",
            "--chart-timezone", timezone, "--bar-magnifier", "true", "--magnifier-samples", "6",
            "--magnifier-distribution", "triangle", "--fixed-input", "BatchPrefixTest", "1",
            *space,
        ]
        if sampler:
            command += ["--sampler", sampler]
        return command + [str(item) for item in extra]

    def run_cli(self, *extra, expected=0, env=None, pass_fds=(), **options):
        if options.get("sampler", "grid") == "grid" and "--max-trials" not in extra:
            extra = (*extra, "--max-trials", "4")
        completed = subprocess.run(
            self.argv(*extra, **options), text=True, capture_output=True, check=False,
            env={**os.environ, **(env or {})}, pass_fds=pass_fds, timeout=600)
        if expected is not None:
            self.assertEqual(completed.returncode, expected, completed.stderr)
        return completed

    def record(self, names=NAMES):
        return [item for name in names for item in ("--record-metric", name)]

    def result(self, completed):
        return json.loads(completed.stdout)

    def signature(self, completed) -> str:
        """Rows and the statistics object: what must not move with workers or repeats."""
        document = json.loads(completed.stdout)
        return json.dumps({"trials": document["trials"],
                           "return_stats": document.get("return_stats")}, sort_keys=True)

    def assert_row_series(self, trial, series, expected):
        for field, value in expected.items():
            name = f"returns.{series}.{field}"
            self.assertIn(name, trial["metrics"])
            self.assertEqual(trial["metrics"][name], value, name)

    def stripped(self, document):
        """The result without the additive pieces, for a structural comparison with OFF."""
        document = json.loads(json.dumps(document))
        document.pop("return_stats", None)
        for trial in document["trials"]:
            for name in NAMES:
                trial["metrics"].pop(name, None)
        return document

    # ---- names and requests ---------------------------------------------------------------

    def test_all_eighteen_names_resolve_and_near_misses_are_refused(self) -> None:
        completed = self.run_cli(*self.record())
        trial = self.result(completed)["trials"][0]
        self.assertEqual(sorted(name for name in trial["metrics"] if name.startswith("returns.")),
                         sorted(NAMES))
        for near in ("returns.bar.sharpe", "returns.bar.Count", "returns.monthly.annual_sharpe",
                     "returns.weekly.count", "returns.count", "returns.bar.", "returns.bar.count ",
                     "returns.bar.skew_excess", "return.bar.count"):
            with self.subTest(name=near):
                refused = self.run_cli("--record-metric", near, expected=1)
                failure = json.loads(refused.stdout)["failure"]
                self.assertEqual(failure["code"], "hpo_study_spec_invalid")
                self.assertEqual(failure["args"], {"reason": "objective"})
                self.assertNotIn("trials", json.loads(refused.stdout))

    def test_objective_and_constraint_requests_derive_the_series(self) -> None:
        command = self.argv("--max-trials", "4", "--constraint", "returns.bar.count >= 1",
                            timezone="UTC")
        command[command.index("--objective") + 1] = "returns.monthly.count + 1"
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["return_stats"]["series"], ["bar", "monthly"])
        self.assertTrue(all(trial["status"] != "constraint_error" for trial in result["trials"]))

    def test_requested_values_on_the_fake_curve(self) -> None:
        taipei = self.result(self.run_cli(*self.record()))
        for trial in taipei["trials"]:
            self.assertEqual(trial["status"], "ok")
            self.assert_row_series(trial, "bar", BAR_FAKE)
            self.assert_row_series(trial, "monthly", MONTHLY_OTHER_FAKE)
        utc = self.result(self.run_cli(*self.record(), timezone="UTC"))
        for trial in utc["trials"]:
            self.assert_row_series(trial, "bar", BAR_FAKE)
            self.assert_row_series(trial, "monthly", MONTHLY_UTC_FAKE)
        for alias in ("Etc/UTC",):
            aliased = self.result(self.run_cli(*self.record(), timezone=alias))
            self.assert_row_series(aliased["trials"][0], "monthly", MONTHLY_UTC_FAKE)

    def test_the_object_shape_series_selection_and_identity(self) -> None:
        for selected, names in ((["bar"], ["returns.bar.count"]),
                                (["monthly"], ["returns.monthly.status"]),
                                (["bar", "monthly"], ["returns.bar.mean", "returns.monthly.count"])):
            with self.subTest(series=selected):
                result = self.result(self.run_cli(*self.record(names), timezone="UTC"))
                stats = result["return_stats"]
                self.assertEqual(set(stats), {"contract", "series", "chart_timezone",
                                              "risk_free_annual", "numeric_build_identity"})
                self.assertEqual(stats["contract"], CONTRACT)
                self.assertEqual(stats["series"], selected)
                self.assertEqual(stats["chart_timezone"], "UTC")
                self.assertEqual(stats["risk_free_annual"], 0.02)
                self.assertTrue(stats["numeric_build_identity"].startswith(IDENTITY_PREFIX))
                self.assertEqual(len(stats["numeric_build_identity"]), len(IDENTITY_PREFIX) + 64)
                present = {name for trial in result["trials"] for name in trial["metrics"]
                           if name.startswith("returns.")}
                self.assertEqual(present, set(names))
        # No new per-row key: a row keeps exactly the keys of an OFF row.
        on = self.result(self.run_cli(*self.record()))["trials"][0]
        off = self.result(self.run_cli())["trials"][0]
        self.assertEqual(set(on), set(off))

    def test_off_run_has_no_object_no_keys_and_no_new_cost_surface(self) -> None:
        off = self.run_cli()
        result = self.result(off)
        self.assertNotIn("return_stats", result)
        for trial in result["trials"]:
            self.assertFalse([name for name in trial["metrics"] if name.startswith("returns.")])
        self.assertNotIn("returns.", off.stdout)

    def test_a_requested_run_differs_from_off_only_by_the_additive_pieces(self) -> None:
        for sampler, extra in (("grid", ()), ("random", ("--max-trials", "6", "--seed", "3")),
                               ("tpe", ("--max-trials", "6", "--seed", "3")),
                               ("dlib_global", ("--max-trials", "6", "--seed", "3"))):
            with self.subTest(sampler=sampler):
                off = self.result(self.run_cli(*extra, sampler=sampler))
                on = self.result(self.run_cli(*extra, *self.record(), sampler=sampler))
                self.assertEqual(self.stripped(on), off)

    def test_a_missing_report_nulls_every_requested_metric_including_status(self) -> None:
        completed = self.run_cli(*self.record(), "--fixed-input", "SequenceScore",
                                 "1,error,3,error", expected=0)
        result = self.result(completed)
        statuses = {trial["trial_id"]: trial["status"] for trial in result["trials"]}
        self.assertIn("engine_error", statuses.values())
        for trial in result["trials"]:
            if trial["status"] == "engine_error":
                for name in NAMES:
                    self.assertIn(name, trial["metrics"])
                    self.assertIsNone(trial["metrics"][name], name)
            else:
                self.assert_row_series(trial, "bar", BAR_FAKE)

    def test_ordinary_metric_semantics_for_objectives_and_constraints(self) -> None:
        command = self.argv("--max-trials", "4", "--constraint", "returns.bar.sharpe_per_period > 0")
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        result = json.loads(completed.stdout)
        # The Sharpe ratio is null here (T = 1): the existing non-finite policy decides, X does not.
        self.assertEqual({trial["status"] for trial in result["trials"]}, {"constraint_error"})
        self.assertEqual(completed.returncode, 2)
        objective = self.argv("--max-trials", "4")
        objective[objective.index("--objective") + 1] = "returns.bar.mean * 100000"
        completed = subprocess.run(objective, text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertAlmostEqual(json.loads(completed.stdout)["best_value"], 1.0, places=6)

    # ---- determinism ----------------------------------------------------------------------

    def test_workers_batches_repeats_and_process_timezone_do_not_change_rows(self) -> None:
        reference = None
        for workers in (1, 2, 4, 8, 4):  # the last entry is a repeat
            for lag in ("0", "1"):
                with self.subTest(workers=workers, lag=lag):
                    completed = self.run_cli(*self.record(), "--workers", workers, "--batch-size",
                                             "2", "--batch-lag", lag, timezone="UTC")
                    if reference is None:
                        reference = {}
                    reference.setdefault(lag, self.signature(completed))
                    self.assertEqual(self.signature(completed), reference[lag])
        for variable in ("Asia/Taipei", "America/New_York", "Pacific/Kiritimati"):
            with self.subTest(process_timezone=variable):
                completed = self.run_cli(*self.record(), "--workers", 2, "--batch-size", "2",
                                         "--batch-lag", "0", timezone="UTC", env={"TZ": variable})
                self.assertEqual(self.signature(completed), reference["0"])

    def test_every_sampler_reports_the_same_statistics_identity(self) -> None:
        identities = set()
        for sampler, extra in (("grid", ()), ("random", ("--max-trials", "5", "--seed", "1")),
                               ("tpe", ("--max-trials", "5", "--seed", "1")),
                               ("dlib_global", ("--max-trials", "5", "--seed", "1"))):
            with self.subTest(sampler=sampler):
                result = self.result(self.run_cli(*extra, *self.record(["returns.bar.count"]),
                                                  sampler=sampler))
                identities.add(result["return_stats"]["numeric_build_identity"])
                if sampler == "tpe":
                    # The TPE checkpoint identity is a different field with a different value.
                    self.assertNotEqual(result["numeric_build_identity"],
                                        result["return_stats"]["numeric_build_identity"])
        listing = self.directory / "list.jsonl"
        listing.write_text('{"Length":10}\n{"Length":12}\n{"Length":10}\n')
        candidates = self.result(self.run_cli(*self.record(["returns.bar.count"]),
                                              "--candidates", listing, sampler="candidates"))
        identities.add(candidates["return_stats"]["numeric_build_identity"])
        self.assertEqual(len(identities), 1, identities)
        self.assertEqual(candidates["candidate_list"]["count"], 3)
        self.assertTrue(all(t["metrics"]["returns.bar.count"] == 1 for t in candidates["trials"]))
        # An OFF candidate run carries neither object.
        off = self.result(self.run_cli("--candidates", listing, sampler="candidates"))
        self.assertNotIn("return_stats", off)

    # ---- terminal paths ---------------------------------------------------------------------

    def test_cancel_deadline_and_trial_timeout_keep_the_object_and_the_rows(self) -> None:
        names = self.record(["returns.bar.count", "returns.bar.status"])
        # Deadline: a prefix of the grid, each finished row carries its statistics.
        wide = ("--int-dim", "Length", "10", "60", "1")
        deadline = self.result(self.run_cli(*names, "--workers", 1, "--max-wall-seconds", "0.6",
                                            "--fixed-input", "DelayMs", "150", "--max-trials",
                                            "40", space=wide))
        self.assertEqual(deadline["stop_reason"], "deadline")
        self.assertIn("return_stats", deadline)
        self.assertTrue(all(t["metrics"]["returns.bar.count"] == 1 for t in deadline["trials"]))
        # Trial timeout: exit 3, the final document keeps the object; the timeout row has no
        # report, so its requested metrics stay null.
        final = self.directory / "timeout.json"
        timed = self.run_cli(*names, "--workers", 2, "--fixed-input", "HangAtLength", "11",
                             "--fixed-input", "DelayMs", "40", "--trial-timeout-seconds", "0.5",
                             "--output", final, expected=3)
        result = self.result(timed)
        self.assertEqual(result, json.loads(final.read_text()))
        self.assertIn("return_stats", result)
        for trial in result["trials"]:
            if trial["status"] == "trial_timeout":
                self.assertIsNone(trial["metrics"]["returns.bar.count"])
                self.assertIsNone(trial["metrics"]["returns.bar.status"])
            else:
                self.assertEqual(trial["metrics"]["returns.bar.count"], 1)
        # SIGTERM while a trial is gated: the started row finishes with its statistics.
        entered_read, entered_write = os.pipe()
        release_read, release_write = os.pipe()
        child = subprocess.Popen(
            self.argv(*names, "--workers", 1, "--max-trials", "4", "--fixed-input",
                      "GateAtLength", "11", "--fixed-input", "GateEnteredFd", entered_write,
                      "--fixed-input", "GateReleaseFd", release_read),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(entered_write, release_read))
        os.close(entered_write)
        os.close(release_read)
        try:
            self.assertTrue(select.select([entered_read], [], [], 30)[0], "gate never entered")
            self.assertEqual(os.read(entered_read, 1), b"G")
            child.send_signal(signal.SIGTERM)
            time.sleep(0.4)
            os.write(release_write, b"R")
            stdout, stderr = child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            os.close(entered_read)
            os.close(release_write)
        self.assertEqual(child.returncode, 0, stderr)
        cancelled = json.loads(stdout)
        self.assertEqual(cancelled["stop_reason"], "cancelled")
        self.assertIn("return_stats", cancelled)
        self.assertEqual([t["trial_id"] for t in cancelled["trials"]], [0, 1])
        self.assertTrue(all(t["metrics"]["returns.bar.count"] == 1 for t in cancelled["trials"]))

    # ---- candidates, C to TPE and warm rows --------------------------------------------------

    def test_a_candidate_result_seeds_tpe_and_each_part_reports_only_its_own_trials(self) -> None:
        listing = self.directory / "list.jsonl"
        listing.write_text("".join(f'{{"Length":{length}}}\n' for length in (10, 11, 12, 13, 10)))
        parent = self.directory / "parent.json"
        names = self.record(["returns.bar.count", "returns.bar.mean"])
        self.run_cli(*names, "--candidates", listing, "--output", parent, sampler="candidates",
                     space=WIDE)
        parent_document = json.loads(parent.read_text())
        self.assertIn("return_stats", parent_document)
        self.assertEqual(parent_document["candidate_list"]["count"], 5)
        children = []
        for workers in (1, 2, 4, 8):
            child = self.run_cli(*names, "--sampler", "tpe", "--max-trials", "6", "--seed", "5",
                                 "--warm-start", parent, "--batch-size", "2", "--workers",
                                 workers, sampler=None, space=WIDE)
            children.append(self.result(child))
        reference = json.dumps(children[0]["trials"], sort_keys=True)
        for child in children:
            self.assertEqual(json.dumps(child["trials"], sort_keys=True), reference)
            self.assertEqual(child["warm_start_model"], "rebuilt_history")
            # The child's object describes its own part; ancestors keep their rows verbatim.
            self.assertIn("return_stats", child)
            self.assertEqual(child["warm_start_trials"], parent_document["trials"])
            self.assertEqual([t["trial_id"] for t in child["trials"]], list(range(5, 11)))
            self.assertTrue(all(t["metrics"]["returns.bar.count"] == 1 for t in child["trials"]))
        # A parent that never computed statistics gives its rows none, and none are invented.
        off_parent = self.directory / "off-parent.json"
        self.run_cli("--candidates", listing, "--output", off_parent, sampler="candidates",
                     space=WIDE)
        child = self.result(self.run_cli(*names, "--sampler", "tpe", "--max-trials", "4",
                                         "--seed", "5", "--warm-start", off_parent,
                                         "--batch-size", "2", sampler=None, space=WIDE))
        for ancestor in child["warm_start_trials"]:
            self.assertFalse([n for n in ancestor["metrics"] if n.startswith("returns.")])
        self.assertTrue(all("returns.bar.count" in t["metrics"] for t in child["trials"]))

    # ---- the pinned release binary -----------------------------------------------------------

    def test_supplied_reference_off_bytes(self) -> None:
        proof = os.environ.get("PFH_RETURN_STATS_PROOF") == "1"
        reference = supplied_reference(required=proof)
        if reference is None:
            self.skipTest("pinned reference binary not supplied: no literal comparison made")
        (self.directory / "reference.json").write_text(json.dumps({
            "path": str(reference),
            "sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
            "contract": "literal stdout bytes; no normalization of version or build fields",
        }) + "\n")
        variants = (
            ("grid", ()), ("random", ("--max-trials", "12", "--seed", "4")),
            ("tpe", ("--max-trials", "12", "--seed", "4", "--batch-size", "4")),
            ("dlib_global", ("--max-trials", "12", "--seed", "4")),
            ("random", ("--max-trials", "12", "--seed", "4", "--no-improvement-trials", "3")),
            ("tpe", ("--max-trials", "12", "--seed", "4", "--pruner", "median")),
        )
        for sampler, extra in variants:
            for workers in (1, 4):
                with self.subTest(sampler=sampler, extra=extra, workers=workers):
                    flags = ("--workers", workers, *extra)
                    old = self.run_cli(*flags, sampler=sampler, native=reference, expected=None)
                    new = self.run_cli(*flags, sampler=sampler, expected=None)
                    self.assertEqual(old.returncode, new.returncode)
                    self.assertEqual(old.stdout, new.stdout)
                    # Requested statistics differ from those bytes only by the additive pieces.
                    requested = self.run_cli(*flags, *self.record(), sampler=sampler,
                                             expected=None)
                    self.assertEqual(requested.returncode, new.returncode)
                    self.assertEqual(self.stripped(json.loads(requested.stdout)),
                                     json.loads(old.stdout))
        # The release binary does not know the metric names: it must refuse, not ignore them.
        refused = self.run_cli("--record-metric", "returns.bar.count", native=reference,
                               expected=1)
        self.assertEqual(json.loads(refused.stdout)["failure"]["code"],
                         "hpo_study_spec_invalid")


if __name__ == "__main__":
    unittest.main()
