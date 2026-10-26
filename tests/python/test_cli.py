from __future__ import annotations

import contextlib
import csv
import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import struct
import tempfile
import unittest
from unittest import mock

from pineforge_hpo.artifact import StrategyArtifact
from pineforge_hpo import __version__
from pineforge_hpo.cli import (
    CliError,
    _artifact_input_metadata,
    _enum_ordinals_needed,
    _precompiled_artifact,
    _parser,
    _native_command,
    _plugin_option_values,
    _resolve_engine_root,
    _validate_manifest_inputs,
    main,
)
from pineforge_hpo.study_spec import StudySpec, StudySpecError, load_study_spec


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

    def test_symbol_feeds_path_and_inline_native_interface(self) -> None:
        for source in (
            "feeds/index.json",
            {
                "symbols": {
                    "BINANCE:ETHUSDT": {
                        "feeds": {"240": "eth.csv", "1D": "eth-daily.csv"}
                    }
                }
            },
            {
                "symbols": {
                    f"E{index}": {"syminfo": {"currency": "α" * 256}}
                    for index in range(256)
                }
            },
        ):
            self._write_study(symbol_feeds=source)
            study = load_study_spec(self.study_path)
            command = _native_command(
                study,
                native=self.native,
                plugin=self.root / "strategy.dylib",
                artifact_key="a" * 64,
            )
            if isinstance(source, str):
                self.assertEqual(study.symbol_feeds, (self.root / source).resolve())
                self.assertEqual(
                    command[command.index("--symbol-feeds") + 1],
                    str((self.root / source).resolve()),
                )
            else:
                self.assertEqual(
                    command[command.index("--symbol-feeds-spec") + 1],
                    str(self.study_path.resolve()),
                )
                self.assertLess(max(map(len, command)), 4096)

    def test_input_symbol_search_dimension_is_refused(self) -> None:
        study = load_study_spec(self.study_path)
        for definition in ({"type": "string", "kind": "symbol"}, {"type": "symbol"}):
            with self.assertRaisesRegex(CliError, "input.symbol.*refused.*D7"):
                _validate_manifest_inputs(
                    study,
                    [
                        {"title": "Length", **definition},
                        {"title": "Threshold", "type": "float"},
                        {"title": "Enabled", "type": "bool"},
                    ],
                    input_kinds_emitted=True,
                )

    def test_fixed_input_symbol_is_allowed(self) -> None:
        self._write_study()
        study = load_study_spec(self.study_path)
        study.strategy.fixed_inputs["Other"] = "BINANCE:ETHUSDT"
        _validate_manifest_inputs(
            study,
            [
                {"title": "Length", "type": "int"},
                {"title": "Threshold", "type": "float"},
                {"title": "Enabled", "type": "bool"},
                {"title": "Other", "type": "string", "kind": "symbol"},
            ],
        )

    def test_ambiguous_string_input_metadata_fails_closed(self) -> None:
        study = load_study_spec(self.study_path)
        inputs = [
            {"title": "Length", "type": "string"},
            {"title": "Threshold", "type": "float"},
            {"title": "Enabled", "type": "bool"},
        ]
        for version in (None, "unknown", "1.0.0", "1.2.0", "invalid"):
            with self.assertRaisesRegex(CliError, "cannot rule out input.symbol.*D7"):
                _validate_manifest_inputs(study, inputs, codegen_version=version)
        document = json.loads(self.study_path.read_text())
        document["strategies"][0]["search_space"]["Length"] = {
            "kind": "categorical",
            "choices": ["fast", "slow"],
        }
        self.study_path.write_text(json.dumps(document))
        study = load_study_spec(self.study_path)
        with self.assertRaisesRegex(
            CliError,
            "manifest not stamped kind-capable.*rebuild the artifact.*codegen version unknown",
        ):
            _validate_manifest_inputs(study, inputs)
        _validate_manifest_inputs(study, inputs, input_kinds_emitted=True)
        inputs.append({"title": "Unrelated", "type": "int", "kind": "symbol"})
        for kind in (None, 42, "symbol", "string"):
            inputs[-1]["kind"] = kind
            with self.assertRaisesRegex(CliError, "manifest not stamped kind-capable"):
                _validate_manifest_inputs(study, inputs, codegen_version="unknown")
        inputs[0]["kind"] = "string"
        with self.assertRaisesRegex(CliError, "manifest not stamped kind-capable"):
            _validate_manifest_inputs(study, inputs)
        _validate_manifest_inputs(study, inputs, input_kinds_emitted=True)
        for kind in ("unknown", [], {}):
            inputs[0]["kind"] = kind
            with self.assertRaisesRegex(CliError, "cannot rule out input.symbol.*D7"):
                _validate_manifest_inputs(
                    study, inputs, codegen_version="1.2.0", input_kinds_emitted=True
                )

    def test_source_enum_and_unrelated_duplicates_are_not_symbols(self) -> None:
        document = json.loads(self.study_path.read_text())
        document["strategies"][0]["search_space"]["Length"] = {
            "kind": "categorical",
            "choices": ["close", "open", "hl2"],
        }
        self.study_path.write_text(json.dumps(document))
        study = load_study_spec(self.study_path)
        inputs = [
            {"title": "Length", "type": "source"},
            {"title": "Threshold", "type": "float"},
            {"title": "Enabled", "type": "bool"},
            {"title": "Unused", "type": "string"},
            {"title": "Unused", "type": "string"},
        ]
        _validate_manifest_inputs(study, inputs, codegen_version="unknown")
        inputs[0]["type"] = "enum"
        _validate_manifest_inputs(study, inputs)
        inputs.append(dict(inputs[0]))
        with self.assertRaisesRegex(CliError, "duplicate title 'Length'"):
            _validate_manifest_inputs(study, inputs)

    def _write_single_input_study(self, name: str, choices: list[object]) -> StudySpec:
        document = json.loads(self.study_path.read_text(encoding="utf-8"))
        strategy = document["strategies"][0]
        strategy["fixed_inputs"] = {}
        strategy["search_space"] = {name: {"kind": "categorical", "choices": choices}}
        self.study_path.write_text(json.dumps(document), encoding="utf-8")
        return load_study_spec(self.study_path)

    def test_enum_subset_choice_follows_the_declared_ordinals(self) -> None:
        # enum Side {neutral, long, short} with options [Side.long, Side.short]:
        # a choice is the member's declaration index, so 1 and 2 are the valid
        # ordinals while 0 and 3 are not. Validating against 0..len(options)-1
        # instead would accept 0 and refuse 2.
        options = ["Side.long", "Side.short"]
        ordinals = ("1", "2")
        for choice in (1, 2):
            study = self._write_single_input_study("Side", [choice])
            _validate_manifest_inputs(
                study,
                [{"title": "Side", "type": "enum", "options": options}],
                option_values={"Side": ordinals},
            )
        for choice in (0, 3):
            study = self._write_single_input_study("Side", [choice])
            with self.assertRaisesRegex(
                CliError,
                r"search_space\.Side\.choices\[0\] is not one of the Pine input options",
            ):
                _validate_manifest_inputs(
                    study,
                    [{"title": "Side", "type": "enum", "options": options}],
                    option_values={"Side": ordinals},
                )

    def test_enum_full_listing_choice_follows_the_declared_ordinals(self) -> None:
        options = ["Side.long", "Side.short"]
        ordinals = ("0", "1")
        for choice in (0, 1):
            study = self._write_single_input_study("Side", [choice])
            _validate_manifest_inputs(
                study,
                [{"title": "Side", "type": "enum", "options": options}],
                option_values={"Side": ordinals},
            )
        for choice in (2, -1):
            study = self._write_single_input_study("Side", [choice])
            with self.assertRaisesRegex(
                CliError,
                r"search_space\.Side\.choices\[0\] is not one of the Pine input options",
            ):
                _validate_manifest_inputs(
                    study,
                    [{"title": "Side", "type": "enum", "options": options}],
                    option_values={"Side": ordinals},
                )

    def test_enum_tag_choice_is_accepted_and_reaches_argv(self) -> None:
        # A manifest tag is a valid choice: the generic options-membership rule
        # of 0.13.x, which needs no settings receipt, is restored, and the tag
        # reaches the native runner unchanged.
        options = ["Side.long", "Side.short"]
        manifest = {"title": "Side", "type": "enum", "options": options}
        study = self._write_single_input_study("Side", ["Side.long"])
        _validate_manifest_inputs(study, [manifest], option_values={"Side": ("0", "1")})
        command = _native_command(
            study,
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )
        first = command.index("--categorical-choice")
        self.assertEqual(
            command[first : first + 3], ["--categorical-choice", "Side", "Side.long"]
        )

    def test_enum_string_choice_outside_the_options_is_refused(self) -> None:
        # A string is a tag only when it is exactly one of the manifest options,
        # so a bare member name, a wrong-case tag and a foreign tag are refused
        # with the named setting error, which names the input.
        options = ["Side.long", "Side.short"]
        manifest = {"title": "Side", "type": "enum", "options": options}
        for choice in ("long", "side.long", "Side.flat"):
            with self.subTest(choice=choice):
                study = self._write_single_input_study("Side", [choice])
                with self.assertRaises(CliError) as caught:
                    _validate_manifest_inputs(
                        study, [manifest], option_values={"Side": ("0", "1")}
                    )
                self.assertEqual(caught.exception.code, "setting_rejected")
                self.assertEqual(
                    caught.exception.failure_args,
                    {
                        "entrypoint": "strategy_set_input",
                        "reason": "invalid_input_option",
                        "input": "Side",
                    },
                )
                self.assertIn(
                    "search_space.Side.choices[0] is not one of the Pine input options",
                    str(caught.exception),
                )

    def test_enum_boolean_choice_is_refused(self) -> None:
        # The choice is refused only by the boolean exclusion in the ordinal
        # test: `isinstance(True, int)` holds. The receipt lists the bool's
        # stringified form, "True", so a rule that dropped that exclusion would
        # accept the choice and turn this test red, and "1" is the ordinal the
        # bool would be accepted as by a rule that coerced it to an int.
        study = self._write_single_input_study("Side", [True])
        with self.assertRaisesRegex(
            CliError,
            r"search_space\.Side\.choices\[0\] is not one of the Pine input options",
        ):
            _validate_manifest_inputs(
                study,
                [
                    {
                        "title": "Side",
                        "type": "enum",
                        "options": ["Side.long", "Side.short"],
                    }
                ],
                option_values={"Side": ("1", "True")},
            )

    def test_enum_float_choice_is_refused(self) -> None:
        # 2.0 is refused only because the ordinal test requires an int, even
        # though it equals the ordinal 2 numerically. The receipt lists "2.0",
        # the float's stringified form, so a rule that dropped the integer test
        # would accept the choice and turn this test red, and "2" is the ordinal
        # a rule that coerced the choice to an int would accept.
        study = self._write_single_input_study("Side", [2.0])
        with self.assertRaisesRegex(
            CliError,
            r"search_space\.Side\.choices\[0\] is not one of the Pine input options",
        ):
            _validate_manifest_inputs(
                study,
                [
                    {
                        "title": "Side",
                        "type": "enum",
                        "options": ["Side.long", "Side.short"],
                    }
                ],
                option_values={"Side": ("2", "2.0")},
            )

    def test_non_enum_option_membership_is_unchanged(self) -> None:
        options = ["fast", "slow"]
        manifest = {"title": "Mode", "type": "string", "options": options}
        for choice in ("fast", "slow"):
            study = self._write_single_input_study("Mode", [choice])
            _validate_manifest_inputs(study, [manifest], input_kinds_emitted=True)
        study = self._write_single_input_study("Mode", ["triple"])
        with self.assertRaisesRegex(
            CliError,
            r"search_space\.Mode\.choices\[0\] is not one of the Pine input options",
        ):
            _validate_manifest_inputs(study, [manifest], input_kinds_emitted=True)

    def test_enum_ordinal_reaches_native_argv_unchanged(self) -> None:
        study = self._write_single_input_study("Side", [0, 1])
        command = _native_command(
            study,
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )
        first = command.index("--categorical-int-choice")
        self.assertEqual(
            command[first : first + 3], ["--categorical-int-choice", "Side", "0"]
        )
        second = command.index("--categorical-int-choice", first + 1)
        self.assertEqual(
            command[second : second + 3], ["--categorical-int-choice", "Side", "1"]
        )

    def test_enum_without_a_readable_receipt_fails_closed(self) -> None:
        # The option positions equal the declaration indices only for a fully
        # listed enum, so with no receipt to read the ordinals from there is
        # nothing to check a choice against and every choice is refused.
        manifest = {
            "title": "Side",
            "type": "enum",
            "options": ["Side.long", "Side.short"],
        }
        for choice in (0, 1, 2):
            with self.subTest(choice=choice):
                study = self._write_single_input_study("Side", [choice])
                with self.assertRaisesRegex(
                    CliError,
                    r"search_space\.Side\.choices\[0\]: the enum ordinals for "
                    r"input 'Side' cannot be read from the strategy's settings "
                    r"receipt; rebuild the strategy with codegen >= 1\.4\.0",
                ) as caught:
                    _validate_manifest_inputs(study, [manifest])
                self.assertEqual(caught.exception.code, "hpo_study_spec_invalid")
                self.assertEqual(
                    caught.exception.failure_args, {"reason": "artifact_metadata"}
                )
        # A receipt that does not list this input is no receipt for it.
        for choice in (1, 2):
            with self.subTest(unlisted=choice):
                study = self._write_single_input_study("Side", [choice])
                with self.assertRaisesRegex(
                    CliError, "the enum ordinals for input 'Side' cannot be read"
                ):
                    _validate_manifest_inputs(
                        study, [manifest], option_values={"Other": ("1", "2")}
                    )

    @mock.patch("pineforge_hpo.cli._plugin_option_values")
    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_enum_tag_choice_needs_no_settings_receipt(
        self, builder_type: mock.Mock, read_ordinals: mock.Mock
    ) -> None:
        # A tag is checked against the manifest options alone, so a study whose
        # enum choices are all tags never loads the plugin: the loader is a
        # recorder that fails the test if it is called at all.
        from pineforge_hpo.cli import prepare_run

        read_ordinals.side_effect = AssertionError("plugin loaded for a tag")
        builder_type.return_value.build.return_value = dataclasses.replace(
            self._artifact(),
            inputs=(
                {
                    "title": "Side",
                    "type": "enum",
                    "options": ["Side.long", "Side.short"],
                },
            ),
        )
        study = self._write_single_input_study("Side", ["Side.long"])
        command, _ = prepare_run(study.spec_path, self.root, native=self.native)
        read_ordinals.assert_not_called()
        first = command.index("--categorical-choice")
        self.assertEqual(
            command[first : first + 3], ["--categorical-choice", "Side", "Side.long"]
        )

    def test_enum_non_contiguous_ordinals_accept_only_the_declared_ones(self) -> None:
        # enum Side {neutral, long, short} with options [Side.neutral,
        # Side.short]: the declared ordinals are 0 and 2, so the value 1 falls
        # in the gap no option names.
        manifest = {
            "title": "Side",
            "type": "enum",
            "options": ["Side.neutral", "Side.short"],
        }
        for choice in (0, 2):
            study = self._write_single_input_study("Side", [choice])
            _validate_manifest_inputs(
                study, [manifest], option_values={"Side": ("0", "2")}
            )
        study = self._write_single_input_study("Side", [1])
        with self.assertRaisesRegex(
            CliError,
            r"search_space\.Side\.choices\[0\] is not one of the Pine input options",
        ):
            _validate_manifest_inputs(
                study, [manifest], option_values={"Side": ("0", "2")}
            )

    def test_plugin_option_values_rejects_a_file_that_is_not_a_library(self) -> None:
        # A plain text file is not a loadable library, so the load fails and no
        # ordinals are read.
        text = self.root / "not-a-plugin.txt"
        text.write_text("not a shared library\n", encoding="utf-8")
        self.assertIsNone(_plugin_option_values(text))

    @mock.patch("pineforge_hpo.cli._plugin_option_values")
    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_prepare_run_reads_enum_ordinals_only_for_enum_dimensions(
        self, builder_type: mock.Mock, read_ordinals: mock.Mock
    ) -> None:
        from pineforge_hpo.cli import prepare_run

        enum_artifact = dataclasses.replace(
            self._artifact(),
            inputs=(
                {
                    "title": "Side",
                    "type": "enum",
                    "options": ["Side.long", "Side.short"],
                },
            ),
        )
        builder_type.return_value.build.return_value = enum_artifact
        study = self._write_single_input_study("Side", [1])

        read_ordinals.return_value = {"Side": ("1", "2")}
        command, _ = prepare_run(study.spec_path, self.root, native=self.native)
        read_ordinals.assert_called_once_with(enum_artifact.plugin_path)
        first = command.index("--categorical-int-choice")
        self.assertEqual(command[first + 1 : first + 3], ["Side", "1"])

        # The patched ordinals are the ones the choice is checked against.
        read_ordinals.reset_mock()
        read_ordinals.return_value = {"Side": ("0",)}
        with self.assertRaisesRegex(
            CliError,
            r"search_space\.Side\.choices\[0\] is not one of the Pine input options",
        ):
            prepare_run(study.spec_path, self.root, native=self.native)
        read_ordinals.assert_called_once_with(enum_artifact.plugin_path)

        # A study whose dimensions are not enums never loads the plugin.
        read_ordinals.reset_mock()
        builder_type.return_value.build.return_value = self._artifact()
        self._write_study()
        prepare_run(self.study_path, self.root, native=self.native)
        read_ordinals.assert_not_called()

    def test_enum_ordinals_gate_admits_only_an_enum_with_options(self) -> None:
        # The loader is worth its in-process load only for a categorical search
        # dimension whose manifest entry is an enum with an `options` list and
        # whose choices include an ordinal. Each False case below is not that
        # shape, so widening the gate to any categorical dimension, or to any
        # enum dimension including a tag-only one, turns it red. The last case
        # has no categorical dimension, so it turns red instead if the gate
        # drops the search-space scan and keys on the manifest alone: its inputs
        # carry an enum with options that no dimension asks for.
        enum = {
            "title": "Side",
            "type": "enum",
            "options": ["Side.long", "Side.short"],
        }
        study = self._write_single_input_study("Side", [1])
        self.assertIs(_enum_ordinals_needed(study, [enum]), True)
        tagged = self._write_single_input_study("Side", ["Side.long", "Side.short"])
        self.assertIs(_enum_ordinals_needed(tagged, [enum]), False)
        self.assertIs(
            _enum_ordinals_needed(study, [{"title": "Side", "type": "enum"}]), False
        )
        string = self._write_single_input_study("Mode", ["fast", "slow"])
        self.assertIs(
            _enum_ordinals_needed(
                string,
                [{"title": "Mode", "type": "string", "options": ["fast", "slow"]}],
            ),
            False,
        )
        source = self._write_single_input_study("Source", ["close", "open"])
        self.assertIs(
            _enum_ordinals_needed(source, [{"title": "Source", "type": "source"}]),
            False,
        )
        boolean = self._write_single_input_study("Enabled", [0, 1])
        self.assertIs(
            _enum_ordinals_needed(boolean, [{"title": "Enabled", "type": "bool"}]),
            False,
        )
        self._write_study()
        numeric = load_study_spec(self.study_path)
        self.assertIs(_enum_ordinals_needed(numeric, [enum]), False)

    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_prepare_run_refuses_an_enum_study_when_the_plugin_cannot_be_loaded(
        self, builder_type: mock.Mock
    ) -> None:
        # The loader is not patched here: the artifact's plugin path is a text
        # file, so the real load fails, no ordinals are read, and every choice
        # is refused before a trial — 0 and 1 included, which the deleted
        # position-based fallback accepted. A rule that guessed the ordinals
        # from the manifest instead of failing closed would accept them and turn
        # this test red.
        from pineforge_hpo.cli import prepare_run

        plugin = self.root / "not-a-plugin.txt"
        plugin.write_text("not a shared library\n", encoding="utf-8")
        builder_type.return_value.build.return_value = dataclasses.replace(
            self._artifact(),
            plugin_path=plugin,
            inputs=(
                {
                    "title": "Side",
                    "type": "enum",
                    "options": ["Side.long", "Side.short"],
                },
            ),
        )
        for choice in (0, 1, 2):
            with self.subTest(choice=choice):
                study = self._write_single_input_study("Side", [choice])
                with self.assertRaises(CliError) as caught:
                    prepare_run(study.spec_path, self.root, native=self.native)
                self.assertIn(
                    "the enum ordinals for input 'Side' cannot be read",
                    str(caught.exception),
                )
                self.assertEqual(caught.exception.code, "hpo_study_spec_invalid")
                self.assertEqual(
                    caught.exception.failure_args, {"reason": "artifact_metadata"}
                )

    def test_input_metadata_capability_marker_and_malformed_identity(self) -> None:
        manifest = self.root / "manifest.json"
        for identity in (None, [], {"codegen": []}):
            manifest.write_text(
                json.dumps({"input_kind_schema": 1, "request_identity": identity})
            )
            self.assertEqual(_artifact_input_metadata(manifest), (None, True))
        for marker in (True, "1", 2):
            manifest.write_text(json.dumps({"input_kind_schema": marker}))
            self.assertEqual(_artifact_input_metadata(manifest), (None, False))
        for version in (
            "1",
            "1.0",
            "1.0.9",
            "v1.0.0",
            "1.1rc1",
            "1.1.0-rc.1",
            "1.1.0rc1",
            "1.1.0.dev1",
        ):
            manifest.write_text(
                json.dumps(
                    {
                        "input_kind_schema": 1,
                        "request_identity": {"codegen": {"version": version}},
                    }
                )
            )
            with self.assertRaisesRegex(CliError, "contradicts recorded codegen"):
                _artifact_input_metadata(manifest)

    def test_precompiled_marker_cross_checks_provenance(self) -> None:
        plugin = self.root / "plugin.so"
        plugin.write_bytes(b"fixture plugin bytes")
        manifest = {
            "schema_version": 1,
            "inputs": [],
            "input_kind_schema": 1,
            "plugin_sha256": hashlib.sha256(plugin.read_bytes()).hexdigest(),
            "artifact_key": "a" * 64,
            "request_identity": {"codegen": {"version": "1.1.0"}},
        }
        (self.root / "manifest.json").write_text(json.dumps(manifest))
        provenance = self.root / "provenance.json"
        provenance.write_text(json.dumps(manifest))
        _precompiled_artifact(plugin)
        for field, value in (
            ("input_kind_schema", True),
            ("input_kind_schema", None),
            ("artifact_key", "b" * 64),
            ("plugin_sha256", "b" * 64),
            ("request_identity", {"codegen": {"version": "1.0.9"}}),
        ):
            provenance.write_text(json.dumps({**manifest, field: value}))
            with self.assertRaisesRegex(CliError, "disagrees with provenance"):
                _precompiled_artifact(plugin)

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
        self.assertEqual(command[4:6], ["--ohlcv", str(self.data.resolve())])
        self.assertNotIn("--ohlcv-bin", command)
        self.assertIn("--int-dim", command)
        self.assertIn("--real-dim", command)
        self.assertIn("--fixed-input", command)
        self.assertIn("--strategy-override", command)
        self.assertEqual(command[command.index("--workers") + 1], "2")
        self.assertEqual(command[command.index("--seed") + 1], "7")
        self.assertEqual(
            command[command.index("--candidate-policy") + 1], "sampler_default"
        )

    def test_native_command_forwards_ohlcv_bin_with_csv_symbol_feeds(self) -> None:
        binary = self.root / "bars.bin"
        with self.data.open(newline="", encoding="utf-8") as source:
            rows = list(csv.DictReader(source))
        binary.write_bytes(
            b"".join(
                struct.pack(
                    "<5dq",
                    *(
                        float(row[key])
                        for key in ("open", "high", "low", "close", "volume")
                    ),
                    int(row["timestamp"]),
                )
                for row in rows
            )
        )
        Path(str(binary) + ".json").write_text(
            json.dumps(
                {
                    "format": "pf_bar_t/v1",
                    "record_size": 48,
                    "symbol": "TEST:BIN",
                    "tf": "1",
                    "count": len(rows),
                    "first_ts": int(rows[0]["timestamp"]),
                    "last_ts": int(rows[-1]["timestamp"]),
                    "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        self._write_study(
            datasets=[{"id": "bars", "ohlcv_bin": "bars.bin", "input_tf": "1"}],
            symbol_feeds="feeds/index.json",
        )
        study = load_study_spec(self.study_path, require_files=True)
        self.assertIsNone(study.datasets[0].ohlcv)
        self.assertEqual(study.datasets[0].ohlcv_bin, binary.resolve())
        command = _native_command(
            study,
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )
        self.assertEqual(
            command[command.index("--ohlcv-bin") + 1], str(binary.resolve())
        )
        self.assertNotIn("--ohlcv", command)
        self.assertEqual(command[command.index("--verify-bin") + 1], "full")
        self.assertEqual(
            command[command.index("--symbol-feeds") + 1],
            str((self.root / "feeds/index.json").resolve()),
        )

    def test_dataset_binary_sidecar_verification_is_forwarded(self) -> None:
        self._write_study(
            datasets=[
                {
                    "id": "bars",
                    "ohlcv_bin": "bars.bin",
                    "input_tf": "1",
                    "verify_bin": "sidecar",
                }
            ]
        )
        study = load_study_spec(self.study_path)
        command = _native_command(
            study,
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )
        self.assertEqual(command[command.index("--verify-bin") + 1], "sidecar")

    def test_dataset_binary_verification_refuses_invalid_mode_or_csv(self) -> None:
        for source, mode in (
            ("ohlcv_bin", "unchecked"),
            ("ohlcv_bin", True),
            ("ohlcv", "sidecar"),
        ):
            with self.subTest(source=source, mode=mode):
                self._write_study(
                    datasets=[
                        {
                            "id": "bars",
                            source: "bars.bin",
                            "input_tf": "1",
                            "verify_bin": mode,
                        }
                    ]
                )
                with self.assertRaisesRegex(StudySpecError, "verify_bin"):
                    load_study_spec(self.study_path)

    def test_dataset_csv_and_binary_sources_are_mutually_exclusive(self) -> None:
        self._write_study(
            datasets=[
                {
                    "id": "bars",
                    "ohlcv": "bars.csv",
                    "ohlcv_bin": "bars.bin",
                    "input_tf": "1",
                }
            ]
        )
        with self.assertRaisesRegex(
            StudySpecError, "ohlcv and ohlcv_bin are mutually exclusive"
        ):
            load_study_spec(self.study_path)

    def test_dataset_requires_csv_or_binary_source(self) -> None:
        self._write_study(datasets=[{"id": "bars", "input_tf": "1"}])
        with self.assertRaisesRegex(
            StudySpecError, "one of ohlcv or ohlcv_bin is required"
        ):
            load_study_spec(self.study_path)

    def test_dataset_binary_file_check_uses_binary_path(self) -> None:
        self._write_study(
            datasets=[{"id": "bars", "ohlcv_bin": "missing.bin", "input_tf": "1"}]
        )
        with self.assertRaises(StudySpecError) as caught:
            load_study_spec(self.study_path, require_files=True)
        self.assertIn("$.datasets[0].ohlcv_bin", str(caught.exception))
        self.assertIn("missing.bin", str(caught.exception))

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

    def test_native_command_forwards_rolling_schedule_only_when_set(self) -> None:
        # Rolling forwards --schedule; the default batches must not add the flag, so a study
        # without schedule keeps its argv (and bytes) as before.
        self._write_study(
            execution={"workers": 2, "isolation": "threads", "schedule": "rolling"}
        )
        rolling = _native_command(
            load_study_spec(self.study_path),
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )
        self.assertEqual(rolling[rolling.index("--schedule") + 1], "rolling")
        self._write_study(execution={"workers": 2, "isolation": "threads"})
        default = _native_command(
            load_study_spec(self.study_path),
            native=self.native,
            plugin=self.root / "strategy.dylib",
            artifact_key="a" * 64,
        )
        self.assertNotIn("--schedule", default)

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
    def test_run_forwards_patience_and_keeps_off_argv(
        self, builder_type: mock.Mock, run: mock.Mock
    ) -> None:
        builder_type.return_value.build.return_value = self._artifact()
        native_result = {
            "ok": True,
            "trials": [],
            "best_trial_id": 0,
            "best_value": 1.5,
        }
        commands = []
        for value in (None, "0", "1", str((1 << 64) - 1)):
            with self.subTest(value=value):
                result = dict(native_result)
                if value not in (None, "0"):
                    result["early_stop"] = {
                        "patience_trials": int(value),
                        "trigger_trial_id": None,
                        "drained_through_trial_id": 0,
                        "reference_scope": "part",
                    }
                run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=json.dumps(result), stderr=""
                )
                arguments = [
                    "run",
                    str(self.study_path),
                    "--engine-root",
                    str(self.root),
                    "--native",
                    str(self.native),
                ]
                if value is not None:
                    arguments += ["--no-improvement-trials", value]
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(main(arguments), 0)
                command = run.call_args.args[0]
                commands.append(command)
                document = json.loads(stdout.getvalue())
                if value in (None, "0"):
                    self.assertNotIn("--no-improvement-trials", command)
                    self.assertNotIn("early_stop", document)
                else:
                    self.assertEqual(
                        command[command.index("--no-improvement-trials") + 1], value
                    )
                    self.assertEqual(document["early_stop"], result["early_stop"])
        self.assertEqual(commands[0], commands[1])

    def test_patience_parser_rejects_non_uint64(self) -> None:
        for value in ("-1", " -1", "1.5", "1e2", str(1 << 64), "", "NaN"):
            with self.subTest(value=value):
                stderr = io.StringIO()
                with (
                    contextlib.redirect_stderr(stderr),
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    _parser().parse_args(
                        ["run", str(self.study_path), "--no-improvement-trials", value]
                    )
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("--no-improvement-trials", stderr.getvalue())
                self.assertIn("expected an unsigned 64-bit integer", stderr.getvalue())
                self.assertNotIn("unrecognized arguments", stderr.getvalue())

    @mock.patch("pineforge_hpo.cli.subprocess.run")
    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_run_forwards_trial_retention_only_when_given(
        self, builder_type: mock.Mock, run: mock.Mock
    ) -> None:
        builder_type.return_value.build.return_value = self._artifact()
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"ok": true, "trials": []}', stderr=""
        )
        arguments = [
            "run",
            str(self.study_path),
            "--engine-root",
            str(self.root),
            "--native",
            str(self.native),
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(arguments), 0)
        run.assert_called_once()
        default_command = list(run.call_args.args[0])
        self.assertNotIn("--trials-out", default_command)
        self.assertNotIn("--best-k", default_command)

        for extra in (
            ["--trials-out", "best-k", "--best-k", "3"],
            ["--trials-out", "none"],
            ["--trials-out", "all"],
            ["--best-k", "7"],
            ["--best-k", "1"],
            ["--best-k", "1000000"],
            ["--best-k", "0007"],
            ["--best-k", "+7"],
        ):
            with self.subTest(arguments=extra):
                run.reset_mock()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main([*arguments, *extra]), 0)
                run.assert_called_once()
                command = run.call_args.args[0]
                self.assertEqual(command, [*default_command, *extra])
                for option in ("--trials-out", "--best-k"):
                    if option in extra:
                        self.assertEqual(command.count(option), 1)
                        self.assertEqual(
                            command[command.index(option) + 1],
                            extra[extra.index(option) + 1],
                        )
                    else:
                        self.assertNotIn(option, command)

    @mock.patch("pineforge_hpo.cli.subprocess.run")
    def test_trial_retention_parser_rejects_invalid_values(self, run: mock.Mock) -> None:
        cases = [
            ("--trials-out", value, "--trials-out must be all, best-k, or none")
            for value in ("ALL", "bestk", "", "summary")
        ]
        cases += [
            ("--best-k", value, "--best-k must be between 1 and 1000000")
            for value in ("0", "1000001")
        ]
        cases += [
            (
                "--best-k",
                value,
                f"--best-k requires a non-negative integer, got `{value}`",
            )
            for value in ("-1", "1.5", "abc", "", "18446744073709551616")
        ]
        for option, value, message in cases:
            with self.subTest(option=option, value=value):
                stderr = io.StringIO()
                with (
                    contextlib.redirect_stderr(stderr),
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    main(["run", str(self.study_path), option, value])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(option, stderr.getvalue())
                self.assertIn(message, stderr.getvalue())
                self.assertNotIn("unrecognized arguments", stderr.getvalue())
                run.assert_not_called()

    @mock.patch("pineforge_hpo.cli.subprocess.run")
    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_run_preserves_native_best_k_result(
        self, builder_type: mock.Mock, run: mock.Mock
    ) -> None:
        artifact = self._artifact()
        builder_type.return_value.build.return_value = artifact
        trials = [
            {"trial_id": 0, "status": "ok", "feasible": True, "objective": 1.5},
            {"trial_id": 2, "status": "ok", "feasible": True, "objective": 3.5},
        ]
        native_result = {
            "ok": True,
            "trials_out": "best-k",
            "best_k": 2,
            "trials": trials,
            "best_trial_id": 2,
            "best_value": 3.5,
            "summary": {"counts_by_status": {"ok": 3}, "best_k": trials},
        }
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(native_result), stderr=""
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(
                main(
                    [
                        "run",
                        str(self.study_path),
                        "--engine-root",
                        str(self.root),
                        "--native",
                        str(self.native),
                        "--trials-out",
                        "best-k",
                        "--best-k",
                        "2",
                    ]
                ),
                0,
            )
        run.assert_called_once()
        document = json.loads(stdout.getvalue())
        self.assertEqual({key: document[key] for key in native_result}, native_result)
        self.assertEqual(
            set(document) - set(native_result),
            {"study", "strategy_id", "dataset_id", "artifact"},
        )
        self.assertEqual(document["study"], str(self.study_path.resolve()))
        self.assertEqual(document["strategy_id"], "test")
        self.assertEqual(document["dataset_id"], "bars")
        self.assertEqual(document["artifact"]["artifact_key"], artifact.artifact_key)

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
            native=self.native.resolve(),
            plugin=self._artifact().plugin_path,
            artifact_key="a" * 64,
        )
        self.assertEqual(command, expected)
        self.assertEqual(artifact["artifact_key"], "a" * 64)
        builder_type.return_value.build.assert_called_once()
        run.assert_not_called()

    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_prepare_exhaustive_continuation_uses_remaining_budget(
        self, builder_type: mock.Mock
    ) -> None:
        from pineforge_hpo.cli import prepare_run
        from pineforge_hpo.continuation import recorded_space

        parent = self.root / "parent.jsonl"
        parent.write_text(
            json.dumps(
                {
                    "trial_id": 7,
                    "status": "ok",
                    "feasible": True,
                    "objective": 1.0,
                    "parameters": {"Length": 2, "Threshold": 0.5},
                    "space": recorded_space(load_study_spec(self.study_path)),
                }
            )
        )
        self._write_study(
            sampler={
                "kind": "grid",
                "seed": 7,
                "trials": 5,
                "candidate_policy": "exhaustive",
            }
        )
        builder_type.return_value.build.return_value = self._artifact()
        command, _ = prepare_run(
            self.study_path, self.root, native=self.native, warm_start=parent
        )
        self.assertEqual(command[command.index("--max-trials") + 1], "5")
        self.assertEqual(command[command.index("--candidate-policy") + 1], "exhaustive")
        builder_type.return_value.build.assert_called_once()

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
                "history_switch": 4096,
                "max_threads": 8,
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
            "--tpe-history-switch": "4096",
            "--tpe-max-threads": "8",
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
        self.assertNotIn("--tpe-history-switch", command)

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
