from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from pineforge_hpo import ArtifactBuildError, HpoError, StudySpecError, ValidationIssue
from pineforge_hpo.cli import (
    CliError,
    _validate_manifest_inputs,
    _validate_strategy_overrides,
    _write_json,
    main,
)
from pineforge_hpo.continuation import SpaceExhaustedError, WarmStartError
from pineforge_hpo.error import failure_document
from pineforge_hpo.study_spec import StrategySpec, StudySpec


ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "python/pineforge_hpo/hpo_failure_codes.json"
CATALOG_BYTES = CATALOG_PATH.read_bytes()
CATALOG = json.loads(CATALOG_BYTES)
SPEC = importlib.util.spec_from_file_location(
    "catalog_diff", ROOT / "scripts/gen_catalog_diff.py"
)
DIFF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIFF)


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
        )
        for error, family, code, arguments in errors:
            with self.subTest(code=code):
                self.assertIsInstance(error, family)
                self.assertIsInstance(error, HpoError)
                self.assertIn("original", str(error))
                self.assertEqual(error.code, code)
                self.assertEqual(error.args, arguments)
                self.assertEqual(error.origin, "hpo")
                declarations = CATALOG["codes"][code]["args"]
                self.assertEqual(arguments.keys(), declarations.keys())
                for name, value in arguments.items():
                    if "values" in declarations[name]:
                        self.assertIn(value, declarations[name]["values"])
        error = CliError("original").with_failure(
            "future_engine_failure", None, "engine"
        )
        self.assertIsNone(error.args)
        self.assertEqual(failure_document(error, 1)["failure"]["origin"], "engine")
        self.assertEqual(str(error), "original")

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
                    caught.exception.args,
                    {"entrypoint": "strategy_set_override", "reason": reason},
                )
        strategy.strategy_overrides.clear()
        strategy.fixed_inputs["Exact Pine Title"] = "bad"
        manifest = [{"title": "Exact Pine Title", "type": "int"}]
        with self.assertRaises(CliError) as caught:
            _validate_manifest_inputs(study, manifest)
        self.assertEqual(
            caught.exception.args,
            {
                "entrypoint": "strategy_set_input",
                "reason": "expected_integer",
                "input": "Exact Pine Title",
            },
        )
        with self.assertRaises(CliError) as caught:
            _validate_manifest_inputs(study, [])
        self.assertEqual(caught.exception.args["reason"], "unknown_key")
        self.assertNotIn("input", caught.exception.args)
        strategy.fixed_inputs.clear()
        strategy.fixed_inputs["forged arbitrary English"] = 1
        with self.assertRaises(CliError) as caught:
            _validate_manifest_inputs(study, manifest)
        self.assertEqual(
            caught.exception.args,
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

    def test_catalog_and_checked_in_diff(self):
        DIFF.validate_catalog(CATALOG)
        document = json.loads(
            (CATALOG_PATH.parent / "hpo_failure_codes_diff.json").read_bytes()
        )
        self.assertEqual(document, DIFF.catalog_diff(None, CATALOG_BYTES, "v0.9.0"))
        self.assertEqual(
            document["to"]["catalogSha256"], hashlib.sha256(CATALOG_BYTES).hexdigest()
        )
        self.assertEqual(len(document["added"]), 17)
        self.assertEqual(document["removed"], [])

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
            git("tag", "v0.9.0")
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
            subprocess.run(command, check=True, capture_output=True)
            subprocess.run([*command, "--check"], check=True, capture_output=True)
            (repo / "diff.json").write_text("{}")
            stale = subprocess.run([*command, "--check"], capture_output=True)
            self.assertNotEqual(stale.returncode, 0)
            subprocess.run(
                [*command, "--release-version", "0.10.0"],
                check=True,
                capture_output=True,
            )
            release = json.loads((repo / "diff.json").read_bytes())
            self.assertEqual(release["to"]["version"], "0.10.0")
            self.assertNotIn("unreleased", release["to"])
            subprocess.run([*command, "--check"], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
