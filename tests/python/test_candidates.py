"""StudySpec and CLI route for `sampler.kind: "candidates"` (the C candidate-list sampler).

Python never reads, validates or rewrites the list: native admission is the single authority and
its failure documents are relayed unchanged. These tests cover only the schema, the native argv
and the Python-side refusals. UNEXECUTED until the spot phase.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))

from pineforge_hpo.artifact import StrategyArtifact  # noqa: E402
from pineforge_hpo.cli import _native_command, main, prepare_run  # noqa: E402
from pineforge_hpo.continuation import WarmStartError  # noqa: E402
from pineforge_hpo.study_spec import StudySpecError, load_study_spec  # noqa: E402


class CandidatesRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        header = self.root / "include" / "pineforge" / "pineforge.h"
        header.parent.mkdir(parents=True)
        header.write_text("/* test engine */\n", encoding="utf-8")
        (self.root / "strategy.pine").write_text("//@version=6\nstrategy('t')\n", encoding="utf-8")
        (self.root / "bars.csv").write_text(
            "timestamp,open,high,low,close,volume\n1,1,1,1,1,1\n", encoding="utf-8"
        )
        self.native = self.root / "pineforge-hpo-native"
        self.native.write_text("", encoding="utf-8")
        self.native.chmod(0o755)
        (self.root / "lists").mkdir()
        self.candidates = self.root / "lists" / "c.jsonl"
        self.candidates.write_text(
            '{"Length":2,"Threshold":0.5}\n{"Length":4,"Threshold":1.0}\n', encoding="utf-8"
        )
        self.study_path = self.root / "study.json"

    def write_study(self, sampler=None, execution=None) -> Path:
        document = {
            "schema_version": 1,
            "mode": "single_strategy",
            "strategies": [{
                "id": "test",
                "source": "strategy.pine",
                "datasets": ["bars"],
                "fixed_inputs": {"Enabled": True},
                "strategy_overrides": {"initial_capital": 10000},
                "search_space": {
                    "Length": {"kind": "integer", "low": 2, "high": 4, "step": 1},
                    "Threshold": {"kind": "real", "low": 0.5, "high": 1.0, "step": 0.5},
                },
            }],
            "datasets": [{
                "id": "bars", "ohlcv": "bars.csv", "input_tf": "1", "script_tf": "1",
                "chart_timezone": "UTC",
            }],
            "objective": {
                "kind": "expression", "direction": "maximize",
                "expression": "metrics.all.net_profit", "constraints": [],
            },
            "sampler": sampler if sampler is not None else {
                "kind": "candidates", "seed": 7, "trials": 2,
                "config": {"candidates_file": "lists/c.jsonl"},
            },
            "execution": execution if execution is not None else
            {"workers": 2, "isolation": "threads"},
        }
        self.study_path.write_text(json.dumps(document), encoding="utf-8")
        return self.study_path

    def artifact(self) -> StrategyArtifact:
        return StrategyArtifact(
            artifact_key="a" * 64, request_key="b" * 64,
            plugin_path=self.root / "strategy.dylib",
            generated_cpp_path=self.root / "strategy.cpp",
            manifest_path=self.root / "manifest.json",
            provenance_path=self.root / "provenance.json",
            cache_hit=False,
            inputs=({"title": "Length", "type": "int"}, {"title": "Threshold", "type": "float"},
                    {"title": "Enabled", "type": "bool"}),
            strategy_params={},
        )

    def command(self):
        return _native_command(load_study_spec(self.study_path), native=self.native,
                               plugin=self.root / "strategy.dylib", artifact_key="a" * 64)

    # ---- schema --------------------------------------------------------------------------

    def test_candidates_file_resolves_like_other_study_paths(self) -> None:
        self.write_study()
        spec = load_study_spec(self.study_path)
        self.assertEqual(spec.sampler.kind, "candidates")
        self.assertEqual(spec.sampler.candidates_file, self.candidates.resolve())
        self.assertEqual(spec.sampler.trials, 2)
        self.assertEqual(spec.sampler.seed, 7)
        self.assertEqual(spec.sampler.candidate_policy, "sampler_default")
        # Dataset paths resolve against the study file in exactly the same way.
        self.assertEqual(spec.datasets[0].ohlcv, (self.root / "bars.csv").resolve())
        absolute = self.write_study({"kind": "candidates", "seed": 0, "trials": 2,
                                     "config": {"candidates_file": str(self.candidates)}})
        self.assertEqual(load_study_spec(absolute).sampler.candidates_file,
                         self.candidates.resolve())

    def test_schema_refusals(self) -> None:
        cases = (
            ({"kind": "candidates", "seed": 1, "trials": 2}, "candidates_file"),
            ({"kind": "candidates", "seed": 1, "trials": 2, "config": {}}, "candidates_file"),
            ({"kind": "candidates", "seed": 1, "trials": 2,
              "config": {"candidates_file": ""}}, "non-empty string"),
            ({"kind": "candidates", "seed": 1, "trials": 2,
              "config": {"candidates_file": 7}}, "non-empty string"),
            ({"kind": "candidates", "seed": 1, "trials": 2,
              "config": {"candidates_file": "lists/c.jsonl", "startup_trials": 3}},
             "unknown"),
            ({"kind": "candidates", "seed": 1, "trials": 0,
              "config": {"candidates_file": "lists/c.jsonl"}}, "must be a positive integer"),
            ({"kind": "candidates", "seed": -1, "trials": 2,
              "config": {"candidates_file": "lists/c.jsonl"}}, "non-negative integer"),
            ({"kind": "candidates", "seed": 1, "trials": 2,
              "candidate_policy": "exhaustive",
              "config": {"candidates_file": "lists/c.jsonl"}}, "candidate"),
            ({"kind": "grid", "seed": 1, "trials": 2,
              "config": {"candidates_file": "lists/c.jsonl"}},
             "is reserved and must be empty"),
            ({"kind": "tpe", "seed": 1, "trials": 2,
              "config": {"candidates_file": "lists/c.jsonl"}}, "unknown"),
        )
        for sampler, expected in cases:
            with self.subTest(sampler=sampler):
                self.write_study(sampler)
                with self.assertRaisesRegex(StudySpecError, expected):
                    load_study_spec(self.study_path)

    def test_pruner_is_refused_with_candidates(self) -> None:
        self.write_study(execution={"workers": 2, "isolation": "threads", "pruner": "median"})
        with self.assertRaisesRegex(StudySpecError, "pruner"):
            load_study_spec(self.study_path)
        self.write_study(execution={"workers": 2, "isolation": "threads", "pruner": "none"})
        load_study_spec(self.study_path)

    def test_missing_list_is_a_spec_issue_without_echoing_the_path(self) -> None:
        self.write_study({"kind": "candidates", "seed": 1, "trials": 2,
                          "config": {"candidates_file": "lists/missing.jsonl"}})
        load_study_spec(self.study_path)  # not required unless files are required
        with self.assertRaises(StudySpecError) as raised:
            load_study_spec(self.study_path, require_files=True)
        self.assertIn("candidates_file", str(raised.exception))
        self.assertNotIn("missing.jsonl", str(raised.exception))
        self.assertNotIn(self.temporary.name, str(raised.exception))

    # ---- native argv -----------------------------------------------------------------------

    def test_native_command_maps_the_sampler(self) -> None:
        self.write_study()
        command = self.command()
        self.assertEqual(command[command.index("--sampler") + 1], "candidates")
        self.assertEqual(command[command.index("--candidates") + 1],
                         str(self.candidates.resolve()))
        self.assertEqual(command[command.index("--max-trials") + 1], "2")
        self.assertEqual(command[command.index("--seed") + 1], "7")
        self.assertEqual(command[command.index("--candidate-policy") + 1], "sampler_default")
        for forbidden in ("--pruner", "--no-improvement-trials", "--warm-start",
                          "--tpe-startup-trials"):
            self.assertNotIn(forbidden, command)
        # A positive sampler.trials is always forwarded; native refuses a value that is not N.
        self.write_study({"kind": "candidates", "seed": 7, "trials": 9,
                          "config": {"candidates_file": "lists/c.jsonl"}})
        self.assertEqual(self.command()[self.command().index("--max-trials") + 1], "9")

    def test_other_samplers_never_receive_the_candidates_flag(self) -> None:
        for sampler in ({"kind": "grid", "seed": 1, "trials": 4},
                        {"kind": "random", "seed": 1, "trials": 4},
                        {"kind": "tpe", "seed": 1, "trials": 4}):
            with self.subTest(sampler=sampler["kind"]):
                self.write_study(sampler)
                self.assertNotIn("--candidates", self.command())

    # ---- run route ---------------------------------------------------------------------------

    @mock.patch("pineforge_hpo.cli.subprocess.run")
    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_run_relays_the_native_document_unchanged(self, builder: mock.Mock,
                                                       run: mock.Mock) -> None:
        self.write_study()
        builder.return_value.build.return_value = self.artifact()
        block = {"format": "pineforge_candidates_v1", "source_sha256": "a" * 64,
                 "list_sha256": "b" * 64, "count": 2, "evaluated": 2, "scored": 2,
                 "complete": True, "unevaluated_ranges": []}
        native_result = {"ok": True, "trials": [], "best_trial_id": 0, "best_value": 1.5,
                         "sampler": "candidates", "candidate_list": block}
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(native_result), stderr="")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["run", str(self.study_path), "--engine-root", str(self.root),
                         "--native", str(self.native)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["candidate_list"], block)
        command = run.call_args.args[0]
        self.assertIn("--candidates", command)

    @mock.patch("pineforge_hpo.cli.subprocess.run")
    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_native_admission_failure_is_relayed_not_reinterpreted(self, builder: mock.Mock,
                                                                    run: mock.Mock) -> None:
        self.write_study()
        builder.return_value.build.return_value = self.artifact()
        failure = {"schema_version": 1, "ok": False, "failure": {
            "origin": "hpo", "code": "hpo_study_spec_invalid", "args": {"reason": "sampler"},
            "exit_code": 1}}
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=json.dumps(failure), stderr="candidate list is empty")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = main(["run", str(self.study_path), "--engine-root", str(self.root),
                         "--native", str(self.native)])
        self.assertEqual(code, 1)
        relayed = json.loads(stdout.getvalue())["failure"]
        self.assertEqual(relayed["code"], "hpo_study_spec_invalid")
        self.assertEqual(relayed["args"], {"reason": "sampler"})

    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_warm_start_is_refused_before_anything_is_built(self, builder: mock.Mock) -> None:
        self.write_study()
        with self.assertRaisesRegex(WarmStartError, "candidates"):
            prepare_run(self.study_path, self.root, native=self.native,
                        warm_start=self.root / "parent.json")
        builder.return_value.build.assert_not_called()

    @mock.patch("pineforge_hpo.cli.subprocess.run")
    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_patience_is_left_to_native(self, builder: mock.Mock, run: mock.Mock) -> None:
        self.write_study()
        builder.return_value.build.return_value = self.artifact()
        failure = {"schema_version": 1, "ok": False, "failure": {
            "origin": "hpo", "code": "hpo_cli_usage", "args": {}, "exit_code": 1}}
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=json.dumps(failure), stderr="usage")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(["run", str(self.study_path), "--engine-root", str(self.root),
                         "--native", str(self.native), "--no-improvement-trials", "3"])
        self.assertEqual(code, 1)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--no-improvement-trials") + 1], "3")


if __name__ == "__main__":
    unittest.main()
