from __future__ import annotations

import json
import os
from pathlib import Path
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
)
from pineforge_hpo.transpile import (  # noqa: E402
    CodegenIdentity,
    TranspileDiagnostic,
    TranspileResult,
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
        "    print('intentional compiler failure', file=sys.stderr); raise SystemExit(23)\n"
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
        self.codegen = CodegenIdentity("0.9.0", "c" * 64, "/fake/codegen/__init__.py")
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


if __name__ == "__main__":
    unittest.main()
