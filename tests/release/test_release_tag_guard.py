"""Execute the shipped tag guard against real git in CI/cloud, never on the Mac."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/release.yml"
TAG_PATTERN = r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(rc[1-9][0-9]*)?$"
PATTERN_BLOCK = (
    f'if [[ ! "$TAG" =~ {TAG_PATTERN} ]]; then\n'
    "  echo '::error::Tag must use vN.N.N or vN.N.NrcM, no leading zeros, M >= 1.'\n"
    "  exit 1\n"
    "fi\n"
)
EXISTENCE_BLOCK = (
    'if ! TAG_COMMIT="$(git rev-parse -q --verify '
    '"refs/tags/${TAG}^{commit}")"; then\n'
    "  echo '::error::Release tag does not exist or does not peel to a commit.'\n"
    "  exit 1\n"
    "fi\n"
)
ANCESTRY_BLOCK = """if ! git merge-base --is-ancestor "$TAG_COMMIT" refs/remotes/origin/main; then
  echo '::error::Release tag commit is not reachable from origin/main.'
  exit 1
fi
"""
MAIN_CHECK = (
    'if [[ "$WORKFLOW_REF" != refs/heads/main || '
    '"$WORKFLOW_EVENT" != workflow_dispatch ]]; then'
)
SHA_REF = "${{ needs.tag-guard.outputs.sha }}"


def parse_workflow(text):
    return yaml.load(text, Loader=yaml.BaseLoader)


def step_with_id(workflow, job_id, step_id):
    steps = workflow.get("jobs", {}).get(job_id, {}).get("steps", [])
    matching = [step for step in steps if step.get("id") == step_id]
    if len(matching) != 1:
        raise AssertionError(f"expected one {job_id}/{step_id} step")
    return matching[0]


def replace_literal(text, old, new, *, count=1):
    if text.count(old) != count:
        raise AssertionError(f"stale workflow mutation: {old!r}")
    changed = text.replace(old, new)
    if changed == text:
        raise AssertionError("workflow mutation did not change the real text")
    return changed


def run_block(block):
    return textwrap.indent(block, " " * 10)


class GuardWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()

    def assert_contract(self, text):
        workflow = parse_workflow(text)
        triggers = workflow.get("on", {})
        self.assertEqual(set(triggers), {"workflow_dispatch"})
        inputs = (triggers.get("workflow_dispatch") or {}).get("inputs", {})
        self.assertEqual(inputs.get("tag", {}).get("required"), "true")
        self.assertEqual(inputs.get("tag", {}).get("type"), "string")
        package_input = inputs.get("package", {})
        self.assertEqual(package_input.get("type"), "boolean")
        self.assertEqual(package_input.get("default"), "false")
        self.assertNotIn("github.event.release.", text)
        self.assertEqual(
            workflow.get("concurrency", {}).get("group"),
            "release-assets-${{ inputs.tag }}",
        )
        jobs = workflow.get("jobs", {})
        self.assertEqual(set(jobs), {"tag-guard", "package", "notify-hub"})
        self.assertEqual(next(iter(jobs)), "tag-guard")
        guard = jobs.get("tag-guard", {})
        self.assertNotIn("if", guard)
        self.assertNotIn("continue-on-error", guard)
        self.assertEqual(guard.get("permissions"), {"contents": "read"})
        self.assertNotIn("secrets.", yaml.dump(guard))
        self.assertEqual(
            guard.get("outputs"),
            {"sha": "${{ steps.verify.outputs.sha }}"},
        )
        steps = guard.get("steps", [])
        self.assertEqual(len(steps), 3)
        self.assertIn(MAIN_CHECK, steps[0].get("run", ""))
        self.assertEqual(
            steps[0].get("env"),
            {
                "WORKFLOW_REF": "${{ github.ref }}",
                "WORKFLOW_EVENT": "${{ github.event_name }}",
            },
        )
        self.assertTrue(steps[1].get("uses", "").startswith("actions/checkout@"))
        self.assertEqual(steps[1].get("with", {}).get("ref"), "main")
        self.assertEqual(steps[1].get("with", {}).get("fetch-depth"), "0")
        verify = step_with_id(workflow, "tag-guard", "verify")
        self.assertEqual(steps[2], verify)
        self.assertEqual(
            verify.get("env"),
            {
                "TAG": "${{ inputs.tag }}",
                "WORKFLOW_REF": "${{ github.ref }}",
                "WORKFLOW_EVENT": "${{ github.event_name }}",
            },
        )
        script = verify.get("run", "")
        self.assertIn(MAIN_CHECK, script)
        for block in (PATTERN_BLOCK, EXISTENCE_BLOCK, ANCESTRY_BLOCK):
            self.assertIn(block, script)
        self.assertLess(script.index(PATTERN_BLOCK), script.index("git "))
        self.assertIn("git fetch --tags", script)
        self.assertIn("+refs/heads/main:refs/remotes/origin/main", script)
        self.assertIn('[[ ! "$TAG_COMMIT" =~ ^[0-9a-f]{40}$ ]]', script)
        output = 'printf \'sha=%s\\n\' "$TAG_COMMIT" >> "$GITHUB_OUTPUT"'
        self.assertIn(output, script)
        self.assertLess(script.index(EXISTENCE_BLOCK), script.index(ANCESTRY_BLOCK))
        self.assertLess(script.index(ANCESTRY_BLOCK), script.index(output))
        for job_id, job in jobs.items():
            if job_id != "tag-guard":
                needs = job.get("needs", [])
                needs = [needs] if isinstance(needs, str) else needs
                self.assertIn("tag-guard", needs)
                self.assertIn("github.ref == 'refs/heads/main'", job.get("if", ""))
            for step in job.get("steps", []):
                self.assertNotIn("${{", step.get("run", ""))
                if job_id == "tag-guard":
                    self.assertNotIn("if", step)
                    self.assertNotIn("continue-on-error", step)
                    if "run" in step:
                        self.assertEqual(step.get("shell"), "bash")
                        self.assertIn("set -euo pipefail", step.get("run", ""))
                if step.get("uses", "").startswith("actions/checkout@"):
                    settings = step.get("with", {})
                    self.assertEqual(settings.get("persist-credentials"), "false")
                    expected = {
                        "tag-guard": "main",
                        "package": SHA_REF,
                        "notify-hub": "${{ github.workflow_sha }}",
                    }
                    self.assertEqual(settings.get("ref"), expected[job_id])
        package = jobs.get("package", {})
        self.assertIn("inputs.package", package.get("if", ""))
        lookup = step_with_id(workflow, "package", "release")
        self.assertEqual(lookup.get("env", {}).get("RELEASE_TAG"), "${{ inputs.tag }}")
        self.assertEqual(lookup.get("env", {}).get("GH_TOKEN"), "${{ github.token }}")
        self.assertIn(
            'gh api "repos/${GH_REPOSITORY}/releases/tags/${RELEASE_TAG}"',
            lookup.get("run", ""),
        )
        self.assertIn('release.get("draft") is not False', lookup.get("run", ""))
        self.assertIn('not release.get("published_at")', lookup.get("run", ""))
        uploads = [
            step
            for step in package.get("steps", [])
            if step.get("name") == "Upload release assets"
        ]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(
            uploads[0].get("env", {}).get("RELEASE_ID"),
            "${{ steps.release.outputs.id }}",
        )
        notification = jobs.get("notify-hub", {})
        refusal = notification.get("steps", [])[0]
        self.assertEqual(
            refusal.get("env", {}).get("TAG_GUARD_RESULT"),
            "${{ needs.tag-guard.result }}",
        )
        self.assertNotIn("uses", refusal)
        self.assertNotIn("if", refusal)
        self.assertNotIn("continue-on-error", refusal)
        self.assertEqual(refusal.get("shell"), "bash")
        self.assertIn('if [[ "$TAG_GUARD_RESULT" != success ]]', refusal.get("run", ""))
        self.assertIn("exit 1", refusal.get("run", ""))

    def test_shipped_workflow_contract(self):
        self.assert_contract(self.text)

    def test_real_workflow_text_mutations_are_refused(self):
        mutations = (
            ("\non:\n", "\non:\n  release:\n    types: [published]\n"),
            (
                "\non:\n",
                "\non:\n  workflow_run:\n    workflows: [CI]\n    types: [completed]\n",
            ),
            ("        required: true\n", "        required: false\n"),
            ("        type: string\n", "        type: boolean\n"),
            ("        type: boolean\n", "        type: string\n"),
            ("        default: false\n", "        default: true\n"),
            (
                "group: release-assets-${{ inputs.tag }}",
                "group: release-assets-${{ github.event.release.tag_name }}",
            ),
            (run_block(PATTERN_BLOCK), ""),
            (run_block(EXISTENCE_BLOCK), ""),
            (run_block(ANCESTRY_BLOCK), ""),
            ("    needs: tag-guard\n", ""),
            ("    needs: [tag-guard, package]\n", "    needs: package\n"),
            (f"ref: {SHA_REF}", "ref: ${{ inputs.tag }}"),
            ("ref: ${{ github.workflow_sha }}", "ref: ${{ inputs.tag }}"),
            (
                '          if [[ ! "$TAG" =~ ',
                '          TAG="${{ inputs.tag }}"\n          if [[ ! "$TAG" =~ ',
            ),
            (
                "github.ref == 'refs/heads/main' && inputs.package",
                "inputs.package",
            ),
            (
                "          github.ref == 'refs/heads/main' &&\n",
                "",
            ),
            ("  tag-guard:\n", "  tag-guard:\n    if: false\n"),
            (
                "  tag-guard:\n",
                "  tag-guard:\n    continue-on-error: true\n",
            ),
            (
                "      contents: read\n    outputs:\n",
                "      contents: write\n    outputs:\n",
            ),
            (
                "    outputs:\n",
                "    env:\n      UNEXPECTED: ${{ secrets.PINEFORGE_APP_ID }}\n"
                "    outputs:\n",
            ),
            (
                "        id: verify\n",
                "        id: verify\n        if: false\n",
            ),
            (
                "        id: verify\n",
                "        id: verify\n        continue-on-error: true\n",
            ),
            ("          ref: main\n", "          ref: side\n"),
            (
                "sha: ${{ steps.verify.outputs.sha }}",
                "sha: ${{ inputs.tag }}",
            ),
            (
                "            echo '::error::Tag guard failed or was skipped; "
                "refusing checkout and notification.'\n            exit 1\n",
                "            echo 'ignored guard failure'\n",
            ),
            ('release.get("draft") is not False', "False"),
            ('not release.get("published_at")', "False"),
            (
                "RELEASE_ID: ${{ steps.release.outputs.id }}",
                "RELEASE_ID: ${{ github.event.release.id }}",
            ),
        )
        for old, new in mutations:
            with self.subTest(old=old, new=new):
                mutated = replace_literal(self.text, old, new)
                with self.assertRaises(AssertionError):
                    self.assert_contract(mutated)
        for old, new, count in (
            (MAIN_CHECK, 'if [[ "$WORKFLOW_EVENT" != workflow_dispatch ]]; then', 2),
            ("          fetch-depth: 0\n", "          fetch-depth: 1\n", 2),
            ("          persist-credentials: false\n", "", 3),
            ("        shell: bash\n", "        shell: sh\n", 4),
            ("          set -euo pipefail\n", "", 4),
            ("          GH_TOKEN: ${{ github.token }}\n", "", 2),
            ("          RELEASE_TAG: ${{ inputs.tag }}\n", "", 3),
        ):
            with self.subTest(old=old):
                mutated = replace_literal(self.text, old, new, count=count)
                with self.assertRaises(AssertionError):
                    self.assert_contract(mutated)


class RealGitGuardTests(unittest.TestCase):
    def setUp(self):
        for tool in ("git", "bash"):
            self.assertIsNotNone(
                shutil.which(tool),
                f"{tool} is required: the real-git guard tests cannot run; no skip",
            )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.origin = self.directory / "origin"
        self.checkout = self.directory / "checkout"
        self.output = self.directory / "guard-output"
        self.text = WORKFLOW.read_text()
        self.environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Guard fixture",
            "GIT_AUTHOR_EMAIL": "guard@example.invalid",
            "GIT_COMMITTER_NAME": "Guard fixture",
            "GIT_COMMITTER_EMAIL": "guard@example.invalid",
        }
        self.git(
            self.directory,
            "init",
            "--initial-branch=main",
            "--object-format=sha1",
            str(self.origin),
        )
        self.git(self.origin, "commit", "--allow-empty", "-m", "main first")
        self.first = self.git(self.origin, "rev-parse", "HEAD")
        self.git(self.origin, "commit", "--allow-empty", "-m", "main second")
        self.tip = self.git(self.origin, "rev-parse", "HEAD")
        self.git(self.origin, "tag", "v1.2.3", self.first)
        self.git(self.origin, "tag", "v0.0.0", self.first)
        for tag in ("v01.2.3", "v1.2.3rc0", "v1.2.3rc01", "v1.2.3-rc.1"):
            self.git(self.origin, "tag", tag)
        self.git(self.origin, "tag", "-a", "v1.2.4", "-m", "annotated main tag")
        self.annotation = self.git(self.origin, "rev-parse", "refs/tags/v1.2.4")
        self.git(self.origin, "tag", "v1.2.5rc1")
        blob = self.git(self.origin, "hash-object", "-w", "--stdin", payload="blob\n")
        self.git(self.origin, "tag", "v2.0.0", blob)
        self.git(self.origin, "switch", "-c", "side", self.first)
        self.git(self.origin, "commit", "--allow-empty", "-m", "side only")
        self.git(self.origin, "tag", "v9.9.9")
        # Decoy tags named like main on the side commit: a short name such as
        # origin/main resolves refs/tags/ before refs/remotes/, so a guard that
        # names main loosely would accept the side-branch tag above.
        for decoy in ("origin/main", "main", "heads/main", "remotes/origin/main"):
            self.git(self.origin, "tag", decoy)
        self.git(self.origin, "switch", "main")
        self.git(
            self.directory,
            "clone",
            "--no-tags",
            "--no-local",
            "--branch",
            "main",
            str(self.origin),
            str(self.checkout),
        )

    def git(self, directory, *arguments, payload=None):
        result = subprocess.run(
            ["git", "-C", str(directory), *arguments],
            env=self.environment,
            input=payload,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    def invoke(self, text, tag, *, ref="refs/heads/main", event="workflow_dispatch"):
        script = step_with_id(parse_workflow(text), "tag-guard", "verify")["run"]
        self.output.write_text("")
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=self.checkout,
            env={
                **self.environment,
                "TAG": tag,
                "WORKFLOW_REF": ref,
                "WORKFLOW_EVENT": event,
                "GITHUB_OUTPUT": str(self.output),
            },
            text=True,
            capture_output=True,
        )
        return result, self.output.read_text()

    def assert_case(self, text, tag, expected, sha=None, **context):
        result, output = self.invoke(text, tag, **context)
        detail = result.stdout + result.stderr
        if expected:
            self.assertEqual(result.returncode, 0, detail)
            self.assertEqual(output, f"sha={sha}\n")
        else:
            self.assertNotEqual(result.returncode, 0, detail)
            self.assertIn("::error::", detail)
            self.assertEqual(output, "")

    def test_extracted_guard_executes_literal_expected_git_cases(self):
        # Expected outcomes are literals, independent of the workflow's pattern.
        cases = (
            ("v1.2.3", True, self.first),
            ("v01.2.3", False, None),
            ("v8.8.8", False, None),
            ("v9.9.9", False, None),
            ("v1.2.4", True, self.tip),
            ("v1.2.5rc1", True, self.tip),
            ("v0.0.0", True, self.first),
            ("v1.2.3rc0", False, None),
            ("v1.2.3rc01", False, None),
            ("v1.2.3-rc.1", False, None),
            ("v2.0.0", False, None),
            ("v1.2.3\n", False, None),
            ("v1.2.3/../../main", False, None),
            ("", False, None),
        )
        self.assertNotEqual(self.annotation, self.tip)
        for tag, expected, sha in cases:
            with self.subTest(tag=tag, expected=expected):
                self.assert_case(self.text, tag, expected, sha)

    def test_removed_guards_are_detected_by_executing_the_mutated_workflow_script(self):
        mutations = (
            (PATTERN_BLOCK, "", "v01.2.3"),
            (ANCESTRY_BLOCK, "", "v9.9.9"),
            # The decoy tags make the short name accept the side-branch tag.
            (
                ANCESTRY_BLOCK,
                ANCESTRY_BLOCK.replace("refs/remotes/origin/main", "origin/main", 1),
                "v9.9.9",
            ),
            (
                EXISTENCE_BLOCK,
                'TAG_COMMIT="$(git rev-parse refs/remotes/origin/main)"\n',
                "v8.8.8",
            ),
        )
        for old, new, tag in mutations:
            with self.subTest(tag=tag):
                self.assert_case(self.text, tag, False)
                mutated = replace_literal(self.text, run_block(old), run_block(new))
                with self.assertRaises(AssertionError):
                    self.assert_case(mutated, tag, False)

    def test_local_branches_named_like_tags_cannot_stand_in_for_tags(self):
        # A local branch resolves by its bare name: v3.0.0^{commit} would find
        # the branch and pass the reachability check although no tag exists.
        self.git(self.checkout, "branch", "v3.0.0", self.tip)
        # A branch next to an honest tag must not change which commit is built.
        self.git(self.checkout, "branch", "v1.2.3", self.tip)
        self.assert_case(self.text, "v3.0.0", False)
        self.assert_case(self.text, "v1.2.3", True, self.first)
        bare = replace_literal(
            self.text,
            'rev-parse -q --verify "refs/tags/${TAG}^{commit}"',
            'rev-parse -q --verify "${TAG}^{commit}"',
        )
        with self.assertRaises(AssertionError):
            self.assert_case(bare, "v3.0.0", False)

    def test_ref_and_event_fail_closed_before_fetch_or_any_output(self):
        self.git(self.checkout, "remote", "set-url", "origin", "missing-origin")
        for ref, event in (
            ("refs/heads/side", "workflow_dispatch"),
            ("refs/tags/v1.2.3", "workflow_dispatch"),
            ("refs/heads/main", "release"),
            ("refs/heads/main", "push"),
        ):
            with self.subTest(ref=ref, event=event):
                self.assert_case(self.text, "v1.2.3", False, ref=ref, event=event)
        self.assert_case(self.text, "v01.2.3", False)

    def test_notification_refusal_precedes_checkout_for_unsuccessful_guard(self):
        workflow = parse_workflow(self.text)
        refusal = workflow["jobs"]["notify-hub"]["steps"][0]
        for status, expected in (
            ("success", 0),
            ("failure", 1),
            ("skipped", 1),
            ("cancelled", 1),
            ("", 1),
        ):
            with self.subTest(status=status):
                result = subprocess.run(
                    ["bash", "-c", refusal["run"]],
                    cwd=self.directory,
                    env={**self.environment, "TAG_GUARD_RESULT": status},
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected:
                    self.assertIn("::error::", result.stdout)


if __name__ == "__main__":
    unittest.main()
