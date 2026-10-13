"""Build distributions and verify their exact release version contract."""

import importlib
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]


class PackagingVersionTests(unittest.TestCase):
    def test_wheel_and_sdist_preserve_rc_and_final_versions(self):
        try:
            importlib.import_module("build")
        except ImportError:
            self.fail(
                "Install build with 'python -m pip install build' to run this test"
            )
        try:
            from packaging.version import Version
        except ImportError:
            self.fail("Install packaging with 'python -m pip install packaging'")

        listed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        tracked = listed.stdout.decode().split("\0")
        cases = (
            (
                b"1.6.0rc1\n",
                "1.6.0rc1",
                True,
                "pineforge_hpo-1.6.0rc1-py3-none-any.whl",
                "Version: 1.6.0rc1",
                "pineforge_hpo-1.6.0rc1.tar.gz",
                "pineforge_hpo-1.6.0rc1/VERSION",
            ),
            (
                b"1.6.0\n",
                "1.6.0",
                False,
                "pineforge_hpo-1.6.0-py3-none-any.whl",
                "Version: 1.6.0",
                "pineforge_hpo-1.6.0.tar.gz",
                "pineforge_hpo-1.6.0/VERSION",
            ),
        )
        for (
            version_bytes,
            expected_version,
            is_prerelease,
            wheel_name,
            metadata_version,
            sdist_name,
            version_member,
        ) in cases:
            with self.subTest(version=expected_version):
                with tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary)
                    source = directory / "source"
                    source.mkdir()
                    for name in tracked:
                        if not name or name.startswith("external/"):
                            continue
                        destination = source / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(ROOT / name, destination)
                    version_file = source / "VERSION"
                    version_file.write_bytes(version_bytes)
                    written_version = version_file.read_text(encoding="utf-8")
                    written_version = written_version.removesuffix("\n")
                    parsed = Version(written_version)
                    self.assertEqual(parsed.is_prerelease, is_prerelease)
                    self.assertEqual(str(parsed), expected_version)
                    self.assertEqual(written_version, str(parsed))

                    output = directory / "dist"
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "build",
                            "--sdist",
                            "--wheel",
                            "--outdir",
                            str(output),
                        ],
                        cwd=source,
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(
                        completed.returncode, 0, completed.stdout + completed.stderr
                    )
                    self.assertEqual(
                        [path.name for path in output.glob("*.whl")], [wheel_name]
                    )
                    self.assertEqual(
                        [path.name for path in output.glob("*.tar.gz")], [sdist_name]
                    )
                    with zipfile.ZipFile(output / wheel_name) as wheel:
                        metadata_files = [
                            name
                            for name in wheel.namelist()
                            if name.endswith(".dist-info/METADATA")
                        ]
                        self.assertEqual(len(metadata_files), 1)
                        metadata = wheel.read(metadata_files[0]).decode("utf-8")
                        self.assertIn(metadata_version, metadata.splitlines())
                    with tarfile.open(output / sdist_name, "r:gz") as sdist:
                        member = sdist.getmember(version_member)
                        self.assertTrue(member.isfile())
                        with sdist.extractfile(member) as contents:
                            self.assertEqual(contents.read(), version_bytes)
                    self.assertEqual(version_file.read_bytes(), version_bytes)


if __name__ == "__main__":
    unittest.main()
