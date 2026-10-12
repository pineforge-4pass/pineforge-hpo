"""Keep test, configure and CI waits free of time limits below the CI platform bound."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
CMAKE_LIMIT = re.compile(r"\b" + "TIME" + "OUT" + r"\s+[0-9]")
WORKFLOW_LIMIT = re.compile("timeout" + r"-minutes\s*:\s*([0-9]+)\b")
PYTHON_LIMIT = re.compile(r"\b" + "time" + "out" + r"\s*=\s*[0-9]")

# Any future exception must name a file and explain the ordering event still needed.
# Exceptions are printed by the repository check so they remain visible in its output.
ALLOWED_TEST_FILES: dict[str, str] = {}


def scan_limits(name: str, text: str) -> list[str]:
    """Return every prohibited limit with its file and one-based line number."""
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        if name.endswith("CMakeLists.txt") or name.endswith(".cmake"):
            if CMAKE_LIMIT.search(line):
                findings.append(f"{name}:{number}: CMake time limit")
        elif name.startswith(".github/workflows/") and name.endswith(".yml"):
            match = WORKFLOW_LIMIT.search(line)
            if match and int(match.group(1)) < 360:
                findings.append(f"{name}:{number}: CI job bound below 360 minutes")
        elif name.startswith("tests/") and name.endswith(".py"):
            if PYTHON_LIMIT.search(line):
                findings.append(f"{name}:{number}: Python wait time limit")
    return findings


class NoTimeLimitsTest(unittest.TestCase):
    def test_repository_has_no_time_limits(self) -> None:
        paths = [ROOT / "CMakeLists.txt", ROOT / "tests/CMakeLists.txt"]
        paths.extend(sorted((ROOT / "cmake").glob("*.cmake")))
        paths.extend(sorted((ROOT / ".github/workflows").glob("*.yml")))
        paths.extend(sorted((ROOT / "tests").glob("*.py")))
        findings = []
        for path in paths:
            name = path.relative_to(ROOT).as_posix()
            if path == Path(__file__).resolve():
                continue
            if name in ALLOWED_TEST_FILES:
                print(
                    f"event-gate needed: {name}: {ALLOWED_TEST_FILES[name]}", flush=True
                )
                continue
            findings.extend(scan_limits(name, path.read_text()))
        self.assertEqual(
            findings, [], "Prohibited time limits:\n" + "\n".join(findings)
        )

    def test_scanner_controls(self) -> None:
        cmake_keyword = "TIME" + "OUT"
        workflow_key = "timeout" + "-minutes"
        python_keyword = "time" + "out"
        controls = (
            (
                "tests/CMakeLists.txt",
                f"set_tests_properties(t PROPERTIES {cmake_keyword} 5)",
            ),
            (
                "cmake/probe.cmake",
                f"execute_process(COMMAND cc --version {cmake_keyword} 30)",
            ),
            ("CMakeLists.txt", f"execute_process(COMMAND cc {cmake_keyword} 5)"),
            (".github/workflows/control.yml", f"  {workflow_key}: 359"),
            ("tests/control.py", f"subprocess.run(command, {python_keyword}=5)"),
            ("tests/control.py", f"    {python_keyword} = 5,"),
        )
        for name, text in controls:
            with self.subTest(name=name, text=text):
                findings = scan_limits(name, "\n" + text)
                self.assertEqual(len(findings), 1)
                self.assertTrue(findings[0].startswith(f"{name}:2:"), findings)
        clean_controls = (
            (
                "tests/CMakeLists.txt",
                "set_tests_properties(t PROPERTIES RUN_SERIAL TRUE)",
            ),
            ("cmake/probe.cmake", "execute_process(COMMAND cc --version)"),
            ("CMakeLists.txt", "project(example)"),
            (".github/workflows/control.yml", f"  {workflow_key}: 360"),
            (".github/workflows/control.yml", f"  {workflow_key}: 720"),
            ("tests/control.py", "child.communicate()\nreader.join()"),
            ("tests/control.py", 'command = ["--trial-timeout-seconds", "0.2"]'),
            ("tests/control.py", 'status = "trial_timeout"'),
        )
        for name, text in clean_controls:
            with self.subTest(name=name, text=text):
                self.assertEqual(scan_limits(name, text), [])


if __name__ == "__main__":
    unittest.main()
