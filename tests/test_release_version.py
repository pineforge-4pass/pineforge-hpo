"""Exact release-version parsing without an engine or compiler."""

from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "cmake/PineForgeHpoVersion.cmake"
INVALID_VERSIONS = (
    "v1.6.0",
    "v1.6.0rc1",
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


def parse_source(version: str) -> str:
    return (
        "cmake_minimum_required(VERSION 3.17)\n"
        f"include([==[{MODULE.as_posix()}]==])\n"
        f"set(PINEFORGE_HPO_VERSION [==[{version}]==])\n"
        "pineforge_hpo_parse_version(\"${PINEFORGE_HPO_VERSION}\" core suffix)\n"
    )


class CMakeReleaseVersionTests(unittest.TestCase):
    def test_script_reports_exact_core_suffix_and_full_version(self):
        for version, expected in (
            ("1.6.0rc1", "1.6.0\nrc1\n1.6.0rc1\n"),
            ("1.6.0", "1.6.0\n\n1.6.0\n"),
            ("0.15.0", "0.15.0\n\n0.15.0\n"),
            ("0.0.0rc1", "0.0.0\nrc1\n0.0.0rc1\n"),
            ("12.34.56rc789", "12.34.56\nrc789\n12.34.56rc789\n"),
        ):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                driver = directory / "driver.cmake"
                driver.write_text(
                    parse_source(version)
                    + 'file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/values.txt" '
                    + '"${core}\\n${suffix}\\n${PINEFORGE_HPO_VERSION}\\n")\n',
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    ["cmake", "-P", str(driver)],
                    cwd=directory,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual((directory / "values.txt").read_text(), expected)

    def test_script_rejects_every_malformed_version_with_accepted_forms(self):
        for version in INVALID_VERSIONS:
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                driver = directory / "driver.cmake"
                driver.write_text(parse_source(version), encoding="utf-8")
                completed = subprocess.run(
                    ["cmake", "-P", str(driver)],
                    cwd=directory,
                    text=True,
                    capture_output=True,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("Invalid VERSION", completed.stderr)
                self.assertIn("N.N.N", completed.stderr)
                self.assertIn("N.N.NrcM", completed.stderr)

    def test_project_uses_numeric_core_and_preserves_full_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            build = directory / "build"
            (directory / "CMakeLists.txt").write_text(
                parse_source("1.6.0rc1")
                + 'project(VersionProbe VERSION "${core}" LANGUAGES NONE)\n'
                + 'file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/values.txt" '
                + '"${CMAKE_PROJECT_VERSION}\\n${PINEFORGE_HPO_VERSION}\\n")\n',
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["cmake", "-S", str(directory), "-B", str(build)],
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                (build / "values.txt").read_text(), "1.6.0\n1.6.0rc1\n"
            )

    def test_final_clears_suffix_after_parsing_an_rc(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            driver = directory / "driver.cmake"
            driver.write_text(
                parse_source("1.6.0rc1")
                + 'pineforge_hpo_parse_version("1.6.0" core suffix)\n'
                + 'file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/values.txt" '
                + '"${core}\\n${suffix}\\n${PINEFORGE_HPO_VERSION}\\n")\n',
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["cmake", "-P", str(driver)],
                cwd=directory,
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                (directory / "values.txt").read_text(), "1.6.0\n\n1.6.0rc1\n"
            )


if __name__ == "__main__":
    unittest.main()
