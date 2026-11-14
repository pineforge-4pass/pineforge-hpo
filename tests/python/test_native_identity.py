"""Focused local-value preparation tests, with no native/plugin execution.

The recorded-artifact fixtures exercise receipt interpretation, not compiler
correctness or authenticated custody. The subject is never mocked.
"""

from __future__ import annotations

from dataclasses import replace
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

from pineforge_hpo.artifact import StrategyArtifact  # noqa: E402
from pineforge_hpo.cli import prepare_run  # noqa: E402
from pineforge_hpo.native_identity import (  # noqa: E402
    EvidenceState,
    FileRole,
    NativeIdentityError,
    ProducerRole,
    decode_producer_identity,
    prepare_producer_values,
    producer_identity_record,
    serialize_producer_identity,
)
from pineforge_hpo.study_spec import load_study_spec  # noqa: E402


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def document_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


# Existing public C ABI names, not provisional external card keys.
SYMBOLS = [
    "pf_abi_version",
    "strategy_create",
    "strategy_free",
    "strategy_set_input",
    "strategy_set_override",
    "strategy_get_last_error",
    "run_backtest_full",
    "report_free",
]


class NativeIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source_path = self.root / "strategy.pine"
        self.source_path.write_text(
            '//@version=6\nstrategy("測試")\n', encoding="utf-8"
        )
        self.data_path = self.root / "bars.csv"
        self.data_path.write_bytes(
            b"time,open,high,low,close,volume\n1,10,12,9,11,100\n"
        )
        self.spec_path = self.root / "study.json"
        self.document = {
            "schema_version": 1,
            "mode": "single_strategy",
            "strategies": [
                {
                    "id": "strategy-a",
                    "source": "strategy.pine",
                    "datasets": ["bars"],
                    "search_space": {
                        "Length": {"kind": "integer", "low": 2, "high": 4}
                    },
                }
            ],
            "datasets": [
                {
                    "id": "bars",
                    "ohlcv": "bars.csv",
                    "input_tf": "1",
                    "script_tf": "5",
                    "chart_timezone": "UTC",
                }
            ],
            "objective": {
                "kind": "expression",
                "direction": "maximize",
                "expression": "metrics.all.net_profit",
            },
            "sampler": {"kind": "random", "seed": 17, "trials": 9},
            "execution": {"workers": 2, "isolation": "threads", "batch_size": 3},
        }
        self.spec_path.write_bytes(document_bytes(self.document))
        self.study = load_study_spec(self.spec_path, require_files=True)

    def receipt(
        self,
        *,
        validation: str = "recorded",
        identity_extra: dict | None = None,
    ) -> StrategyArtifact:
        """Create data for the existing internal manifest/provenance domains."""
        source = self.source_path.read_text(encoding="utf-8")
        generated = b"// recorded fixture\n" + source.encode("utf-8")
        # Opaque bytes are deliberately never loaded. RECORDED means only that
        # the fixture contains an ABI/symbol record, not that this is executable.
        plugin = b"recorded plugin fixture\0" + hashlib.sha256(generated).digest()
        self.plugin_path = self.root / "strategy.so"
        self.cpp_path = self.root / "generated.cpp"
        self.plugin_path.write_bytes(plugin)
        self.cpp_path.write_bytes(generated)
        identity = {
            "input_metadata_revision": 2,
            "source_sha256": digest(source.encode("utf-8")),
            "codegen": {"version": "fixture-codegen"},
            "engine": {"version": "fixture-engine", "abi_version": 2},
            "compiler": {"version": "fixture-cxx", "target": "fixture-target"},
            "compile": {"flags": ["-std=c++17", "-ffp-contract=off"]},
        }
        identity.update(identity_extra or {})
        request_key = digest(document_bytes(identity))
        artifact_key = digest(
            document_bytes(
                {
                    "request_identity": identity,
                    "generated_cpp_sha256": digest(generated),
                }
            )
        )
        common = {
            "schema_version": 1,
            "artifact_key": artifact_key,
            "request_key": request_key,
            "request_identity": identity,
            "source_name": str(self.source_path),
            "generated_cpp_sha256": digest(generated),
            "plugin_sha256": digest(plugin),
        }
        if validation != "missing":
            common["plugin_validation"] = (
                {"status": "deferred"}
                if validation == "deferred"
                else {"abi_version": 2, "required_symbols": SYMBOLS}
            )
        self.manifest = {
            **common,
            "plugin_filename": self.plugin_path.name,
            "inputs": [{"title": "Length", "type": "int"}],
            "strategy_params": {},
        }
        self.provenance = {**common, "compile": identity["compile"]}
        self.manifest_path = self.root / "manifest.json"
        self.provenance_path = self.root / "provenance.json"
        self.write_records()
        return StrategyArtifact(
            artifact_key=artifact_key,
            request_key=request_key,
            plugin_path=self.plugin_path,
            generated_cpp_path=self.cpp_path,
            manifest_path=self.manifest_path,
            provenance_path=self.provenance_path,
            cache_hit=False,
            inputs=({"title": "Length", "type": "int"},),
            strategy_params={},
        )

    def write_records(self) -> None:
        self.manifest_path.write_bytes(document_bytes(self.manifest))
        self.provenance_path.write_bytes(document_bytes(self.provenance))

    def prepare(self, artifact: StrategyArtifact):
        return artifact.producer_values(
            self.study, compiled_source=self.source_path.read_text(encoding="utf-8")
        )

    def test_source_build_observations_preserve_metadata_bytes(self) -> None:
        artifact = self.receipt()
        before = (self.manifest_path.read_bytes(), self.provenance_path.read_bytes())
        values = self.prepare(artifact)
        self.assertEqual(values.source.file.role, FileRole.PINE_SOURCE)
        self.assertEqual(
            values.source.file.sha256, digest(self.source_path.read_bytes())
        )
        self.assertEqual(values.plugin.role, FileRole.PLUGIN)
        self.assertEqual(values.plugin.sha256, digest(self.plugin_path.read_bytes()))
        self.assertNotEqual(values.source.file.path, values.plugin.path)
        self.assertEqual(values.build.request_key, artifact.request_key)
        self.assertEqual(values.build.manifest_schema_version, 1)
        self.assertEqual(values.build.input_metadata_revision, 2)
        self.assertEqual(
            json.loads(values.build.request_identity_json)["compile"]["flags"],
            self.manifest["request_identity"]["compile"]["flags"],
        )
        self.assertEqual(values.build.plugin_validation.state, EvidenceState.RECORDED)
        self.assertEqual(
            (self.manifest_path.read_bytes(), self.provenance_path.read_bytes()), before
        )

    def test_raw_source_and_normalized_compile_input_have_separate_hashes(self) -> None:
        raw = '//@version=6\r\nstrategy("測試")\r\n'.encode("utf-8")
        self.source_path.write_bytes(raw)
        values = self.prepare(self.receipt())
        self.assertEqual(values.source.file.byte_count, len(raw))
        self.assertEqual(values.source.file.sha256, digest(raw))
        self.assertEqual(
            values.source.compile_input_sha256,
            digest(self.source_path.read_text(encoding="utf-8").encode("utf-8")),
        )
        self.assertNotEqual(
            values.source.file.sha256, values.source.compile_input_sha256
        )

    def test_pine_source_must_be_utf8(self) -> None:
        self.source_path.write_bytes(b"//@version=6\n\xff")
        with self.assertRaises(NativeIdentityError):
            prepare_producer_values(self.study)

    def test_changed_source_requires_matching_new_build_identity(self) -> None:
        artifact = self.receipt()
        first = self.prepare(artifact)
        old_input = self.source_path.read_text(encoding="utf-8")
        self.source_path.write_text(old_input + "// changed\n", encoding="utf-8")
        with self.assertRaises(NativeIdentityError):
            artifact.producer_values(self.study, compiled_source=old_input)
        with self.assertRaises(NativeIdentityError):
            self.prepare(artifact)
        second = self.prepare(self.receipt())
        self.assertNotEqual(first.source.file.sha256, second.source.file.sha256)
        self.assertNotEqual(first.build.request_key, second.build.request_key)
        self.assertNotEqual(first.build.artifact_key, second.build.artifact_key)
        self.assertNotEqual(first.plugin.sha256, second.plugin.sha256)

    def test_changed_plugin_or_generated_bytes_refuse_stale_receipts(self) -> None:
        for member in ("plugin_path", "generated_cpp_path"):
            with self.subTest(member=member):
                artifact = self.receipt()
                path = getattr(artifact, member)
                original = path.read_bytes()
                path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
                with self.assertRaises(NativeIdentityError):
                    self.prepare(artifact)

    def test_request_digest_is_checked_beyond_record_equality(self) -> None:
        artifact = self.receipt()
        self.manifest["request_identity"]["compiler"]["target"] = "different-target"
        self.write_records()
        with self.assertRaises(NativeIdentityError):
            self.prepare(artifact)

    def test_manifest_and_provenance_must_corroborate_types_and_values(self) -> None:
        artifact = self.receipt()
        self.provenance = {**self.provenance, "schema_version": True}
        self.write_records()
        with self.assertRaises(NativeIdentityError):
            self.prepare(artifact)

    def test_missing_build_record_is_not_a_qualified_identity(self) -> None:
        artifact = replace(
            self.receipt(), provenance_path=self.root / "absent-provenance.json"
        )
        with self.assertRaises(NativeIdentityError):
            self.prepare(artifact)

    def test_source_without_build_has_explicit_missing_chain_evidence(self) -> None:
        values = prepare_producer_values(self.study)
        self.assertIsNone(values.source.compile_input_sha256)
        self.assertIsNone(values.plugin)
        self.assertIsNone(values.build)
        states = {entry.role: entry.state for entry in values.unavailable}
        self.assertEqual(
            states[ProducerRole.SOURCE_BUILD_RELATION], EvidenceState.MISSING
        )

    def test_deferred_validation_stays_pending(self) -> None:
        values = self.prepare(self.receipt(validation="deferred"))
        evidence = values.build.plugin_validation
        self.assertEqual(evidence.state, EvidenceState.DEFERRED)
        self.assertIsNone(evidence.abi_version)
        self.assertEqual(evidence.required_symbols, ())

    def test_absent_validation_stays_missing(self) -> None:
        values = self.prepare(self.receipt(validation="missing"))
        self.assertEqual(values.build.plugin_validation.state, EvidenceState.MISSING)

    def test_conflicting_or_coerced_validation_stays_unqualified(self) -> None:
        cases = (
            {"abi_version": 2.0, "required_symbols": SYMBOLS},
            {"abi_version": True, "required_symbols": SYMBOLS},
            {"abi_version": 2, "required_symbols": ["pf_abi_version"]},
            {"status": "deferred", "abi_version": 2, "required_symbols": SYMBOLS},
        )
        for validation in cases:
            with self.subTest(validation=validation):
                artifact = self.receipt()
                self.manifest["plugin_validation"] = validation
                self.provenance["plugin_validation"] = validation
                self.write_records()
                evidence = self.prepare(artifact).build.plugin_validation
                self.assertEqual(evidence.state, EvidenceState.UNQUALIFIED)
                self.assertIsNone(evidence.abi_version)

    def test_checked_cpp_cannot_supply_a_pine_source_build_chain(self) -> None:
        artifact = self.receipt(identity_extra={"source_kind": "checked_cpp"})
        with self.assertRaises(NativeIdentityError):
            self.prepare(artifact)

    def test_pine_path_cannot_be_used_as_the_native_plugin_argument(self) -> None:
        artifact = self.receipt()
        with self.assertRaises(NativeIdentityError):
            artifact.producer_values(
                self.study,
                compiled_source=self.source_path.read_text(encoding="utf-8"),
                native_command=["native", "run", "--strategy", str(self.source_path)],
            )

    def test_native_argument_values_can_contain_an_option_name(self) -> None:
        artifact = self.receipt()
        command = [
            "native",
            "run",
            "--strategy",
            str(artifact.plugin_path),
            "--fixed-input",
            "Label",
            "--strategy",
        ]
        values = artifact.producer_values(
            self.study,
            compiled_source=self.source_path.read_text(encoding="utf-8"),
            native_command=command,
        )
        self.assertEqual(values.native_command, tuple(command))

    def test_local_dataset_bytes_do_not_supply_counts_or_authority(self) -> None:
        artifact = self.receipt()
        first = self.prepare(artifact)
        self.data_path.write_bytes(b"")
        second = self.prepare(artifact)
        self.assertNotEqual(
            first.datasets[0].file.sha256, second.datasets[0].file.sha256
        )
        self.assertEqual(second.datasets[0].file.byte_count, 0)
        states = {entry.role: entry.state for entry in second.unavailable}
        self.assertNotIn(ProducerRole.PRODUCER_CONTRACT, states)
        for role in (
            ProducerRole.RESOURCE_ADMISSION_POLICY,
            ProducerRole.RETAINED_AUTHORITY,
            ProducerRole.EXECUTED_ARTIFACT_ADMISSION,
        ):
            self.assertEqual(states[role], EvidenceState.UNBOUND)
        for role in (
            ProducerRole.PRIMARY_EVALUATION_COUNT,
            ProducerRole.TOTAL_PHYSICAL_RECORDS,
            ProducerRole.TRUSTED_DATASET_CSV_RECEIPTS,
            ProducerRole.FOREIGN_FEED_COUNTS,
            ProducerRole.RETENTION,
        ):
            self.assertEqual(states[role], EvidenceState.UNSUPPORTED)

    def test_dataset_associations_preserve_ids_and_declaration_order(self) -> None:
        second_path = self.root / "second.csv"
        second_path.write_bytes(self.data_path.read_bytes())
        other = replace(self.study.datasets[0], id="other", ohlcv=second_path)
        study = replace(
            self.study,
            datasets=(*self.study.datasets, other),
            strategy=replace(self.study.strategy, dataset_ids=("other", "bars")),
        )
        values = prepare_producer_values(study)
        self.assertEqual(
            [
                (item.id, item.declared_ordinal, item.strategy_association_ordinals)
                for item in values.datasets
            ],
            [("bars", 0, (1,)), ("other", 1, (0,))],
        )
        self.assertEqual(values.datasets[0].input_tf, "1")
        self.assertEqual(values.datasets[0].script_tf, "5")
        # Equal hashes do not establish selected primary/foreign membership.
        self.assertEqual(values.datasets[0].file.sha256, values.datasets[1].file.sha256)
        states = {entry.role: entry.state for entry in values.unavailable}
        self.assertEqual(
            states[ProducerRole.SELECTED_PAIR_ORDINALS], EvidenceState.UNSUPPORTED
        )

    def test_binary_dataset_bytes_keep_their_own_role(self) -> None:
        binary_path = self.root / "bars.pfhbin"
        binary_path.write_bytes(self.data_path.read_bytes())
        study = replace(
            self.study,
            datasets=(
                replace(self.study.datasets[0], ohlcv=None, ohlcv_bin=binary_path),
            ),
        )
        values = prepare_producer_values(study)
        self.assertEqual(values.datasets[0].file.role, FileRole.DATASET_BINARY)
        self.assertEqual(
            values.datasets[0].file.byte_count, len(binary_path.read_bytes())
        )

    def test_seed_trials_workers_and_batch_size_remain_distinct_inputs(self) -> None:
        artifact = self.receipt()
        changed = replace(
            self.study,
            sampler=replace(self.study.sampler, seed=41, trials=11),
            execution=replace(self.study.execution, workers=4, batch_size=7),
        )
        values = artifact.producer_values(
            changed, compiled_source=self.source_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            (
                values.study.sampler.seed,
                values.study.sampler.trials,
                values.study.execution.workers,
                values.study.execution.batch_size,
            ),
            (41, 11, 4, 7),
        )
        self.assertEqual(values.build.artifact_key, artifact.artifact_key)

    def test_control_values_do_not_coerce_bool_or_float_to_integer(self) -> None:
        studies = (
            replace(self.study, sampler=replace(self.study.sampler, seed=True)),
            replace(self.study, sampler=replace(self.study.sampler, trials=1.0)),
            replace(self.study, execution=replace(self.study.execution, workers=0)),
            replace(
                self.study, execution=replace(self.study.execution, batch_size=True)
            ),
        )
        for study in studies:
            with self.subTest(study=study):
                with self.assertRaises(NativeIdentityError):
                    prepare_producer_values(study)

    def test_precompiled_observation_does_not_acquire_source_build_evidence(
        self,
    ) -> None:
        artifact = self.receipt()
        study = replace(
            self.study,
            strategy=replace(
                self.study.strategy, source=None, artifact=artifact.plugin_path
            ),
        )
        values = prepare_producer_values(study)
        self.assertEqual(values.plugin.role, FileRole.PLUGIN)
        self.assertIsNone(values.source)
        self.assertIsNone(values.build)
        states = {entry.role: entry.state for entry in values.unavailable}
        self.assertEqual(
            states[ProducerRole.SOURCE_BUILD_RELATION], EvidenceState.UNSUPPORTED
        )
        with self.assertRaises(NativeIdentityError):
            prepare_producer_values(study, wrapper_launched=True)
        with self.assertRaises(NativeIdentityError):
            prepare_producer_values(study, artifact)

    def test_prepare_run_optional_sink_preserves_argv_and_artifact_json(self) -> None:
        artifact = self.receipt()
        strategy = {
            key: value
            for key, value in self.document["strategies"][0].items()
            if key != "source"
        }
        strategy["artifact"] = str(artifact.plugin_path)
        self.spec_path.write_bytes(
            document_bytes({**self.document, "strategies": [strategy]})
        )
        # prepare_run checks the executable bit but must never run this file.
        native = self.root / "native"
        native.write_bytes(b"not native executable code")
        native.chmod(0o700)
        override = self.root / "selected-symbol-feeds.json"
        before = (self.manifest_path.read_bytes(), self.provenance_path.read_bytes())
        plain = prepare_run(self.spec_path, native=native, symbol_feeds=override)
        seen = []
        exported = prepare_run(
            self.spec_path,
            native=native,
            symbol_feeds=override,
            producer_values_sink=seen.append,
        )
        self.assertEqual(exported, plain)
        self.assertEqual(len(seen), 1)
        values = seen[0]
        self.assertEqual(values.native_command, tuple(exported[0]))
        self.assertEqual(values.plugin.path, artifact.plugin_path)
        self.assertEqual(
            values.native_command[values.native_command.index("--symbol-feeds") + 1],
            str(override.resolve()),
        )
        self.assertIsNone(values.source)
        self.assertIsNone(values.build)
        self.assertEqual(
            (self.manifest_path.read_bytes(), self.provenance_path.read_bytes()), before
        )

    def identity_record(self, *, validation: str = "recorded") -> dict:
        return producer_identity_record(
            self.prepare(self.receipt(validation=validation))
        )

    def decode_record(self, record: dict) -> dict:
        return decode_producer_identity(document_bytes(record) + b"\n")

    def test_selected_identity_canonical_utf8_and_decoded_data(self) -> None:
        self.document["strategies"][0]["id"] = "策略é"
        self.spec_path.write_bytes(document_bytes(self.document))
        self.study = load_study_spec(self.spec_path, require_files=True)
        artifact = self.receipt(
            identity_extra={"compiler_environment": {"CPATH": "private-build-env"}}
        )
        values = self.prepare(artifact)
        record = producer_identity_record(values)
        raw = serialize_producer_identity(values)
        self.assertEqual(
            record["producer_identity"],
            {
                "name": "pineforge-hpo/native-card-producer",
                "version": 1,
                "type": "pineforge.hpo.native-producer-evidence/1",
                "issuer": "pineforge-core",
            },
        )
        self.assertIs(type(record["producer_identity"]["version"]), int)
        self.assertEqual(raw, document_bytes(record) + b"\n")
        self.assertIn("策略é".encode("utf-8"), raw)
        self.assertEqual(decode_producer_identity(raw), record)
        self.assertIs(type(decode_producer_identity(raw)), dict)
        self.assertEqual(
            record["plugin"]["sha256"], digest(self.plugin_path.read_bytes())
        )
        self.assertEqual(record["build"]["request_key"], artifact.request_key)
        self.assertEqual(
            record["build"]["manifest"]["sha256"],
            digest(self.manifest_path.read_bytes()),
        )
        self.assertEqual(
            set(record["build"]),
            {
                "artifact_key",
                "request_key",
                "manifest_schema_version",
                "input_metadata_revision",
                "generated_cpp",
                "manifest",
                "provenance",
                "plugin_validation",
            },
        )
        self.assertNotIn(b"private-build-env", raw)
        self.assertNotIn(b"compiler_environment", raw)
        self.assertNotIn(b"request_identity_json", raw)

    def test_encoded_source_change_drives_build_and_plugin_values(self) -> None:
        first = serialize_producer_identity(self.prepare(self.receipt()))
        self.source_path.write_text(
            self.source_path.read_text(encoding="utf-8") + "// new compile input\n",
            encoding="utf-8",
        )
        second = serialize_producer_identity(self.prepare(self.receipt()))
        self.assertNotEqual(first, second)
        a, b = decode_producer_identity(first), decode_producer_identity(second)
        self.assertEqual(a["producer_identity"], b["producer_identity"])
        for member in ("source", "plugin", "build"):
            self.assertNotEqual(a[member], b[member])

    def test_encoded_dataset_and_controls_follow_actual_inputs(self) -> None:
        artifact = self.receipt()
        first = serialize_producer_identity(self.prepare(artifact))
        self.data_path.write_bytes(b"time,open,high,low,close,volume\n")
        changed = replace(
            self.study,
            sampler=replace(self.study.sampler, seed=43, trials=13),
            execution=replace(self.study.execution, workers=5, batch_size=None),
        )
        values = artifact.producer_values(
            changed, compiled_source=self.source_path.read_text(encoding="utf-8")
        )
        second = serialize_producer_identity(values)
        self.assertNotEqual(first, second)
        record = decode_producer_identity(second)
        self.assertEqual(
            [
                record["study"][key]
                for key in ("seed", "trials", "workers", "batch_size")
            ],
            [43, 13, 5, None],
        )
        self.assertEqual(
            record["datasets"][0]["file"]["byte_count"],
            len(self.data_path.read_bytes()),
        )
        self.assertEqual(
            record["datasets"][0]["file"]["sha256"], digest(self.data_path.read_bytes())
        )

    def test_encoded_plugin_observation_changes_with_corroborated_bytes(self) -> None:
        artifact = self.receipt()
        first = serialize_producer_identity(self.prepare(artifact))
        self.plugin_path.write_bytes(
            b"different opaque observation, not executable proof"
        )
        for document in (self.manifest, self.provenance):
            document["plugin_sha256"] = digest(self.plugin_path.read_bytes())
        self.write_records()
        second = serialize_producer_identity(self.prepare(artifact))
        self.assertNotEqual(first, second)
        self.assertEqual(
            decode_producer_identity(second)["plugin"]["sha256"],
            digest(self.plugin_path.read_bytes()),
        )

    def test_decode_validates_data_without_reading_referenced_files(self) -> None:
        values = self.prepare(self.receipt())
        record = producer_identity_record(values)
        raw = serialize_producer_identity(values)
        self.source_path.write_bytes(b"\xff")
        self.plugin_path.write_bytes(b"changed after observation")
        self.manifest_path.write_bytes(b"invalid JSON after observation")
        self.data_path.write_bytes(b"")
        self.assertEqual(decode_producer_identity(raw), record)
        dispositions = {row["role"]: row["state"] for row in record["unavailable"]}
        self.assertEqual(dispositions["RETAINED_AUTHORITY"], "UNBOUND")
        self.assertEqual(dispositions["RESOURCE_ADMISSION_POLICY"], "UNBOUND")
        self.assertEqual(dispositions["EXECUTED_ARTIFACT_ADMISSION"], "UNBOUND")
        self.assertNotIn("PRODUCER_CONTRACT", dispositions)

    def test_decoder_identity_dispatch_rejects_version_coercion_and_foreign_types(
        self,
    ) -> None:
        original = self.identity_record()
        for member, invalid in (
            ("version", True),
            ("version", 1.0),
            ("version", "1"),
            ("version", 2),
            ("name", "pineforge.compute.fprime-b.foreign-adapter/1"),
            ("type", "foreign-card"),
            ("issuer", "unselected-issuer"),
        ):
            with self.subTest(member=member, invalid=invalid):
                record = copy.deepcopy(original)
                record["producer_identity"][member] = invalid
                with self.assertRaises(NativeIdentityError):
                    self.decode_record(record)

    def test_decoder_rejects_unknown_and_missing_keys_at_every_object_level(
        self,
    ) -> None:
        original = self.identity_record()
        paths = (
            (),
            ("producer_identity",),
            ("study",),
            ("source",),
            ("source", "file"),
            ("plugin",),
            ("build",),
            ("build", "generated_cpp"),
            ("build", "manifest"),
            ("build", "provenance"),
            ("build", "plugin_validation"),
            ("datasets", 0),
            ("datasets", 0, "file"),
            ("unavailable", 0),
        )
        for path in paths:
            with self.subTest(path=path, change="unknown"):
                record = copy.deepcopy(original)
                target = record
                for key in path:
                    target = target[key]
                target["unknown_member"] = 0
                with self.assertRaises(NativeIdentityError):
                    self.decode_record(record)
            target = original
            for key in path:
                target = target[key]
            for missing in target:
                with self.subTest(path=path, missing=missing):
                    record = copy.deepcopy(original)
                    altered = record
                    for key in path:
                        altered = altered[key]
                    altered.pop(missing)
                    with self.assertRaises(NativeIdentityError):
                        self.decode_record(record)

    def test_decoder_rejects_duplicate_keys_at_root_and_nested_levels(self) -> None:
        record = self.identity_record()
        raw = document_bytes(record) + b"\n"
        identity = b'"producer_identity":' + document_bytes(record["producer_identity"])
        nested = b'"workers":' + str(record["study"]["workers"]).encode("ascii")
        for member in (identity, nested):
            with self.subTest(member=member):
                duplicated = raw.replace(member, member + b"," + member, 1)
                self.assertNotEqual(duplicated, raw)
                with self.assertRaises(NativeIdentityError):
                    decode_producer_identity(duplicated)

    def test_decoder_rejects_nonfinite_numbers_invalid_utf8_and_surrogates(
        self,
    ) -> None:
        raw = document_bytes(self.identity_record()) + b"\n"
        seed = b'"seed":17'
        for number in (b"NaN", b"Infinity", b"-Infinity", b"1e10000", b"17.0"):
            with self.subTest(number=number):
                invalid = raw.replace(seed, b'"seed":' + number, 1)
                self.assertNotEqual(invalid, raw)
                with self.assertRaises(NativeIdentityError):
                    decode_producer_identity(invalid)
        for invalid in (
            b"\xff",
            raw.replace(b'"strategy_id":"strategy-a"', b'"strategy_id":"\\ud800"', 1),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(NativeIdentityError):
                    decode_producer_identity(invalid)

    def test_decoder_enforces_canonical_encoding_and_single_lf(self) -> None:
        record = self.identity_record()
        canonical = document_bytes(record) + b"\n"
        alternatives = (
            canonical[:-1],
            canonical + b"\n",
            canonical[:-1] + b"\r\n",
            b" " + canonical,
            json.dumps(record, indent=2).encode("utf-8") + b"\n",
        )
        for raw in alternatives:
            with self.subTest(raw=raw):
                with self.assertRaises(NativeIdentityError):
                    decode_producer_identity(raw)

    def test_decoder_checks_integer_domains_without_inventing_policy_caps(self) -> None:
        original = self.identity_record()
        for member, invalid in (
            ("schema_version", True),
            ("schema_version", 2),
            ("seed", -1),
            ("seed", True),
            ("trials", 0),
            ("trials", 1.0),
            ("workers", False),
            ("batch_size", 0),
        ):
            with self.subTest(member=member, invalid=invalid):
                record = copy.deepcopy(original)
                record["study"][member] = invalid
                with self.assertRaises(NativeIdentityError):
                    self.decode_record(record)
        record = copy.deepcopy(original)
        record["study"]["seed"] = 1 << 200
        record["study"]["trials"] = 1 << 200
        record["study"]["workers"] = 1 << 200
        self.assertEqual(self.decode_record(record)["study"], record["study"])

    def test_decoder_checks_nested_file_types_roles_and_byte_domains(self) -> None:
        original = self.identity_record()
        for member, invalid in (
            ("role", "PLUGIN"),
            ("path", None),
            ("sha256", "A" * 64),
            ("sha256", "0" * 63),
            ("byte_count", True),
            ("byte_count", 1.0),
            ("byte_count", -1),
        ):
            with self.subTest(member=member, invalid=invalid):
                record = copy.deepcopy(original)
                record["source"]["file"][member] = invalid
                with self.assertRaises(NativeIdentityError):
                    self.decode_record(record)
        for member, role in (
            ("generated_cpp", "BUILD_MANIFEST"),
            ("manifest", "GENERATED_CPP"),
            ("provenance", "PLUGIN"),
        ):
            with self.subTest(member=member):
                record = copy.deepcopy(original)
                record["build"][member]["role"] = role
                with self.assertRaises(NativeIdentityError):
                    self.decode_record(record)
        record = copy.deepcopy(original)
        record["datasets"][0]["file"]["role"] = "PINE_SOURCE"
        with self.assertRaises(NativeIdentityError):
            self.decode_record(record)

    def test_decoder_checks_dataset_association_and_declaration_order(self) -> None:
        original = self.identity_record()
        for member, invalid in (
            ("id", "unassociated"),
            ("declared_ordinal", True),
            ("declared_ordinal", 1),
            ("strategy_association_ordinals", [True]),
            ("strategy_association_ordinals", []),
            ("input_tf", None),
            ("script_tf", 5),
            ("chart_timezone", []),
        ):
            with self.subTest(member=member, invalid=invalid):
                record = copy.deepcopy(original)
                record["datasets"][0][member] = invalid
                with self.assertRaises(NativeIdentityError):
                    self.decode_record(record)

    def test_encoded_validation_states_preserve_recorded_and_pending_domains(
        self,
    ) -> None:
        for validation, state in (
            ("recorded", "RECORDED"),
            ("deferred", "DEFERRED"),
            ("missing", "MISSING"),
        ):
            with self.subTest(validation=validation):
                record = self.decode_record(self.identity_record(validation=validation))
                evidence = record["build"]["plugin_validation"]
                self.assertEqual(evidence["state"], state)
                if state == "RECORDED":
                    self.assertEqual(evidence["abi_version"], 2)
                    self.assertEqual(evidence["required_symbols"], SYMBOLS)
                else:
                    self.assertIsNone(evidence["abi_version"])
                    self.assertEqual(evidence["required_symbols"], [])
        artifact = self.receipt()
        for document in (self.manifest, self.provenance):
            document["plugin_validation"] = {"abi_version": True}
        self.write_records()
        record = self.decode_record(producer_identity_record(self.prepare(artifact)))
        self.assertEqual(record["build"]["plugin_validation"]["state"], "UNQUALIFIED")

    def test_decoder_refuses_contradictory_plugin_validation_tuples(self) -> None:
        original = self.identity_record()
        cases = (
            {"state": "RECORDED", "abi_version": None, "required_symbols": SYMBOLS},
            {"state": "RECORDED", "abi_version": True, "required_symbols": SYMBOLS},
            {"state": "RECORDED", "abi_version": 2, "required_symbols": []},
            {"state": "DEFERRED", "abi_version": 2, "required_symbols": []},
            {"state": "MISSING", "abi_version": None, "required_symbols": SYMBOLS},
            {"state": "UNQUALIFIED", "abi_version": 2, "required_symbols": []},
            {"state": "UNBOUND", "abi_version": None, "required_symbols": []},
        )
        for values in cases:
            with self.subTest(values=values):
                record = copy.deepcopy(original)
                record["build"]["plugin_validation"].update(values)
                with self.assertRaises(NativeIdentityError):
                    self.decode_record(record)

    def test_decoder_refuses_missing_or_promoted_unsupported_roles(self) -> None:
        original = self.identity_record()
        changes = (
            {"role": "PRODUCER_CONTRACT"},
            {"state": "RECORDED"},
            {"state": None},
            {"role": "guessed_policy"},
            {"reason": ""},
            {"reason": 0},
        )
        for change in changes:
            with self.subTest(change=change):
                record = copy.deepcopy(original)
                record["unavailable"][0].update(change)
                with self.assertRaises(NativeIdentityError):
                    self.decode_record(record)
        for rows in (
            original["unavailable"][1:],
            [*original["unavailable"], original["unavailable"][0]],
            list(reversed(original["unavailable"])),
        ):
            with self.subTest(rows=rows):
                record = {**original, "unavailable": rows}
                with self.assertRaises(NativeIdentityError):
                    self.decode_record(record)

    def test_encoder_and_decoder_preserve_unbuilt_and_precompiled_variants(
        self,
    ) -> None:
        unbuilt = prepare_producer_values(self.study)
        record = decode_producer_identity(serialize_producer_identity(unbuilt))
        self.assertIsNotNone(record["source"])
        self.assertIsNone(record["plugin"])
        self.assertIsNone(record["build"])
        artifact = self.receipt()
        precompiled = replace(
            self.study,
            strategy=replace(
                self.study.strategy, source=None, artifact=artifact.plugin_path
            ),
        )
        record = self.decode_record(
            producer_identity_record(prepare_producer_values(precompiled))
        )
        self.assertIsNone(record["source"])
        self.assertIsNotNone(record["plugin"])
        self.assertIsNone(record["build"])
        invalid = self.identity_record()
        invalid["source"] = None
        with self.assertRaises(NativeIdentityError):
            self.decode_record(invalid)
        invalid = self.identity_record()
        invalid["source"]["compile_input_sha256"] = None
        with self.assertRaises(NativeIdentityError):
            self.decode_record(invalid)

    def test_prepare_run_identity_and_values_sinks_share_one_observation(self) -> None:
        artifact = self.receipt()
        strategy = {
            key: value
            for key, value in self.document["strategies"][0].items()
            if key != "source"
        }
        strategy["artifact"] = str(artifact.plugin_path)
        self.spec_path.write_bytes(
            document_bytes({**self.document, "strategies": [strategy]})
        )
        native = self.root / "native"
        native.write_bytes(b"not executable proof")
        native.chmod(0o700)
        expected = prepare_run(self.spec_path, native=native)
        before = (self.manifest_path.read_bytes(), self.provenance_path.read_bytes())
        data_hash = digest(self.data_path.read_bytes())
        seen_values, seen_bytes = [], []

        def values_sink(values):
            seen_values.append(values)
            self.data_path.write_bytes(b"changed after values callback")

        actual = prepare_run(
            self.spec_path,
            native=native,
            producer_values_sink=values_sink,
            producer_identity_sink=seen_bytes.append,
        )
        self.assertEqual(actual, expected)
        self.assertEqual(len(seen_values), 1)
        self.assertEqual(len(seen_bytes), 1)
        record = decode_producer_identity(seen_bytes[0])
        self.assertEqual(record["datasets"][0]["file"]["sha256"], data_hash)
        self.assertEqual(seen_values[0].datasets[0].file.sha256, data_hash)
        self.assertNotEqual(digest(self.data_path.read_bytes()), data_hash)
        self.assertEqual(record["native_command"], actual[0])
        self.assertEqual(
            (self.manifest_path.read_bytes(), self.provenance_path.read_bytes()), before
        )
        identity_only = []
        self.assertEqual(
            prepare_run(
                self.spec_path,
                native=native,
                producer_identity_sink=identity_only.append,
            ),
            expected,
        )
        self.assertEqual(len(identity_only), 1)
        self.assertEqual(
            decode_producer_identity(identity_only[0])["datasets"][0]["file"]["sha256"],
            digest(self.data_path.read_bytes()),
        )


if __name__ == "__main__":
    unittest.main()
