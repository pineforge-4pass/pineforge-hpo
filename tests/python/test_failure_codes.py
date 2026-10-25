from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

from pineforge_hpo import ArtifactBuildError, HpoError, StudySpecError, ValidationIssue
from pineforge_hpo.cli import (
    CliError,
    _native_command,
    _validate_manifest_inputs,
    _validate_strategy_overrides,
    _validate_value_against_manifest,
    _write_json,
    main,
)
from pineforge_hpo.continuation import SpaceExhaustedError, WarmStartError
from pineforge_hpo.error import failure_document
from pineforge_hpo.study_spec import (
    DatasetSpec,
    ExecutionSpec,
    ObjectiveSpec,
    ParameterSpec,
    SamplerSpec,
    StrategySpec,
    StudySpec,
)
from pineforge_hpo.transpile import TranspileFailure, TranspileResult
from pineforge_hpo.window_plan import WindowPlanError


ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "python/pineforge_hpo/hpo_failure_codes.json"
CATALOG_BYTES = CATALOG_PATH.read_bytes()
CATALOG = json.loads(CATALOG_BYTES)
SPEC = importlib.util.spec_from_file_location(
    "catalog_diff", ROOT / "scripts/gen_catalog_diff.py"
)
DIFF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIFF)


class ColorInputTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {
            "title": "Line color",
            "type": "color",
            "default": 4294063685,
        }
        self.inputs = [{"title": "Length", "type": "int"}, self.manifest]

    def study(self, *, fixed_inputs=None, search_space=None):
        return StudySpec(
            schema_version=1,
            mode="single_strategy",
            strategy=StrategySpec(
                id="strategy",
                source=None,
                artifact=Path("plugin.so"),
                dataset_ids=("bars",),
                fixed_inputs={} if fixed_inputs is None else fixed_inputs,
                search_space=(
                    {"Length": ParameterSpec(kind="integer", low=1, high=1, step=1)}
                    if search_space is None
                    else search_space
                ),
                strategy_overrides={},
            ),
            datasets=(
                DatasetSpec(
                    id="bars",
                    ohlcv=Path("bars.csv"),
                    input_tf="1",
                    script_tf="1",
                    chart_timezone="UTC",
                ),
            ),
            objective=ObjectiveSpec(
                kind="expression",
                direction="maximize",
                expression="metrics.all.net_profit",
            ),
            sampler=SamplerSpec(kind="grid", seed=17, trials=1),
            execution=ExecutionSpec(workers=1, isolation="threads"),
            spec_path=Path("study.json"),
        )

    def test_color_manifest_accepts_packed_uint32_values(self):
        # Proves the packed default and both inclusive uint32 endpoints are valid.
        # False pass: only the named color literal works, or fixed/choice paths reject it.
        for value in (4294063685, 0, 4294967295):
            with self.subTest(value=value):
                self.assertIsNone(
                    _validate_value_against_manifest(
                        value, self.manifest, where="search_space.Line color.choices[0]"
                    )
                )
                self.assertIsNone(
                    _validate_manifest_inputs(
                        self.study(fixed_inputs={"Line color": value}), self.inputs
                    )
                )
                self.assertIsNone(
                    _validate_manifest_inputs(
                        self.study(
                            search_space={
                                "Line color": ParameterSpec(
                                    kind="categorical", choices=(value,)
                                )
                            }
                        ),
                        self.inputs,
                    )
                )

    def test_color_manifest_rejects_non_uint32_values_with_integer_reason(self):
        # Proves strict integer typing/range and the engine failure reason/title.
        # False pass: coercion, bool-as-int, unchecked bounds, bypassed validation,
        # or a generic error instead of the required integer reason.
        for value in (
            True,
            False,
            1.5,
            4294063685.0,
            "color.red",
            None,
            -1,
            4294967296,
        ):
            for route in ("value", "fixed", "categorical"):
                with self.subTest(value=value, route=route):
                    with self.assertRaises(CliError) as caught:
                        if route == "value":
                            _validate_value_against_manifest(
                                value,
                                self.manifest,
                                where="search_space.Line color.choices[0]",
                            )
                        elif route == "fixed":
                            _validate_manifest_inputs(
                                self.study(fixed_inputs={"Line color": value}),
                                self.inputs,
                            )
                        else:
                            _validate_manifest_inputs(
                                self.study(
                                    search_space={
                                        "Line color": ParameterSpec(
                                            kind="categorical", choices=(value,)
                                        )
                                    }
                                ),
                                self.inputs,
                            )
                    self.assertEqual(caught.exception.code, "setting_rejected")
                    self.assertEqual(caught.exception.origin, "engine")
                    self.assertEqual(
                        caught.exception.failure_args,
                        {
                            "entrypoint": "strategy_set_input",
                            "reason": "expected_integer",
                            "input": "Line color",
                        },
                    )

    def test_legacy_string_color_manifest_keeps_string_validation(self):
        # Proves the old manifest still accepts strings and rejects packed integers.
        # False pass: migrating color support silently changes string input semantics.
        manifest = {"title": "Line color", "type": "string", "default": "color.red"}
        inputs = [{"title": "Length", "type": "int"}, manifest]
        self.assertIsNone(
            _validate_value_against_manifest(
                "color.red", manifest, where="fixed_inputs.Line color"
            )
        )
        self.assertIsNone(
            _validate_manifest_inputs(
                self.study(fixed_inputs={"Line color": "color.red"}), inputs
            )
        )
        self.assertIsNone(
            _validate_manifest_inputs(
                self.study(
                    search_space={
                        "Line color": ParameterSpec(
                            kind="categorical", choices=("color.red",)
                        )
                    }
                ),
                [manifest],
                input_kinds_emitted=True,
            )
        )
        with self.assertRaises(CliError) as caught:
            _validate_value_against_manifest(
                4294063685, manifest, where="fixed_inputs.Line color"
            )
        self.assertEqual(caught.exception.code, "setting_rejected")
        self.assertEqual(caught.exception.origin, "engine")
        self.assertEqual(
            caught.exception.failure_args,
            {
                "entrypoint": "strategy_set_input",
                "reason": "unparseable_value",
                "input": "Line color",
            },
        )

    def test_color_manifest_default_round_trips_into_sweep_validation(self):
        # Proves a JSON-reported packed default can be reused as a sweep choice.
        # False pass: comparing a default to itself without validating the sweep value.
        manifest = json.loads(json.dumps(self.manifest))
        self.assertEqual(manifest["default"], 4294063685)
        self.assertIs(type(manifest["default"]), int)
        self.assertIsNone(
            _validate_value_against_manifest(
                manifest["default"],
                manifest,
                where="search_space.Line color.choices[0]",
            )
        )
        self.assertIsNone(
            _validate_manifest_inputs(
                self.study(
                    search_space={
                        "Line color": ParameterSpec(
                            kind="categorical", choices=(manifest["default"],)
                        )
                    }
                ),
                [manifest],
            )
        )

    def test_color_integer_dimension_preserves_packed_bounds(self):
        # Proves integer sweeps accept uint32 bounds and serialize them losslessly.
        # False pass: scalar validation works but dimension admission or argv truncates them.
        study = self.study(
            search_space={
                "Line color": ParameterSpec(
                    kind="integer", low=0, high=4294967295, step=1
                )
            }
        )
        self.assertIsNone(_validate_manifest_inputs(study, self.inputs))
        command = _native_command(
            study,
            native=Path("native"),
            plugin=Path("plugin.so"),
            artifact_key="a" * 64,
        )
        self.assertEqual(
            command[-5:], ["--int-dim", "Line color", "0", "4294967295", "1"]
        )

    def test_color_integer_dimension_rejects_bounds_outside_uint32(self):
        # Proves both dimension endpoints use the same packed-word range check.
        # False pass: integer dimension admission bypasses low/high validation.
        for low, high in ((-1, 0), (0, 4294967296)):
            with self.subTest(low=low, high=high):
                study = self.study(
                    search_space={
                        "Line color": ParameterSpec(
                            kind="integer", low=low, high=high, step=1
                        )
                    }
                )
                with self.assertRaises(CliError) as caught:
                    _validate_manifest_inputs(study, self.inputs)
                self.assertEqual(caught.exception.code, "setting_rejected")
                self.assertEqual(caught.exception.origin, "engine")
                self.assertEqual(
                    caught.exception.failure_args,
                    {
                        "entrypoint": "strategy_set_input",
                        "reason": "expected_integer",
                        "input": "Line color",
                    },
                )


class FailureCodeTests(unittest.TestCase):
    def test_exception_types_messages_and_closed_arguments(self):
        errors = (
            (CliError("original"), RuntimeError, "hpo_cli_usage", {}),
            (
                StudySpecError([ValidationIssue("study", "original")]),
                ValueError,
                "hpo_study_spec_invalid",
                {"reason": "study"},
            ),
            (
                ArtifactBuildError("compile", "original"),
                RuntimeError,
                "hpo_toolchain_unavailable",
                {"reason": "compile"},
            ),
            (
                ArtifactBuildError("plugin_validation", "original"),
                RuntimeError,
                "hpo_plugin_invalid",
                {"reason": "validation"},
            ),
            (WarmStartError("original"), ValueError, "hpo_warm_start_rejected", {}),
            (SpaceExhaustedError("original"), ValueError, "hpo_space_exhausted", {}),
            (
                WindowPlanError("window_plan_path", "original"),
                RuntimeError,
                "hpo_walkforward_unavailable",
                {"reason": "window_plan_path"},
            ),
            (
                TranspileFailure(TranspileResult(None)),
                RuntimeError,
                "hpo_transpile_failed",
                {},
            ),
        )
        for error, family, code, arguments in errors:
            with self.subTest(code=code):
                self.assertIsInstance(error, family)
                self.assertIsInstance(error, HpoError)
                if not isinstance(error, TranspileFailure):
                    self.assertIn("original", str(error))
                self.assertEqual(error.code, code)
                self.assertEqual(error.failure_args, arguments)
                self.assertEqual(error.args, (str(error),))
                try:
                    raise error
                except HpoError as caught:
                    self.assertIs(caught, error)
                self.assertEqual(error.origin, "hpo")
                declarations = CATALOG["codes"][code]["args"]
                self.assertEqual(arguments.keys(), declarations.keys())
                for name, value in arguments.items():
                    if "values" in declarations[name]:
                        self.assertIn(value, declarations[name]["values"])
        error = CliError("original").with_failure(
            "future_engine_failure", None, "engine"
        )
        self.assertIsNone(error.failure_args)
        self.assertEqual(failure_document(error, 1)["failure"]["origin"], "engine")
        self.assertEqual(str(error), "original")
        for error_type in (HpoError, CliError, WarmStartError, SpaceExhaustedError):
            error = error_type("original")
            self.assertEqual(str(error_type(*error.args)), "original")
            error.args = ("replacement",)
            self.assertEqual(error.args, ("replacement",))
            self.assertEqual(str(error), "replacement")

    def test_argparse_and_init_failure_documents(self):
        for arguments, exit_code, code in (
            ([], 2, "hpo_cli_usage"),
            (["run", "/missing-study.json"], 1, "hpo_input_file_invalid"),
        ):
            with self.subTest(arguments=arguments):
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    if exit_code == 2:
                        with self.assertRaises(SystemExit) as caught:
                            main(arguments)
                        self.assertEqual(caught.exception.code, 2)
                    else:
                        self.assertEqual(main(arguments), exit_code)
                document = json.loads(stdout.getvalue())
                self.assertFalse(document["ok"])
                self.assertEqual(document["schema_version"], 1)
                self.assertEqual(document["failure"]["code"], code)
                self.assertEqual(document["failure"]["exit_code"], exit_code)
                self.assertTrue(stderr.getvalue())

    def test_preflight_settings_use_engine_codes_and_literal_titles(self):
        strategy = StrategySpec(
            id="strategy",
            source=None,
            artifact=Path("plugin.so"),
            dataset_ids=("bars",),
            fixed_inputs={},
            search_space={},
            strategy_overrides={},
        )
        study = mock.Mock(spec=StudySpec, strategy=strategy)
        for name, value, reason in (
            ("unknown", 1, "unknown_key"),
            ("commission_type", "forged English", "invalid_enum_option"),
            ("initial_capital", "not-numeric", "unparseable_value"),
            ("slippage", "bad", "unparseable_value"),
            ("calc_on_order_fills", "maybe", "invalid_boolean"),
        ):
            with self.subTest(name=name):
                strategy.strategy_overrides.clear()
                strategy.strategy_overrides[name] = value
                with self.assertRaises(CliError) as caught:
                    _validate_strategy_overrides(study)
                self.assertEqual(caught.exception.code, "setting_rejected")
                self.assertEqual(caught.exception.origin, "engine")
                self.assertEqual(
                    caught.exception.failure_args,
                    {"entrypoint": "strategy_set_override", "reason": reason},
                )
        strategy.strategy_overrides.clear()
        strategy.fixed_inputs["Exact Pine Title"] = "bad"
        manifest = [{"title": "Exact Pine Title", "type": "int"}]
        with self.assertRaises(CliError) as caught:
            _validate_manifest_inputs(study, manifest)
        self.assertEqual(
            caught.exception.failure_args,
            {
                "entrypoint": "strategy_set_input",
                "reason": "expected_integer",
                "input": "Exact Pine Title",
            },
        )
        with self.assertRaises(CliError) as caught:
            _validate_manifest_inputs(study, [])
        self.assertEqual(caught.exception.failure_args["reason"], "unknown_key")
        self.assertNotIn("input", caught.exception.failure_args)
        strategy.fixed_inputs.clear()
        strategy.fixed_inputs["forged arbitrary English"] = 1
        with self.assertRaises(CliError) as caught:
            _validate_manifest_inputs(study, manifest)
        self.assertEqual(
            caught.exception.failure_args,
            {"entrypoint": "strategy_set_input", "reason": "unknown_key"},
        )
        self.assertIn("forged arbitrary English", str(caught.exception))

    def test_output_io_failure_metadata(self):
        with mock.patch(
            "pineforge_hpo.cli.tempfile.mkstemp", side_effect=OSError("original")
        ):
            with tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(CliError) as caught:
                    _write_json(Path(temporary) / "result.json", {"ok": True})
        self.assertEqual(caught.exception.code, "hpo_output_io_failed")
        self.assertEqual(str(caught.exception), "original")

    def test_window_plan_catalog_declares_only_implemented_reasons(self):
        # Proves the catalog publishes exactly the twelve closed reasons implemented by the
        # Python identity checks and the native loader.
        # False pass: a generic string argument admits future unimplemented reasons.
        self.assertEqual(
            CATALOG["codes"]["hpo_walkforward_unavailable"],
            {
                "class": "environment",
                "retryable": False,
                "args": {
                    "reason": {
                        "kind": "string",
                        "values": [
                            "window_plan_changed",
                            "window_plan_hash_mismatch",
                            "window_plan_identity",
                            "window_plan_load",
                            "window_plan_manifest_invalid",
                            "window_plan_manifest_missing",
                            "window_plan_missing",
                            "window_plan_missing_pf_plan_selected_primary_v1",
                            "window_plan_missing_pf_selected_plan_version",
                            "window_plan_path",
                            "window_plan_self_test",
                            "window_plan_version",
                        ],
                    }
                },
                "templates": ["Native walk-forward is unavailable ({reason})."],
                "since": "1.7.1",
            },
        )

    def test_catalog_and_checked_in_diff(self):
        DIFF.validate_catalog(CATALOG)
        document = json.loads(
            (CATALOG_PATH.parent / "hpo_failure_codes_diff.json").read_bytes()
        )
        baseline = document["from"]["tag"]
        exists = DIFF.git(ROOT, "ls-tree", baseline, "--", DIFF.CATALOG).strip()
        before = (
            DIFF.git(ROOT, "show", f"{baseline}:{DIFF.CATALOG}") if exists else None
        )
        self.assertEqual(
            document,
            DIFF.catalog_diff(
                before, CATALOG_BYTES, baseline, document["to"]["version"]
            ),
        )
        self.assertEqual(
            document["to"]["catalogSha256"], hashlib.sha256(CATALOG_BYTES).hexdigest()
        )
        before_codes = json.loads(before)["codes"] if before else {}
        self.assertEqual(
            {entry["code"] for entry in document["added"]},
            CATALOG["codes"].keys() - before_codes.keys(),
        )
        self.assertEqual(document["removed"], [])
        if document["to"]["version"] is not None:
            self.assertEqual(
                document["to"]["version"], (ROOT / "VERSION").read_text().strip()
            )
            self.assertNotIn("unreleased", document["to"])

    def test_catalog_diff_vocab_paths_presence_and_deprecation(self):
        updated = copy.deepcopy(CATALOG)
        updated["codes"]["hpo_plugin_invalid"]["args"]["reason"]["values"].append(
            "new_reason"
        )
        updated["codes"]["hpo_invariant"].update(
            {
                "deprecated": True,
                "deprecatedSince": "0.11.0",
                "replacedBy": ["hpo_unclassified_error"],
            }
        )
        document = DIFF.catalog_diff(CATALOG_BYTES, DIFF.dump(updated), "v0.10.0")
        changed = {entry["code"]: entry["fields"] for entry in document["changed"]}
        field = changed["hpo_plugin_invalid"][0]
        self.assertEqual(field["path"], "args.reason.values")
        self.assertTrue(field["beforePresent"] and field["afterPresent"])
        self.assertEqual(field["after"][-1], "new_reason")
        self.assertTrue(
            any(not field["beforePresent"] for field in changed["hpo_invariant"])
        )
        self.assertEqual(
            document["deprecated"][0]["replacedBy"], ["hpo_unclassified_error"]
        )
        del updated["codes"]["hpo_invariant"]
        with self.assertRaisesRegex(ValueError, "cannot be removed"):
            DIFF.catalog_diff(CATALOG_BYTES, DIFF.dump(updated), "v0.10.0")

    def test_check_rejects_stale_diff_and_release_stamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)

            def git(*arguments):
                subprocess.run(
                    ["git", "-C", str(repo), *arguments],
                    check=True,
                    capture_output=True,
                )

            git("init", "-q")
            git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                "baseline",
            )
            baseline = json.loads(
                (CATALOG_PATH.parent / "hpo_failure_codes_diff.json").read_bytes()
            )["from"]["tag"]
            baseline_version = DIFF.version_key(baseline)
            release_version = ".".join(
                map(str, (*baseline_version[:2], baseline_version[2] + 1))
            )
            git("tag", baseline)
            (repo / "catalog.json").write_bytes(CATALOG_BYTES)
            command = [
                "python3",
                str(ROOT / "scripts/gen_catalog_diff.py"),
                "--repo",
                str(repo),
                "--catalog",
                "catalog.json",
                "--output",
                "diff.json",
            ]
            missing = subprocess.run(command, capture_output=True)
            self.assertNotEqual(missing.returncode, 0)
            bad_baseline = subprocess.run(
                [*command, "--from-tag", "v999.0.0"], capture_output=True
            )
            self.assertNotEqual(bad_baseline.returncode, 0)
            self.assertIn(b"baseline tag v999.0.0 is missing", bad_baseline.stderr)
            self.assertIn(b"fetch release tags", bad_baseline.stderr)
            self.assertNotIn(b"Command '[", bad_baseline.stderr)
            subprocess.run(
                [*command, "--from-tag", baseline], check=True, capture_output=True
            )
            subprocess.run([*command, "--check"], check=True, capture_output=True)
            git("add", "catalog.json", "diff.json")
            git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "catalog",
            )
            git("tag", "v" + release_version)
            subprocess.run([*command, "--check"], check=True, capture_output=True)
            altered = json.loads((repo / "diff.json").read_bytes())
            altered["to"]["catalogSha256"] = "0" * 64
            (repo / "diff.json").write_bytes(DIFF.dump(altered))
            stale = subprocess.run([*command, "--check"], capture_output=True)
            self.assertNotEqual(stale.returncode, 0)
            self.assertIn(b"catalog diff is stale", stale.stderr)
            subprocess.run(
                [
                    *command,
                    "--from-tag",
                    baseline,
                    "--release-version",
                    release_version,
                ],
                check=True,
                capture_output=True,
            )
            release = json.loads((repo / "diff.json").read_bytes())
            self.assertEqual(release["to"]["version"], release_version)
            self.assertNotIn("unreleased", release["to"])
            subprocess.run([*command, "--check"], check=True, capture_output=True)
            stamped_bytes = (repo / "diff.json").read_bytes()
            subprocess.run(
                [*command, "--release-version", release_version],
                check=True,
                capture_output=True,
            )
            self.assertEqual((repo / "diff.json").read_bytes(), stamped_bytes)
            subprocess.run(
                [*command, "--check", "--release-version", release_version],
                check=True,
                capture_output=True,
            )
            old_unreleased = subprocess.run(command, capture_output=True)
            self.assertNotEqual(old_unreleased.returncode, 0)
            self.assertIn(
                f"unreleased diff requires baseline v{release_version}".encode(),
                old_unreleased.stderr,
            )
            self.assertEqual((repo / "diff.json").read_bytes(), stamped_bytes)
            next_version = ".".join(
                map(str, (*baseline_version[:2], baseline_version[2] + 2))
            )
            outdated = subprocess.run(
                [*command, "--release-version", next_version], capture_output=True
            )
            self.assertNotEqual(outdated.returncode, 0)
            self.assertIn(
                f"requires baseline v{release_version}, not {baseline}".encode(),
                outdated.stderr,
            )
            self.assertEqual(json.loads((repo / "diff.json").read_bytes()), release)
            subprocess.run(
                [*command, "--from-tag", "v" + release_version],
                check=True,
                capture_output=True,
            )
            unreleased = json.loads((repo / "diff.json").read_bytes())
            self.assertEqual(unreleased["from"]["tag"], "v" + release_version)
            self.assertEqual(
                unreleased["from"]["catalogSha256"],
                hashlib.sha256(CATALOG_BYTES).hexdigest(),
            )
            self.assertIsNone(unreleased["to"]["version"])
            self.assertTrue(unreleased["to"]["unreleased"])
            self.assertEqual(unreleased["added"], [])
            subprocess.run([*command, "--check"], check=True, capture_output=True)
            subprocess.run(
                [*command, "--release-version", next_version],
                check=True,
                capture_output=True,
            )
            advanced = json.loads((repo / "diff.json").read_bytes())
            self.assertEqual(advanced["from"]["tag"], "v" + release_version)
            self.assertEqual(advanced["to"]["version"], next_version)
            subprocess.run([*command, "--check"], check=True, capture_output=True)
            unrelated = subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit-tree",
                    "HEAD^{tree}",
                    "-m",
                    "unrelated",
                ],
                text=True,
            ).strip()
            git("tag", "v999.0.1", unrelated)
            unmerged = subprocess.run(
                [*command, "--from-tag", "v999.0.1"], capture_output=True
            )
            self.assertNotEqual(unmerged.returncode, 0)
            self.assertIn(b"baseline tag v999.0.1 is not an ancestor", unmerged.stderr)
            self.assertNotIn(b"Command '[", unmerged.stderr)


INVALID_RELEASE_VERSIONS = (
    "1.6.0-rc.1",
    "1.6",
    "1.6.0rc",
    "1.6.0rc0",
    "1.6.0rc01",
    "1.6.0RC1",
    "1.6.0a1",
    "1.6.0b1",
    "1.6.0.post1",
    "01.6.0",
    "1.06.0",
    "1.6.00",
    "01.6.0rc1",
    "1.06.0rc1",
    "1.6.00rc1",
    "1.6.0rc-1",
    "1.6.0rc1.0",
    "1.6.0+local",
    "1.6.0 ",
    "1.6.0 trailing text",
    " 1.6.0",
    "1.6.0\n",
    "1.6０.0",
    "",
)


class ReleaseVersionTests(unittest.TestCase):
    def test_version_keys_accept_exact_final_and_rc_forms(self):
        for version, expected in (
            ("0.0.0", (0, 0, 0, 1, 0)),
            ("0.15.0", (0, 15, 0, 1, 0)),
            ("1.6.0", (1, 6, 0, 1, 0)),
            ("0.0.0rc1", (0, 0, 0, 0, 1)),
            ("1.6.0rc1", (1, 6, 0, 0, 1)),
            ("1.6.0rc2", (1, 6, 0, 0, 2)),
            ("1.6.0rc10", (1, 6, 0, 0, 10)),
            ("12.34.56rc789", (12, 34, 56, 0, 789)),
        ):
            for prefix in ("", "v"):
                with self.subTest(version=prefix + version):
                    self.assertEqual(DIFF.version_key(prefix + version), expected)

    def test_version_keys_reject_malformed_forms(self):
        for version in INVALID_RELEASE_VERSIONS:
            for prefix in ("", "v"):
                with self.subTest(version=prefix + version):
                    with self.assertRaisesRegex(ValueError, "N[.]N[.]N"):
                        DIFF.version_key(prefix + version)
        with self.assertRaises(ValueError):
            DIFF.version_key("vv1.6.0")

    def test_version_keys_order_rcs_before_the_final_release(self):
        expected = [
            "1.5.9",
            "1.6.0rc1",
            "1.6.0rc2",
            "1.6.0rc10",
            "1.6.0",
            "1.6.1",
        ]
        for prefix in ("", "v"):
            with self.subTest(prefix=prefix):
                versions = [prefix + version for version in reversed(expected)]
                self.assertEqual(
                    sorted(versions, key=DIFF.version_key),
                    [prefix + version for version in expected],
                )

    def test_catalog_since_accepts_rc1(self):
        catalog = copy.deepcopy(CATALOG)
        catalog["codes"]["hpo_cli_usage"]["since"] = "1.6.0rc1"
        self.assertIsNone(DIFF.validate_catalog(catalog))

    def test_catalog_since_rejects_malformed_versions(self):
        for version in (*INVALID_RELEASE_VERSIONS, "v1.6.0", "v1.6.0rc1"):
            with self.subTest(version=version):
                catalog = copy.deepcopy(CATALOG)
                catalog["codes"]["hpo_cli_usage"]["since"] = version
                with self.assertRaises(ValueError):
                    DIFF.validate_catalog(catalog)


class CatalogReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.git("init", "-q")
        (self.repo / "catalog.json").write_bytes(CATALOG_BYTES)
        self.git("add", "catalog.json")
        self.git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "final catalog",
        )
        self.git("tag", "v0.15.0")
        (self.repo / "VERSION").write_text("1.6.0rc1\n", encoding="utf-8")
        self.output = self.repo / "diff.json"
        self.command = [
            sys.executable,
            str(ROOT / "scripts/gen_catalog_diff.py"),
            "--repo",
            str(self.repo),
            "--catalog",
            "catalog.json",
            "--output",
            "diff.json",
        ]

    def git(self, *arguments):
        subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            check=True,
            capture_output=True,
        )

    def invoke(self, *arguments):
        return subprocess.run(
            [*self.command, *arguments], text=True, capture_output=True
        )

    def stamp(self, version):
        completed = self.invoke("--from-tag", "v0.15.0", "--release-version", version)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(self.output.read_bytes())

    def check_stamp(self, version):
        stamped = self.output.read_bytes()
        for arguments in (("--check",), ("--check", "--release-version", version)):
            with self.subTest(arguments=arguments):
                checked = self.invoke(*arguments)
                self.assertEqual(checked.returncode, 0, checked.stderr)
                self.assertEqual(self.output.read_bytes(), stamped)

    def test_rc1_version_file_stamps_full_version_and_checks(self):
        version = (self.repo / "VERSION").read_text().splitlines()[0]
        document = self.stamp(version)
        self.assertEqual(document["from"]["tag"], "v0.15.0")
        self.assertEqual(document["to"]["version"], "1.6.0rc1")
        self.assertNotIn("unreleased", document["to"])
        self.check_stamp("1.6.0rc1")

    def test_rc2_keeps_final_baseline_and_refuses_explicit_rc_baseline(self):
        self.git("tag", "v1.6.0rc1")
        document = self.stamp("1.6.0rc2")
        self.assertEqual(document["from"]["tag"], "v0.15.0")
        self.assertEqual(document["to"]["version"], "1.6.0rc2")
        self.check_stamp("1.6.0rc2")
        stamped = self.output.read_bytes()
        refused = self.invoke(
            "--from-tag", "v1.6.0rc1", "--release-version", "1.6.0rc2"
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("baselines are final releases", refused.stderr)
        self.assertEqual(self.output.read_bytes(), stamped)

    def test_stored_rc_baseline_is_refused(self):
        self.git("tag", "v1.6.0rc1")
        document = self.stamp("1.6.0rc2")
        document["from"]["tag"] = "v1.6.0rc1"
        document["from"]["version"] = "1.6.0rc1"
        self.output.write_bytes(DIFF.dump(document))
        stored = self.output.read_bytes()
        for arguments in (("--check",), ("--release-version", "1.6.0rc2")):
            with self.subTest(arguments=arguments):
                refused = self.invoke(*arguments)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("baselines are final releases", refused.stderr)
                self.assertEqual(self.output.read_bytes(), stored)

    def test_final_after_rc_tags_stamps_and_checks_against_last_final(self):
        self.git("tag", "v1.6.0rc1")
        self.git("tag", "v1.6.0rc2")
        self.git("tag", "v1.6.0")
        document = self.stamp("1.6.0")
        self.assertEqual(document["from"]["tag"], "v0.15.0")
        self.assertEqual(document["to"]["version"], "1.6.0")
        self.check_stamp("1.6.0")

    def test_rc_ignores_final_tags_at_or_after_its_core(self):
        self.git("tag", "v1.6.0")
        self.git("tag", "v1.6.1")
        document = self.stamp("1.6.0rc1")
        self.assertEqual(document["from"]["tag"], "v0.15.0")
        self.assertEqual(document["to"]["version"], "1.6.0rc1")
        self.check_stamp("1.6.0rc1")

    def test_unreleased_baseline_does_not_advance_to_rc_tags(self):
        self.git("tag", "v1.6.0rc1")
        self.git("tag", "v1.6.0rc2")
        completed = self.invoke("--from-tag", "v0.15.0")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        document = json.loads(self.output.read_bytes())
        self.assertEqual(document["from"]["tag"], "v0.15.0")
        self.assertIsNone(document["to"]["version"])
        self.assertTrue(document["to"]["unreleased"])
        checked = self.invoke("--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_rc_still_requires_latest_older_final_baseline(self):
        self.git("tag", "v0.14.0")
        refused = self.invoke("--from-tag", "v0.14.0", "--release-version", "1.6.0rc1")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("requires baseline v0.15.0, not v0.14.0", refused.stderr)
        self.assertFalse(self.output.exists())

    def test_target_must_be_newer_than_final_baseline(self):
        for version in ("0.14.9", "0.15.0rc1", "0.15.0", "0.15.0rc2"):
            with self.subTest(version=version):
                refused = self.invoke(
                    "--from-tag", "v0.15.0", "--release-version", version
                )
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("target version must be newer", refused.stderr)
                self.assertFalse(self.output.exists())

    def test_release_option_rejects_malformed_and_prefixed_versions(self):
        for version in (*INVALID_RELEASE_VERSIONS, "v1.6.0", "v1.6.0rc1"):
            with self.subTest(version=version):
                refused = self.invoke(
                    "--from-tag", "v0.15.0", "--release-version", version
                )
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("N.N.N", refused.stderr)
                self.assertFalse(self.output.exists())


class WheelReleaseVersionTests(unittest.TestCase):
    def test_hatch_version_pattern_preserves_pep440_rc1(self):
        try:
            from packaging.version import Version
        except ImportError:
            self.fail("Install packaging with 'python -m pip install packaging'")
        configuration = tomllib.loads((ROOT / "pyproject.toml").read_text())
        version_configuration = configuration["tool"]["hatch"]["version"]
        self.assertEqual(version_configuration["path"], "VERSION")
        with tempfile.TemporaryDirectory() as temporary:
            version_file = Path(temporary) / version_configuration["path"]
            version_file.write_text("1.6.0rc1\n", encoding="utf-8")
            match = re.search(
                version_configuration["pattern"], version_file.read_text()
            )
        self.assertIsNotNone(match)
        self.assertEqual(match.group("version"), "1.6.0rc1")
        self.assertEqual(str(Version(match.group("version"))), "1.6.0rc1")


if __name__ == "__main__":
    unittest.main()
