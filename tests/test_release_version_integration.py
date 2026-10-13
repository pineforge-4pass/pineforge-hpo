"""Build an rc copy of the tracked source and verify its native result and catalog.

Usage: test_release_version_integration.py ENGINE_ROOT DLIB_SOURCE PLUGIN [unittest arguments]
"""

from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = Path(sys.argv.pop(1)).resolve()
DLIB_SOURCE = Path(sys.argv.pop(1)).resolve()
PLUGIN = Path(sys.argv.pop(1)).resolve()


class ReleaseVersionIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "source"
        self.source.mkdir()
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            check=True,
            capture_output=True,
        ).stdout.decode().split("\0")
        for name in tracked:
            if not name or name.startswith("external/"):
                continue
            destination = self.source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, destination)
        (self.source / "VERSION").write_text("1.6.0rc1\n", encoding="utf-8")

    def invoke(self, *command):
        return subprocess.run(command, text=True, capture_output=True)

    def git(self, *arguments):
        completed = self.invoke("git", "-C", str(self.source), *arguments)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_real_rc_tree_preserves_full_native_result_version(self):
        build = self.directory / "build"
        configured = self.invoke(
            "cmake",
            "-S", str(self.source),
            "-B", str(build),
            "-DCMAKE_BUILD_TYPE=Release",
            "-DPINEFORGE_HPO_BUILD_TESTS=OFF",
            "-DPINEFORGE_HPO_BUILD_NATIVE_CLI=ON",
            "-DPINEFORGE_HPO_BUILD_ENGINE_ADAPTER=ON",
            f"-DPINEFORGE_ENGINE_ROOT={ENGINE_ROOT}",
            f"-DFETCHCONTENT_SOURCE_DIR_DLIB={DLIB_SOURCE}",
        )
        self.assertEqual(configured.returncode, 0, configured.stderr)
        self.assertIn(
            "CMAKE_PROJECT_VERSION:STATIC=1.6.0\n",
            (build / "CMakeCache.txt").read_text(),
        )
        built = self.invoke(
            "cmake", "--build", str(build), "--target", "pineforge-hpo-native", "-j4"
        )
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        native = build / "bin/pineforge-hpo-native"
        self.assertTrue(native.is_file())
        csv = self.directory / "bars.csv"
        csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            "1700000000000,100,102,99,101,10\n"
            "1700000060000,101,103,100,102,11\n",
            encoding="utf-8",
        )
        completed = self.invoke(
            str(native), "run",
            "--strategy", str(PLUGIN),
            "--ohlcv", str(csv),
            "--objective", "metrics.all.net_profit",
            "--sampler", "grid",
            "--max-trials", "1",
            "--workers", "1",
            "--input-tf", "1",
            "--script-tf", "5",
            "--chart-timezone", "Asia/Taipei",
            "--bar-magnifier", "true",
            "--magnifier-samples", "6",
            "--magnifier-distribution", "triangle",
            "--int-dim", "Length", "14", "14", "1",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        document = json.loads(completed.stdout)
        self.assertTrue(document["ok"])
        self.assertEqual(document["pineforge_hpo_version"], "1.6.0rc1")
        self.assertNotEqual(document["pineforge_hpo_version"], "1.6.0")

    def test_copied_catalog_script_stamps_and_checks_full_rc_version(self):
        self.git("init", "-q")
        self.git("add", "python/pineforge_hpo/hpo_failure_codes.json")
        self.git(
            "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid",
            "commit", "-m", "final catalog",
        )
        self.git("tag", "v0.15.0")
        self.git("tag", "v1.6.0rc1")
        version = (self.source / "VERSION").read_text().splitlines()[0]
        command = (
            sys.executable, str(self.source / "scripts/gen_catalog_diff.py"),
            "--repo", str(self.source),
            "--from-tag", "v0.15.0",
            "--release-version", version,
        )
        stamped = self.invoke(*command)
        self.assertEqual(stamped.returncode, 0, stamped.stderr)
        output = self.source / "python/pineforge_hpo/hpo_failure_codes_diff.json"
        document = json.loads(output.read_bytes())
        self.assertEqual(document["from"]["tag"], "v0.15.0")
        self.assertEqual(document["to"]["version"], "1.6.0rc1")
        checked = self.invoke(*command, "--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        checked_stored = self.invoke(
            sys.executable, str(self.source / "scripts/gen_catalog_diff.py"),
            "--repo", str(self.source), "--check",
        )
        self.assertEqual(checked_stored.returncode, 0, checked_stored.stderr)


if __name__ == "__main__":
    unittest.main()
