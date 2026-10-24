"""Pin the hosted heavy job and exercise the canonical aggregate's judgments."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
HEAVY_CONDITION = (
    "github.event.pull_request.draft != true && needs.changes.outputs.code == 'true'"
)


class MemoryGateWiringTests(unittest.TestCase):
    def setUp(self):
        print(f"FILES workflow read: {WORKFLOW}", file=sys.stderr, flush=True)
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.jobs = yaml.load(self.text, Loader=yaml.BaseLoader)["jobs"]

    def test_memory_job_is_hosted_authorized_and_skips_drafts(self):
        memory = self.jobs["memory-gate"]
        self.assertEqual(memory["name"], "HPO memory growth gate")
        self.assertEqual(memory["needs"], ["authz", "changes"])
        self.assertEqual(memory["if"], HEAVY_CONDITION)
        self.assertEqual(memory["runs-on"], "ubuntu-24.04")
        self.assertNotIn("strategy", memory)
        self.assertNotIn("continue-on-error", memory)
        for step in memory["steps"]:
            self.assertNotIn("continue-on-error", step)
            self.assertNotIn("if", step)

    def test_external_actions_are_sha_pinned_with_version_comments(self):
        memory = self.jobs["memory-gate"]
        actions = [step for step in memory["steps"] if "uses" in step]
        self.assertEqual(
            [step["uses"] for step in actions],
            [
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
            ],
        )
        self.assertEqual(
            actions[0]["with"],
            {
                "ref": "${{ github.event.pull_request.head.sha || github.sha }}",
                "submodules": "true",
                "persist-credentials": "false",
            },
        )
        body = self.text.split("\n  memory-gate:\n")[1].split("\n  build-gate:\n")[0]
        lines = [line.strip() for line in body.splitlines() if "uses:" in line]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertRegex(
                line, r"^uses: actions/[\w-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$"
            )

    def test_job_builds_native_fixture_and_runs_the_real_gate_directly(self):
        steps = {step["name"]: step for step in self.jobs["memory-gate"]["steps"]}
        configure = steps["Configure Release native CLI and fixture"]["run"]
        for flag in (
            "-DCMAKE_BUILD_TYPE=Release",
            "-DPINEFORGE_HPO_BUILD_ENGINE_ADAPTER=ON",
            "-DPINEFORGE_HPO_BUILD_NATIVE_CLI=ON",
            "-DPINEFORGE_HPO_BUILD_TESTS=ON",
        ):
            self.assertIn(flag, configure)
        self.assertIn("external/pineforge-engine", configure)
        build = steps["Build native CLI and fixture"]["run"]
        self.assertIn(
            "--target pineforge-hpo-native pineforge_hpo_fake_strategy", build
        )
        run = steps["Gate child peak RSS growth"]["run"]
        for command in (
            "set -euo pipefail",
            "python3 -I scripts/ci_memory_gate.py",
            "--native build/bin/pineforge-hpo-native",
            "--plugin build/lib/fake_strategy.so",
            "--ohlcv build/memory-gate-bars.csv",
            "--artifacts build/memory-gate --bars-axis",
        ):
            self.assertIn(command, run)
        self.assertNotIn("||", run)
        self.assertNotIn("run_ci_memory_gate.sh", run)
        release = self.jobs["release-notification"]["steps"]
        runs = "\n".join(step.get("run", "") for step in release)
        self.assertIn("unittest discover -s tests/release -v", runs)
        self.assertIn(
            "ruff check scripts/notify_release_hub.py scripts/ci_memory_gate.py", runs
        )
        self.assertIn(
            "ruff format --check --diff scripts/notify_release_hub.py "
            "scripts/ci_memory_gate.py",
            runs,
        )

    def assert_aggregate_contract(self, jobs):
        aggregate = jobs["build-gate"]
        self.assertEqual(aggregate["if"], "${{ always() }}")
        self.assertEqual(aggregate["needs"], sorted(set(jobs) - {"build-gate"}))
        step = aggregate["steps"][-1]
        self.assertEqual(step["env"]["NEEDS"], "${{ toJSON(needs) }}")
        self.assertEqual(
            step["env"]["DOCS_ONLY"], "${{ steps.classify.outputs.docs_only }}"
        )
        self.assertEqual(
            step["env"]["WORKFLOWS_ONLY"],
            "${{ steps.classify.outputs.workflows_only }}",
        )
        self.assertEqual(
            json.loads(step["env"]["HEAVY_JOBS"]),
            sorted(
                name
                for name, job in jobs.items()
                if job.get("needs") == ["authz", "changes"]
            ),
        )

    def aggregate(self, docs_only="false", workflows_only="false", **results):
        self.assert_aggregate_contract(self.jobs)
        step = self.jobs["build-gate"]["steps"][-1]
        required = self.jobs["build-gate"]["needs"]
        needs = {name: {"result": "success"} for name in required}
        self.assertLessEqual(results.keys(), needs.keys())
        for name, result in results.items():
            needs[name]["result"] = result
        scratch = ROOT / "build/memory-gate-wiring-tests"
        print(f"FILES aggregate test directory: {scratch}", file=sys.stderr, flush=True)
        scratch.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="case-", dir=scratch))
        print(
            f"FILES aggregate executable and cwd: /bin/bash, {directory}",
            file=sys.stderr,
            flush=True,
        )
        return subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-c", step["run"]],
            cwd=directory,
            env={
                **os.environ,
                **step["env"],
                "NEEDS": json.dumps(needs),
                "DOCS_ONLY": docs_only,
                "WORKFLOWS_ONLY": workflows_only,
                "RUNNER_TEMP": str(directory),
            },
            capture_output=True,
            text=True,
        )

    def test_aggregate_requires_memory_success_on_code_changes(self):
        result = self.aggregate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for status in ("failure", "cancelled", "skipped"):
            with self.subTest(status=status):
                result = self.aggregate(**{"memory-gate": status})
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("::error::", result.stdout)

    def test_only_proven_cheap_changes_accept_skips(self):
        skipped = {
            name: "skipped"
            for name in ("memory-gate", "native", "python", "release-notification")
        }
        for docs_only, workflows_only in (("true", "false"), ("false", "true")):
            result = self.aggregate(docs_only, workflows_only, **skipped)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name in ("authz", "changes", "workflow-lint", *skipped):
                statuses = (
                    ("failure", "cancelled")
                    if name in skipped
                    else ("failure", "cancelled", "skipped")
                )
                for status in statuses:
                    with self.subTest(job=name, status=status):
                        result = self.aggregate(
                            docs_only, workflows_only, **{**skipped, name: status}
                        )
                        self.assertNotEqual(
                            result.returncode, 0, result.stdout + result.stderr
                        )
        for proof in (
            ("false", "false"),
            ("true", "true"),
            ("", "false"),
            ("true", ""),
        ):
            with self.subTest(proof=proof):
                result = self.aggregate(*proof, **skipped)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_omitting_memory_from_aggregate_or_heavy_jobs_is_rejected(self):
        self.assert_aggregate_contract(self.jobs)
        for field in ("needs", "HEAVY_JOBS"):
            mutated = copy.deepcopy(self.jobs)
            aggregate = mutated["build-gate"]
            if field == "needs":
                aggregate["needs"].remove("memory-gate")
            else:
                environment = aggregate["steps"][-1]["env"]
                heavy = json.loads(environment["HEAVY_JOBS"])
                heavy.remove("memory-gate")
                environment["HEAVY_JOBS"] = json.dumps(heavy)
            with self.subTest(field=field), self.assertRaises(AssertionError):
                self.assert_aggregate_contract(mutated)


if __name__ == "__main__":
    unittest.main()
