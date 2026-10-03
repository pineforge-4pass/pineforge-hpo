from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from pineforge_hpo.artifact import StrategyArtifact
from pineforge_hpo import __version__
from pineforge_hpo.cli import (
    CliError,
    _parser,
    _native_command,
    _resolve_engine_root,
    _validate_manifest_inputs,
    main,
)
from pineforge_hpo.study_spec import load_study_spec


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        engine_header = self.root / "include" / "pineforge" / "pineforge.h"
        engine_header.parent.mkdir(parents=True)
        engine_header.write_text("/* test engine */\n", encoding="utf-8")
        self.source = self.root / "strategy.pine"
        self.source.write_text("//@version=6\nstrategy('test')\n", encoding="utf-8")
        self.data = self.root / "bars.csv"
        self.data.write_text(
            "timestamp,open,high,low,close,volume\n1,1,1,1,1,1\n",
            encoding="utf-8",
        )
        self.native = self.root / "pineforge-hpo-native"
        self.native.write_text("", encoding="utf-8")
        self.native.chmod(0o755)
        self.study_path = self.root / "study.json"
        self._write_study()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_version_comes_from_package_metadata(self) -> None:
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as caught,
        ):
            _parser().parse_args(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"pineforge-hpo {__version__}")

    def test_engine_resolution_prefers_pinned_submodule(self) -> None:
        repository = self.root / "repository"
        submodule = repository / "external" / "pineforge-engine"
        sibling = repository.parent / "pineforge-engine"
        for engine in (submodule, sibling):
            header = engine / "include" / "pineforge" / "pineforge.h"
            header.parent.mkdir(parents=True)
            header.write_text("/* test engine */\n", encoding="utf-8")

        with (
            mock.patch("pineforge_hpo.cli._repository_root", return_value=repository),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(_resolve_engine_root(None), submodule.resolve())

    def _write_study(self, **updates: object) -> None:
        document: dict[str, object] = {
            "schema_version": 1,
            "mode": "single_strategy",
            "strategies": [
                {
                    "id": "test",
                    "source": "strategy.pine",
                    "datasets": ["bars"],
                    "fixed_inputs": {"Enabled": True},
                    "strategy_overrides": {"initial_capital": 10000},
                    "search_space": {
                        "Length": {"kind": "integer", "low": 2, "high": 4, "step": 1},
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
                "expression": "metrics.all.net_profit - metrics.equity.max_equity_drawdown",
                "constraints": ["metrics.all.num_trades >= 1"],
            },
            "sampler": {"kind": "grid", "seed": 7, "trials": 6},
            "execution": {"workers": 2, "isolation": "threads"},
        }
        document.update(updates)
        self.study_path.write_text(json.dumps(document), encoding="utf-8")

    def _artifact(self) -> StrategyArtifact:
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

    def test_native_command_maps_study_contract(self) -> None:
        study = load_study_spec(self.study_path, require_files=True)
        command = _native_command(
            study,
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )
        self.assertEqual(command[:2], [str(self.native), "run"])
        self.assertIn("--int-dim", command)
        self.assertIn("--real-dim", command)
        self.assertIn("--fixed-input", command)
        self.assertIn("--strategy-override", command)
        self.assertEqual(command[command.index("--workers") + 1], "2")
        self.assertEqual(command[command.index("--seed") + 1], "7")
        self.assertEqual(
            command[command.index("--candidate-policy") + 1], "sampler_default"
        )

    def test_native_command_forwards_batching_and_pruning(self) -> None:
        self._write_study(
            execution={
                "workers": 2,
                "isolation": "threads",
                "batch_size": 8,
                "batch_lag": 1,
                "pruner": "halving",
                "pruner_eta": 3,
                "pruner_rungs": [0.125, 0.5],
            }
        )
        command = _native_command(
            load_study_spec(self.study_path),
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )
        for name, value in (
            ("--batch-size", "8"),
            ("--batch-lag", "1"),
            ("--pruner", "halving"),
            ("--pruner-eta", "3"),
            ("--pruner-rungs", "0.125,0.5"),
        ):
            self.assertEqual(command[command.index(name) + 1], value)

    def test_candidate_policy_maps_to_native(self) -> None:
        document = json.loads(self.study_path.read_text(encoding="utf-8"))
        document["sampler"] = {
            "kind": "tpe",
            "seed": 17,
            "trials": 6,
            "candidate_policy": "exhaustive",
        }
        self.study_path.write_text(json.dumps(document), encoding="utf-8")

        command = _native_command(
            load_study_spec(self.study_path),
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )

        self.assertEqual(
            command[command.index("--candidate-policy") + 1],
            "exhaustive",
        )

    @mock.patch("pineforge_hpo.cli.subprocess.run")
    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_run_builds_once_and_emits_augmented_result(
        self, builder_type: mock.Mock, run: mock.Mock
    ) -> None:
        builder_type.return_value.build.return_value = self._artifact()
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "artifact_key": "a" * 64,
                    "best_trial_id": 0,
                    "best_value": 1.5,
                    "trials": [],
                }
            ),
            stderr="",
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "run",
                    str(self.study_path),
                    "--engine-root",
                    str(self.root),
                    "--native",
                    str(self.native),
                ]
            )
        self.assertEqual(exit_code, 0)
        builder_type.return_value.build.assert_called_once()
        document = json.loads(stdout.getvalue())
        self.assertEqual(document["strategy_id"], "test")
        self.assertEqual(document["dataset_id"], "bars")
        self.assertEqual(document["artifact"]["artifact_key"], "a" * 64)
        self.assertFalse(document["artifact"]["cache_hit"])

    @mock.patch("pineforge_hpo.cli.subprocess.run")
    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_public_prepare_run_does_not_launch_native(
        self, builder_type: mock.Mock, run: mock.Mock
    ) -> None:
        import pineforge_hpo

        builder_type.return_value.build.return_value = self._artifact()
        with mock.patch.dict(os.environ, {"PINEFORGE_HPO_NATIVE": str(self.native)}):
            command, artifact = pineforge_hpo.prepare_run(
                self.study_path, self.root, self.root / "cache"
            )
        expected = _native_command(
            load_study_spec(self.study_path, require_files=True),
            native=self.native,
            plugin=self._artifact().plugin_path,
            artifact_key="a" * 64,
        )
        self.assertEqual(command, expected)
        self.assertEqual(artifact["artifact_key"], "a" * 64)
        builder_type.return_value.build.assert_called_once()
        run.assert_not_called()

    def test_grid_continuous_real_is_rejected_before_native_execution(self) -> None:
        study = load_study_spec(self.study_path)
        parameter = study.strategy.search_space["Threshold"]
        object.__setattr__(parameter, "step", None)
        with self.assertRaisesRegex(CliError, "grid sampling a real requires step"):
            _native_command(
                study,
                native=self.native,
                plugin=self.root / "strategy.dylib",
                artifact_key="a" * 64,
            )

    def test_dlib_global_maps_to_native_and_accepts_continuous_real(self) -> None:
        document = json.loads(self.study_path.read_text(encoding="utf-8"))
        document["sampler"] = {
            "kind": "dlib_global",
            "seed": 17,
            "trials": 24,
        }
        document["strategies"][0]["search_space"]["Threshold"].pop("step")
        self.study_path.write_text(json.dumps(document), encoding="utf-8")

        study = load_study_spec(self.study_path)
        command = _native_command(
            study,
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )

        self.assertEqual(command[command.index("--sampler") + 1], "dlib_global")
        real = command.index("--real-dim")
        self.assertEqual(command[real + 4], "continuous")

    def test_dlib_global_rejects_nonportable_seed(self) -> None:
        document = json.loads(self.study_path.read_text(encoding="utf-8"))
        document["sampler"] = {
            "kind": "dlib_global",
            "seed": 17,
            "trials": 2,
        }
        self.study_path.write_text(json.dumps(document), encoding="utf-8")
        study = load_study_spec(self.study_path)
        object.__setattr__(study.sampler, "seed", 2_147_483_648)

        with self.assertRaisesRegex(CliError, "portable dlib_global seeding"):
            _native_command(
                study,
                native=self.native,
                plugin=self.root / "strategy.dylib",
                artifact_key="a" * 64,
            )

    def test_tpe_config_maps_to_native_flags_and_accepts_continuous_real(self) -> None:
        document = json.loads(self.study_path.read_text(encoding="utf-8"))
        document["sampler"] = {
            "kind": "tpe",
            "seed": 17,
            "trials": 100,
            "config": {
                "startup_trials": 12,
                "ei_candidates": 48,
                "gamma_fraction": 0.2,
                "gamma_cap": 30,
                "prior_weight": 2.5,
                "constant_liar": False,
            },
        }
        document["strategies"][0]["search_space"]["Threshold"].pop("step")
        self.study_path.write_text(json.dumps(document), encoding="utf-8")

        study = load_study_spec(self.study_path)
        command = _native_command(
            study,
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )

        self.assertEqual(command[command.index("--sampler") + 1], "tpe")
        expected = {
            "--tpe-startup-trials": "12",
            "--tpe-ei-candidates": "48",
            "--tpe-gamma-fraction": "0.2",
            "--tpe-gamma-cap": "30",
            "--tpe-prior-weight": "2.5",
            "--tpe-constant-liar": "false",
        }
        for option, value in expected.items():
            self.assertEqual(command[command.index(option) + 1], value)
        real = command.index("--real-dim")
        self.assertEqual(command[real + 4], "continuous")

    def test_tpe_defaults_are_always_forwarded_to_native(self) -> None:
        document = json.loads(self.study_path.read_text(encoding="utf-8"))
        document["sampler"] = {"kind": "tpe", "seed": 17, "trials": 100}
        self.study_path.write_text(json.dumps(document), encoding="utf-8")

        study = load_study_spec(self.study_path)
        command = _native_command(
            study,
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )

        expected = {
            "--tpe-startup-trials": "10",
            "--tpe-ei-candidates": "24",
            "--tpe-gamma-fraction": "0.1",
            "--tpe-gamma-cap": "25",
            "--tpe-prior-weight": "1.0",
            "--tpe-constant-liar": "true",
        }
        for option, value in expected.items():
            self.assertEqual(command[command.index(option) + 1], value)

    def test_log_dimensions_map_to_native_flags(self) -> None:
        document = json.loads(self.study_path.read_text(encoding="utf-8"))
        document["sampler"] = {"kind": "tpe", "seed": 17, "trials": 100}
        document["strategies"][0]["search_space"] = {
            "Length": {
                "kind": "integer",
                "low": 1,
                "high": 1_000_000,
                "step": 1,
                "log": True,
            },
            "Threshold": {
                "kind": "real",
                "low": 1e-6,
                "high": 1e3,
                "log": True,
            },
        }
        self.study_path.write_text(json.dumps(document), encoding="utf-8")

        command = _native_command(
            load_study_spec(self.study_path),
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )

        integer = command.index("--log-int-dim")
        self.assertEqual(command[integer + 1 : integer + 4], ["Length", "1", "1000000"])
        real = command.index("--log-real-dim")
        self.assertEqual(command[real + 1 : real + 4], ["Threshold", "1e-06", "1000.0"])
        self.assertNotIn("--int-dim", command)
        self.assertNotIn("--real-dim", command)

    def test_grid_accepts_log_integer_but_rejects_log_real(self) -> None:
        document = json.loads(self.study_path.read_text(encoding="utf-8"))
        search_space = document["strategies"][0]["search_space"]
        search_space.pop("Threshold")
        search_space["Length"]["log"] = True
        self.study_path.write_text(json.dumps(document), encoding="utf-8")

        command = _native_command(
            load_study_spec(self.study_path),
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )
        self.assertIn("--log-int-dim", command)

        search_space["Threshold"] = {
            "kind": "real",
            "low": 1e-3,
            "high": 1e3,
            "log": True,
        }
        self.study_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(CliError, "grid sampling a real requires step"):
            _native_command(
                load_study_spec(self.study_path),
                native=self.native,
                plugin=self.root / "strategy.dylib",
                artifact_key="a" * 64,
            )

    def test_native_failure_is_reported_without_json(self) -> None:
        artifact_path = self.root / "strategy.dylib"
        artifact_path.write_bytes(b"plugin")
        import hashlib

        (self.root / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "artifact_key": "a" * 64,
                    "request_key": "b" * 64,
                    "plugin_sha256": hashlib.sha256(b"plugin").hexdigest(),
                    "inputs": list(self._artifact().inputs),
                }
            ),
            encoding="utf-8",
        )
        document = json.loads(self.study_path.read_text(encoding="utf-8"))
        strategy = document["strategies"][0]
        strategy.pop("source")
        strategy["artifact"] = "strategy.dylib"
        self.study_path.write_text(json.dumps(document), encoding="utf-8")
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="bad plugin"
        )
        stderr = io.StringIO()
        with mock.patch("pineforge_hpo.cli.subprocess.run", return_value=completed):
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    ["run", str(self.study_path), "--native", str(self.native)]
                )
        self.assertEqual(exit_code, 1)
        self.assertIn("native runner failed with exit 1: bad plugin", stderr.getvalue())

    def test_manifest_rejects_wrong_input_kind_and_bounds(self) -> None:
        study = load_study_spec(self.study_path)
        inputs = list(self._artifact().inputs)
        inputs[0] = {"title": "Length", "type": "float", "min": 2, "max": 3}
        with self.assertRaisesRegex(CliError, "kind 'integer'.*type 'float'"):
            _validate_manifest_inputs(study, inputs)

        inputs[0] = {"title": "Length", "type": "int", "min": 2, "max": 3}
        with self.assertRaisesRegex(CliError, "above the Pine input maximum"):
            _validate_manifest_inputs(study, inputs)

    def test_unsupported_override_and_execution_controls_fail_at_startup(self) -> None:
        study = load_study_spec(self.study_path)
        study.strategy.strategy_overrides["unknown"] = 1
        with self.assertRaisesRegex(CliError, "unsupported strategy overrides"):
            _native_command(
                study,
                native=self.native,
                plugin=self.root / "strategy.dylib",
                artifact_key="a" * 64,
            )

        study.strategy.strategy_overrides.pop("unknown")
        study.strategy.strategy_overrides["initial_capital"] = -1
        with self.assertRaisesRegex(CliError, "initial_capital must be positive"):
            _native_command(
                study,
                native=self.native,
                plugin=self.root / "strategy.dylib",
                artifact_key="a" * 64,
            )

        self._write_study(
            execution={
                "workers": 2,
                "isolation": "threads",
                "timeout_seconds": 1,
            }
        )
        timed = load_study_spec(self.study_path)
        with self.assertRaisesRegex(CliError, "timeout_seconds is not implemented"):
            _native_command(
                timed,
                native=self.native,
                plugin=self.root / "strategy.dylib",
                artifact_key="a" * 64,
            )

    def test_categorical_abi_collision_is_rejected(self) -> None:
        document = json.loads(self.study_path.read_text(encoding="utf-8"))
        document["strategies"][0]["search_space"] = {
            "Threshold": {"kind": "categorical", "choices": [1, 1.0]}
        }
        document["strategies"][0]["fixed_inputs"] = {}
        self.study_path.write_text(json.dumps(document), encoding="utf-8")
        study = load_study_spec(self.study_path)
        with self.assertRaisesRegex(
            CliError, "collide after strategy ABI serialization"
        ):
            _native_command(
                study,
                native=self.native,
                plugin=self.root / "strategy.dylib",
                artifact_key="a" * 64,
            )


if __name__ == "__main__":
    unittest.main()
