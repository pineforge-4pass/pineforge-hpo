"""Guard the default fake's source and defined dynamic exports; run only on CI/the box."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


DENIED = (
    "pf_selected_window_version",
    "strategy_set_selected_window_v1",
    "strategy_selected_window_counts_v1",
    "pf_execution_observer_version",
    "strategy_set_execution_observer_v1",
    "strategy_execution_observation_v1",
)
OPT_IN = "PINEFORGE_HPO_FAKE_SELECTED"
DEFINITION = re.compile(
    r"\b(" + "|".join(DENIED) + r")\s*\([^;{}]*\)\s*(?:noexcept\s*)?\{"
)
CPP_NONCODE = re.compile(
    r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\[^\n]|[^\'\\\n])\'', re.DOTALL
)


def checked_path(value: str) -> Path:
    """Refuse sensitive names before following links, and again on the resolved path."""
    path = Path(value).expanduser().absolute()

    def refuse_named(candidate: Path) -> None:
        parts = candidate.parts
        if (
            any(part in {".ssh", ".aws", ".codex"} or part.endswith(".env") for part in parts)
            or (".config", "pineforge") in tuple(zip(parts, parts[1:]))
            or (".config", "zsh", "keys") in tuple(zip(parts, parts[1:], parts[2:]))
        ):
            raise RuntimeError(f"refused sensitive path by name: {candidate}")

    refuse_named(path)
    resolved = path.resolve(strict=True)
    refuse_named(resolved)
    if not resolved.is_file():
        raise RuntimeError(f"not a regular inspection input: {path}")
    return resolved


def source_findings(text: str) -> list[str]:
    """Require a positive opt-in branch around each protocol definition and local source include."""
    clean = CPP_NONCODE.sub(lambda match: re.sub(r"[^\n]", " ", match.group()), text)
    protected = False
    stack: list[bool] = []
    states = []
    findings = []
    original_lines = text.splitlines()
    for number, line in enumerate(clean.splitlines(), 1):
        states.append(protected)
        directive = re.match(r"\s*#\s*(if|ifdef|ifndef|elif|else|endif|define|include)\b(.*)", line)
        if not directive:
            continue
        command, expression = directive.groups()
        expression = expression.strip()
        positive = expression in {OPT_IN, f"defined({OPT_IN})", f"defined {OPT_IN}"}
        if command in {"if", "ifdef", "ifndef"}:
            stack.append(protected)
            protected = protected or (positive and command != "ifndef")
        elif command == "elif":
            if not stack:
                raise RuntimeError("unbalanced #elif")
            protected = stack[-1] or positive
        elif command == "else":
            if not stack:
                raise RuntimeError("unbalanced #else")
            protected = stack[-1]
        elif command == "endif":
            if not stack:
                raise RuntimeError("unbalanced #endif")
            protected = stack.pop()
        elif command == "define" and expression.split()[:1] == [OPT_IN]:
            findings.append(f"{number}: source must not define its own opt-in")
        elif command == "include":
            local = re.search(r'#\s*include\s*"([^"]+)"', original_lines[number - 1])
            if local and (not protected or local.group(1) != "test_selected_evaluation.cpp"):
                findings.append(f"{number}: unguarded or uninspected local include")
    if stack:
        raise RuntimeError("unbalanced source conditionals")
    for match in DEFINITION.finditer(clean):
        line = clean.count("\n", 0, match.start())
        if not states[line]:
            findings.append(f"{line + 1}: unguarded definition of {match.group(1)}")
    return findings


def defined_symbols(output: str, darwin: bool) -> set[str]:
    """Normalize defined nm rows; do not treat an undefined reference as an export."""
    names = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[-2].upper() == "U":
            continue
        name = fields[-1].split("@", 1)[0]
        if darwin and name.startswith("_"):
            name = name[1:]
        names.add(name)
    return names


def scanner_controls() -> None:
    """Literal negative/positive controls prevent a guard that silently matches nothing."""
    for name in DENIED:
        definition = f"unsigned {name}(void) {{ return 1; }}\n"
        if not source_findings(definition):
            raise RuntimeError(f"false pass: source scanner missed {name}")
        if source_findings(f"#if defined({OPT_IN})\n{definition}#endif\n"):
            raise RuntimeError(f"source scanner rejected an opt-in definition of {name}")
        if not source_findings(f"#if defined({OPT_IN})\n#else\n{definition}#endif\n"):
            raise RuntimeError(f"false pass: source scanner trusted the opt-in's else: {name}")
    if not source_findings('#include "test_selected_evaluation.cpp"\n'):
        raise RuntimeError("false pass: source scanner allowed an unguarded fixture include")
    if not source_findings(f"#define {OPT_IN} 1\n"):
        raise RuntimeError("false pass: source scanner allowed a self-enabled opt-in")
    for name in DENIED:
        linux = defined_symbols(f"0000 T {name}\n U strategy_create\n", False)
        darwin = defined_symbols(f"0000 T _{name}\n U _strategy_create\n", True)
        if linux != {name} or darwin != linux:
            raise RuntimeError("false pass: defined-symbol normalization missed a denied export")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--plugin", required=True)
    arguments = parser.parse_args()
    # Enumerate every file this inspection follows before opening any of them. No recursive
    # include traversal, environment files, home configuration or system temporary files.
    for value in [*arguments.source, arguments.plugin, __file__, sys.executable]:
        print(f"will inspect: {value}", flush=True)
    sources = [checked_path(value) for value in arguments.source]
    plugin = checked_path(arguments.plugin)
    script = checked_path(__file__)
    interpreter = checked_path(sys.executable)
    expected_sources = {"test_engine_adapter.cpp", "test_selected_evaluation.cpp"}
    if {path.name for path in sources} != expected_sources:
        raise RuntimeError("both explicit fake source files are required")
    scanner_controls()
    # This script is itself the CTest work: an unhandled crash must have its own nonzero status.
    # Judge the child's actual return code and exception, never an exit marker or wrapper echo.
    crash = subprocess.run(
        [str(interpreter), "-I", str(script), "--deliberate-crash"],
        text=True, capture_output=True,
    )
    if crash.returncode != 1 or "deliberate export-guard crash" not in crash.stderr:
        raise RuntimeError("false pass: deliberate crash did not propagate the work's exit status")
    print("deliberate-crash row: returncode=1, verdict=expected failure", flush=True)
    for path in sources:
        findings = source_findings(path.read_text(encoding="utf-8"))
        if findings:
            raise RuntimeError(f"{path}:\n" + "\n".join(findings))
    darwin = sys.platform == "darwin"
    command = ["nm", "-gU"] if darwin else ["nm", "-D", "--defined-only"]
    result = subprocess.run([*command, str(plugin)], check=True, text=True, capture_output=True)
    symbols = defined_symbols(result.stdout, darwin)
    if not {"strategy_create", "run_backtest_full"}.issubset(symbols):
        raise RuntimeError("false pass: nm did not report the default fake's required exports")
    forbidden = sorted(symbols.intersection(DENIED))
    if forbidden:
        raise RuntimeError("default fake defines selected exports: " + ", ".join(forbidden))
    print("default fake source and defined-export guards passed")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--deliberate-crash"]:
        raise RuntimeError("deliberate export-guard crash")
    sys.exit(main())
