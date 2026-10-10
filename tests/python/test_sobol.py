"""StudySpec and CLI route for `sampler.kind: "sobol"` (the 64-bit Gray-code Sobol sampler).

Python validates the schema, forwards the native flags and relays the native result and failure
documents. It never generates, reads, validates or re-serializes a Sobol stream or a parent
history: the native runner is the single authority for the sampler, the descriptor and the parent
admission. UNEXECUTED until the proof phase.
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
from pineforge_hpo.study_spec import StudySpecError, load_study_spec  # noqa: E402

UINT64_MAX = (1 << 64) - 1


class SobolRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        header = self.root / "include" / "pineforge" / "pineforge.h"
        header.parent.mkdir(parents=True)
        header.write_text("/* test engine */\n", encoding="utf-8")
        (self.root / "strategy.pine").write_text(
            "//@version=6\nstrategy('t')\n", encoding="utf-8"
        )
        (self.root / "bars.csv").write_text(
            "timestamp,open,high,low,close,volume\n1,1,1,1,1,1\n", encoding="utf-8"
        )
        self.native = self.root / "pineforge-hpo-native"
        self.native.write_text("", encoding="utf-8")
        self.native.chmod(0o755)
        self.study_path = self.root / "study.json"
        self.parent = self.root / "parent.json"
        # Deliberately not valid JSON: Python must pass the path through without reading it.
        self.parent.write_bytes(b"\x00 not json, not read by python \xff")

    def write_study(self, sampler=None, execution=None, search_space=None) -> Path:
        self.study_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "single_strategy",
                    "strategies": [
                        {
                            "id": "test",
                            "source": "strategy.pine",
                            "datasets": ["bars"],
                            "fixed_inputs": {"Enabled": True},
                            "strategy_overrides": {},
                            "search_space": search_space
                            or {
                                "Length": {
                                    "kind": "integer",
                                    "low": 2,
                                    "high": 4,
                                    "step": 1,
                                },
                                "Threshold": {
                                    "kind": "real",
                                    "low": 0.5,
                                    "high": 1.0,
                                    "step": 0.5,
                                },
                            },
                        }
                    ],
                    "datasets": [
                        {
                            "id": "bars",
                            "ohlcv": "bars.csv",
                            "input_tf": "1",
                            "script_tf": "1",
                            "chart_timezone": "UTC",
                        }
                    ],
                    "objective": {
                        "kind": "expression",
                        "direction": "maximize",
                        "expression": "metrics.all.net_profit",
                        "constraints": [],
                    },
                    "sampler": sampler
                    if sampler is not None
                    else {"kind": "sobol", "seed": 7, "trials": 8},
                    "execution": execution
                    if execution is not None
                    else {"workers": 2, "isolation": "threads"},
                }
            ),
            encoding="utf-8",
        )
        return self.study_path

    def artifact(self) -> StrategyArtifact:
        return StrategyArtifact(
            artifact_key="a" * 64,
            request_key="b" * 64,
            plugin_path=self.root / "strategy.dylib",
            generated_cpp_path=self.root / "strategy.cpp",
            manifest_path=self.root / "manifest.json",
            provenance_path=self.root / "provenance.json",
            cache_hit=False,
            inputs=(
                {"title": "Length", "type": "int"},
                {"title": "Threshold", "type": "float"},
                {"title": "Enabled", "type": "bool"},
            ),
            strategy_params={},
        )

    def command(self):
        return _native_command(
            load_study_spec(self.study_path),
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )

    # ---- schema ---------------------------------------------------------------------------

    def test_defaults_and_both_scrambles(self) -> None:
        self.write_study()
        spec = load_study_spec(self.study_path)
        self.assertEqual(spec.sampler.kind, "sobol")
        self.assertEqual(spec.sampler.scramble, "digital_shift")
        self.assertEqual(spec.sampler.seed, 7)
        self.assertEqual(spec.sampler.trials, 8)
        self.assertEqual(spec.sampler.candidate_policy, "sampler_default")
        for scramble in ("digital_shift", "none"):
            self.write_study(
                {
                    "kind": "sobol",
                    "seed": 0,
                    "trials": 1,
                    "config": {"scramble": scramble},
                }
            )
            self.assertEqual(
                load_study_spec(self.study_path).sampler.scramble, scramble
            )
        for sampler in (
            {"kind": "tpe", "seed": 1, "trials": 2},
            {"kind": "grid", "seed": 1, "trials": 2},
            {"kind": "random", "seed": 1, "trials": 2},
        ):
            self.write_study(sampler)
            self.assertIsNone(load_study_spec(self.study_path).sampler.scramble)

    def test_seed_is_an_exact_unsigned_64_bit_integer(self) -> None:
        for seed in (0, 1, 2**53 + 1, 2**63, UINT64_MAX):
            with self.subTest(seed=seed):
                self.write_study({"kind": "sobol", "seed": seed, "trials": 3})
                self.assertEqual(load_study_spec(self.study_path).sampler.seed, seed)
                command = self.command()
                self.assertEqual(command[command.index("--seed") + 1], str(seed))
        for seed in (-1, UINT64_MAX + 1, 1.5, "7", True):
            with self.subTest(refused=seed):
                self.write_study({"kind": "sobol", "seed": seed, "trials": 3})
                with self.assertRaises(StudySpecError):
                    load_study_spec(self.study_path)

    def test_schema_refusals(self) -> None:
        cases = (
            (
                {
                    "kind": "sobol",
                    "seed": 1,
                    "trials": 4,
                    "config": {"scramble": "owen"},
                },
                "must be digital_shift or none",
            ),
            (
                {"kind": "sobol", "seed": 1, "trials": 4, "config": {"scramble": 1}},
                "must be digital_shift or none",
            ),
            (
                {"kind": "sobol", "seed": 1, "trials": 4, "config": {"scramble": None}},
                "must be digital_shift or none",
            ),
            (
                {
                    "kind": "sobol",
                    "seed": 1,
                    "trials": 4,
                    "config": {"startup_trials": 3},
                },
                "unknown field",
            ),
            (
                {
                    "kind": "sobol",
                    "seed": 1,
                    "trials": 4,
                    "config": {"candidates_file": "x.jsonl"},
                },
                "unknown field",
            ),
            ({"kind": "sobol", "seed": 1, "trials": 0}, "must be a positive integer"),
            ({"kind": "sobol", "seed": 1, "trials": -3}, "must be a positive integer"),
            (
                {
                    "kind": "sobol",
                    "seed": 1,
                    "trials": 4,
                    "candidate_policy": "exhaustive",
                },
                "candidate",
            ),
            (
                {
                    "kind": "sobol",
                    "seed": 1,
                    "trials": 4,
                    "candidate_policy": "without_replacement",
                },
                "candidate",
            ),
            (
                {"kind": "sobol", "seed": 1, "trials": 4, "config": "digital_shift"},
                "must be an object",
            ),
            (
                {
                    "kind": "grid",
                    "seed": 1,
                    "trials": 4,
                    "config": {"scramble": "none"},
                },
                "is reserved and must be empty",
            ),
            (
                {"kind": "tpe", "seed": 1, "trials": 4, "config": {"scramble": "none"}},
                "unknown field",
            ),
            ({"kind": "sobolev", "seed": 1, "trials": 4}, "or sobol"),
        )
        for sampler, expected in cases:
            with self.subTest(sampler=sampler):
                self.write_study(sampler)
                with self.assertRaisesRegex(StudySpecError, expected):
                    load_study_spec(self.study_path)

    def test_patience_and_pruner_stay_ordinary_for_sobol(self) -> None:
        # A pruned row has a prefix objective but the stream does not depend on feedback, so the
        # study spec keeps accepting a pruner (unlike the candidate list).
        self.write_study(
            execution={
                "workers": 2,
                "isolation": "threads",
                "pruner": "median",
                "batch_size": 4,
            }
        )
        load_study_spec(self.study_path)

    def test_continuous_and_overflowing_spaces_are_not_a_python_concern(self) -> None:
        wide = {
            f"p{index}": {"kind": "integer", "low": 0, "high": 10**9, "step": 1}
            for index in range(8)
        }
        wide["Level"] = {"kind": "real", "low": 0.1, "high": 9.0}
        wide["Scale"] = {"kind": "real", "low": 1e-6, "high": 1e6, "log": True}
        self.write_study(search_space=wide)
        load_study_spec(
            self.study_path
        )  # counts, columns and limits are admitted natively

    # ---- native argv -----------------------------------------------------------------------

    def test_native_command(self) -> None:
        self.write_study(
            {
                "kind": "sobol",
                "seed": 2**64 - 1,
                "trials": 100,
                "config": {"scramble": "none"},
            }
        )
        command = self.command()
        self.assertEqual(command[command.index("--sampler") + 1], "sobol")
        self.assertEqual(command[command.index("--sobol-scramble") + 1], "none")
        self.assertEqual(command[command.index("--seed") + 1], str(UINT64_MAX))
        self.assertEqual(command[command.index("--max-trials") + 1], "100")
        self.assertEqual(
            command[command.index("--candidate-policy") + 1], "sampler_default"
        )
        for forbidden in (
            "--candidates",
            "--warm-start",
            "--tpe-startup-trials",
            "--no-improvement-trials",
        ):
            self.assertNotIn(forbidden, command)
        self.write_study()
        self.assertEqual(
            self.command()[self.command().index("--sobol-scramble") + 1],
            "digital_shift",
        )

    def test_other_samplers_never_receive_the_sobol_flag(self) -> None:
        for sampler in (
            {"kind": "grid", "seed": 1, "trials": 4},
            {"kind": "random", "seed": 1, "trials": 4},
            {"kind": "tpe", "seed": 1, "trials": 4},
        ):
            with self.subTest(sampler=sampler["kind"]):
                self.write_study(sampler)
                self.assertNotIn("--sobol-scramble", self.command())

    # ---- run route -----------------------------------------------------------------------------

    @mock.patch("pineforge_hpo.cli.subprocess.run")
    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_the_native_sobol_block_is_relayed_unchanged(
        self, builder: mock.Mock, run: mock.Mock
    ) -> None:
        self.write_study()
        builder.return_value.build.return_value = self.artifact()
        block = {
            "contract": "pineforge_sobol_v1",
            "scramble": "digital_shift",
            "seed": "7",
            "first_index": "0",
            "next_index": "8",
            "exact_stream": True,
            "identity": "0" * 64,
            "columns": ["Length", "Threshold"],
            "numeric_build_identity": None,
        }
        native_result = {
            "ok": True,
            "trials": [],
            "best_trial_id": 0,
            "best_value": 1.5,
            "sampler": "sobol",
            "sobol": block,
        }
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(native_result), stderr=""
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(
                [
                    "run",
                    str(self.study_path),
                    "--engine-root",
                    str(self.root),
                    "--native",
                    str(self.native),
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["sobol"], block)

    @mock.patch("pineforge_hpo.cli.subprocess.run")
    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_native_refusals_are_relayed_with_their_registered_codes(
        self, builder: mock.Mock, run: mock.Mock
    ) -> None:
        self.write_study()
        builder.return_value.build.return_value = self.artifact()
        cases = (
            (1, "hpo_study_spec_invalid", {"reason": "sampler"}),
            (1, "hpo_toolchain_unavailable", {"reason": "native_runner"}),
            (1, "hpo_portable_math_unavailable", {"requirement": "gradual_underflow"}),
            (4, "hpo_warm_start_rejected", {}),
        )
        for exit_code, code, args in cases:
            with self.subTest(code=code):
                failure = {
                    "schema_version": 1,
                    "ok": False,
                    "failure": {
                        "origin": "hpo",
                        "code": code,
                        "args": args,
                        "exit_code": exit_code,
                    },
                }
                run.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=exit_code,
                    stdout=json.dumps(failure),
                    stderr="native diagnostic",
                )
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    result = main(
                        [
                            "run",
                            str(self.study_path),
                            "--engine-root",
                            str(self.root),
                            "--native",
                            str(self.native),
                        ]
                    )
                self.assertEqual(result, exit_code)
                relayed = json.loads(stdout.getvalue())["failure"]
                self.assertEqual((relayed["code"], relayed["args"]), (code, args))

    @mock.patch("pineforge_hpo.cli.warm_start_metadata")
    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_warm_start_passes_the_parent_through_without_a_python_preflight(
        self, builder: mock.Mock, metadata: mock.Mock
    ) -> None:
        self.write_study()
        builder.return_value.build.return_value = self.artifact()
        metadata.side_effect = AssertionError("python must not parse a sobol parent")
        command, _ = prepare_run(
            self.study_path, self.root, native=self.native, warm_start=self.parent
        )
        metadata.assert_not_called()
        self.assertEqual(
            command[command.index("--warm-start") + 1], str(self.parent.resolve())
        )
        self.assertEqual(
            self.parent.read_bytes(), b"\x00 not json, not read by python \xff"
        )
        # No finite-space exhaustion test applies: a one-point space still continues.
        one_point = {"Length": {"kind": "integer", "low": 3, "high": 3, "step": 1}}
        self.write_study(search_space=one_point)
        command, _ = prepare_run(
            self.study_path, self.root, native=self.native, warm_start=self.parent
        )
        self.assertIn("--warm-start", command)
        metadata.assert_not_called()


if __name__ == "__main__":
    unittest.main()
