"""Bind documented release pins and producer versions to the committed gitlinks.

The light CI job checks repository files and gitlinks without the producers.
The Python CI job also initializes both submodules and installs pineforge-codegen.
Controls change in-memory inputs to the same checks used for the live checkout.
"""

from dataclasses import dataclass, replace
from importlib import metadata
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
ENGINE = "external/pineforge-engine"
CODEGEN = "external/pineforge-codegen-oss"
README_PIN = re.compile(
    r"^The gitlinks pin the releases this HPO revision targets:\s*"
    r"engine\s+(?P<engine_tag>\S+)\s+\(`(?P<engine_sha>[0-9a-f]{7,40})`,\s*"
    r"C ABI [0-9]+\)\s+and codegen\s+(?P<codegen_tag>\S+)\s+"
    r"\(`(?P<codegen_sha>[0-9a-f]{7,40})`\)\.",
    re.MULTILINE,
)
VERSION_PATTERN = re.compile(
    r"(?P<core>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))"
    r"(?:rc(?P<rc>[1-9][0-9]*))?"
)


@dataclass(frozen=True)
class RepositoryPins:
    version: str
    readme: str
    changelog: str
    gitlinks: dict[str, str]


@dataclass(frozen=True)
class ReleasePins(RepositoryPins):
    engine_version: str
    codegen_version: str


def read_repository_pins() -> RepositoryPins:
    listed = subprocess.run(
        ["git", "ls-tree", "HEAD", "--", ENGINE, CODEGEN],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    gitlinks = {}
    for row in listed.stdout.splitlines():
        match = re.fullmatch(r"160000 commit ([0-9a-f]{40})\t(.+)", row)
        if match is None:
            raise AssertionError(f"expected a mode-160000 gitlink, got {row!r}")
        sha, path = match.groups()
        gitlinks[path] = sha
    if set(gitlinks) != {ENGINE, CODEGEN}:
        raise AssertionError(f"expected both dependency gitlinks, got {gitlinks!r}")
    return RepositoryPins(
        version=(ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        readme=(ROOT / "README.md").read_text(encoding="utf-8"),
        changelog=(ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
        gitlinks=gitlinks,
    )


def read_release_pins() -> ReleasePins:
    pins = read_repository_pins()
    return ReleasePins(
        version=pins.version,
        readme=pins.readme,
        changelog=pins.changelog,
        gitlinks=pins.gitlinks,
        engine_version=(ROOT / ENGINE / "VERSION").read_text(encoding="utf-8").strip(),
        codegen_version=metadata.version("pineforge-codegen"),
    )


def repository_pin_errors(pins: RepositoryPins) -> dict[str, str]:
    errors = {}
    version = VERSION_PATTERN.fullmatch(pins.version)
    if version is None:
        return {"VERSION": f"expected N.N.N or N.N.NrcM, got {pins.version!r}"}
    tag = f"v{version.group('core')}"
    if version.group("rc"):
        tag += f"-rc.{version.group('rc')}"

    def equal(key, actual, expected):
        if actual != expected:
            errors[key] = f"expected {expected!r}, got {actual!r}"

    def prefix(document, path, short):
        sha = pins.gitlinks[path]
        if not sha.startswith(short):
            errors[f"{document} {path}"] = (
                f"{short!r} is not a prefix of gitlink {sha!r}"
            )

    matches = list(README_PIN.finditer(pins.readme))
    if len(matches) != 1:
        errors["README.md pin sentence"] = (
            f"expected one pin sentence, found {len(matches)}"
        )
    else:
        match = matches[0]
        for name, path in (("engine", ENGINE), ("codegen", CODEGEN)):
            equal(f"README.md {name} tag", match.group(f"{name}_tag"), tag)
            prefix("README.md", path, match.group(f"{name}_sha"))

    for section in re.split(r"(?m)^## ", pins.changelog)[1:]:
        heading, _, entry = section.partition("\n")
        if heading.strip() == "Unreleased":
            continue
        equal("CHANGELOG.md release", heading.split(" ", 1)[0], pins.version)
        for path in (ENGINE, CODEGEN):
            shorts = re.findall(
                rf"`{re.escape(path)}`\s+(?:\(\s*)?`([0-9a-f]{{7,40}})`", entry
            )
            if len(shorts) != 1:
                errors[f"CHANGELOG.md {path}"] = (
                    f"expected one top-entry pin, found {len(shorts)}"
                )
            else:
                prefix("CHANGELOG.md", path, shorts[0])
        break
    else:
        errors["CHANGELOG.md release"] = "missing versioned entry"
    return errors


def release_pin_errors(pins: ReleasePins) -> dict[str, str]:
    errors = repository_pin_errors(pins)
    version = VERSION_PATTERN.fullmatch(pins.version)
    if version is None:
        return errors
    tag = f"v{version.group('core')}"
    if version.group("rc"):
        tag += f"-rc.{version.group('rc')}"

    engine_versions = (pins.version,)
    if version.group("rc"):
        engine_versions += (tag.removeprefix("v"), tag)
    if pins.engine_version not in engine_versions:
        errors["engine VERSION"] = (
            f"expected one of {engine_versions!r}, got {pins.engine_version!r}"
        )
    if pins.codegen_version != pins.version:
        errors["pineforge-codegen version"] = (
            f"expected {pins.version!r}, got {pins.codegen_version!r}"
        )
    return errors


class RepositoryReleasePinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pins = read_repository_pins()

    def assert_errors(self, pins, expected_keys):
        errors = repository_pin_errors(pins)
        self.assertEqual(set(errors), set(expected_keys), errors)
        return errors

    def change_readme_field(self, pins, field, value):
        match = README_PIN.search(pins.readme)
        self.assertIsNotNone(match, "README release pin sentence must be present")
        start, end = match.span(field)
        return replace(pins, readme=pins.readme[:start] + value + pins.readme[end:])

    def test_live_release_pins_agree(self):
        self.assert_errors(self.pins, ())

    def test_reader_one_character_readme_typo_is_rejected(self):
        match = README_PIN.search(self.pins.readme)
        self.assertIsNotNone(match, "README release pin sentence must be present")
        for name, path in (("engine", ENGINE), ("codegen", CODEGEN)):
            with self.subTest(dependency=name):
                short = match.group(f"{name}_sha")
                typo = short[:-1] + format((int(short[-1], 16) + 1) % 16, "x")
                changed = self.change_readme_field(self.pins, f"{name}_sha", typo)
                key = f"README.md {path}"
                errors = self.assert_errors(changed, (key,))
                self.assertIn(typo, errors[key])
                self.assertIn(self.pins.gitlinks[path], errors[key])

    def test_codegen_gitlink_change_is_rejected(self):
        sha = self.pins.gitlinks[CODEGEN]
        moved = format((int(sha[0], 16) + 1) % 16, "x") + sha[1:]
        changed = replace(self.pins, gitlinks={**self.pins.gitlinks, CODEGEN: moved})
        errors = self.assert_errors(
            changed, (f"README.md {CODEGEN}", f"CHANGELOG.md {CODEGEN}")
        )
        for error in errors.values():
            self.assertIn(moved, error)

    def test_historical_changelog_pins_cannot_replace_missing_top_pins(self):
        changed = replace(
            self.pins,
            changelog=(
                f"# Changelog\n\n## Unreleased\n\n## {self.pins.version} (2026-10-23)\n"
                "\nNo dependency pins in this entry.\n\n" + self.pins.changelog
            ),
        )
        errors = self.assert_errors(
            changed, (f"CHANGELOG.md {ENGINE}", f"CHANGELOG.md {CODEGEN}")
        )
        for error in errors.values():
            self.assertEqual(error, "expected one top-entry pin, found 0")

    def test_missing_readme_sentence_is_rejected(self):
        changed = replace(
            self.pins,
            readme=self.pins.readme.replace(
                "The gitlinks pin the releases this HPO revision targets:",
                "The dependencies target:",
            ),
        )
        errors = self.assert_errors(changed, ("README.md pin sentence",))
        self.assertEqual(
            errors["README.md pin sentence"], "expected one pin sentence, found 0"
        )


class ReleasePinTests(RepositoryReleasePinTests):
    @classmethod
    def setUpClass(cls):
        cls.pins = read_release_pins()

    def assert_errors(self, pins, expected_keys):
        errors = release_pin_errors(pins)
        self.assertEqual(set(errors), set(expected_keys), errors)
        return errors

    def test_reader_old_engine_gitlink_is_rejected(self):
        old_engine = "dfe5f367e8935ef549ee7a8a38c1d437d1e8e42e"
        changed = replace(
            self.pins,
            gitlinks={**self.pins.gitlinks, ENGINE: old_engine},
            engine_version="1.6.2",
        )
        errors = self.assert_errors(
            changed,
            (f"README.md {ENGINE}", f"CHANGELOG.md {ENGINE}", "engine VERSION"),
        )
        for document in ("README.md", "CHANGELOG.md"):
            self.assertIn(old_engine, errors[f"{document} {ENGINE}"])
        self.assertIn("1.6.2", errors["engine VERSION"])
        self.assertIn(self.pins.version, errors["engine VERSION"])

    def test_final_dependency_versions_must_match_version(self):
        for field, key in (
            ("engine_version", "engine VERSION"),
            ("codegen_version", "pineforge-codegen version"),
        ):
            with self.subTest(field=field):
                changed = replace(self.pins, **{field: self.pins.version + "rc1"})
                errors = self.assert_errors(changed, (key,))
                self.assertIn(self.pins.version + "rc1", errors[key])
                self.assertIn(self.pins.version, errors[key])

    def test_changing_version_requires_docs_and_producer_versions_to_follow(self):
        major = int(self.pins.version.split(".")[0]) + 1
        changed = replace(self.pins, version=f"{major}.0.0")
        self.assert_errors(
            changed,
            (
                "README.md engine tag",
                "README.md codegen tag",
                "CHANGELOG.md release",
                "engine VERSION",
                "pineforge-codegen version",
            ),
        )

    def test_release_candidate_spelling_map(self):
        for version, tag in (
            ("1.6.2rc1", "v1.6.2-rc.1"),
            ("2.10.0rc12", "v2.10.0-rc.12"),
        ):
            pins = self.change_readme_field(self.pins, "engine_tag", tag)
            pins = self.change_readme_field(pins, "codegen_tag", tag)
            pins = replace(
                pins,
                version=version,
                changelog=pins.changelog.replace(
                    f"## {self.pins.version} ", f"## {version} ", 1
                ),
                codegen_version=version,
            )
            for engine_version in (version, tag, tag.removeprefix("v")):
                with self.subTest(version=version, engine_version=engine_version):
                    self.assert_errors(replace(pins, engine_version=engine_version), ())
            self.assert_errors(
                replace(pins, engine_version="1.6.2", codegen_version="1.6.2"),
                ("engine VERSION", "pineforge-codegen version"),
            )
            core, number = version.split("rc")
            wrong_version = f"{core}rc{int(number) + 1}"
            pins = replace(pins, engine_version=version)
            for field, key in (
                ("engine_version", "engine VERSION"),
                ("codegen_version", "pineforge-codegen version"),
            ):
                with self.subTest(version=version, field=field):
                    errors = self.assert_errors(
                        replace(pins, **{field: wrong_version}), (key,)
                    )
                    self.assertIn(wrong_version, errors[key])
