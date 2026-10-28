"""Exercise the configured workflow policy in CI; never run on the owner's Mac."""

from __future__ import annotations

import copy
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = ["python3", "scripts/check_app_token_environments.py"]
ACTION = "actions/create-github-app-token@v2"


def safe_path(path):
    """List inputs and refuse sensitive names before a diagnostic follows them."""
    def candidates():
        yield path.absolute()
        yield path.resolve()

    for candidate in candidates():
        parts = tuple(part.casefold() for part in candidate.parts)
        if any(
            part in (".ssh", ".aws", ".codex") or part.endswith(".env")
            for part in parts
        ) or any(
            parts[index : index + 3] == (".config", "zsh", "keys")
            or parts[index : index + 2] == (".config", "pineforge")
            for index in range(len(parts))
        ):
            raise ValueError(f"refusing prohibited path: {candidate}")
    print(f"Diagnostic path: {candidate}", flush=True)
    return candidate


def workflow(environment="", *, action=ACTION, step=None):
    """Real Actions YAML, with the requested job environment syntax left intact."""
    if step is None:
        step = (
            f"uses: {action}\n"
            "with:\n"
            "  app-id: '123'\n"
            "  private-key: fixture-only-not-a-real-key\n"
        )
    return (
        "name: Fixture\n"
        "on: workflow_dispatch\n"
        "permissions: {}\n"
        "jobs:\n"
        "  mint:\n"
        "    runs-on: ubuntu-24.04\n"
        + textwrap.indent(environment, "    ")
        + "    steps:\n"
        + textwrap.indent("- " + step.replace("\n", "\n  ").rstrip() + "\n", "      ")
    )


def preflight_commands(ci):
    """Read the security command from the real required workflow-lint job."""
    commands = []
    for step in ci["jobs"]["workflow-lint"]["steps"]:
        for line in step.get("run", "").splitlines():
            if line.strip() == " ".join(PREFLIGHT):
                commands.append(PREFLIGHT.copy())
    return commands


class AppTokenEnvironmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The post-merge workflow holds the required workflow-lint job and its
        # build-gate aggregate; ci.yml keeps only authz.
        cls.ci = yaml.load(
            safe_path(ROOT / ".github/workflows/ci-post.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        cls.admitted = yaml.load(
            safe_path(ROOT / ".github/workflows/gke-pr.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        # The existing, checksum-pinned actionlint step supplies this binary.
        # No replacement linter or stand-in policy is used for the main trace.
        cls.actionlint = safe_path(Path(os.environ["ACTIONLINT"]))

    def check(self, sources, *, ci=None, linter_may_reject=False):
        """Run main's actual linter, then any configured environment preflight.

        linter_may_reject is for fixtures whose syntax actionlint itself refuses (an empty
        environment name): the preflight must still refuse them on its own, so the linter
        result is dropped for those fixtures instead of being counted as the refusal.
        """
        ci = self.ci if ci is None else ci
        directory = safe_path(
            Path(
                tempfile.mkdtemp(
                    prefix="app-token-environments-", dir=os.environ["RUNNER_TEMP"]
                )
            )
        )
        config = safe_path(directory / "actionlint.config")
        config.write_text("{}\n")
        fixtures = safe_path(directory / "workflows")
        fixtures.mkdir()
        for name, source in sources.items():
            safe_path(fixtures / name).write_text(source)
        results = []
        for name, source in sources.items():
            print(f"actionlint stdin from fixture: {name}", flush=True)
            lint = subprocess.run(
                [
                    str(self.actionlint),
                    "-config-file",
                    str(config),
                    "-shellcheck=",
                    "-pyflakes=",
                    "-no-color",
                    "-",
                ],
                input=source,
                capture_output=True,
                text=True,
                check=False,
                cwd=directory,
            )
            # A YAML/schema error must never stand in for the security refusal.
            if linter_may_reject and lint.returncode != 0:
                continue
            self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
            results.append(lint)
        for command in preflight_commands(ci):
            script = safe_path(ROOT / command[1])
            print(
                "preflight fixture files: "
                + ", ".join(str(fixtures / name) for name in sources),
                flush=True,
            )
            results.append(
                subprocess.run(
                    [command[0], str(script), "--workflows-dir", str(fixtures)],
                    capture_output=True,
                    text=True,
                    check=False,
                    cwd=directory,
                )
            )
        # Without a preflight, these are the actual actionlint results.
        # This is main's existing policy, not a stub of the new checker.
        return results

    def assert_refused(self, results, name="release.yml", reason="literal environment"):
        failures = [result for result in results if result.returncode != 0]
        self.assertEqual(len(failures), 1, [r.stdout + r.stderr for r in results])
        result = failures[0]
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"{name}: job mint:", result.stderr)
        self.assertIn(reason, result.stderr)

    def assert_accepted(self, results):
        for result in results:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_environment_is_refused_at_any_action_ref(self):
        for reference in ("v1", "v2", "main", "a" * 40):
            with self.subTest(reference=reference):
                source = workflow(action=f"actions/create-github-app-token@{reference}")
                self.assert_refused(self.check({"release.yml": source}))

    def test_literal_environment_and_mapping_name_are_accepted(self):
        for environment in (
            "environment: release\n",
            "environment: release-bot\n",
            "environment:\n  name: release\n",
            '"environment": {name: release, url: "https://example.com"}\n',
        ):
            with self.subTest(environment=environment):
                self.assert_accepted(self.check({"release.yml": workflow(environment)}))

    def test_empty_environment_is_refused(self):
        for environment in (
            "environment: ''\n",
            "environment: '   '\n",
            "environment: {name: ''}\n",
        ):
            with self.subTest(environment=environment):
                self.assert_refused(
                    self.check(
                        {"release.yml": workflow(environment)}, linter_may_reject=True
                    )
                )

    def test_expression_environment_is_refused(self):
        for environment in (
            "environment: ${{ github.ref_name }}\n",
            "environment: release-${{ github.ref_name }}\n",
            "environment: {name: '${{ github.ref_name }}'}\n",
        ):
            with self.subTest(environment=environment):
                self.assert_refused(self.check({"release.yml": workflow(environment)}))

    def test_minting_job_in_second_workflow_is_refused(self):
        sources = {
            "release.yml": workflow("environment: release\n"),
            "second.yaml": workflow(),
        }
        self.assert_refused(self.check(sources), name="second.yaml")

    def test_quoted_keys_and_flow_style_cannot_hide_minting(self):
        sources = (
            workflow('"environment": ""\n'),
            (
                "name: Fixture\n"
                "on: workflow_dispatch\n"
                '"jobs": {"mint": {"runs-on": ubuntu-24.04, "steps": '
                '[{"uses": "actions/create-github-app-token@v2", "with": '
                '{"app-id": "123", "private-key": "fixture-key"}}]}}\n'
            ),
        )
        for source in sources:
            with self.subTest(source=source):
                self.assert_refused(
                    self.check({"release.yml": source}, linter_may_reject=True)
                )

    def test_private_key_secret_and_script_minting_are_refused(self):
        steps = (
            "uses: example/token@v1\n"
            "with:\n  private-key: ${{ secrets.PINEFORGE_APP_PRIVATE_KEY }}\n",
            "run: echo mint\n"
            "env:\n  KEY: ${{ secrets['PINEFORGE_APP_PRIVATE_KEY'] }}\n",
            "uses: example/token@v1\n"
            "with:\n  private-key: ${{ secrets.SIGNING_MATERIAL }}\n",
            'run: gh api --method POST "/app/installations/123/access_tokens"\n',
            "run: |\n  node -e 'createAppAuth({appId: 123})'\n",
        )
        for step in steps:
            with self.subTest(step=step):
                self.assert_refused(
                    self.check({"release.yml": workflow(step=step)})
                )
                self.assert_accepted(
                    self.check(
                        {"release.yml": workflow("environment: release\n", step=step)}
                    )
                )

    def test_every_job_is_checked_and_job_environment_is_not_step_environment(self):
        source = workflow("environment: release\n")
        source += (
            "  second:\n"
            "    runs-on: ubuntu-24.04\n"
            "    steps:\n"
            "      - run: echo mint\n"
            "        env:\n"
            "          environment: release\n"
            "          KEY: ${{ secrets.PINEFORGE_APP_PRIVATE_KEY }}\n"
        )
        results = self.check({"release.yml": source})
        failures = [result for result in results if result.returncode != 0]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].returncode, 1)
        self.assertIn("release.yml: job second:", failures[0].stderr)
        self.assertIn("literal environment", failures[0].stderr)

    def test_non_minting_job_does_not_need_an_environment(self):
        self.assert_accepted(
            self.check({"ordinary.yml": workflow(step="run: echo ordinary\n")})
        )

    def test_preflight_is_enabled_in_the_required_lint_job(self):
        self.assertEqual(preflight_commands(self.ci), [PREFLIGHT])
        jobs = self.ci["jobs"]
        self.assertIn("workflow-lint", jobs["build-gate"]["needs"])
        self.assertNotIn("if", jobs["workflow-lint"])

    def test_preflight_is_enabled_in_the_admitted_lint_job(self):
        commands = []
        for step in self.admitted["jobs"]["lint"]["steps"]:
            for line in step.get("run", "").splitlines():
                if line.strip() == " ".join(PREFLIGHT):
                    commands.append(PREFLIGHT.copy())
        self.assertEqual(commands, [PREFLIGHT])
        self.assertIn("lint", self.admitted["jobs"]["result"]["needs"])
        self.assertNotIn("workflow-lint", self.admitted["jobs"])

    def test_disabling_the_preflight_breaks_the_refusal_assertion(self):
        disabled = copy.deepcopy(self.ci)
        for step in disabled["jobs"]["workflow-lint"]["steps"]:
            if "run" in step:
                step["run"] = "\n".join(
                    line
                    for line in step["run"].splitlines()
                    if line.strip() != " ".join(PREFLIGHT)
                )
        self.assertEqual(preflight_commands(disabled), [])
        results = self.check({"release.yml": workflow()}, ci=disabled)
        self.assert_accepted(results)
        with self.assertRaises(AssertionError):
            self.assert_refused(results)

    def test_notify_hub_uses_the_reviewed_release_environment(self):
        release = yaml.load(
            safe_path(ROOT / ".github/workflows/release.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(release["jobs"]["notify-hub"].get("environment"), "release")
        self.assertNotIn("environment", release["jobs"]["package"])


if __name__ == "__main__":
    unittest.main()
