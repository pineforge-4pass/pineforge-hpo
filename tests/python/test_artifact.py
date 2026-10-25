from __future__ import annotations

from dataclasses import replace

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))

from pineforge_hpo.artifact import (  # noqa: E402
    ArtifactBuildError,
    ArtifactBuilder,
    CANONICAL_COMPILE_FLAGS,
    _KeyLock,
)
from pineforge_hpo.transpile import (  # noqa: E402
    CodegenIdentity,
    TranspileDiagnostic,
    TranspileResult,
)
from pineforge_hpo.window_plan import (  # noqa: E402
    WindowPlanError,
    associate_window_plan,
    derive_window_plan_path,
    require_window_plan_block,
    resolve_window_plan_path,
    verify_window_plan,
)

REQUIRED_SYMBOLS = [
    "pf_abi_version",
    "strategy_create",
    "strategy_free",
    "strategy_set_input",
    "strategy_set_override",
    "strategy_get_last_error",
    "run_backtest_full",
    "report_free",
]


def make_engine(root: Path) -> Path:
    engine = root / "engine"
    (engine / "include" / "pineforge").mkdir(parents=True)
    (engine / "build" / "include" / "pineforge").mkdir(parents=True)
    (engine / "build" / "lib").mkdir(parents=True)
    (engine / "VERSION").write_text("0.11.0\n", encoding="utf-8")
    (engine / "include" / "pineforge" / "pineforge.h").write_text(
        "#define PF_ABI_VERSION 2\n", encoding="utf-8"
    )
    (engine / "include" / "pineforge" / "engine.hpp").write_text(
        "// fake public header\n", encoding="utf-8"
    )
    (engine / "build" / "include" / "pineforge" / "version.h").write_text(
        '#define PINEFORGE_VERSION_STRING "0.11.0"\n', encoding="utf-8"
    )
    (engine / "build" / "lib" / "libpineforge.a").write_bytes(b"fake archive")
    return engine


def make_eigen(root: Path) -> Path:
    eigen = root / "eigen3"
    (eigen / "Eigen" / "src" / "Core" / "util").mkdir(parents=True)
    (eigen / "Eigen" / "Core").write_text("// fake Eigen\n", encoding="utf-8")
    (eigen / "Eigen" / "src" / "Core" / "util" / "Macros.h").write_text(
        "#define EIGEN_WORLD_VERSION 3\n"
        "#define EIGEN_MAJOR_VERSION 4\n"
        "#define EIGEN_MINOR_VERSION 0\n",
        encoding="utf-8",
    )
    return eigen


def make_compiler(root: Path, version: str = "fake-cxx 1.2.3") -> tuple[Path, Path]:
    compiler = root / "fake-cxx"
    log = root / "compiler-log.jsonl"
    compiler.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "if '--version' in sys.argv:\n"
        f"    print({version!r}); raise SystemExit(0)\n"
        "if '-dumpmachine' in sys.argv:\n"
        "    print('fake-target'); raise SystemExit(0)\n"
        "log = pathlib.Path(os.environ['FAKE_COMPILER_LOG'])\n"
        "with log.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if os.environ.get('FAKE_COMPILER_FAIL') == '1':\n"
        "    print('intentional compiler failure', file=sys.stderr); "
        "raise SystemExit(23)\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "output.write_bytes(b'fake strategy plugin')\n",
        encoding="utf-8",
    )
    compiler.chmod(0o755)
    return compiler, log


def validator(_path: Path, abi: int) -> dict:
    return {"abi_version": abi, "required_symbols": REQUIRED_SYMBOLS}


class ArtifactBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.engine = make_engine(self.root)
        self.eigen = make_eigen(self.root)
        self.compiler, self.log = make_compiler(self.root)
        self.cache = self.root / "cache"
        self.codegen = CodegenIdentity("1.1.0", "c" * 64, "/fake/codegen/__init__.py")
        self.transpiled = TranspileResult(
            generated_cpp='extern "C" int generated_strategy = 1;\n',
            inputs=({"title": "Length", "type": "int", "default": 14},),
            strategy_params={"initial_capital": 10000},
        )
        self.environment = patch.dict(
            os.environ, {"FAKE_COMPILER_LOG": str(self.log)}, clear=False
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def builder(self, **kwargs) -> ArtifactBuilder:
        return ArtifactBuilder(
            engine_root=self.engine,
            cache_dir=self.cache,
            compiler=self.compiler,
            eigen_include=self.eigen,
            plugin_validator=validator,
            **kwargs,
        )

    def build_with_recorded_compile_timeouts(self, **kwargs) -> list[float | None]:
        compile_timeouts = []

        def run(command, **options):
            if "--version" in command:
                stdout = "fake-cxx 1.2.3"
            elif "-dumpmachine" in command:
                stdout = "fake-target"
            else:
                compile_timeouts.append(options["timeout"])
                output = Path(command[command.index("-o") + 1])
                output.write_bytes(b"fake strategy plugin")
                stdout = ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with (
            patch("pineforge_hpo.artifact.subprocess.run", side_effect=run),
            patch("pineforge_hpo.artifact.codegen_identity", return_value=self.codegen),
            patch(
                "pineforge_hpo.artifact.transpile_source", return_value=self.transpiled
            ),
        ):
            artifact = self.builder(**kwargs).build("pine bytes")

        self.assertFalse(artifact.cache_hit)
        return compile_timeouts

    def test_default_compile_timeout_is_unlimited(self) -> None:
        self.assertEqual(self.build_with_recorded_compile_timeouts(), [None])

    def test_explicit_compile_timeout_reaches_subprocess(self) -> None:
        self.assertEqual(
            self.build_with_recorded_compile_timeouts(compile_timeout_seconds=17.0),
            [17.0],
        )

    def test_non_positive_compile_timeout_is_refused(self) -> None:
        for timeout in (0.0, -1.0):
            with (
                self.subTest(timeout=timeout),
                patch(
                    "pineforge_hpo.artifact.subprocess.run",
                    side_effect=AssertionError("invalid timeout reached subprocess"),
                ),
            ):
                with self.assertRaisesRegex(
                    ArtifactBuildError, "build timeouts must be positive"
                ) as caught:
                    self.builder(compile_timeout_seconds=timeout).build("pine bytes")
                self.assertEqual(caught.exception.stage, "configuration")

    def test_lock_timeout_reports_deadlock_and_path(self) -> None:
        timeout = self.builder().lock_timeout_seconds
        self.assertEqual(timeout, 120.0)
        lock_path = self.cache / "locks" / "stuck.lock"
        lock = _KeyLock(lock_path, timeout)
        locking = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
        with (
            patch(locking, side_effect=BlockingIOError),
            patch("pineforge_hpo.artifact.time.monotonic", side_effect=[0.0, 120.0]),
            patch(
                "pineforge_hpo.artifact.time.sleep",
                side_effect=AssertionError("expired lock wait must not sleep"),
            ),
        ):
            with self.assertRaises(ArtifactBuildError) as caught:
                with lock:
                    self.fail("unavailable lock was acquired")

        self.assertEqual(caught.exception.stage, "cache")
        self.assertIn(str(lock_path), str(caught.exception))
        self.assertIn(
            "A lock that never frees is a deadlock to report.", str(caught.exception)
        )
        self.assertIsNone(lock._handle)

    def test_content_cache_hit_skips_transpile_and_compile(self) -> None:
        with (
            patch("pineforge_hpo.artifact.codegen_identity", return_value=self.codegen),
            patch(
                "pineforge_hpo.artifact.transpile_source", return_value=self.transpiled
            ) as transpile,
        ):
            first = self.builder().build("pine bytes", filename="first.pine")
            second = self.builder().build("pine bytes", filename="renamed.pine")

        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.artifact_key, second.artifact_key)
        self.assertEqual(first.plugin_path, second.plugin_path)
        self.assertTrue(first.plugin_path.is_file())
        self.assertEqual(transpile.call_count, 1)
        compile_commands = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(compile_commands), 1)

        command = json.loads(compile_commands[0])
        for flag in CANONICAL_COMPILE_FLAGS:
            self.assertIn(flag, command)
        self.assertNotIn("-fbracket-depth=1024", command)
        if sys.platform == "darwin":
            self.assertTrue(
                any(item.startswith("-Wl,-force_load,") for item in command)
            )
            self.assertEqual(first.plugin_path.suffix, ".dylib")
        else:
            self.assertIn("-Wl,--whole-archive", command)
            self.assertIn("-Wl,--no-whole-archive", command)
            self.assertEqual(first.plugin_path.suffix, ".so")

        manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
        provenance = json.loads(first.provenance_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["artifact_key"], first.artifact_key)
        self.assertEqual(manifest["plugin_validation"]["abi_version"], 2)
        self.assertEqual(provenance["request_identity"]["engine"]["version"], "0.11.0")
        self.assertEqual(
            provenance["request_identity"]["compiler"]["target"], "fake-target"
        )
        self.assertEqual(
            provenance["request_identity"]["compile"]["flags"],
            list(CANONICAL_COMPILE_FLAGS),
        )

    def test_builder_stamps_capability_and_preserves_it_on_cache_hits(self) -> None:
        capable = replace(self.transpiled, input_kind_schema=1)
        with (
            patch("pineforge_hpo.artifact.codegen_identity", return_value=self.codegen),
            patch(
                "pineforge_hpo.artifact.transpile_source", return_value=capable
            ) as transpile,
        ):
            first = self.builder().build("symbol-free source")
            second = self.builder().build("symbol-free source")
            self.assertTrue(second.cache_hit)
            self.assertEqual(transpile.call_count, 1)
            manifest = json.loads(first.manifest_path.read_text())
            provenance = json.loads(first.provenance_path.read_text())
            self.assertEqual(manifest["input_kind_schema"], 1)
            self.assertEqual(provenance["input_kind_schema"], 1)
            manifest.pop("input_kind_schema")
            first.manifest_path.write_text(json.dumps(manifest))
            repaired = self.builder().build("symbol-free source")
            self.assertFalse(repaired.cache_hit)
            self.assertEqual(transpile.call_count, 2)
            self.assertEqual(
                json.loads(repaired.manifest_path.read_text())["input_kind_schema"], 1
            )

    def test_builder_refuses_marker_with_known_legacy_codegen(self) -> None:
        with (
            patch(
                "pineforge_hpo.artifact.codegen_identity",
                return_value=replace(self.codegen, version="1.0.9"),
            ),
            patch(
                "pineforge_hpo.artifact.transpile_source",
                return_value=replace(self.transpiled, input_kind_schema=1),
            ),
        ):
            with self.assertRaisesRegex(
                ArtifactBuildError, "contradicts recorded codegen"
            ):
                self.builder().build("legacy symbol metadata")

    def test_old_metadata_cache_is_not_reused_or_stamped_without_capability(
        self,
    ) -> None:
        with (
            patch("pineforge_hpo.artifact.codegen_identity", return_value=self.codegen),
            patch(
                "pineforge_hpo.artifact.transpile_source", return_value=self.transpiled
            ),
            patch("pineforge_hpo.artifact.INPUT_METADATA_REVISION", 0),
        ):
            old = self.builder().build("same source")
        self.assertNotIn("input_kind_schema", json.loads(old.manifest_path.read_text()))
        with (
            patch("pineforge_hpo.artifact.codegen_identity", return_value=self.codegen),
            patch(
                "pineforge_hpo.artifact.transpile_source",
                return_value=replace(self.transpiled, input_kind_schema=1),
            ),
        ):
            rebuilt = self.builder().build("same source")
        self.assertFalse(rebuilt.cache_hit)
        self.assertNotEqual(old.request_key, rebuilt.request_key)
        self.assertNotEqual(old.artifact_key, rebuilt.artifact_key)
        self.assertEqual(
            json.loads(rebuilt.manifest_path.read_text())["input_kind_schema"], 1
        )

    def test_clang_compile_lifts_the_bracket_depth_limit(self) -> None:
        clang_root = self.root / "clang"
        clang_root.mkdir()
        clang, _log = make_compiler(
            clang_root, version="Apple clang version 17.0.0 (clang-1700.6.4.2)"
        )
        builder = ArtifactBuilder(
            engine_root=self.engine,
            cache_dir=self.cache,
            compiler=clang,
            eigen_include=self.eigen,
            plugin_validator=validator,
        )
        with (
            patch("pineforge_hpo.artifact.codegen_identity", return_value=self.codegen),
            patch(
                "pineforge_hpo.artifact.transpile_source", return_value=self.transpiled
            ),
        ):
            artifact = builder.build("pine bytes")

        command = json.loads(self.log.read_text(encoding="utf-8").splitlines()[0])
        self.assertIn("-fbracket-depth=1024", command)
        provenance = json.loads(artifact.provenance_path.read_text(encoding="utf-8"))
        self.assertIn(
            "-fbracket-depth=1024", provenance["request_identity"]["compile"]["flags"]
        )

    def test_source_change_produces_new_artifact(self) -> None:
        with (
            patch("pineforge_hpo.artifact.codegen_identity", return_value=self.codegen),
            patch(
                "pineforge_hpo.artifact.transpile_source", return_value=self.transpiled
            ) as transpile,
        ):
            first = self.builder().build("source one")
            second = self.builder().build("source two")
        self.assertNotEqual(first.request_key, second.request_key)
        self.assertEqual(transpile.call_count, 2)
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 2)

    def test_transpile_failure_is_not_reported_as_compiler_failure(self) -> None:
        failure = TranspileResult(
            generated_cpp=None,
            diagnostics=(
                TranspileDiagnostic(
                    "error", "PARSER", "bad syntax", "bad.pine", 2, 1, 4
                ),
            ),
        )
        with (
            patch("pineforge_hpo.artifact.codegen_identity", return_value=self.codegen),
            patch("pineforge_hpo.artifact.transpile_source", return_value=failure),
        ):
            with self.assertRaises(ArtifactBuildError) as caught:
                self.builder().build("bad", filename="bad.pine")
        self.assertEqual(caught.exception.stage, "transpile")
        self.assertEqual(caught.exception.diagnostics[0].phase, "PARSER")
        self.assertFalse(self.log.exists())

    def test_native_compile_failure_preserves_stderr_and_command(self) -> None:
        with (
            patch.dict(os.environ, {"FAKE_COMPILER_FAIL": "1"}),
            patch("pineforge_hpo.artifact.codegen_identity", return_value=self.codegen),
            patch(
                "pineforge_hpo.artifact.transpile_source", return_value=self.transpiled
            ),
        ):
            with self.assertRaises(ArtifactBuildError) as caught:
                self.builder().build("source")
        self.assertEqual(caught.exception.stage, "compile")
        self.assertIn("intentional compiler failure", caught.exception.stderr)
        self.assertIn("-ffp-contract=off", caught.exception.command)

    def test_default_plugin_validation_rejects_non_loadable_output(self) -> None:
        builder = ArtifactBuilder(
            engine_root=self.engine,
            cache_dir=self.cache,
            compiler=self.compiler,
            eigen_include=self.eigen,
        )
        with (
            patch("pineforge_hpo.artifact.codegen_identity", return_value=self.codegen),
            patch(
                "pineforge_hpo.artifact.transpile_source", return_value=self.transpiled
            ),
        ):
            with self.assertRaises(ArtifactBuildError) as caught:
                builder.build("source")
        self.assertEqual(caught.exception.stage, "plugin_validation")


class WindowPlanArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.engine = make_engine(self.root)
        self.eigen = make_eigen(self.root)
        self.compiler, self.log = make_compiler(self.root)
        self.cache = self.root / "cache"
        self.helper = self.engine / "build/lib/libpineforge_window_plan.so"
        self.codegen = CodegenIdentity("1.1.0", "c" * 64, "/fake/codegen/__init__.py")
        self.transpiled = TranspileResult(
            generated_cpp='extern "C" int generated_strategy = 1;\n',
            inputs=({"title": "Length", "type": "int", "default": 14},),
            strategy_params={"initial_capital": 10000},
        )
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"FAKE_COMPILER_LOG": str(self.log)}).start()
        patch("pineforge_hpo.artifact.platform.system", return_value="Linux").start()
        patch("pineforge_hpo.artifact.platform.machine", return_value="x86_64").start()
        patch(
            "pineforge_hpo.artifact.codegen_identity",
            return_value=self.codegen,
        ).start()
        self.transpile = patch(
            "pineforge_hpo.artifact.transpile_source",
            return_value=self.transpiled,
        ).start()
        self.builder = self.make_builder(self.cache)

    def make_builder(self, cache: Path) -> ArtifactBuilder:
        return ArtifactBuilder(
            engine_root=self.engine,
            cache_dir=cache,
            compiler=self.compiler,
            eigen_include=self.eigen,
            plugin_validator=validator,
        )

    def expected_block(self, helper: Path) -> dict:
        return {
            "schema": "pineforge-hpo-window-plan/v1",
            "build_path": str(helper),
            "sha256": (
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
            ),
            "planner_abi_version": 1,
            "engine_version": "0.11.0",
            "engine_abi_version": 2,
            "engine_headers_sha256": hashlib.sha256(
                b"0:engine.hpp\0// fake public header\n\0"
                b"0:pineforge.h\0#define PF_ABI_VERSION 2\n\0"
                b'1:version.h\0#define PINEFORGE_VERSION_STRING "0.11.0"\n\0'
            ).hexdigest(),
            "engine_static_library_sha256": hashlib.sha256(b"fake archive").hexdigest(),
            "platform_system": "Linux",
            "platform_machine": "x86_64",
            "self_test_id": "selected-plan-utc-six-hours/v1",
        }

    def documents(self, artifact) -> tuple[dict, dict]:
        return (
            json.loads(artifact.manifest_path.read_text(encoding="utf-8")),
            json.loads(artifact.provenance_path.read_text(encoding="utf-8")),
        )

    def assert_reason(self, caught, reason: str) -> None:
        self.assertEqual(caught.exception.code, "hpo_walkforward_unavailable")
        self.assertEqual(caught.exception.failure_args, {"reason": reason})

    def test_new_build_writes_exact_block_outside_compile_identity(self) -> None:
        # Proves normal publication writes the exact independently specified block.
        # False pass: a round-trip comparison accepts a missing or misplaced block.
        self.helper.write_bytes(b"abc")
        artifact = self.builder.build("source")
        manifest, provenance = self.documents(artifact)
        expected = self.expected_block(self.helper)
        self.assertEqual(manifest["window_plan"], expected)
        self.assertEqual(provenance["window_plan"], expected)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertNotIn("window_plan", manifest["request_identity"])
        self.assertEqual(
            set(manifest["request_identity"]["engine"]),
            {
                "root",
                "version",
                "abi_version",
                "include_dirs",
                "headers_sha256",
                "static_library",
                "static_library_sha256",
            },
        )
        self.assertIn(
            "-ffp-contract=off",
            manifest["request_identity"]["compile"]["flags"],
        )

    def test_missing_helper_leaves_ordinary_build_and_hit_unchanged(self) -> None:
        # Proves helper absence is optional for ordinary source builds and cache hits.
        # False pass: tests only see a stubbed successful association or skip the hit.
        first = self.builder.build("source")
        hit = self.builder.build("source")
        self.assertTrue(hit.cache_hit)
        for document in self.documents(first):
            self.assertNotIn("window_plan", document)
        self.assertIsNone(associate_window_plan(self.builder._resolve_engine_layout()))
        with self.assertRaises(WindowPlanError) as caught:
            self.builder.enrich_window_plan(hit)
        self.assert_reason(caught, "window_plan_missing")
        self.assertEqual(self.transpile.call_count, 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_unreadable_helper_leaves_ordinary_build_without_a_block(self) -> None:
        # Proves a real unreadable helper cannot prevent ordinary publication.
        # False pass: root reads chmod-000 files; use a directory in that case.
        self.helper.write_bytes(b"abc")
        protected_path = self.helper
        original_mode = protected_path.stat().st_mode

        def restore_mode():
            try:
                protected_path.chmod(original_mode)
            except OSError:
                pass

        self.addCleanup(restore_mode)
        self.helper.chmod(0o000)
        try:
            self.helper.read_bytes()
        except PermissionError:
            self.assertTrue(self.helper.is_file())
        else:
            protected_path = self.root / "permission-probe.so"
            self.helper.rename(protected_path)
            self.helper.mkdir()
            self.assertTrue(self.helper.is_dir())

        artifact = self.builder.build("source")
        self.assertFalse(artifact.cache_hit)
        self.assertTrue(self.builder.build("source").cache_hit)
        for document in self.documents(artifact):
            self.assertNotIn("window_plan", document)
        with self.assertRaises(WindowPlanError) as caught:
            require_window_plan_block(*self.documents(artifact))
        self.assert_reason(caught, "window_plan_manifest_missing")
        with self.assertRaises(WindowPlanError) as caught:
            self.builder.enrich_window_plan(artifact)
        self.assert_reason(caught, "window_plan_missing")
        self.assertEqual(self.transpile.call_count, 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_association_refusal_does_not_fail_an_ordinary_build(self) -> None:
        # Proves the ordinary hook catches helper errors even on a root CI worker.
        # False pass: a directory is filtered before the failing hash call.
        self.helper.write_bytes(b"abc")
        with patch(
            "pineforge_hpo.artifact.associate_window_plan",
            side_effect=WindowPlanError("window_plan_missing", "unreadable helper"),
        ) as associate:
            artifact = self.builder.build("source")
        associate.assert_called_once()
        for document in self.documents(artifact):
            self.assertNotIn("window_plan", document)
        with self.assertRaises(WindowPlanError) as caught:
            require_window_plan_block(*self.documents(artifact))
        self.assert_reason(caught, "window_plan_manifest_missing")
        self.assertEqual(
            self.builder.enrich_window_plan(artifact),
            self.expected_block(self.helper),
        )

    def test_candidate_three_never_derives_a_companion(self) -> None:
        # Proves candidate three ignores both the flat and build/lib helper guesses.
        # False pass: a basename search finds either deliberately planted helper.
        archive = self.engine / "build/lib/libpineforge.a"
        archive.rename(self.engine / "build/libpineforge.a")
        self.helper.write_bytes(b"abc")
        (self.engine / "build/libpineforge_window_plan.so").write_bytes(b"abc")
        artifact = self.builder.build("source")
        for document in self.documents(artifact):
            self.assertNotIn("window_plan", document)
        self.assertIsNone(
            derive_window_plan_path(self.builder._resolve_engine_layout())
        )

    def test_candidate_three_source_enrichment_requires_an_explicit_path(self) -> None:
        # Proves source authority can record an explicit path for candidate three.
        # False pass: the ordinary build guessed a sibling or runtime derivation works.
        (self.engine / "build/lib/libpineforge.a").rename(
            self.engine / "build/libpineforge.a"
        )
        self.helper.write_bytes(b"abc")
        artifact = self.builder.build("source")
        self.assertNotIn("window_plan", self.documents(artifact)[0])
        with self.assertRaises(WindowPlanError) as caught:
            self.builder.enrich_window_plan(artifact)
        self.assert_reason(caught, "window_plan_path")
        block = self.builder.enrich_window_plan(artifact, explicit_path=self.helper)
        self.assertEqual(block, self.expected_block(self.helper))
        with self.assertRaises(WindowPlanError) as caught:
            resolve_window_plan_path(None, self.engine, block)
        self.assert_reason(caught, "window_plan_path")
        self.assertEqual(
            resolve_window_plan_path(self.helper, self.engine, block),
            self.helper,
        )

    def test_install_archive_uses_only_its_own_sibling(self) -> None:
        # Proves candidate two uses lib/, independently of build/lib/ helper bytes.
        # False pass: derivation always chooses the build directory.
        installed = self.engine / "lib"
        installed.mkdir()
        (self.engine / "build/lib/libpineforge.a").rename(installed / "libpineforge.a")
        helper = installed / "libpineforge_window_plan.so"
        helper.write_bytes(b"abc")
        self.helper.write_bytes(b"wrong sibling")
        artifact = self.builder.build("source")
        for document in self.documents(artifact):
            self.assertEqual(document["window_plan"], self.expected_block(helper))

    def test_archive_symlink_preserves_the_selected_candidate_for_derivation(
        self,
    ) -> None:
        # Proves canonicalizing the archive cannot lose its selected layout candidate.
        # False pass: derivation follows the archive target and records another helper.
        archive = self.engine / "build/lib/libpineforge.a"
        target = self.engine / "archive-target.a"
        archive.rename(target)
        archive.symlink_to(target)
        self.helper.write_bytes(b"abc")
        artifact = self.builder.build("source")
        for document in self.documents(artifact):
            self.assertEqual(document["window_plan"], self.expected_block(self.helper))
            self.assertEqual(
                document["request_identity"]["engine"]["static_library"],
                str(target),
            )

    def test_selected_archive_does_not_search_other_helper_locations(self) -> None:
        # Proves an unselected install tree cannot supply the build tree's helper.
        # False pass: searching all sibling candidates accidentally finds this decoy.
        installed = self.engine / "lib"
        installed.mkdir()
        (installed / "libpineforge.a").write_bytes(b"fake archive")
        (installed / "libpineforge_window_plan.so").write_bytes(b"abc")
        artifact = self.builder.build("source")
        for document in self.documents(artifact):
            self.assertNotIn("window_plan", document)

    def test_derivation_uses_darwin_and_freebsd_suffixes(self) -> None:
        # Proves the shared platform suffix rule covers both supported suffixes.
        # False pass: a Linux-only hard-coded basename passes every other fixture.
        layout = self.builder._resolve_engine_layout()
        for system, filename in (
            ("Darwin", "libpineforge_window_plan.dylib"),
            ("FreeBSD", "libpineforge_window_plan.so"),
        ):
            with (
                self.subTest(system=system),
                patch("pineforge_hpo.artifact.platform.system", return_value=system),
            ):
                self.assertEqual(
                    derive_window_plan_path(layout),
                    self.engine / "build/lib" / filename,
                )

    def test_helper_does_not_change_any_compile_key_bytes(self) -> None:
        # Proves fresh builds with/without the helper have byte-identical identities.
        # False pass: a cache hit hides a helper-dependent key in a fresh build.
        before = self.builder.build("source")
        before_manifest, _ = self.documents(before)
        self.helper.write_bytes(b"abc")
        after = self.make_builder(self.root / "separate-cache").build("source")
        after_manifest, _ = self.documents(after)
        self.assertFalse(after.cache_hit)
        self.assertNotIn("window_plan", before_manifest)
        self.assertIn("window_plan", after_manifest)
        for field in ("request_key", "artifact_key", "request_identity"):
            self.assertEqual(
                json.dumps(
                    before_manifest[field],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode(),
                json.dumps(
                    after_manifest[field],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode(),
            )
        self.assertEqual(self.transpile.call_count, 2)
        self.assertEqual(len(self.log.read_text().splitlines()), 2)

    def test_compile_keys_match_independently_derived_fixture_identity(self) -> None:
        # Proves both keys retain the pre-window-plan canonical identity.
        # False pass: deriving expectations from the builder accepts new fields.
        # The exact engine key set is already pinned by
        # test_new_build_writes_exact_block_outside_compile_identity.
        include_dirs = [
            str(self.engine / "include"),
            str(self.engine / "build/include"),
        ]
        request_identity = {
            "input_metadata_revision": 2,
            "source_sha256": hashlib.sha256(b"source").hexdigest(),
            "codegen": {
                "version": "1.1.0",
                "implementation_sha256": "c" * 64,
                "module_path": "/fake/codegen/__init__.py",
            },
            "engine": {
                "root": str(self.engine),
                "version": "0.11.0",
                "abi_version": 2,
                "include_dirs": include_dirs,
                "headers_sha256": hashlib.sha256(
                    b"0:engine.hpp\0// fake public header\n\0"
                    b"0:pineforge.h\0#define PF_ABI_VERSION 2\n\0"
                    b'1:version.h\0#define PINEFORGE_VERSION_STRING "0.11.0"\n\0'
                ).hexdigest(),
                "static_library": str(self.engine / "build/lib/libpineforge.a"),
                "static_library_sha256": hashlib.sha256(b"fake archive").hexdigest(),
            },
            "compiler": {
                "path": str(self.compiler),
                "version": "fake-cxx 1.2.3",
                "target": "fake-target",
            },
            "eigen": {
                "include_dir": str(self.eigen),
                "headers_sha256": hashlib.sha256(
                    b"0:Core\0// fake Eigen\n\0"
                    b"0:src/Core/util/Macros.h\0"
                    b"#define EIGEN_WORLD_VERSION 3\n"
                    b"#define EIGEN_MAJOR_VERSION 4\n"
                    b"#define EIGEN_MINOR_VERSION 0\n\0"
                ).hexdigest(),
                "version": "3.4.0",
            },
            "platform": {
                "system": "Linux",
                "release": "fixture-release",
                "machine": "x86_64",
                "sys_platform": "linux",
                "python_implementation": "CPython",
                "python_version": "3.11.0",
            },
            "compiler_environment": {
                "CPATH": "",
                "CPLUS_INCLUDE_PATH": "",
                "LIBRARY_PATH": "",
                "SDKROOT": "",
                "MACOSX_DEPLOYMENT_TARGET": "",
            },
            "compile": {
                "flags": [
                    "-std=c++17",
                    "-O2",
                    "-ffp-contract=off",
                    "-fPIC",
                    "-shared",
                ],
                "include_dirs": [*include_dirs, str(self.eigen)],
                "link_mode": "whole_archive",
                "output_extension": ".so",
            },
        }
        # Hand-written canonical JSON derivation; no builder hash/identity helper.
        request_key = hashlib.sha256(
            json.dumps(
                request_identity,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        artifact_key = hashlib.sha256(
            json.dumps(
                {
                    "request_identity": request_identity,
                    "generated_cpp_sha256": hashlib.sha256(
                        b'extern "C" int generated_strategy = 1;\n'
                    ).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        with (
            patch.dict(os.environ, request_identity["compiler_environment"]),
            patch(
                "pineforge_hpo.artifact.platform.release",
                return_value="fixture-release",
            ),
            patch("pineforge_hpo.artifact.sys.platform", "linux"),
            patch(
                "pineforge_hpo.artifact.platform.python_implementation",
                return_value="CPython",
            ),
            patch(
                "pineforge_hpo.artifact.platform.python_version",
                return_value="3.11.0",
            ),
        ):
            artifact = self.builder.build("source")
        self.assertEqual(artifact.request_key, request_key)
        self.assertEqual(artifact.artifact_key, artifact_key)
        self.assertEqual(
            self.documents(artifact)[0]["request_identity"],
            request_identity,
        )

    def test_old_cache_enrichment_is_locked_and_idempotent(self) -> None:
        # Proves fast-hit enrichment uses the same lock and publishes both files once.
        # False pass: recompilation, one-file publication, or a rewrite on retry.
        # The _KeyLock spy proves use of the lock, not mutual exclusion.
        artifact = self.builder.build("source")
        plugin_bytes = artifact.plugin_path.read_bytes()
        before, _ = self.documents(artifact)
        self.helper.write_bytes(b"abc")
        hit = self.builder.build("source")
        self.assertTrue(hit.cache_hit)
        self.assertNotIn("window_plan", self.documents(hit)[0])
        with (
            patch("pineforge_hpo.artifact._KeyLock", wraps=_KeyLock) as lock,
            patch.object(
                self.builder,
                "_write_window_plan_metadata",
                wraps=self.builder._write_window_plan_metadata,
            ) as publish,
        ):
            self.assertEqual(
                self.builder.enrich_window_plan(hit),
                self.expected_block(self.helper),
            )
            published_bytes = (
                hit.manifest_path.read_bytes(),
                hit.provenance_path.read_bytes(),
            )
            self.assertEqual(
                self.builder.enrich_window_plan(hit),
                self.expected_block(self.helper),
            )
        self.assertEqual(publish.call_count, 2)
        self.assertEqual(lock.call_count, 2)
        for call in lock.call_args_list:
            self.assertEqual(
                call.args,
                (
                    self.cache / "locks" / f"{artifact.request_key}.lock",
                    120.0,
                ),
            )
        self.assertEqual(
            published_bytes,
            (hit.manifest_path.read_bytes(), hit.provenance_path.read_bytes()),
        )
        for document in self.documents(hit):
            self.assertEqual(document["window_plan"], self.expected_block(self.helper))
            for field in ("request_key", "artifact_key", "request_identity"):
                self.assertEqual(document[field], before[field])
        self.assertEqual(hit.plugin_path.read_bytes(), plugin_bytes)
        self.assertEqual(plugin_bytes, b"fake strategy plugin")
        self.assertEqual(self.transpile.call_count, 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_second_metadata_write_failure_leaves_selected_execution_refused(
        self,
    ) -> None:
        # Proves a real first publication followed by failure cannot admit a half pair.
        # False pass: both writes are mocked away or only the exception is tested.
        artifact = self.builder.build("source")
        self.helper.write_bytes(b"abc")
        original = self.builder._write_window_plan_metadata

        def publish(path, payload):
            if path == artifact.provenance_path:
                raise OSError("intentional second publication failure")
            original(path, payload)

        with patch.object(
            self.builder,
            "_write_window_plan_metadata",
            side_effect=publish,
        ) as failed_publish:
            with self.assertRaises(ArtifactBuildError) as caught:
                self.builder.enrich_window_plan(artifact)
        self.assertEqual(caught.exception.code, "hpo_output_io_failed")
        self.assertEqual(
            [call.args[0] for call in failed_publish.call_args_list],
            [artifact.manifest_path, artifact.provenance_path],
        )
        manifest, provenance = self.documents(artifact)
        self.assertEqual(manifest["window_plan"], self.expected_block(self.helper))
        self.assertNotIn("window_plan", provenance)
        first_block = manifest["window_plan"]
        half_written_bytes = (
            artifact.manifest_path.read_bytes(),
            artifact.provenance_path.read_bytes(),
        )
        hit = self.builder.build("source")
        self.assertTrue(hit.cache_hit)
        self.assertEqual(hit.artifact_key, artifact.artifact_key)
        self.assertEqual(
            half_written_bytes,
            (hit.manifest_path.read_bytes(), hit.provenance_path.read_bytes()),
        )
        with self.assertRaises(WindowPlanError) as caught:
            require_window_plan_block(manifest, provenance)
        self.assert_reason(caught, "window_plan_identity")
        with patch.object(
            self.builder,
            "_write_window_plan_metadata",
            wraps=original,
        ) as retry_publish:
            repaired = self.builder.enrich_window_plan(hit)
        self.assertEqual(repaired, first_block)
        self.assertEqual(repaired["sha256"], first_block["sha256"])
        retry_publish.assert_called_once()
        self.assertEqual(retry_publish.call_args.args[0], artifact.provenance_path)
        self.assertEqual(artifact.manifest_path.read_bytes(), half_written_bytes[0])
        self.assertEqual(require_window_plan_block(*self.documents(hit)), first_block)
        self.assertEqual(artifact.plugin_path.read_bytes(), b"fake strategy plugin")
        self.assertEqual(self.transpile.call_count, 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_ordinary_reader_succeeds_between_enrichment_writes(self) -> None:
        # Proves the unlocked ordinary lookup tolerates a concurrent half pair.
        # False pass: the reader runs only before or after the complete publication.
        artifact = self.builder.build("source")
        self.helper.write_bytes(b"abc")
        original = self.builder._write_window_plan_metadata
        observed = []

        def publish(path, payload):
            original(path, payload)
            if path != artifact.manifest_path:
                return
            manifest, provenance = self.documents(artifact)
            self.assertEqual(manifest["window_plan"], self.expected_block(self.helper))
            self.assertNotIn("window_plan", provenance)
            before = (
                artifact.manifest_path.read_bytes(),
                artifact.provenance_path.read_bytes(),
            )
            observed.append(self.builder.build("source"))
            self.assertTrue(observed[0].cache_hit)
            self.assertEqual(
                before,
                (
                    artifact.manifest_path.read_bytes(),
                    artifact.provenance_path.read_bytes(),
                ),
            )
            with self.assertRaises(WindowPlanError) as caught:
                require_window_plan_block(manifest, provenance)
            self.assert_reason(caught, "window_plan_identity")

        with patch.object(
            self.builder,
            "_write_window_plan_metadata",
            side_effect=publish,
        ) as writes:
            self.builder.enrich_window_plan(artifact)
        self.assertEqual(len(observed), 1)
        self.assertEqual(writes.call_count, 2)
        self.assertEqual(
            require_window_plan_block(*self.documents(artifact)),
            self.expected_block(self.helper),
        )
        self.assertEqual(self.transpile.call_count, 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_enrichment_repairs_either_missing_or_stale_metadata_side(self) -> None:
        # Proves repair selects the validating side, including provenance-only state.
        # False pass: manifest is always trusted, or both files are rewritten.
        self.helper.write_bytes(b"abc")
        artifact = self.builder.build("source")
        originals = self.documents(artifact)
        for side in (0, 1):
            for case in (
                "missing",
                "missing_stale_engine",
                "stale_engine",
                "stale_source",
            ):
                with self.subTest(side=side, case=case):
                    documents = copy.deepcopy(originals)
                    stale = documents[side]
                    if case in {"missing", "missing_stale_engine"}:
                        del stale["window_plan"]
                    if case in {"stale_engine", "missing_stale_engine"}:
                        stale["request_identity"]["engine"]["headers_sha256"] = "0" * 64
                    elif case == "stale_source":
                        stale["request_identity"]["source_sha256"] = "0" * 64
                    paths = (artifact.manifest_path, artifact.provenance_path)
                    for path, document in zip(paths, documents):
                        path.write_text(json.dumps(document), encoding="utf-8")
                    surviving_bytes = paths[1 - side].read_bytes()
                    with patch.object(
                        self.builder,
                        "_write_window_plan_metadata",
                        wraps=self.builder._write_window_plan_metadata,
                    ) as publish:
                        block = self.builder.enrich_window_plan(artifact)
                    self.assertEqual(block, self.expected_block(self.helper))
                    publish.assert_called_once()
                    self.assertEqual(publish.call_args.args[0], paths[side])
                    self.assertEqual(paths[1 - side].read_bytes(), surviving_bytes)
                    self.assertEqual(
                        require_window_plan_block(*self.documents(artifact)),
                        self.expected_block(self.helper),
                    )
                    for repaired, original_document in zip(
                        self.documents(artifact),
                        originals,
                    ):
                        self.assertEqual(repaired, original_document)
        self.assertEqual(self.transpile.call_count, 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_repair_never_reassociates_changed_helper_bytes(self) -> None:
        # Proves a surviving block remains authoritative after interruption.
        # False pass: retry hashes current bytes into a new expected identity.
        self.helper.write_bytes(b"abc")
        artifact = self.builder.build("source")
        provenance = self.documents(artifact)[1]
        del provenance["window_plan"]
        artifact.provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
        before = (
            artifact.manifest_path.read_bytes(),
            artifact.provenance_path.read_bytes(),
        )
        self.helper.write_bytes(b"abd")
        with patch.object(self.builder, "_write_window_plan_metadata") as publish:
            with self.assertRaises(WindowPlanError) as caught:
                self.builder.enrich_window_plan(artifact)
        self.assert_reason(caught, "window_plan_hash_mismatch")
        publish.assert_not_called()
        self.assertEqual(
            before,
            (
                artifact.manifest_path.read_bytes(),
                artifact.provenance_path.read_bytes(),
            ),
        )

    def test_repair_refuses_different_valid_blocks_or_an_invalid_survivor(self) -> None:
        # Proves repair never chooses between valid disagreements or invents a hash.
        # False pass: malformed surviving blocks are treated as absent old metadata.
        self.helper.write_bytes(b"abc")
        artifact = self.builder.build("source")
        originals = self.documents(artifact)
        for case, reason in (
            ("different_valid_blocks", "window_plan_identity"),
            ("invalid_manifest", "window_plan_manifest_invalid"),
            ("invalid_provenance", "window_plan_manifest_invalid"),
            ("misassociated_survivor", "window_plan_manifest_invalid"),
        ):
            with self.subTest(case=case):
                manifest, provenance = copy.deepcopy(originals)
                if case == "different_valid_blocks":
                    provenance["window_plan"]["sha256"] = "0" * 64
                elif case == "invalid_provenance":
                    del manifest["window_plan"]
                    provenance["window_plan"]["schema"] = "invalid"
                else:
                    del provenance["window_plan"]
                    if case == "invalid_manifest":
                        manifest["window_plan"]["schema"] = "invalid"
                    else:
                        manifest["window_plan"]["engine_headers_sha256"] = "0" * 64
                artifact.manifest_path.write_text(
                    json.dumps(manifest),
                    encoding="utf-8",
                )
                artifact.provenance_path.write_text(
                    json.dumps(provenance),
                    encoding="utf-8",
                )
                before = (
                    artifact.manifest_path.read_bytes(),
                    artifact.provenance_path.read_bytes(),
                )
                with patch.object(
                    self.builder,
                    "_write_window_plan_metadata",
                ) as publish:
                    with self.assertRaises(WindowPlanError) as caught:
                        self.builder.enrich_window_plan(artifact)
                self.assert_reason(caught, reason)
                publish.assert_not_called()
                self.assertEqual(
                    before,
                    (
                        artifact.manifest_path.read_bytes(),
                        artifact.provenance_path.read_bytes(),
                    ),
                )

    def test_existing_block_reverifies_bytes_without_reassociation(self) -> None:
        # Proves a selected cache hit retains its expected hash when the helper changes.
        # False pass: a new hash is silently associated and the metadata is rewritten.
        self.helper.write_bytes(b"abc")
        artifact = self.builder.build("source")
        before = (
            artifact.manifest_path.read_bytes(),
            artifact.provenance_path.read_bytes(),
        )
        self.helper.write_bytes(b"abd")
        hit = self.builder.build("source")
        self.assertTrue(hit.cache_hit)
        with self.assertRaises(WindowPlanError) as caught:
            self.builder.enrich_window_plan(hit)
        self.assert_reason(caught, "window_plan_hash_mismatch")
        self.assertEqual(
            before,
            (
                artifact.manifest_path.read_bytes(),
                artifact.provenance_path.read_bytes(),
            ),
        )
        self.assertEqual(self.transpile.call_count, 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_invalid_cache_blocks_are_refusals_instead_of_cache_misses(self) -> None:
        # Proves ordinary hits ignore blocks while selected admission still refuses.
        # False pass: an ordinary cache miss rewrites the invalid selected metadata.
        self.helper.write_bytes(b"abc")
        artifact = self.builder.build("source")
        original_manifest, original_provenance = self.documents(artifact)
        for case, reason in (
            ("malformed", "window_plan_manifest_invalid"),
            ("disagreement", "window_plan_identity"),
            ("association", "window_plan_identity"),
        ):
            manifest = copy.deepcopy(original_manifest)
            provenance = copy.deepcopy(original_provenance)
            if case == "malformed":
                manifest["window_plan"]["schema"] = "unexpected"
            elif case == "disagreement":
                provenance["window_plan"]["sha256"] = "0" * 64
            else:
                for document in (manifest, provenance):
                    document["window_plan"]["engine_headers_sha256"] = "0" * 64
            artifact.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            artifact.provenance_path.write_text(
                json.dumps(provenance),
                encoding="utf-8",
            )
            with self.subTest(case=case):
                before = (
                    artifact.manifest_path.read_bytes(),
                    artifact.provenance_path.read_bytes(),
                )
                hit = self.builder.build("source")
                self.assertTrue(hit.cache_hit)
                self.assertEqual(hit.artifact_key, artifact.artifact_key)
                self.assertEqual(
                    before,
                    (hit.manifest_path.read_bytes(), hit.provenance_path.read_bytes()),
                )
                with self.assertRaises(WindowPlanError) as caught:
                    require_window_plan_block(*self.documents(hit))
                self.assert_reason(caught, reason)
        self.assertEqual(self.transpile.call_count, 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_unreadable_metadata_cannot_hide_an_existing_companion_block(self) -> None:
        # Proves selected enrichment refuses unreadable pairs; ordinary rebuilds work.
        # False pass: rebuilding first erases the corrupt state before selected checks.
        self.helper.write_bytes(b"abc")
        artifact = self.builder.build("source")
        original = (
            artifact.manifest_path.read_bytes(),
            artifact.provenance_path.read_bytes(),
        )
        for path, corrupt in (
            (artifact.manifest_path, b"{"),
            (artifact.provenance_path, b"{"),
            (artifact.provenance_path, b"[]"),
        ):
            artifact.manifest_path.write_bytes(original[0])
            artifact.provenance_path.write_bytes(original[1])
            path.write_bytes(corrupt)
            with self.subTest(path=path, corrupt=corrupt):
                before = (
                    artifact.manifest_path.read_bytes(),
                    artifact.provenance_path.read_bytes(),
                )
                with self.assertRaises(WindowPlanError) as caught:
                    self.builder.enrich_window_plan(artifact)
                self.assert_reason(caught, "window_plan_manifest_invalid")
                self.assertEqual(
                    before,
                    (
                        artifact.manifest_path.read_bytes(),
                        artifact.provenance_path.read_bytes(),
                    ),
                )
                rebuilt = self.builder.build("source")
                self.assertFalse(rebuilt.cache_hit)
                self.assertEqual(rebuilt.artifact_key, artifact.artifact_key)
                self.assertEqual(
                    require_window_plan_block(*self.documents(rebuilt)),
                    self.expected_block(self.helper),
                )
        self.assertEqual(self.transpile.call_count, 4)
        self.assertEqual(len(self.log.read_text().splitlines()), 4)

    def test_old_cache_requires_matching_current_engine_before_association(
        self,
    ) -> None:
        # Proves metadata enrichment checks current archive and headers before writes.
        # False pass: co-location alone associates the helper to a different engine.
        artifact = self.builder.build("source")
        self.helper.write_bytes(b"abc")
        for path in (
            self.engine / "build/lib/libpineforge.a",
            self.engine / "include/pineforge/engine.hpp",
        ):
            original = path.read_bytes()
            path.write_bytes(original + b"changed")
            with self.subTest(path=path):
                with self.assertRaises(WindowPlanError) as caught:
                    self.builder.enrich_window_plan(artifact)
                self.assert_reason(caught, "window_plan_identity")
                for document in self.documents(artifact):
                    self.assertNotIn("window_plan", document)
            path.write_bytes(original)
        self.assertEqual(self.transpile.call_count, 1)

    def test_runtime_derivation_accepts_a_matching_relocated_engine_root(self) -> None:
        # Proves only an explicitly supplied root with matching content may derive.
        # False pass: the resolver silently opens the recorded build-machine root.
        self.helper.write_bytes(b"abc")
        artifact = self.builder.build("source")
        block = self.documents(artifact)[0]["window_plan"]
        relocated = self.root / "relocated-engine"
        self.engine.rename(relocated)
        path = resolve_window_plan_path(None, relocated, block)
        self.assertEqual(path, relocated / "build/lib/libpineforge_window_plan.so")
        self.assertIsNone(verify_window_plan(block, path))
        (relocated / "build/lib/libpineforge.a").write_bytes(b"different archive")
        with self.assertRaises(WindowPlanError) as caught:
            resolve_window_plan_path(None, relocated, block)
        self.assert_reason(caught, "window_plan_identity")

    def test_builder_cannot_enrich_a_precompiled_artifact_outside_its_cache(
        self,
    ) -> None:
        # Proves source-cache authority does not extend to arbitrary precompiled files.
        # False pass: passing any StrategyArtifact authorizes creating expected hashes.
        artifact = self.builder.build("source")
        foreign = replace(
            artifact,
            manifest_path=self.root / "precompiled/manifest.json",
            provenance_path=self.root / "precompiled/provenance.json",
        )
        self.helper.write_bytes(b"abc")
        with self.assertRaises(WindowPlanError) as caught:
            self.builder.enrich_window_plan(foreign)
        self.assert_reason(caught, "window_plan_identity")


if __name__ == "__main__":
    unittest.main()
