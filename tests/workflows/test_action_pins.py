"""Require immutable action references throughout the GitHub YAML tree."""

from collections.abc import Iterator
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
# Recounted at 243637e: ci-post 14, gke-pr 16, release 6, docs 4, benchmark-post 4.
MIN_USES = 44

QUOTED = r'"(?:\\.|[^"\\])*"' + r"|'(?:''|[^'])*'"
# Skip quoted text and comments before looking for the next mapping key.
KEY_OR_TOKEN = re.compile(
    r"(?P<uses>(?:^[ \t]*(?:-[ \t]+)?|(?P<flow>(?<=[{,\[])[ \t]*))"
    r"""(?:uses|"uses"|'uses')[ \t]*:)"""
    rf"|{QUOTED}|(?P<comment>(?<!\S)#[^\r\n]*)"
)
# A hash starts a comment only after whitespace and outside a quoted scalar.
BLOCK_SCALAR = re.compile(rf"(?:{QUOTED}|" r"""[^#"']|(?<!\s)#)*""")
FLOW_SCALAR = re.compile(rf"(?:{QUOTED}|" r"""[^#,\]}"']|(?<!\s)#)*""")
REPOSITORY_PIN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+(?:/[^\s@]+)?@[0-9a-f]{40}"
)
LOCAL_ACTION = re.compile(r"\./[^\s@]+")
DOCKER_PIN = re.compile(r"docker://[^\s@]+@sha256:[0-9a-fA-F]{64}")


def uses_entries(text: str) -> Iterator[tuple[int, str]]:
    """Yield literal uses values, including empty ones, with one-based lines."""
    for number, line in enumerate(text.splitlines(), 1):
        position = 0
        while token := KEY_OR_TOKEN.search(line, position):
            position = token.end()
            if token.group("comment") is not None:
                break
            if token.group("uses") is None:
                continue
            pattern = FLOW_SCALAR if token.group("flow") is not None else BLOCK_SCALAR
            scalar = pattern.match(line, position)
            assert scalar is not None  # The scalar pattern also matches empty values.
            position = scalar.end()
            reference = scalar.group().strip()
            if re.fullmatch(QUOTED, reference):
                reference = reference[1:-1]
            yield number, reference


def scan_action_pins(name: str, text: str) -> tuple[int, list[str]]:
    """Pure text validator returning the number of uses entries and findings."""
    count = 0
    findings = []
    for number, reference in uses_entries(text):
        count += 1
        if reference.startswith("./"):
            pattern = LOCAL_ACTION
        elif reference.startswith("docker://"):
            pattern = DOCKER_PIN
        else:
            pattern = REPOSITORY_PIN
        if pattern.fullmatch(reference) is None:
            findings.append(f"{name}:{number}: unpinned uses: {reference!r}")
    return count, findings


def checked_path(path: Path) -> Path:
    """Refuse protected names and links before following a diagnostic input."""
    parts = tuple(part.casefold() for part in path.parts)
    if any(
        part in (".ssh", ".aws", ".codex") or part.endswith(".env")
        for part in parts
    ) or any(
        parts[index : index + 3] == (".config", "zsh", "keys")
        or parts[index : index + 2] == (".config", "pineforge")
        for index in range(len(parts))
    ):
        raise ValueError(f"refusing prohibited path: {path}")
    # Check parents first so even metadata checks cannot follow a linked parent.
    for candidate in reversed((path, *path.parents)):
        if candidate.is_symlink():
            raise ValueError(f"refusing symbolic link: {candidate}")
    return path


def github_yaml_files(directory: Path) -> list[Path]:
    """Walk every directory, including composite actions, without ignore rules."""
    checked_path(directory)
    paths = []
    for path in sorted(directory.iterdir()):
        checked_path(path)
        if path.is_dir():
            paths.extend(github_yaml_files(path))
        elif path.suffix in (".yml", ".yaml"):
            paths.append(path)
    return paths


class ActionPinScannerTest(unittest.TestCase):
    def test_invalid_reference_controls(self) -> None:
        sha = "0123456789abcdef" * 2 + "01234567"
        controls = (
            ("tag", "uses: example/action@v4"),
            ("branch", "- uses: example/action@main"),
            ("short-sha", "uses: example/action@abc1234"),
            ("uppercase-sha", f"uses: example/action@{sha.upper()}"),
            ("39-hex", f"uses: example/action@{sha[:-1]}"),
            ("41-hex", f"uses: example/action@{sha}0"),
            ("single-quoted-tag", "uses: 'example/action@v4' # version"),
            ("double-quoted-tag", 'uses: "example/action@v4"'),
            ("flow-tag", "- {uses: example/action@v4}"),
            ("quoted-flow-tag", '- {"uses": "example/action@v4"}'),
            ("docker-tag", "uses: docker://example/image:latest"),
            ("docker-short-digest", "uses: docker://x@sha256:abc1234"),
            ("local-with-ref", f"uses: ./local@{sha}"),
            ("missing-ref", "uses: example/action"),
            ("empty", "uses: # missing reference"),
            ("attached-hash", f"uses: example/action@{sha}#version"),
            ("quoted-hash", f'uses: "example/action@{sha} #version"'),
            ("quoted-suffix", f'uses: "example/action@{sha}" extra'),
            ("block-comma-suffix", f"uses: example/action@{sha},tag"),
            ("block-brace-suffix", f"uses: example/action@{sha}}}"),
        )
        name = ".github/actions/control/action.yaml"
        for case, text in controls:
            with self.subTest(case=case):
                count, findings = scan_action_pins(name, "\n" + text + "\n")
                self.assertEqual(count, 1)
                self.assertEqual(len(findings), 1, findings)
                self.assertTrue(findings[0].startswith(f"{name}:2:"), findings)

    def test_valid_reference_controls(self) -> None:
        sha = "0123456789abcdef" * 2 + "01234567"
        digest = "0123456789abcdef" * 4
        controls = (
            ("sha", f"uses: example/action@{sha}"),
            ("comment", f"- uses: example/action@{sha} # v4.1.0"),
            ("single-quoted", f"uses: 'example/action@{sha}' # v4"),
            ("double-quoted", f'uses: "example/action@{sha}"'),
            ("flow", f"- {{uses: example/action@{sha}, name: pinned}}"),
            ("quoted-flow", f'- {{"uses": "example/action@{sha}"}}'),
            ("reusable", f"uses: example/repo/.github/workflows/reuse.yml@{sha}"),
            ("local", "uses: ./local"),
            ("docker-digest", f"uses: docker://x@sha256:{digest}"),
            ("docker-uppercase-digest", f"uses: docker://x@sha256:{digest.upper()}"),
        )
        for case, text in controls:
            with self.subTest(case=case):
                self.assertEqual(
                    scan_action_pins(".github/control.yml", "\n" + text + "\n"),
                    (1, []),
                )

    def test_comments_and_quoted_text_are_not_entries(self) -> None:
        text = (
            "# uses: example/action@v4\n"
            'name: "uses: example/action@v4"\n'
            "name: 'uses: example/action@main'\n"
            "name: ordinary # uses: example/action@v4\n"
        )
        self.assertEqual(scan_action_pins(".github/control.yaml", text), (0, []))
        self.assertEqual(scan_action_pins(".github/empty.yaml", ""), (0, []))

    def test_every_flow_entry_is_checked(self) -> None:
        name = ".github/actions/nested/action.yaml"
        text = (
            "\nsteps: [{uses: example/first@v4}, "
            '{"uses": "example/second@main"}] # uses: ignored/action@v4\n'
        )
        self.assertEqual(
            scan_action_pins(name, text),
            (
                2,
                [
                    f"{name}:2: unpinned uses: 'example/first@v4'",
                    f"{name}:2: unpinned uses: 'example/second@main'",
                ],
            ),
        )


class ActionPinTreeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        paths = github_yaml_files(ROOT / ".github")
        print("Action pin input files:\n" + "\n".join(map(str, paths)), flush=True)
        cls.sources = {
            path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in paths
        }

    def test_repository_action_pins(self) -> None:
        count = 0
        findings = []
        for name, text in self.sources.items():
            seen, problems = scan_action_pins(name, text)
            count += seen
            findings.extend(problems)
        self.assertEqual(findings, [], "Unpinned action references:\n" + "\n".join(findings))
        self.assertGreaterEqual(
            count, MIN_USES, f"Only {count} uses entries found; expected at least {MIN_USES}"
        )

    def test_real_workflow_tag_mutation_is_refused(self) -> None:
        candidates = [
            (name, text, number, reference)
            for name, text in self.sources.items()
            if name.startswith(".github/workflows/")
            for number, reference in uses_entries(text)
            if REPOSITORY_PIN.fullmatch(reference)
        ]
        self.assertTrue(candidates, "No real workflow reference available for mutation")
        name, text, number, reference = candidates[0]
        original_count, original_findings = scan_action_pins(name, text)
        self.assertEqual(original_findings, [])
        tagged = reference.rsplit("@", 1)[0] + "@v4"
        lines = text.splitlines(keepends=True)
        lines[number - 1] = lines[number - 1].replace(reference, tagged, 1)
        mutated = "".join(lines)
        self.assertNotEqual(mutated, text)
        count, findings = scan_action_pins(name, mutated)
        self.assertEqual(count, original_count)
        self.assertEqual(findings, [f"{name}:{number}: unpinned uses: {tagged!r}"])


if __name__ == "__main__":
    unittest.main()
