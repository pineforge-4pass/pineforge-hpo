"""Checked-C++ custody tests; real engine/compiler rows require explicit opt-in.

PFH_CHECKED_CPP_INTEGRATION=1 enables the real integration class. Normal Python CI
does not build an engine archive, so those rows skip there and prove nothing there.
With opt-in, missing codegen, compiler, engine, Eigen or native inputs fail the row.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pineforge_hpo import ArtifactBuildError, ArtifactBuilder, prepare_run
from pineforge_hpo.artifact import _KeyLock
from pineforge_hpo.error import failure_document
from pineforge_hpo.transpile import (
    TranspileDiagnostic,
    codegen_identity,
    transpile_source,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (
    '//@version=6\nstrategy("Checked C++ custody", initial_capital=10000)\n'
    'length = input.int(7, "Length", minval=1)\n'
    "plot(ta.sma(close, length))\n"
)


def checked_emission(source: str = SOURCE) -> tuple[bytes, dict]:
    """Use actual codegen and its metadata; this helper never stubs emission."""

    result = transpile_source(source, filename="checked.pine")
    return result.require_success().encode("utf-8"), {
        "inputs": result.inputs,
        "input_kind_schema": result.input_kind_schema,
        "strategy_params": result.strategy_params,
        "request_feed_inventory": result.request_feed_inventory,
        "diagnostics": result.diagnostics,
        "filename": "checked.pine",
    }


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


@contextmanager
def observe_compiler():
    """Record commands while invoking the real subprocess.run unchanged."""

    run = subprocess.run
    commands = []

    def observe(command, **options):
        completed = run(command, **options)
        commands.append((tuple(map(str, command)), completed.returncode))
        if "-shared" in command:
            print(
                json.dumps(
                    {
                        "compiler_command": list(map(str, command)),
                        "exit_code": completed.returncode,
                    }
                ),
                file=sys.stderr,
            )
        return completed

    with patch("pineforge_hpo.artifact.subprocess.run", side_effect=observe):
        yield commands


def compile_commands(commands):
    return [(argv, code) for argv, code in commands if "-shared" in argv]


class CheckedCppRefusalTests(unittest.TestCase):
    """No-compile assertions only; these rows claim no compiler integration."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        (self.cache / "existing-output").write_bytes(b"preserve public output")
        self.builder = ArtifactBuilder(
            engine_root=self.root / "unused-engine", cache_dir=self.cache
        )
        self.cpp, self.metadata = checked_emission()

    def assert_no_compiler(self, cpp, digest, code, reason=None, **overrides):
        before = snapshot(self.cache)
        metadata = {**self.metadata, **overrides}
        with (
            patch.object(self.builder, "_resolve_compiler_identity") as resolve,
            patch("pineforge_hpo.artifact.subprocess.run") as run,
            patch("pineforge_hpo.artifact.transpile_source") as transpile,
        ):
            with self.assertRaises(ArtifactBuildError) as caught:
                self.builder.build_cpp(cpp, expected_sha256=digest, **metadata)
            resolve.assert_not_called()
            run.assert_not_called()
            transpile.assert_not_called()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(
            caught.exception.failure_args, {} if reason is None else {"reason": reason}
        )
        self.assertEqual(snapshot(self.cache), before)
        return caught.exception

    def test_wrong_digest_refuses_before_compiler_and_preserves_output(self):
        actual = hashlib.sha256(self.cpp).hexdigest()
        wrong = ("1" if actual[0] == "0" else "0") + actual[1:]
        error = self.assert_no_compiler(
            self.cpp, wrong, "hpo_generated_cpp_hash_mismatch"
        )
        self.assertEqual(error.actual_sha256, actual)
        self.assertEqual(error.expected_sha256, wrong)
        self.assertIn(actual, str(error))
        self.assertIn(wrong, str(error))
        self.assertEqual(
            failure_document(error, 1)["failure"],
            {
                "origin": "hpo",
                "code": "hpo_generated_cpp_hash_mismatch",
                "args": {},
                "exit_code": 1,
            },
        )

    def test_invalid_digest_and_bytes_refuse_honestly(self):
        for digest in (None, "", "a" * 63, "g" * 64, "a" * 65, " " + "a" * 64):
            with self.subTest(digest=digest):
                self.assert_no_compiler(
                    self.cpp,
                    digest,
                    "hpo_generated_cpp_invalid",
                    "expected_sha256",
                )
        for cpp, reason in (("text", "bytes"), (b"\xff\r\n", "utf8")):
            with self.subTest(reason=reason):
                digest = hashlib.sha256(
                    cpp if isinstance(cpp, bytes) else b""
                ).hexdigest()
                self.assert_no_compiler(
                    cpp, digest, "hpo_generated_cpp_invalid", reason
                )

    def test_missing_or_contradictory_checked_metadata_is_not_invented(self):
        for overrides in (
            {"inputs": [7]},
            {"input_kind_schema": True},
            {"input_kind_schema": 2},
            {"strategy_params": []},
            {"request_feed_inventory": None},
            {
                "request_feed_inventory": {
                    **self.metadata["request_feed_inventory"],
                    "source_sha256": "0" * 64,
                }
            },
        ):
            with self.subTest(overrides=overrides):
                self.assert_no_compiler(
                    self.cpp,
                    hashlib.sha256(self.cpp).hexdigest(),
                    "hpo_generated_cpp_invalid",
                    "metadata",
                    **overrides,
                )

    def test_checked_diagnostics_must_be_valid_and_error_free(self):
        digest = hashlib.sha256(self.cpp).hexdigest()
        self.assert_no_compiler(
            self.cpp,
            digest,
            "hpo_generated_cpp_invalid",
            "diagnostics",
            diagnostics=[{}],
        )
        self.assert_no_compiler(
            self.cpp,
            digest,
            "hpo_transpile_failed",
            diagnostics=[
                TranspileDiagnostic(
                    severity="error",
                    phase="TRANSPILE",
                    message="checked emission failed",
                )
            ],
        )

    def test_wrapper_codegen_refuses_before_build_or_precompiled_admission(self):
        # Patching distribution version tests policy ordering only. It does not
        # claim that this loaded package came from any historical Git revision.
        builder = ArtifactBuilder(
            engine_root=self.root / "unused-engine",
            cache_dir=self.cache,
            wrapper_launched=True,
        )
        for version, reason in (
            ("1.9.99", "below_floor"),
            ("unknown", "version_unknown"),
            ("1.10.0rc1", "prerelease"),
            ("1.10", "version_invalid"),
        ):
            for route in ("pine", "cpp", "prepare"):
                with (
                    self.subTest(version=version, route=route),
                    patch(
                        "pineforge_hpo.transpile.metadata.version", return_value=version
                    ),
                    patch("pineforge_hpo.artifact.subprocess.run") as run,
                    patch("pineforge_hpo.cli._precompiled_artifact") as precompiled,
                    patch("pineforge_hpo.artifact.transpile_source") as transpile,
                ):
                    with self.assertRaises(ArtifactBuildError) as caught:
                        if route == "pine":
                            builder.build(SOURCE)
                        elif route == "cpp":
                            builder.build_cpp(
                                self.cpp,
                                expected_sha256=hashlib.sha256(self.cpp).hexdigest(),
                                **self.metadata,
                            )
                        else:
                            prepare_run(
                                self.root / "unread-study.json", wrapper_launched=True
                            )
                    self.assertEqual(
                        caught.exception.code, "hpo_wrapper_codegen_unsupported"
                    )
                    self.assertEqual(
                        caught.exception.failure_args,
                        {
                            "policy": "installed_version_and_source_hash",
                            "reason": reason,
                        },
                    )
                    run.assert_not_called()
                    precompiled.assert_not_called()
                    transpile.assert_not_called()


@unittest.skipUnless(
    os.environ.get("PFH_CHECKED_CPP_INTEGRATION") == "1",
    "real checked-C++ integration requires PFH_CHECKED_CPP_INTEGRATION=1",
)
class CheckedCppIntegrationTests(unittest.TestCase):
    """Actual codegen, engine archive, native runner, compiler and plugin loader."""

    def setUp(self) -> None:
        from pineforge_hpo.transpile import wrapper_codegen_refusal

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.engine = Path(
            os.environ.get("PINEFORGE_ENGINE_ROOT", ROOT / "external/pineforge-engine")
        ).resolve()
        self.native = Path(
            os.environ.get(
                "PINEFORGE_HPO_NATIVE", ROOT / "build/bin/pineforge-hpo-native"
            )
        ).resolve()
        self.cache = self.root / "cache"
        self.source = self.root / "checked.pine"
        self.source.write_text(SOURCE, encoding="utf-8")
        self.cpp, self.metadata = checked_emission()
        self.identity = codegen_identity()
        self.assertIsNone(wrapper_codegen_refusal(self.identity))
        self.study = self.root / "study.json"
        self.data = self.root / "bars.csv"
        self.data.write_text(
            "timestamp,open,high,low,close,volume\n1700000000000,1,2,1,2,10\n",
            encoding="utf-8",
        )

    def builder(self, *, wrapper_launched=False):
        return ArtifactBuilder(
            engine_root=self.engine,
            cache_dir=self.cache,
            wrapper_launched=wrapper_launched,
        )

    def write_study(self, *, artifact=None):
        strategy = {
            "id": "checked",
            "datasets": ["bars"],
            "search_space": {
                "Length": {"kind": "integer", "low": 1, "high": 2, "step": 1}
            },
        }
        strategy["source" if artifact is None else "artifact"] = str(
            self.source if artifact is None else artifact.plugin_path
        )
        self.study.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "single_strategy",
                    "strategies": [strategy],
                    "datasets": [
                        {
                            "id": "bars",
                            "ohlcv": str(self.data),
                            "input_tf": "1",
                            "script_tf": "1",
                            "chart_timezone": "UTC",
                        }
                    ],
                    "objective": {
                        "kind": "expression",
                        "expression": "metrics.all.net_profit",
                        "direction": "maximize",
                    },
                    "sampler": {"kind": "grid", "seed": 17, "trials": 2},
                    "execution": {"workers": 1, "isolation": "threads"},
                }
            ),
            encoding="utf-8",
        )

    def assert_custody(self, artifact, cpp, metadata):
        digest = hashlib.sha256(cpp).hexdigest()
        self.assertEqual(artifact.generated_cpp_path.read_bytes(), cpp)
        manifest = json.loads(artifact.manifest_path.read_bytes())
        provenance = json.loads(artifact.provenance_path.read_bytes())
        plugin_hash = hashlib.sha256(artifact.plugin_path.read_bytes()).hexdigest()
        for document in (manifest, provenance):
            self.assertEqual(document["generated_cpp_sha256"], digest)
            self.assertEqual(document["request_identity"]["source_sha256"], digest)
            self.assertEqual(
                document["request_identity"]["codegen"], self.identity.to_dict()
            )
            self.assertEqual(
                document["request_feed_inventory"]["source_sha256"], digest
            )
            self.assertEqual(
                document["request_feed_inventory"]["artifact_sha256"], plugin_hash
            )
            self.assertEqual(document["plugin_sha256"], plugin_hash)
        self.assertEqual(manifest["inputs"], list(metadata["inputs"]))
        self.assertEqual(manifest["strategy_params"], dict(metadata["strategy_params"]))
        self.assertEqual(
            manifest["diagnostics"],
            [item.to_dict() for item in metadata["diagnostics"]],
        )
        library = ctypes.CDLL(str(artifact.plugin_path))
        library.pf_abi_version.argtypes = []
        library.pf_abi_version.restype = ctypes.c_int
        self.assertEqual(
            library.pf_abi_version(),
            manifest["request_identity"]["engine"]["abi_version"],
        )
        return plugin_hash

    def test_checked_bytes_compile_load_and_change_with_input(self):
        builder = self.builder(wrapper_launched=True)
        other_cpp, other_metadata = checked_emission(
            SOURCE.replace("input.int(7", "input.int(9")
        )
        self.assertNotEqual(self.cpp, other_cpp)
        with (
            observe_compiler() as commands,
            patch(
                "pineforge_hpo.artifact.transpile_source",
                side_effect=AssertionError("checked C++ must not re-transpile"),
            ),
        ):
            first = builder.build_cpp(
                self.cpp,
                expected_sha256=hashlib.sha256(self.cpp).hexdigest().upper(),
                **self.metadata,
            )
            second = builder.build_cpp(
                other_cpp,
                expected_sha256=hashlib.sha256(other_cpp).hexdigest(),
                **other_metadata,
            )
        self.assertEqual(len(compile_commands(commands)), 2)
        self.assertTrue(all(code == 0 for _, code in compile_commands(commands)))
        self.assertFalse(first.cache_hit)
        self.assertFalse(second.cache_hit)
        self.assertNotEqual(first.artifact_key, second.artifact_key)
        self.assertNotEqual(
            self.assert_custody(first, self.cpp, self.metadata),
            self.assert_custody(second, other_cpp, other_metadata),
        )

    def test_checked_repeated_request_really_compiles_before_publisher_reuse(self):
        builder = self.builder(wrapper_launched=True)
        digest = hashlib.sha256(self.cpp).hexdigest()
        first = builder.build_cpp(self.cpp, expected_sha256=digest, **self.metadata)
        with observe_compiler() as commands:
            second = builder.build_cpp(
                self.cpp, expected_sha256=digest, **self.metadata
            )
        self.assertEqual(len(compile_commands(commands)), 1)
        self.assertEqual(compile_commands(commands)[0][1], 0)
        self.assertFalse(second.cache_hit)
        self.assertEqual(first.artifact_key, second.artifact_key)
        self.assert_custody(second, self.cpp, self.metadata)

    def test_wrong_digest_leaves_real_seeded_artifact_and_index_unchanged(self):
        builder = self.builder(wrapper_launched=True)
        digest = hashlib.sha256(self.cpp).hexdigest()
        builder.build_cpp(self.cpp, expected_sha256=digest, **self.metadata)
        before = snapshot(self.cache)
        wrong = ("1" if digest[0] == "0" else "0") + digest[1:]
        with observe_compiler() as commands:
            with self.assertRaises(ArtifactBuildError) as caught:
                builder.build_cpp(self.cpp, expected_sha256=wrong, **self.metadata)
        self.assertEqual(caught.exception.code, "hpo_generated_cpp_hash_mismatch")
        self.assertEqual(commands, [])
        self.assertEqual(snapshot(self.cache), before)

    def test_wrapper_pine_publisher_race_follows_real_compile(self):
        ordinary = self.builder()
        seeded = ordinary.build(SOURCE)
        final_dir = seeded.plugin_path.parent
        hidden = self.root / "publisher-race-artifact"
        final_dir.rename(hidden)
        index = self.cache / "requests" / f"{seeded.request_key}.json"
        index.rename(self.root / "publisher-race-index.json")
        wrapper = self.builder(wrapper_launched=True)
        validate = wrapper._run_plugin_validator

        def reveal_after_compile(path, abi):
            result = validate(path, abi)
            hidden.rename(final_dir)
            return result

        with (
            observe_compiler() as commands,
            patch.object(
                wrapper, "_run_plugin_validator", side_effect=reveal_after_compile
            ),
        ):
            raced = wrapper.build(SOURCE)
        self.assertEqual(len(compile_commands(commands)), 1)
        self.assertEqual(compile_commands(commands)[0][1], 0)
        self.assertFalse(raced.cache_hit)
        self.assertEqual(raced.artifact_key, seeded.artifact_key)
        self.assertEqual(raced.generated_cpp_path.read_bytes(), self.cpp)

    def test_three_real_early_cache_paths_refuse_only_in_wrapper_mode(self):
        ordinary = self.builder()
        seeded = ordinary.build(SOURCE)
        manifest = json.loads(seeded.manifest_path.read_bytes())
        wrapper = self.builder(wrapper_launched=True)
        index = self.cache / "requests" / f"{seeded.request_key}.json"
        index_bytes = index.read_bytes()
        before = snapshot(self.cache)

        def refusal(route):
            with observe_compiler() as commands:
                with self.assertRaises(ArtifactBuildError) as caught:
                    wrapper.build(SOURCE)
            self.assertEqual(caught.exception.code, "hpo_wrapper_bypass_refused")
            self.assertEqual(caught.exception.failure_args, {"route": route})
            self.assertEqual(compile_commands(commands), [])

        refusal("request_cache")
        self.assertEqual(snapshot(self.cache), before)
        # Simulate a request index becoming visible only after lock acquisition.
        # The second hit still reads/validates the real seeded index and artifact.
        index.rename(self.root / "hidden-index.json")

        @contextmanager
        def publish_index_at_lock(path, timeout):
            with _KeyLock(path, timeout):
                (self.root / "hidden-index.json").rename(index)
                yield

        with patch(
            "pineforge_hpo.artifact._KeyLock", side_effect=publish_index_at_lock
        ):
            refusal("request_cache_locked")
        self.assertEqual(snapshot(self.cache), before)
        index.rename(self.root / "hidden-index.json")
        without_index = snapshot(self.cache)
        refusal("artifact_cache")
        self.assertEqual(snapshot(self.cache), without_index)
        self.assertFalse(index.exists())
        self.assertEqual((self.root / "hidden-index.json").read_bytes(), index_bytes)
        # Ordinary Pine builds still recover an artifact hit and repair its index.
        with observe_compiler() as commands:
            hit = ordinary.build(SOURCE)
            request_hit = ordinary.build(SOURCE)
        self.assertTrue(hit.cache_hit)
        self.assertTrue(request_hit.cache_hit)
        self.assertEqual(hit.artifact_key, seeded.artifact_key)
        self.assertEqual(request_hit.artifact_key, seeded.artifact_key)
        self.assertEqual(compile_commands(commands), [])
        self.assertEqual(json.loads(hit.manifest_path.read_bytes()), manifest)

    def test_prepare_run_compiles_source_and_preserves_ordinary_precompiled_route(self):
        self.write_study()
        with observe_compiler() as commands:
            argv, artifact_json = prepare_run(
                self.study,
                self.engine,
                self.cache,
                native=self.native,
                wrapper_launched=True,
            )
        self.assertEqual(len(compile_commands(commands)), 1)
        self.assertEqual(compile_commands(commands)[0][1], 0)
        self.assertFalse(artifact_json["cache_hit"])
        self.assertIn(str(self.native), argv)
        built = self.builder().build(SOURCE)
        self.assertTrue(built.cache_hit)
        self.write_study(artifact=built)
        with observe_compiler() as commands:
            ordinary_argv, ordinary_artifact = prepare_run(
                self.study, native=self.native
            )
        self.assertEqual(commands, [])
        self.assertTrue(ordinary_artifact["cache_hit"])
        self.assertEqual(ordinary_artifact["plugin"], str(built.plugin_path))
        self.assertIn(str(built.plugin_path), ordinary_argv)
        with patch("pineforge_hpo.cli._precompiled_artifact") as precompiled:
            with self.assertRaises(ArtifactBuildError) as caught:
                prepare_run(self.study, native=self.native, wrapper_launched=True)
            precompiled.assert_not_called()
        self.assertEqual(caught.exception.code, "hpo_wrapper_bypass_refused")
        self.assertEqual(
            caught.exception.failure_args, {"route": "precompiled_artifact"}
        )


if __name__ == "__main__":
    unittest.main()
