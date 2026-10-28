"""Admission boundary and real workflow mutation controls; run in CI only."""

from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
SMALL = "gke-8cpu-16gb-amd64"
LARGE = "gke-16cpu-32gb-amd64"


def read(path):
    candidate = path.absolute()
    parts = tuple(part.casefold() for part in candidate.parts)
    if any(
        part in (".ssh", ".aws", ".codex") or part.endswith(".env") for part in parts
    ) or any(
        parts[index : index + 3] == (".config", "zsh", "keys")
        or parts[index : index + 2] == (".config", "pineforge")
        for index in range(len(parts))
    ):
        raise ValueError(f"refusing prohibited path: {candidate}")
    for component in reversed((candidate, *candidate.parents)):
        if component.is_symlink():
            raise ValueError(f"refusing symlink path: {component}")
    print(f"FILES read: {path}", file=sys.stderr, flush=True)
    return path.read_text(encoding="utf-8")


def workflow(name):
    return yaml.load(read(ROOT / ".github/workflows" / name), Loader=yaml.BaseLoader)


class MigrationRedTests(unittest.TestCase):
    """These inputs exist on main: failure is a routing/cache assertion."""

    def test_existing_linux_proofs_use_exact_bare_pod_labels(self):
        for name, jobs in {
            "ci-post.yml": {
                "workflow-lint": SMALL,
                "light": SMALL,
                "release-notification": SMALL,
                "native": LARGE,
                "python": LARGE,
                "memory-gate": LARGE,
            },
            "benchmark-smoke-post.yml": {
                "workflow-lint": SMALL,
                "optuna": LARGE,
            },
            "docs-pages.yml": {"build": SMALL},
        }.items():
            actual = workflow(name)["jobs"]
            for job, runner in jobs.items():
                with self.subTest(workflow=name, job=job):
                    self.assertEqual(actual[job]["runs-on"], runner)

    def test_trusted_workflows_cannot_restore_pr_written_actions_caches(self):
        for name in (
            "ci.yml",
            "ci-post.yml",
            "benchmark-smoke.yml",
            "benchmark-smoke-post.yml",
            "docs-pages.yml",
            "release.yml",
        ):
            data = workflow(name)
            for job in data["jobs"].values():
                for step in job.get("steps", []):
                    self.assertNotIn("cache", step.get("with", {}))
                    self.assertFalse(
                        any(key.casefold() == "cache" for key in step.get("with", {}))
                    )
                    self.assertFalse(
                        step.get("uses", "").casefold().startswith("actions/cache")
                    )

    def test_legacy_pr_events_cannot_allocate_a_pod(self):
        for name, post in (
            ("ci.yml", "ci-post.yml"),
            ("benchmark-smoke.yml", "benchmark-smoke-post.yml"),
        ):
            pr = workflow(name)
            trusted = workflow(post)
            with self.subTest(workflow=name):
                self.assertEqual(set(pr["on"]), {"pull_request", "workflow_dispatch"})
                self.assertEqual(set(pr["jobs"]), {"authz"})
                self.assertEqual(pr["jobs"]["authz"]["runs-on"], "ubuntu-24.04")
                self.assertEqual(pr["jobs"]["authz"], trusted["jobs"]["authz"])
                self.assertEqual(set(trusted["on"]), {"push", "workflow_dispatch"})
                self.assertEqual(trusted["on"]["push"], {"branches": ["main"]})
                self.assertNotIn("changes", trusted["jobs"])
                self.assertNotIn("if", trusted["jobs"]["workflow-lint"])
                self.assertEqual(trusted["jobs"]["workflow-lint"]["needs"], ["authz"])
        self.assertNotIn("if", workflow("ci-post.yml")["jobs"]["light"])
        pages = workflow("docs-pages.yml")
        self.assertEqual(set(pages["on"]), {"push", "workflow_dispatch"})
        self.assertEqual(pages["on"]["push"]["branches"], ["main"])
        self.assertNotIn("if", pages["jobs"]["build"])
        self.assertEqual(pages["jobs"]["deploy"]["runs-on"], "ubuntu-24.04")


class AdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = ROOT / "tests/release/pr_admission.py"
        read(source)
        spec = importlib.util.spec_from_file_location("hpo_pr_admission", source)
        cls.policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.policy)
        cls.sources = cls.policy.local_sources(ROOT)

    def test_admission_rejects_canonical_job_ids_even_as_its_own_baseline(self):
        name = ".github/workflows/gke-pr.yml"
        original = yaml.load(self.sources[name], Loader=yaml.BaseLoader)
        for job_id in ("authz", "changes", "workflow-lint"):
            bad = copy.deepcopy(original)
            bad["jobs"].setdefault(job_id, copy.deepcopy(original["jobs"]["light"]))
            sources = {
                **self.sources,
                name: yaml.safe_dump(bad, sort_keys=False, width=1000),
            }
            with (
                self.subTest(job=job_id),
                self.assertRaisesRegex(ValueError, "admission job inventory envelope"),
            ):
                self.policy.check_sources(sources, sources)

    def test_only_result_uses_job_level_always_and_displays_build_gate(self):
        jobs = workflow("gke-pr.yml")["jobs"]
        with self.subTest(property="job-level always"):
            self.assertEqual(
                [
                    name
                    for name, job in jobs.items()
                    if "always(" in job.get("if", "").replace(" ", "")
                ],
                ["result"],
            )
        with self.subTest(property="displayed check"):
            self.assertEqual(jobs["result"].get("name", "result"), "build-gate")

    def test_main_refuses_multiple_job_level_always_even_as_its_own_baseline(self):
        name = ".github/workflows/gke-pr.yml"
        original = yaml.load(self.sources[name], Loader=yaml.BaseLoader)
        bad = copy.deepcopy(original)
        bad["jobs"].setdefault(
            "benchmark-check", copy.deepcopy(original["jobs"]["result"])
        )
        sources = {
            **self.sources,
            name: yaml.safe_dump(bad, sort_keys=False, width=1000),
        }
        with self.assertRaisesRegex(ValueError, "result job-level always envelope"):
            self.policy.check_sources(sources, sources)

    def test_each_proof_uses_admission_directly_before_any_head_checkout(self):
        jobs = workflow("gke-pr.yml")["jobs"]
        for name, job in jobs.items():
            if name in ("admit", "result"):
                continue
            with self.subTest(job=name):
                self.assertEqual(job["needs"], ["admit"])
                condition = "needs.admit.outputs.allowed == 'true'"
                if name in self.policy.HEAVY_JOBS:
                    condition += " && needs.admit.outputs.docs_only != 'true'"
                self.assertEqual(job["if"], "${{ " + condition + " }}")
                checkout = next(
                    step
                    for step in job["steps"]
                    if step.get("uses", "").startswith("actions/checkout@")
                )
                self.assertEqual(
                    checkout["with"]["ref"], "${{ needs.admit.outputs.head_sha }}"
                )

    def test_real_workflows_pass_main_rules_without_docs_or_head_tests(self):
        self.policy.check_sources(self.sources, self.sources)
        parent = ROOT / "build/pr-admission-tests"
        print(f"FILES test trees: {parent}", flush=True)
        parent.mkdir(parents=True, exist_ok=True)
        roots = [Path(tempfile.mkdtemp(dir=parent)) for _ in range(2)]
        for root in roots:
            self.policy.stage_sources(self.sources, root)
        normal = {
            "docs/ci.md": read(ROOT / "docs/ci.md"),
            "tests/release/pr_admission.py": read(
                ROOT / "tests/release/pr_admission.py"
            ),
            "scripts/check_app_token_environments.py": read(
                ROOT / "scripts/check_app_token_environments.py"
            ),
        }
        contradictory = {
            "docs/ci.md": "Every pod may deploy with a write token.",
            "tests/release/pr_admission.py": "raise RuntimeError('head code')",
            "scripts/check_app_token_environments.py": (
                "raise RuntimeError('head helper')"
            ),
        }
        for root, data in zip(roots, (normal, contradictory)):
            for name, text in data.items():
                path = root / name
                print(f"FILES source-tree data: {path}", flush=True)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        original_root = self.policy.ROOT
        try:
            for root in roots:
                # Change the checker's actual source root, not a filtered input.
                # A later rule reading ROOT/docs or another helper fails here.
                self.policy.ROOT = root
                actual = self.policy.local_sources(root)
                self.assertEqual(actual, self.sources)
                self.policy.lint_sources(actual, self.sources, root)
                self.policy.check_sources(actual, self.sources)
            # A real hostile workflow must produce the same refusal in both trees.
            for root in roots:
                self.policy.ROOT = root
                path = root / ".github/workflows/gke-pr.yml"
                text = read(path).replace(
                    "runs-on: gke-16cpu-32gb-amd64", "runs-on: ubuntu-24.04", 1
                )
                path.write_text(text, encoding="utf-8")
                actual = self.policy.local_sources(root)
                self.policy.lint_sources(actual, self.sources, root)
                with self.assertRaisesRegex(ValueError, "envelope"):
                    self.policy.check_sources(actual, self.sources)
        finally:
            self.policy.ROOT = original_root

    def test_actionlint_rejects_head_grammar_using_only_the_trusted_config(self):
        parent = ROOT / "build/pr-admission-tests"
        print(f"FILES actionlint data trees: {parent}", flush=True)
        parent.mkdir(parents=True, exist_ok=True)
        good = Path(tempfile.mkdtemp(dir=parent))
        self.policy.lint_sources(self.sources, self.sources, good)
        bad = dict(self.sources)
        name = ".github/workflows/gke-pr.yml"
        bad[name] = bad[name].replace(
            "name: Refuse unsuccessful admission",
            "unknown-step-key: invalid",
            1,
        )
        self.assertNotEqual(bad[name], self.sources[name])
        with self.assertRaises(subprocess.CalledProcessError):
            self.policy.lint_sources(
                bad, self.sources, Path(tempfile.mkdtemp(dir=parent))
            )
        for reference, reason in (
            ("./.ssh/action", "prohibited"),
            ("./action.env", "prohibited"),
            ("./../action", "escapes"),
        ):
            bad = dict(self.sources)
            bad[name] = bad[name].replace(
                "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "uses: " + reference,
                1,
            )
            self.assertNotEqual(bad[name], self.sources[name])
            with (
                self.subTest(reference=reference),
                self.assertRaisesRegex(ValueError, reason),
            ):
                self.policy.lint_sources(
                    bad, self.sources, Path(tempfile.mkdtemp(dir=parent))
                )
        # A head-written configuration cannot add a runner label.
        bad = dict(self.sources)
        bad[name] = bad[name].replace("gke-16cpu-32gb-amd64", "gke-99cpu-99gb-amd64")
        bad[".github/actionlint.yaml"] += "    - gke-99cpu-99gb-amd64\n"
        with self.assertRaises(subprocess.CalledProcessError):
            self.policy.lint_sources(
                bad, self.sources, Path(tempfile.mkdtemp(dir=parent))
            )

    def test_identity_requires_three_writers_and_live_payload_binding(self):
        identity = {
            "repo": "owner/project",
            "head_repo": "owner/project",
            "fork": False,
            "draft": False,
            "base_ref": "main",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "number": 7,
            "author": "author",
            "actor": "actor",
            "triggering_actor": "rerunner",
        }
        live = {
            "state": "open",
            "draft": False,
            "base": {"ref": "main"},
            "head": {
                "sha": "b" * 40,
                "repo": {"full_name": "owner/project", "fork": False},
            },
            "user": {"login": "author"},
        }
        permissions = {name: "write" for name in ("author", "actor", "rerunner")}
        self.policy.check_identity(identity, live, permissions)
        for name in permissions:
            with self.subTest(nonwriter=name):
                bad = {**permissions, name: "read"}
                with self.assertRaisesRegex(ValueError, "write permission"):
                    self.policy.check_identity(identity, live, bad)
        for field, value, reason in (
            ("head_repo", "fork/project", "repository"),
            ("fork", True, "fork"),
            ("draft", True, "draft"),
            ("base_ref", "other", "base"),
            ("base_sha", "a" * 40 + "\n", "SHA"),
            ("head_sha", "", "SHA"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, reason):
                self.policy.check_identity(
                    {**identity, field: value}, live, permissions
                )
        for field, value, reason in (
            ("state", "closed", "open"),
            ("draft", True, "draft"),
            ("head", {**live["head"], "sha": "c" * 40}, "head"),
            ("base", {"ref": "other"}, "base"),
        ):
            with self.subTest(live=field), self.assertRaisesRegex(ValueError, reason):
                self.policy.check_identity(
                    identity, {**live, field: value}, permissions
                )
        with self.assertRaises(ValueError) as refusal:
            self.policy.check_identity({**identity, "draft": True}, live, permissions)
        self.assertEqual(self.policy.refusal_reason(refusal.exception), "draft refused")
        self.assertEqual(
            self.policy.refusal_reason(ValueError("head-written source must not echo")),
            "workflow or API validation failed",
        )

    def test_head_cannot_change_the_main_admission_or_routing_envelope(self):
        original = yaml.load(
            self.sources[".github/workflows/gke-pr.yml"], Loader=yaml.BaseLoader
        )
        mutations = (
            ("on", {"pull_request": None}),
            ("permissions", {"contents": "write"}),
            ("run-name", "gke-pr spoof"),
        )
        for key, value in mutations:
            bad = copy.deepcopy(original)
            bad[key] = value
            self.assert_policy_refuses(bad, "envelope")
        for job, key, value, reason in (
            ("admit", "steps", [{"run": "echo allowed=true"}], "envelope"),
            ("result", "if", "success()", "envelope"),
            ("memory-gate", "runs-on", {"group": "gke-runners"}, "runs-on"),
            ("memory-gate", "environment", "production", "credential"),
            ("memory-gate", "permissions", {"id-token": "write"}, "credential"),
            ("memory-gate", "continue-on-error", "true", "envelope"),
        ):
            bad = copy.deepcopy(original)
            bad["jobs"][job][key] = value
            self.assert_policy_refuses(bad, reason)
        for step, reason in (
            ({"uses": "actions/cache/save@" + "a" * 40}, "cache"),
            ({"uses": "Actions/Cache/save@" + "a" * 40}, "cache"),
            (
                {"uses": "actions/setup-python@" + "a" * 40, "with": {"cache": "pip"}},
                "cache",
            ),
            (
                {"uses": "actions/setup-python@" + "a" * 40, "with": {"cAcHe": "pip"}},
                "cache",
            ),
            ({"run": "echo ${{ secrets.TOKEN }}"}, "credential"),
            (
                {"run": "echo ${{ github.event.pull_request.head.ref }}"},
                "interpolation",
            ),
            ({"run": "echo ${{ toJSON(github.event) }}"}, "interpolation"),
            ({"run": "echo ${{ toJSON(secrets) }}"}, "credential"),
            ({"run": "echo ${{ toJSON(SeCrEtS) }}"}, "credential"),
            ({"run": "echo ${{ toJSON(GitHub.Event) }}"}, "interpolation"),
            ({"run": "timeout 10 python3 tool.py"}, "time limit"),
            ({"uses": "./.github/workflows/ci.yml"}, "reusable"),
        ):
            bad = copy.deepcopy(original)
            bad["jobs"]["memory-gate"]["steps"].append(step)
            self.assert_policy_refuses(bad, reason)

    def test_main_contract_cannot_make_an_unsafe_envelope_its_own_baseline(self):
        name = ".github/workflows/gke-pr.yml"
        original = yaml.load(self.sources[name], Loader=yaml.BaseLoader)
        bad = copy.deepcopy(original)
        bad["env"] = {"TOKEN": "${{ toJSON(secrets) }}"}
        sources = {
            **self.sources,
            name: yaml.safe_dump(bad, sort_keys=False, width=1000),
        }
        with self.assertRaisesRegex(ValueError, "credential"):
            self.policy.check_sources(sources, sources)
        for job, key, value, reason in (
            ("light", "if", "always()", "result job-level always"),
            ("native", "needs", ["changes"], "admission"),
            ("native", "if", "always()", "result job-level always"),
            ("admit", "outputs", {"head_sha": "${{ github.sha }}"}, "admission"),
            ("result", "if", "success()", "result"),
            ("result", "needs", ["admit"], "result"),
            (
                "result",
                "needs",
                original["jobs"]["result"]["needs"] + ["admit"],
                "result",
            ),
            ("result", "name", "result", "result"),
        ):
            bad = copy.deepcopy(original)
            bad["jobs"][job][key] = value
            sources = {
                **self.sources,
                name: yaml.safe_dump(bad, sort_keys=False, width=1000),
            }
            with (
                self.subTest(job=job, key=key),
                self.assertRaisesRegex(ValueError, reason),
            ):
                self.policy.check_sources(sources, sources)
        for job in ("admit", "result", "native", "memory-gate"):
            bad = copy.deepcopy(original)
            checkout = next(
                step
                for step in bad["jobs"][job]["steps"]
                if step.get("uses", "").startswith("actions/checkout@")
            )
            checkout["with"]["ref"] = "${{ github.event.pull_request.head.sha }}"
            sources = {
                **self.sources,
                name: yaml.safe_dump(bad, sort_keys=False, width=1000),
            }
            with (
                self.subTest(checkout=job),
                self.assertRaisesRegex(ValueError, "checkout"),
            ):
                self.policy.check_sources(sources, sources)

    def test_pull_request_workflows_cannot_name_gke_routing_even_in_main(self):
        for name, text in self.sources.items():
            if not name.startswith(".github/workflows/"):
                continue
            data = yaml.load(text, Loader=yaml.BaseLoader)
            triggers = data.get("on", {})
            if not any(
                event.startswith("pull_request") and event != "pull_request_target"
                for event in triggers
            ):
                continue
            for job_id, job in data["jobs"].items():
                with self.subTest(workflow=name, job=job_id):
                    self.assertFalse(str(job.get("runs-on", "")).startswith("gke-"))
            # Freeze the same unsafe document as its own baseline: an envelope
            # comparison alone cannot establish the trigger/routing boundary.
            bad = copy.deepcopy(data)
            job_id = next(iter(bad["jobs"]))
            bad["jobs"][job_id]["runs-on"] = SMALL
            sources = {
                **self.sources,
                name: yaml.safe_dump(bad, sort_keys=False, width=1000),
            }
            with (
                self.subTest(workflow=name),
                self.assertRaisesRegex(ValueError, "pull_request.*GKE"),
            ):
                self.policy.check_sources(sources, sources)
        target_workflows = {
            name
            for name, text in self.sources.items()
            if name.startswith(".github/workflows/")
            and "pull_request_target"
            in yaml.load(text, Loader=yaml.BaseLoader).get("on", {})
        }
        self.assertEqual(target_workflows, {".github/workflows/gke-pr.yml"})
        self.policy.check_sources(self.sources, self.sources)

    def test_pull_request_event_family_cannot_name_gke_routing_even_in_main(self):
        name = ".github/workflows/ci.yml"
        original = yaml.load(self.sources[name], Loader=yaml.BaseLoader)
        for event in ("pull_request_review", "pull_request_review_comment"):
            bad = copy.deepcopy(original)
            bad["on"] = {event: ""}
            bad["jobs"]["authz"]["runs-on"] = SMALL
            sources = {
                **self.sources,
                name: yaml.safe_dump(bad, sort_keys=False, width=1000),
            }
            with (
                self.subTest(event=event),
                self.assertRaisesRegex(ValueError, "pull_request.*GKE"),
            ):
                self.policy.check_sources(sources, sources)

    def test_post_merge_classification_is_refused_even_as_its_own_baseline(self):
        for filename in ("ci-post.yml", "benchmark-smoke-post.yml"):
            name = ".github/workflows/" + filename
            original = yaml.load(self.sources[name], Loader=yaml.BaseLoader)
            for mutation in ("job", "needs", "condition", "output", "environment"):
                bad = copy.deepcopy(original)
                jobs = bad["jobs"]
                if mutation == "job":
                    jobs["changes"] = copy.deepcopy(jobs["workflow-lint"])
                    jobs["changes"]["needs"] = ["authz"]
                elif mutation == "needs":
                    jobs["workflow-lint"]["needs"] = ["authz", "changes"]
                elif mutation == "condition":
                    jobs["workflow-lint"]["if"] = "needs.changes.outputs.code == 'true'"
                elif mutation == "output":
                    jobs["workflow-lint"]["outputs"] = {"code": "true"}
                else:
                    jobs["workflow-lint"]["env"] = {
                        "CLASS": "${{ needs.changes.outputs }}"
                    }
                sources = {
                    **self.sources,
                    name: yaml.safe_dump(bad, sort_keys=False, width=1000),
                }
                with (
                    self.subTest(workflow=filename, mutation=mutation),
                    self.assertRaisesRegex(ValueError, "post-merge classification"),
                ):
                    self.policy.check_sources(sources, sources)

    def test_post_merge_pr_triggers_are_refused_independently_of_routing(self):
        for filename in ("ci-post.yml", "benchmark-smoke-post.yml"):
            name = ".github/workflows/" + filename
            original = yaml.load(self.sources[name], Loader=yaml.BaseLoader)
            for event in (
                "pull_request",
                "pull_request_review",
                "pull_request_review_comment",
                "pull_request_target",
            ):
                bad = copy.deepcopy(original)
                bad["on"][event] = ""
                # Isolate the event boundary from the separate GKE routing rule.
                for job in bad["jobs"].values():
                    job["runs-on"] = "ubuntu-24.04"
                sources = {
                    **self.sources,
                    name: yaml.safe_dump(bad, sort_keys=False, width=1000),
                }
                with (
                    self.subTest(workflow=filename, event=event),
                    self.assertRaisesRegex(ValueError, "post-merge trigger"),
                ):
                    self.policy.check_sources(sources, sources)

    def test_checkout_and_hosted_boundaries_cannot_use_the_head_or_credentials(self):
        original = yaml.load(
            self.sources[".github/workflows/gke-pr.yml"], Loader=yaml.BaseLoader
        )
        for job in ("admit", "result", "memory-gate", "native", "native-macos"):
            for key, value in (
                ("ref", "${{ github.event.pull_request.head.ref }}"),
                ("persist-credentials", "true"),
                ("repository", "other/project"),
            ):
                bad = copy.deepcopy(original)
                checkout = next(
                    step
                    for step in bad["jobs"][job]["steps"]
                    if step.get("uses", "").startswith("actions/checkout@")
                )
                checkout["with"][key] = value
                with self.subTest(job=job, key=key):
                    self.assert_policy_refuses(bad, "envelope")
        for name in (
            "ci.yml",
            "ci-post.yml",
            "benchmark-smoke.yml",
            "benchmark-smoke-post.yml",
            "docs-pages.yml",
            "release.yml",
        ):
            bad = dict(self.sources)
            name = ".github/workflows/" + name
            document = yaml.load(bad[name], Loader=yaml.BaseLoader)
            job = next(iter(document["jobs"].values()))
            job["steps"].append({"uses": "actions/cache/restore@" + "a" * 40})
            bad[name] = yaml.safe_dump(document, sort_keys=False, width=1000)
            with (
                self.subTest(workflow=name),
                self.assertRaisesRegex(ValueError, "cache"),
            ):
                self.policy.check_sources(bad, self.sources)

    def test_result_refuses_unsuccessful_admission_before_conditional_steps(self):
        jobs = workflow("gke-pr.yml")["jobs"]
        first = jobs["result"]["steps"][0]
        self.assertNotIn("if", first)
        self.assertEqual(first["env"]["ADMIT_RESULT"], "${{ needs.admit.result }}")
        self.assertEqual(first["env"]["ALLOWED"], "${{ needs.admit.outputs.allowed }}")
        self.assertEqual(
            first["run"],
            "set -euo pipefail\n"
            'test "$ADMIT_RESULT" = success\n'
            'test "$ALLOWED" = true\n',
        )
        name = ".github/workflows/gke-pr.yml"
        original = yaml.load(self.sources[name], Loader=yaml.BaseLoader)
        for key, value in (
            ("if", "${{ needs.admit.result == 'success' }}"),
            ("run", "true"),
            ("env", {"ADMIT_RESULT": "${{ needs.admit.result }}"}),
        ):
            bad = copy.deepcopy(original)
            bad["jobs"]["result"]["steps"][0][key] = value
            sources = {
                **self.sources,
                name: yaml.safe_dump(bad, sort_keys=False, width=1000),
            }
            with (
                self.subTest(field=key),
                self.assertRaisesRegex(ValueError, "result authorization envelope"),
            ):
                self.policy.check_sources(sources, sources)

    def test_result_inventory_matches_every_real_workflow_job(self):
        jobs = workflow("gke-pr.yml")["jobs"]

        def assert_inventory(candidate):
            self.assertEqual(
                set(candidate["result"]["needs"]), set(candidate) - {"result"}
            )
            self.assertEqual(
                len(candidate["result"]["needs"]), len(set(candidate) - {"result"})
            )
            self.assertEqual(set(candidate["result"]["needs"]), self.policy.RESULT_JOBS)
            self.assertEqual(candidate["result"]["if"], "always()")
            last = candidate["result"]["steps"][-1]
            self.assertEqual(last["env"]["NEEDS"], "${{ toJSON(needs) }}")
            self.assertEqual(
                last["run"], "python3 -I tests/release/pr_admission.py result"
            )

        assert_inventory(jobs)
        for name in jobs["result"]["needs"]:
            bad = copy.deepcopy(jobs)
            bad["result"]["needs"].remove(name)
            with self.subTest(missing=name), self.assertRaises(AssertionError):
                assert_inventory(bad)

    def test_admitted_python_release_smoke_and_docs_keep_the_existing_workloads(self):
        admitted = workflow("gke-pr.yml")["jobs"]
        for source, old_name, new_name in (
            ("ci-post.yml", "release-notification", "release-notification"),
            ("ci-post.yml", "python", "python"),
            ("benchmark-smoke-post.yml", "optuna", "optuna"),
            ("docs-pages.yml", "build", "docs"),
        ):
            legacy = copy.deepcopy(workflow(source)["jobs"][old_name])
            if new_name == "docs":
                self.assertNotIn("if", legacy)
                upload = legacy["steps"].pop()
                self.assertEqual(upload["name"], "Upload GitHub Pages artifact")
                self.assertEqual(
                    upload["if"],
                    "github.ref == 'refs/heads/main' && "
                    "github.event_name != 'pull_request'",
                )
            checkout = next(
                step
                for step in legacy["steps"]
                if step.get("uses", "").startswith("actions/checkout@")
            )
            self.assertEqual(
                checkout["with"]["ref"],
                "${{ github.event.pull_request.head.sha || github.sha }}",
            )
            checkout["with"]["ref"] = "${{ needs.admit.outputs.head_sha }}"
            if new_name != "docs":
                self.assertEqual(legacy["needs"], ["authz"])
                self.assertNotIn("if", legacy)
            legacy["needs"] = ["admit"]
            condition = "needs.admit.outputs.allowed == 'true'"
            if new_name != "docs":
                condition += " && needs.admit.outputs.docs_only != 'true'"
            legacy["if"] = "${{ " + condition + " }}"
            with self.subTest(job=new_name):
                self.assertEqual(admitted[new_name], legacy)

    def test_renamed_lint_preserves_all_work_and_its_displayed_check(self):
        legacy = copy.deepcopy(workflow("ci-post.yml")["jobs"]["workflow-lint"])
        self.assertEqual(legacy["needs"], ["authz"])
        self.assertNotIn("if", legacy)
        legacy["needs"] = ["admit"]
        legacy["if"] = "${{ needs.admit.outputs.allowed == 'true' }}"
        for step in legacy["steps"]:
            self.assertEqual(step["if"], "${{ needs.authz.result == 'success' }}")
            step["if"] = "${{ needs.admit.result == 'success' }}"
            if step.get("uses", "").startswith("actions/checkout@"):
                self.assertEqual(
                    step["with"]["ref"],
                    "${{ github.event.pull_request.head.sha || github.sha }}",
                )
                step["with"]["ref"] = "${{ needs.admit.outputs.head_sha }}"
        jobs = workflow("gke-pr.yml")["jobs"]
        self.assertIn("lint", jobs)
        admitted = jobs["lint"]
        self.assertEqual(admitted, legacy)
        self.assertEqual(admitted["name"], "workflow-lint")

    def assert_policy_refuses(self, workflow_data, reason):
        sources = dict(self.sources)
        sources[".github/workflows/gke-pr.yml"] = yaml.safe_dump(
            workflow_data, sort_keys=False, width=1000
        )
        with self.assertRaisesRegex(ValueError, reason):
            self.policy.check_sources(sources, self.sources)

    def test_tree_refusals_precede_blob_fetches(self):
        row = {
            "path": ".github/workflows/ci.yml",
            "type": "blob",
            "mode": "100644",
            "size": 10,
            "sha": "a" * 40,
        }
        self.assertEqual(
            self.policy.tree_entries({"tree": [row], "truncated": False}), [row]
        )
        for update, reason in (
            ({"type": "commit"}, "type"),
            ({"mode": "120000"}, "mode"),
            ({"size": 1024 * 1024 + 1}, "size"),
            ({"path": ".github/workflows/ci.yml\n"}, "path"),
            ({"path": ".github/workflows/../ci.yml"}, "path"),
            ({"path": ".github/workflows/secret.env"}, "prohibited"),
        ):
            with (
                self.subTest(update=update),
                self.assertRaisesRegex(ValueError, reason),
            ):
                self.policy.tree_entries(
                    {"tree": [{**row, **update}], "truncated": False}
                )
        for tree, reason in (
            ({"tree": [row], "truncated": True}, "truncated"),
            ({"tree": [row] * 101, "truncated": False}, "count"),
            ({"tree": [row, row], "truncated": False}, "duplicate"),
            (
                {
                    "tree": [
                        {
                            **row,
                            "path": f".github/workflows/{index}.yml",
                            "size": 1024 * 1024 if index < 4 else 1,
                        }
                        for index in range(5)
                    ],
                    "truncated": False,
                },
                "total",
            ),
        ):
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(ValueError, reason),
            ):
                self.policy.tree_entries(tree)
        exact_total = [
            {**row, "path": f".github/workflows/{index}.yml", "size": 1024 * 1024}
            for index in range(4)
        ]
        self.assertEqual(
            self.policy.tree_entries({"tree": exact_total, "truncated": False}),
            exact_total,
        )
        exact_count = [
            {**row, "path": f".github/workflows/{index}.yml", "size": 0}
            for index in range(100)
        ]
        self.assertEqual(
            self.policy.tree_entries({"tree": exact_count, "truncated": False}),
            exact_count,
        )
        directory = {"path": ".github/actions/nested", "type": "tree"}
        nested = {**row, "path": ".github/actions/nested/action.yml"}
        self.assertEqual(
            self.policy.tree_entries({"tree": [directory, nested], "truncated": False}),
            [nested],
        )

    def test_yaml_parser_differentials_and_shape_bounds_refuse(self):
        good = "name: résumé\npermissions: {}\njobs:\n  x:\n    runs-on: ubuntu-24.04\n"
        self.policy.parse_yaml(good)
        for text, reason in (
            (good + "jobs: {}\n", "duplicate"),
            (good + "<<: value\n", "merge"),
            (good + "x: &anchor value\n", "anchor"),
            (good + "x: *anchor\n", "alias"),
            (good + "x: !!str value\n", "tag"),
            (good + "x: {runs-on: ubuntu-24.04}\n", "flow mapping"),
            (good + "---\nx: y\n", "document"),
            (good + "...\n", "document"),
            (good + "? key\n: value\n", "explicit"),
            (good + '"escaped\\x20key": value\n', "quoted key"),
            ("\ufeff" + good, "character"),
            (good + "x: a\rb\n", "character"),
            (good + "x: \x00\n", "character"),
            (good + "x: \u202evalue\n", "character"),
            (good + "\tx: y\n", "tab"),
            ("x: " + "a" * 4094, "line length"),
            ("x: y\n" + "\n" * 19999, "line count"),
            ("\n".join("  " * index + "x:" for index in range(41)), "depth"),
            ("x: " + "[" * 41 + "a" + "]" * 41, "depth"),
            ('permissions: {contents: "read"}\n', "plain permission"),
        ):
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(ValueError, reason),
            ):
                self.policy.parse_yaml(text)
        positive = (
            "permissions: {contents: read} # trailing\n"
            "x: ['#', '*literal'] # trailing\n"
            "run: |\n  echo && echo '${{ github.event.pull_request.title }}'\n"
        )
        self.policy.parse_yaml(positive)
        self.policy.parse_yaml("---\n" + good)
        self.policy.parse_yaml(good.replace("\n", "\r\n"))
        self.policy.parse_yaml("x: " + "a" * 4093)
        self.policy.parse_yaml("x: " + "[" * 40 + "a" + "]" * 40)
        self.policy.parse_yaml("\n".join("  " * index + "x:" for index in range(40)))
        self.policy.parse_yaml("x: y\n" + "\n" * 19998)
        alias = "runner: &pool\n  group: gke-runners\njobs:\n  x:\n    runs-on: *pool\n"
        self.assertEqual(
            yaml.load(alias, Loader=yaml.BaseLoader)["jobs"]["x"]["runs-on"],
            {"group": "gke-runners"},
        )
        with self.assertRaisesRegex(ValueError, "anchor|alias"):
            self.policy.parse_yaml(alias)
        # A multiline simple key may be refused by the scanner before it emits
        # the key token; either path must identify this specific syntax refusal.
        with self.assertRaisesRegex(
            (ValueError, yaml.YAMLError),
            r"quoted key|expected ':'|mapping values are not allowed",
        ):
            self.policy.parse_yaml(good + '"multi\n line": value\n')

    def test_blob_length_hash_encoding_and_utf8_are_checked_before_use(self):
        import base64
        import hashlib

        content = b"name: Valid\n"
        digest = hashlib.sha1(
            b"blob " + str(len(content)).encode() + b"\0" + content,
            usedforsecurity=False,
        ).hexdigest()
        row = {"size": len(content), "sha": digest}
        payload = {
            "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        }
        self.assertEqual(self.policy.decode_blob(row, payload), content.decode())
        for update, response, reason in (
            ({"size": len(content) + 1}, payload, "size"),
            ({"sha": "a" * 40}, payload, "SHA"),
            ({}, {**payload, "encoding": "utf-8"}, "encoding"),
            ({}, {**payload, "content": "!"}, "base64"),
        ):
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(ValueError, reason),
            ):
                self.policy.decode_blob({**row, **update}, response)
        invalid = b"\xff"
        invalid_row = {
            "size": 1,
            "sha": hashlib.sha1(b"blob 1\0\xff", usedforsecurity=False).hexdigest(),
        }
        with self.assertRaises(UnicodeDecodeError):
            self.policy.decode_blob(
                invalid_row,
                {"encoding": "base64", "content": base64.b64encode(invalid).decode()},
            )

    def test_sensitive_names_and_symlinks_are_refused_without_opening_targets(self):
        for name in (
            ".config/zsh/keys/example",
            ".ssh/example",
            ".aws/example",
            ".codex/example",
            ".config/pineforge/example",
            "example.env",
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "prohibited"),
            ):
                self.policy.guarded(ROOT / "build/pr-admission-tests" / name)
            with self.assertRaisesRegex(ValueError, "prohibited"):
                read(ROOT / "build/pr-admission-tests" / name)
        parent = ROOT / "build/pr-admission-tests"
        print(f"FILES symlink fixture directory: {parent}", flush=True)
        parent.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(dir=parent))
        link = root / "workflow.yml"
        print(f"FILES symlink fixture: {link} -> missing.env", flush=True)
        link.symlink_to("missing.env")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.policy.read_files([link])
        with self.assertRaisesRegex(ValueError, "symlink"):
            read(link)

    def test_compare_rename_truncation_and_code_veto(self):
        self.assertEqual(
            self.policy.compare_paths(
                {"files": [{"filename": "docs/a.md", "status": "modified"}]}
            ),
            ["docs/a.md"],
        )
        paths = self.policy.compare_paths(
            {
                "files": [
                    {
                        "filename": "docs/a.md",
                        "previous_filename": "src/a.cpp",
                        "status": "renamed",
                    }
                ]
            }
        )
        self.assertFalse(self.policy.docs_only(paths))
        for paths in (
            [],
            ["docs-evil/a.md"],
            [".github_evil/x"],
            ["tests/release/pr_admission.py"],
            ["docs/a.md", "src/a.cpp"],
            [".github/workflows/ci.yml"],
            ["docs/CMakeLists.txt"],
            ["LICENSE.txt"],
        ):
            self.assertFalse(self.policy.docs_only(paths), paths)
        self.assertTrue(self.policy.docs_only(["docs/a.md", "README.md"]))
        for response in (
            {},
            {"files": []},
            {"files": [{"filename": "docs/a.md", "status": "renamed"}]},
            {"files": [{"filename": "docs/a.md"}] * 300},
            {"files": [{"filename": "../docs/a.md"}]},
        ):
            with self.assertRaisesRegex(ValueError, "compare"):
                self.policy.compare_paths(response)

    def run_compare_case(self, mode, pages, *, admitted_docs=True):
        """Exercise real entry points; replace only the remote GitHub API."""
        import base64
        import hashlib

        repo, base, head = "owner/project", "a" * 40, "b" * 40
        compare = f"repos/{repo}/compare/{base}...{head}?per_page=100&page="
        responses = {
            f"repos/{repo}/collaborators/{login}/permission": {"permission": "write"}
            for login in ("author", "actor", "rerunner")
        }
        responses[f"repos/{repo}/pulls/7"] = {
            "state": "open",
            "draft": False,
            "base": {"ref": "main"},
            "head": {
                "sha": head,
                "repo": {"full_name": repo, "fork": False},
            },
            "user": {"login": "author"},
        }
        tree = []
        for name, text in self.sources.items():
            content = text.encode("utf-8")
            digest = hashlib.sha1(
                f"blob {len(content)}\0".encode("ascii") + content,
                usedforsecurity=False,
            ).hexdigest()
            tree.append(
                {
                    "path": name,
                    "type": "blob",
                    "mode": "100644",
                    "size": len(content),
                    "sha": digest,
                }
            )
            responses[f"repos/{repo}/git/blobs/{digest}"] = {
                "encoding": "base64",
                "content": base64.b64encode(content).decode("ascii"),
            }
        responses[f"repos/{repo}/git/trees/{head}?recursive=1"] = {
            "tree": tree,
            "truncated": False,
        }
        requests = []

        def remote(endpoint):
            requests.append(endpoint)
            if endpoint.startswith(compare):
                page = int(endpoint.removeprefix(compare))
                if page not in pages:
                    raise subprocess.CalledProcessError(1, ["gh", "api", endpoint])
                response = pages[page]
                if isinstance(response, Exception):
                    raise response
                return copy.deepcopy(response)
            return copy.deepcopy(responses[endpoint])

        needs = {
            name: {
                "result": (
                    "skipped"
                    if admitted_docs and name in self.policy.HEAVY_JOBS
                    else "success"
                )
            }
            for name in self.policy.RESULT_JOBS
        }
        needs["admit"]["outputs"] = {
            "allowed": "true",
            "docs_only": str(admitted_docs).lower(),
        }
        parent = ROOT / "build/pr-admission-tests"
        print(f"FILES compare fixture directory: {parent}", flush=True)
        parent.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="compare-", dir=parent))
        output = directory / "admit-output"
        print(f"FILES compare output: {output}", flush=True)
        environment = {
            "REPO": repo,
            "HEAD_REPO": repo,
            "HEAD_IS_FORK": "false",
            "DRAFT": "false",
            "BASE_REF": "main",
            "BASE_SHA": base,
            "HEAD_SHA": head,
            "PR_NUMBER": "7",
            "PR_AUTHOR": "author",
            "ACTOR": "actor",
            "TRIGGERING_ACTOR": "rerunner",
            "RUNNER_TEMP": str(directory),
            "GITHUB_OUTPUT": str(output),
            "NEEDS": json.dumps(needs),
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(self.policy, "api", side_effect=remote),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = self.policy.main([mode])
        stream = stdout if status == 0 else stderr
        verdict = json.loads(stream.getvalue().splitlines()[-1])
        values = (
            dict(line.split("=", 1) for line in read(output).splitlines())
            if output.exists()
            else {}
        )
        compared = [endpoint for endpoint in requests if endpoint.startswith(compare)]
        return status, verdict, values, compared, compare

    @staticmethod
    def docs_page(start=0, count=100):
        return {
            "files": [
                {"filename": f"docs/{index:04d}.md", "status": "modified"}
                for index in range(start, start + count)
            ]
        }

    def test_compare_full_page_is_not_complete_without_a_short_continuation(self):
        with self.assertRaisesRegex(ValueError, "compare"):
            self.policy.compare_paths(self.docs_page())
        for mode in ("admit", "result"):
            with self.subTest(mode=mode):
                status, verdict, values, requested, prefix = self.run_compare_case(
                    mode, {1: self.docs_page(), 2: {"files": []}}
                )
                self.assertEqual(status, 0, verdict)
                self.assertEqual(verdict["verdict"], "PASS")
                self.assertEqual(requested, [prefix + "1", prefix + "2"])
                if mode == "admit":
                    self.assertEqual(values["docs_only"], "true")
                    self.assertEqual(values["code"], "false")
                    self.assertEqual(values["allowed"], "true")

    def test_compare_code_after_one_hundred_docs_cannot_skip_heavy_proofs(self):
        pages = {
            1: self.docs_page(),
            2: {"files": [{"filename": "src/x.cpp", "status": "modified"}]},
        }
        for mode in ("admit", "result"):
            with self.subTest(mode=mode):
                status, verdict, values, requested, prefix = self.run_compare_case(
                    mode, pages, admitted_docs=False
                )
                self.assertEqual(status, 0, verdict)
                self.assertEqual(verdict["verdict"], "PASS")
                self.assertEqual(requested, [prefix + "1", prefix + "2"])
                if mode == "admit":
                    self.assertEqual(values["docs_only"], "false")
                    self.assertEqual(values["code"], "true")
                    self.assertEqual(values["allowed"], "true")
        status, verdict, _, requested, prefix = self.run_compare_case(
            "result", pages, admitted_docs=True
        )
        self.assertEqual(status, 1, verdict)
        self.assertEqual(verdict["verdict"], "REFUSED")
        self.assertEqual(requested, [prefix + "1", prefix + "2"])

    def test_compare_three_thousand_rows_are_refused_in_both_modes(self):
        pages = {page: self.docs_page((page - 1) * 100) for page in range(1, 31)}
        for mode in ("admit", "result"):
            with self.subTest(mode=mode):
                status, verdict, values, requested, prefix = self.run_compare_case(
                    mode, pages
                )
                self.assertEqual(status, 1, verdict)
                self.assertEqual(verdict["verdict"], "REFUSED")
                self.assertEqual(values, {})
                self.assertEqual(
                    requested, [prefix + str(page) for page in range(1, 31)]
                )

    def test_compare_last_short_page_keeps_rename_origins_below_the_cap(self):
        pages = {page: self.docs_page((page - 1) * 100) for page in range(1, 30)}
        pages[30] = self.docs_page(2900, 98)
        pages[30]["files"].append(
            {
                "filename": "docs/moved.md",
                "previous_filename": "src/x.cpp",
                "status": "renamed",
            }
        )
        for mode in ("admit", "result"):
            with self.subTest(mode=mode):
                status, verdict, values, requested, prefix = self.run_compare_case(
                    mode, pages, admitted_docs=False
                )
                self.assertEqual(status, 0, verdict)
                self.assertEqual(verdict["verdict"], "PASS")
                self.assertEqual(
                    requested, [prefix + str(page) for page in range(1, 31)]
                )
                if mode == "admit":
                    self.assertEqual(values["docs_only"], "false")
                    self.assertEqual(values["code"], "true")
                    self.assertEqual(values["allowed"], "true")

    def test_compare_unreadable_second_page_refuses_in_both_modes(self):
        for response in (
            {},
            {"files": None},
            subprocess.CalledProcessError(1, ["gh", "api", "compare"]),
            {"files": [{"filename": "docs/moved.md", "status": "renamed"}]},
            {"files": [{"filename": "../src/x.cpp"}]},
        ):
            for mode in ("admit", "result"):
                with self.subTest(mode=mode, response=response):
                    status, verdict, values, requested, prefix = self.run_compare_case(
                        mode, {1: self.docs_page(), 2: response}
                    )
                    self.assertEqual(status, 1, verdict)
                    self.assertEqual(verdict["verdict"], "REFUSED")
                    self.assertEqual(values, {})
                    self.assertEqual(requested, [prefix + "1", prefix + "2"])

    def test_result_requires_every_job_and_rejects_disagreement(self):
        names = self.policy.RESULT_JOBS
        needs = {name: {"result": "success"} for name in names}
        needs["admit"]["outputs"] = {"allowed": "true", "docs_only": "false"}
        self.policy.judge(needs, ["src/a.cpp"], False)
        for name in names:
            for status in ("failure", "cancelled", "skipped"):
                bad = copy.deepcopy(needs)
                bad[name]["result"] = status
                with (
                    self.subTest(job=name, status=status),
                    self.assertRaisesRegex(ValueError, "result"),
                ):
                    self.policy.judge(bad, ["src/a.cpp"], False)
        for name in self.policy.HEAVY_JOBS:
            needs[name]["result"] = "skipped"
        needs["admit"]["outputs"]["docs_only"] = "true"
        self.policy.judge(needs, ["docs/a.md"], True)
        for name in names:
            expected = "skipped" if name in self.policy.HEAVY_JOBS else "success"
            for status in ("success", "failure", "cancelled", "skipped"):
                if status == expected:
                    continue
                bad = copy.deepcopy(needs)
                bad[name]["result"] = status
                with (
                    self.subTest(docs_job=name, status=status),
                    self.assertRaisesRegex(
                        ValueError, f"result {name} must be {expected}"
                    ),
                ):
                    self.policy.judge(bad, ["docs/a.md"], True)
        with self.assertRaisesRegex(ValueError, "classification"):
            self.policy.judge(needs, ["docs/a.md"], False)
        with self.assertRaisesRegex(ValueError, "veto"):
            self.policy.judge(needs, ["src/a.py"], True)
        with self.assertRaisesRegex(ValueError, "veto"):
            self.policy.judge(needs, ["docs/a.py"], True)
        del needs["memory-gate"]
        with self.assertRaisesRegex(ValueError, "inventory"):
            self.policy.judge(needs, ["docs/a.md"], True)

    def test_run_binding_refuses_spoofs_and_old_success(self):
        run = {
            "id": 12,
            "path": ".github/workflows/gke-pr.yml@refs/heads/main",
            "event": "pull_request_target",
            "display_title": "gke-pr #7 " + "b" * 40,
            "conclusion": "success",
        }
        self.assertTrue(self.policy.bound_run([run], 7, "b" * 40))
        for field, value in (
            ("path", ".github/workflows/gke-pr.yml"),
            ("path", ".github/workflows/gke-pr.yml@refs/heads/other"),
            ("event", "pull_request"),
            ("display_title", run["display_title"] + " spoof"),
        ):
            self.assertFalse(
                self.policy.bound_run([{**run, field: value}], 7, "b" * 40)
            )
        self.assertFalse(
            self.policy.bound_run(
                [run, {**run, "id": 13, "conclusion": "cancelled"}], 7, "b" * 40
            )
        )
        self.assertFalse(self.policy.bound_run([], 7, "b" * 40))
        self.assertFalse(
            self.policy.bound_run(
                [{**run, "display_title": run["display_title"] + "\n"}],
                7,
                "b" * 40 + "\n",
            )
        )


if __name__ == "__main__":
    unittest.main()
