"""Keep admitted routing literal and preserve the complete native proof suites."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
POOL = ["self-hosted", "pf-ci", "pf-ci-x64"]
POOL_ROUTE = """${{ github.event_name != 'pull_request' &&
    github.event_name != 'pull_request_target' &&
    github.actor != 'dependabot[bot]' && github.triggering_actor != 'dependabot[bot]' &&
    contains(fromJSON('["push","workflow_dispatch","schedule","release"]'), github.event_name) &&
    fromJSON('["self-hosted","pf-ci","pf-ci-x64"]') || 'ubuntu-24.04' }}"""
OLD_ROUTE = """${{ github.event_name != 'pull_request_target' &&
    github.actor != 'dependabot[bot]' && github.triggering_actor != 'dependabot[bot]' &&
    contains(fromJSON('["push","pull_request","workflow_dispatch","schedule","release"]'), github.event_name) &&
    (github.event_name != 'pull_request' ||
     (github.event.pull_request.head.repo.full_name == github.repository && github.event.pull_request.head.repo.fork == false)) &&
    fromJSON('["self-hosted","pf-ci","pf-ci-x64"]') || 'ubuntu-24.04' }}"""
POST_MERGE_STEPS = (
    "Configure PineForge Engine headers",
    "Configure PineForge HPO",
    "Build PineForge window planner helper",
    "Build",
    "Test",
    "Cross-vendor reproducibility and Intel checkpoint restore",
    "Upload cross-vendor evidence",
)
LINUX_POST_MERGE_STEPS = (
    "Set up Python for end-to-end test",
    "Build PineForge Engine runtime",
    "Install end-to-end Python packages",
    "Run README Quick Start study",
    "Real symbol-feed and D7 equality gate",
    "v0.8.0 no-feed and canonical harness contract gate",
    "v0.9.0 valid-study and silent-failure contract gate",
)
PR_CORE_STEPS = (
    "Configure PR native core",
    "Build PR native core",
    "Test PR native core",
)
PR_CORE_TEST = (
    "ctest --test-dir build -V --no-tests=error -R '^pineforge_hpo_(serial_golden|portable_math_accuracy|portable_math_contraction)$'",
    "ctest --test-dir build --output-on-failure --no-tests=error",
)
ADAPTER_BLOCK_OPEN = "if(UNIX AND TARGET PineForgeHPO::engine_adapter)"
ENGINE_REFERENCE = re.compile(
    r"engine_adapter|pineforge-hpo-native|PINEFORGE_ENGINE_|fake_strategy"
)
AUTHZ_STEP_NAME = "Only our own pull requests and our own events run CI"
AGGREGATE_STEP_NAME = "Every needed job succeeded"
RELEASE_PIN_LIGHT_COMMAND = (
    "python3 -m unittest tests.python.test_release_pins.RepositoryReleasePinTests -v"
)


def compact(text):
    return " ".join(text.split())


def commands(run):
    """Shell commands of a step: continuation lines joined, blank lines dropped."""
    joined = run.replace("\\\n", " ")
    return [compact(line) for line in joined.splitlines() if line.strip()]


def adapter_block(lines):
    """First and last index of the engine-adapter block of tests/CMakeLists.txt."""
    start = lines.index(ADAPTER_BLOCK_OPEN)
    depth = 0
    for index in range(start, len(lines)):
        text = lines[index].strip()
        if text.startswith("if("):
            depth += 1
        elif text == "endif()":
            depth -= 1
            if depth == 0:
                return start, index
    raise AssertionError("the engine-adapter block is not closed")


def engine_references_outside_block(cmake_text):
    lines = cmake_text.splitlines()
    start, end = adapter_block(lines)
    outside = lines[:start] + lines[end + 1 :]
    return [line for line in outside if ENGINE_REFERENCE.search(line)]


def workflow_step(workflow, job_id, step_name):
    return job_step(workflow["jobs"][job_id], step_name)


def job_step(job, step_name):
    return next(step for step in job["steps"] if step["name"] == step_name)


def evaluate(expression, **context):
    expression = compact(expression).removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", "not ", expression)
    return eval(
        expression,
        {"__builtins__": {}},
        {
            "fromJSON": json.loads,
            "contains": lambda values, value: value in values,
            "false": False,
            "true": True,
            **context,
        },
    )


class PrTierWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.pr_workflows = {
            name: yaml.load(
                (ROOT / ".github/workflows" / name).read_text(),
                Loader=yaml.BaseLoader,
            )
            for name in ("ci.yml", "benchmark-smoke.yml")
        }
        self.workflows = {
            name: yaml.load(
                (ROOT / ".github/workflows" / name).read_text(),
                Loader=yaml.BaseLoader,
            )
            for name in ("ci-post.yml", "benchmark-smoke-post.yml")
        }
        self.admitted = yaml.load(
            (ROOT / ".github/workflows/gke-pr.yml").read_text(),
            Loader=yaml.BaseLoader,
        )

    def assert_routing(self, workflows):
        ci = workflows["ci-post.yml"]["jobs"]
        benchmark = workflows["benchmark-smoke-post.yml"]["jobs"]
        for job_id, runner in (
            ("release-notification", "gke-8cpu-16gb-amd64"),
            ("python", "gke-16cpu-32gb-amd64"),
            ("memory-gate", "gke-16cpu-32gb-amd64"),
            ("native", "gke-16cpu-32gb-amd64"),
        ):
            self.assertEqual(ci[job_id]["runs-on"], runner)
        self.assertEqual(benchmark["optuna"]["runs-on"], "gke-16cpu-32gb-amd64")
        self.assertEqual(ci["native"]["strategy"]["matrix"], {"os": ["ubuntu-latest"]})
        self.assertEqual(ci["native-macos"]["runs-on"], "macos-latest")
        self.assertEqual(
            ci["native-macos"]["strategy"]["matrix"], {"os": ["macos-latest"]}
        )
        for jobs, aggregate in ((ci, "build-gate"), (benchmark, "benchmark-check")):
            for job_id in ("authz", aggregate):
                self.assertEqual(jobs[job_id]["runs-on"], "ubuntu-24.04")
            self.assertNotIn("changes", jobs)
            self.assertEqual(jobs["workflow-lint"]["runs-on"], "gke-8cpu-16gb-amd64")

    def test_no_route_is_a_conditional_expression(self):
        # Every pod allocation uses a scalar label. Authorization happens before
        # allocation; a conditional route cannot substitute another runner.
        ci = self.workflows["ci-post.yml"]["jobs"]
        benchmark = self.workflows["benchmark-smoke-post.yml"]["jobs"]
        routes = (
            ci["release-notification"]["runs-on"],
            ci["python"]["runs-on"],
            ci["memory-gate"]["runs-on"],
            ci["native"]["runs-on"],
            benchmark["optuna"]["runs-on"],
        )
        for route in routes:
            with self.subTest(route=route):
                self.assertNotIn("${{", route)
                self.assertRegex(route, r"^gke-(8cpu-16gb|16cpu-32gb)-amd64$")
        self.assert_routing(self.workflows)

    def test_old_pool_routing_and_pool_fallback_are_rejected(self):
        for bad_route in (
            OLD_ROUTE,
            POOL_ROUTE,
            POOL_ROUTE.replace("'ubuntu-24.04'", f"fromJSON('{json.dumps(POOL)}')"),
        ):
            for job_id in ("release-notification", "python", "memory-gate"):
                with self.subTest(job=job_id, route=bad_route):
                    mutated = copy.deepcopy(self.workflows)
                    mutated["ci-post.yml"]["jobs"][job_id]["runs-on"] = bad_route
                    with self.assertRaises(AssertionError):
                        self.assert_routing(mutated)
            mutated = copy.deepcopy(self.workflows)
            mutated["ci-post.yml"]["jobs"]["native"]["runs-on"] = bad_route
            with self.assertRaises(AssertionError):
                self.assert_routing(mutated)
            mutated = copy.deepcopy(self.workflows)
            mutated["benchmark-smoke-post.yml"]["jobs"]["optuna"]["runs-on"] = bad_route
            with self.assertRaises(AssertionError):
                self.assert_routing(mutated)

    def test_check_names_permissions_and_unfiltered_pr_triggers_are_preserved(self):
        for name, workflow in self.pr_workflows.items():
            with self.subTest(pr_workflow=name):
                self.assertEqual(
                    workflow["name"],
                    "CI" if name == "ci.yml" else "Benchmark smoke",
                )
                self.assertEqual(
                    set(workflow["on"]), {"pull_request", "workflow_dispatch"}
                )
                self.assertEqual(set(workflow["jobs"]), {"authz"})
                trigger = workflow["on"]["pull_request"] or {}
                self.assertNotIn("paths", trigger)
                self.assertNotIn("paths-ignore", trigger)
                self.assertEqual(workflow["jobs"]["authz"]["name"], "authz")
                self.assertEqual(workflow["jobs"]["authz"]["runs-on"], "ubuntu-24.04")
                self.assertEqual(workflow["jobs"]["authz"]["permissions"], {})
                self.assertEqual(
                    workflow["jobs"]["authz"],
                    self.workflows[name.replace(".yml", "-post.yml")]["jobs"]["authz"],
                )
                self.assertEqual(workflow["permissions"], {"contents": "read"})
                text = (ROOT / ".github/workflows" / name).read_text()
                self.assertNotIn("secrets.", text)
                self.assertNotIn("create-github-app-token@", text)
                self.assertNotIn("id-token:", text)
                self.assertNotIn("contents: write", text)
        expected_names = {
            "ci-post.yml": (
                "CI post-merge",
                {
                    "authz": "authz",
                    "workflow-lint": "workflow-lint",
                    "light": "Light",
                    "release-notification": "Release notification contract",
                    "native": "Native (${{ matrix.os }})",
                    "native-macos": "Native (${{ matrix.os }})",
                    "python": "Python ${{ matrix.python }}",
                    "memory-gate": "HPO memory growth gate",
                    "build-gate": "build-gate",
                },
            ),
            "benchmark-smoke-post.yml": (
                "Benchmark smoke post-merge",
                {
                    "authz": "authz",
                    "workflow-lint": "workflow-lint",
                    "optuna": "Native TPE vs Optuna",
                    "benchmark-check": "benchmark-check",
                },
            ),
        }
        for name, workflow in self.workflows.items():
            with self.subTest(workflow=name):
                expected_workflow, expected_jobs = expected_names[name]
                self.assertEqual(workflow["name"], expected_workflow)
                self.assertEqual(
                    {
                        job_id: job.get("name", job_id)
                        for job_id, job in workflow["jobs"].items()
                    },
                    expected_jobs,
                )
                self.assertEqual(workflow["permissions"], {"contents": "read"})
                self.assertEqual(set(workflow["on"]), {"push", "workflow_dispatch"})
                self.assertEqual(workflow["on"]["push"], {"branches": ["main"]})
                for job_id, job in workflow["jobs"].items():
                    self.assertEqual(
                        job.get("permissions", workflow["permissions"]),
                        {} if job_id == "authz" else {"contents": "read"},
                    )
                text = (ROOT / ".github/workflows" / name).read_text()
                self.assertNotIn("secrets.", text)
                self.assertNotIn("create-github-app-token@", text)
                self.assertNotIn("id-token:", text)
                self.assertNotIn("contents: write", text)
        matrix = self.workflows["ci-post.yml"]["jobs"]["native"]["strategy"]["matrix"]
        self.assertEqual(matrix["os"], ["ubuntu-latest"])
        macos = self.workflows["ci-post.yml"]["jobs"]["native-macos"]
        self.assertEqual(macos["strategy"]["matrix"]["os"], ["macos-latest"])
        self.assertEqual(
            self.workflows["ci-post.yml"]["jobs"]["python"]["strategy"]["matrix"]["python"],
            ["3.11", "3.14"],
        )

    def test_license_entity_is_an_unconditional_light_check(self):
        jobs = self.workflows["ci-post.yml"]["jobs"]
        light = self.admitted["jobs"]["light"]
        self.assertEqual(light["needs"], ["admit"])
        self.assertEqual(light["if"], "${{ needs.admit.outputs.allowed == 'true' }}")
        self.assertEqual(light["runs-on"], "gke-8cpu-16gb-amd64")
        self.assertNotIn("if", jobs["light"])
        for step in light["steps"]:
            self.assertEqual(step["if"], "${{ needs.admit.result == 'success' }}")
        self.assertEqual(
            job_step(light, "Check license entity")["run"],
            "python3 -m unittest tests/python/test_license_entity.py -v",
        )
        self.assertEqual(
            jobs["build-gate"]["needs"],
            [
                "authz",
                "workflow-lint",
                "light",
                "release-notification",
                "native",
                "native-macos",
                "python",
                "memory-gate",
            ],
        )

    def assert_light_release_pin_check(self, light):
        self.assertEqual(
            [
                command
                for step in light.get("steps", [])
                for command in commands(step.get("run", ""))
            ],
            [
                "python3 -m unittest tests/python/test_license_entity.py -v",
                RELEASE_PIN_LIGHT_COMMAND,
            ],
        )
        self.assertEqual(light["needs"], ["admit"])
        self.assertEqual(light["if"], "${{ needs.admit.outputs.allowed == 'true' }}")
        for step in light["steps"]:
            self.assertEqual(step.get("if"), "${{ needs.admit.result == 'success' }}")
        checkout = job_step(light, "Check out PineForge HPO")
        self.assertEqual(checkout.get("with", {}).get("submodules", "false"), "false")

    def test_documentation_only_prs_run_release_pin_checks(self):
        light = self.admitted["jobs"].get("light", {})
        self.assert_light_release_pin_check(light)
        # Exact executable commands: mentioning the file cannot cover the README
        # typo, and selecting the full suite would require the producers.
        for run in (
            "true",
            "# " + RELEASE_PIN_LIGHT_COMMAND,
            "echo " + RELEASE_PIN_LIGHT_COMMAND,
            RELEASE_PIN_LIGHT_COMMAND + " || true",
            RELEASE_PIN_LIGHT_COMMAND.replace(
                "RepositoryReleasePinTests", "ReleasePinTests"
            ),
            "python3 -m unittest discover -s tests/python -v",
        ):
            with self.subTest(run=run):
                mutated = copy.deepcopy(light)
                job_step(mutated, "Check release pins")["run"] = run
                with self.assertRaises(AssertionError):
                    self.assert_light_release_pin_check(mutated)
        mutated = copy.deepcopy(light)
        mutated["steps"] = [
            step for step in mutated["steps"] if step["name"] != "Check release pins"
        ]
        with self.assertRaises(AssertionError):
            self.assert_light_release_pin_check(mutated)
        for key, value in (
            ("needs", ["authz", "changes"]),
            ("if", "needs.changes.outputs.code == 'true'"),
        ):
            with self.subTest(job_key=key):
                mutated = copy.deepcopy(light)
                mutated[key] = value
                with self.assertRaises(AssertionError):
                    self.assert_light_release_pin_check(mutated)
        for step in light["steps"]:
            with self.subTest(unguarded_step=step["name"]):
                mutated = copy.deepcopy(light)
                job_step(mutated, step["name"])["if"] = "true"
                with self.assertRaises(AssertionError):
                    self.assert_light_release_pin_check(mutated)
        mutated = copy.deepcopy(light)
        job_step(mutated, "Check out PineForge HPO")["with"] = {"submodules": "true"}
        with self.assertRaises(AssertionError):
            self.assert_light_release_pin_check(mutated)
        mutated = copy.deepcopy(light)
        job_step(mutated, "Check release pins")["run"] += (
            "\npython3 -m pip install -e external/pineforge-codegen-oss"
        )
        with self.assertRaises(AssertionError):
            self.assert_light_release_pin_check(mutated)

    def test_legacy_light_and_both_native_platforms_keep_the_complete_commands(self):
        legacy = copy.deepcopy(self.workflows["ci-post.yml"]["jobs"]["light"])
        self.assertNotIn("if", legacy)
        checkout = job_step(legacy, "Check out PineForge HPO")["with"]
        self.assertEqual(
            checkout["ref"], "${{ github.event.pull_request.head.sha || github.sha }}"
        )
        checkout["ref"] = "${{ needs.admit.outputs.head_sha }}"
        self.assertEqual(legacy["needs"], ["authz"])
        legacy["needs"] = ["admit"]
        legacy["if"] = "${{ needs.admit.outputs.allowed == 'true' }}"
        for step in legacy["steps"]:
            self.assertEqual(step["if"], "${{ needs.authz.result == 'success' }}")
            step["if"] = "${{ needs.admit.result == 'success' }}"
        self.assertEqual(legacy, self.admitted["jobs"]["light"])
        jobs = self.workflows["ci-post.yml"]["jobs"]
        self.assertEqual(jobs["native-macos"]["steps"], jobs["native"]["steps"])
        for job_id in ("native", "native-macos"):
            native = self.admitted["jobs"][job_id]
            self.assertEqual(native["needs"], ["admit"])
            self.assertEqual(
                native["if"],
                "${{ needs.admit.outputs.allowed == 'true' "
                "&& needs.admit.outputs.docs_only != 'true' }}",
            )
            checkout = job_step(native, "Check out PineForge HPO")["with"]
            self.assertEqual(checkout["ref"], "${{ needs.admit.outputs.head_sha }}")
            self.assertEqual(checkout["persist-credentials"], "false")
            self.assertEqual(
                checkout["submodules"],
                "${{ github.event_name != 'pull_request_target' }}",
            )
            for name in PR_CORE_STEPS:
                actual = job_step(native, name)
                self.assertEqual(
                    actual["if"], "github.event_name == 'pull_request_target'"
                )
                self.assertEqual(actual["run"], job_step(jobs[job_id], name)["run"])
            for name in ("Check failure catalog diff", "Run public C++ API example"):
                self.assertEqual(job_step(native, name), job_step(jobs[job_id], name))

    def authz_environment(self, **overrides):
        environment = {
            "EVENT": "push",
            "REPO": "pineforge-4pass/pineforge-hpo",
            "HEAD_REPO": "pineforge-4pass/pineforge-hpo",
            "HEAD_IS_FORK": "false",
            "ASSOCIATION": "OWNER",
            "PR_AUTHOR": "owner",
            "WR_HEAD_REPO": "pineforge-4pass/pineforge-hpo",
            "WR_ACTOR": "pineforge-app[bot]",
            "ALLOWED_BOTS": "dependabot[bot] pineforge-app[bot]",
        }
        environment.update(overrides)
        return environment

    @staticmethod
    def run_workflow_script(script, environment):
        process_environment = os.environ.copy()
        process_environment.update(
            {
                "GITHUB_STEP_SUMMARY": "/dev/null",
                "RUNNER_TEMP": str(ROOT),
            }
        )
        process_environment.update(environment)
        return subprocess.run(
            ["bash", "-c", script],
            cwd=ROOT,
            env=process_environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_authz_script_decision_table_refuses_untrusted_inputs(self):
        script = workflow_step(self.pr_workflows["ci.yml"], "authz", AUTHZ_STEP_NAME)[
            "run"
        ]
        repository = "pineforge-4pass/pineforge-hpo"
        cases = (
            ("trusted push", {"EVENT": "push"}, True),
            (
                "trusted workflow run",
                {
                    "EVENT": "workflow_run",
                    "WR_HEAD_REPO": repository,
                    "WR_ACTOR": "pineforge-app[bot]",
                },
                True,
            ),
            (
                "same-repository owner pull request",
                {
                    "EVENT": "pull_request",
                    "HEAD_REPO": repository,
                    "HEAD_IS_FORK": "false",
                    "ASSOCIATION": "OWNER",
                },
                True,
            ),
            (
                "same-repository allowlisted bot pull request",
                {
                    "EVENT": "pull_request",
                    "HEAD_REPO": repository,
                    "HEAD_IS_FORK": "false",
                    "ASSOCIATION": "CONTRIBUTOR",
                    "PR_AUTHOR": "dependabot[bot]",
                },
                True,
            ),
            (
                "foreign fork pull request",
                {
                    "EVENT": "pull_request",
                    "HEAD_REPO": "attacker/example",
                    "HEAD_IS_FORK": "true",
                    "ASSOCIATION": "OWNER",
                },
                False,
            ),
            ("pull request target", {"EVENT": "pull_request_target"}, False),
        )
        for name, overrides, expected_success in cases:
            with self.subTest(case=name):
                result = self.run_workflow_script(
                    script, self.authz_environment(**overrides)
                )
                self.assertEqual(
                    result.returncode == 0, expected_success, result.stderr
                )

    def test_authz_script_rejects_a_noop_fail_mutation(self):
        script = workflow_step(self.pr_workflows["ci.yml"], "authz", AUTHZ_STEP_NAME)[
            "run"
        ]
        fail_function = (
            'fail() { echo "::error title=authz::$1"; echo "authz: REFUSED: $1" '
            '>> "$GITHUB_STEP_SUMMARY"; exit 1; }'
        )
        self.assertIn(fail_function, script)
        environment = self.authz_environment(
            EVENT="pull_request",
            HEAD_REPO="attacker/example",
            HEAD_IS_FORK="true",
            ASSOCIATION="OWNER",
        )
        shipped_result = self.run_workflow_script(script, environment)
        self.assertNotEqual(shipped_result.returncode, 0, shipped_result.stderr)

        mutated = script.replace(fail_function, "fail() { :; }", 1)
        self.assertNotEqual(mutated, script)
        mutant_result = self.run_workflow_script(mutated, environment)
        self.assertEqual(mutant_result.returncode, 0, mutant_result.stderr)

    def aggregate_environment(self, **overrides):
        aggregate = self.workflows["ci-post.yml"]["jobs"]["build-gate"]
        needs = {job_id: {"result": "success"} for job_id in aggregate["needs"]}
        self.assertLessEqual(overrides.keys(), needs.keys())
        for job_id, result in overrides.items():
            needs[job_id] = {"result": result}
        return {
            "RESULTS": json.dumps([value["result"] for value in needs.values()]),
        }

    def test_aggregate_script_rejects_a_failed_native_result(self):
        script = workflow_step(
            self.workflows["ci-post.yml"], "build-gate", AGGREGATE_STEP_NAME
        )["run"]
        result = self.run_workflow_script(
            script, self.aggregate_environment(native="failure")
        )
        self.assertNotEqual(result.returncode, 0)

    def test_memory_gate_is_required_and_runs_in_parallel_with_native(self):
        jobs = self.workflows["ci-post.yml"]["jobs"]
        memory = jobs["memory-gate"]
        self.assertEqual(memory["needs"], ["authz"])
        self.assertNotIn("if", memory)
        self.assertNotIn("if", jobs["native"])
        self.assertIn("memory-gate", jobs["build-gate"]["needs"])
        step = workflow_step(
            self.workflows["ci-post.yml"], "build-gate", AGGREGATE_STEP_NAME
        )
        self.assertEqual(step["env"], {"RESULTS": "${{ toJSON(needs.*.result) }}"})
        for status in ("failure", "cancelled", "skipped"):
            with self.subTest(memory=status):
                result = self.run_workflow_script(
                    step["run"],
                    self.aggregate_environment(**{"memory-gate": status}),
                )
                self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_aggregate_script_rejects_a_skipped_or_failed_light_job(self):
        script = workflow_step(
            self.workflows["ci-post.yml"], "build-gate", AGGREGATE_STEP_NAME
        )["run"]
        for light_result in ("skipped", "failure"):
            with self.subTest(light=light_result):
                result = self.run_workflow_script(
                    script, self.aggregate_environment(light=light_result)
                )
                self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_aggregate_script_rejects_a_noop_run_mutation(self):
        for name, workflow in self.workflows.items():
            aggregate_id = "build-gate" if name == "ci-post.yml" else "benchmark-check"
            aggregate = workflow["jobs"][aggregate_id]
            step = aggregate["steps"][-1]
            self.assertEqual(step["name"], AGGREGATE_STEP_NAME)
            for job_id in aggregate["needs"]:
                with self.subTest(workflow=name, failed_job=job_id):
                    environment = {
                        "RESULTS": json.dumps(
                            [
                                "failure" if needed == job_id else "success"
                                for needed in aggregate["needs"]
                            ]
                        )
                    }
                    shipped_result = self.run_workflow_script(step["run"], environment)
                    self.assertNotEqual(
                        shipped_result.returncode, 0, shipped_result.stderr
                    )
                    self.assertIn(
                        "::error::a needed job did not succeed", shipped_result.stdout
                    )
                    mutated = copy.deepcopy(step)
                    mutated["run"] = "true"
                    self.assertNotEqual(mutated["run"], step["run"])
                    mutant_result = self.run_workflow_script(
                        mutated["run"], environment
                    )
                    self.assertEqual(mutant_result.returncode, 0, mutant_result.stderr)

    def test_post_merge_aggregates_reject_every_unsuccessful_dependency(self):
        for name, workflow in self.workflows.items():
            aggregate_id = "build-gate" if name == "ci-post.yml" else "benchmark-check"
            jobs = workflow["jobs"]
            aggregate = jobs[aggregate_id]
            self.assertEqual(aggregate.get("name", aggregate_id), aggregate_id)
            self.assertEqual(aggregate["if"], "${{ always() }}")
            self.assertEqual(set(aggregate["needs"]), set(jobs) - {aggregate_id})
            self.assertEqual(len(aggregate["needs"]), len(jobs) - 1)
            self.assertEqual(len(aggregate["steps"]), 2)
            first, gate = aggregate["steps"]
            self.assertEqual(first["if"], "${{ needs.authz.result != 'success' }}")
            self.assertEqual(gate["env"], {"RESULTS": "${{ toJSON(needs.*.result) }}"})
            self.assertNotIn("if", gate)
            results = ["success"] * len(aggregate["needs"])
            success = self.run_workflow_script(
                gate["run"], {"RESULTS": json.dumps(results)}
            )
            self.assertEqual(success.returncode, 0, success.stdout + success.stderr)
            for job_id in aggregate["needs"]:
                for status in ("failure", "cancelled", "skipped"):
                    with self.subTest(workflow=name, job=job_id, result=status):
                        results = [
                            status if needed == job_id else "success"
                            for needed in aggregate["needs"]
                        ]
                        result = self.run_workflow_script(
                            gate["run"], {"RESULTS": json.dumps(results)}
                        )
                        self.assertNotEqual(
                            result.returncode, 0, result.stdout + result.stderr
                        )
                        self.assertIn(
                            "::error::a needed job did not succeed", result.stdout
                        )
                        if job_id == "authz":
                            refusal = self.run_workflow_script(first["run"], {})
                            self.assertNotEqual(refusal.returncode, 0)
                            self.assertIn("::error::authz did not pass", refusal.stdout)

    def test_concurrency_cancels_only_runs_of_the_same_pull_request(self):
        for workflow in (*self.pr_workflows.values(), *self.workflows.values()):
            concurrency = workflow["concurrency"]
            self.assertIn(
                "${{ github.event.pull_request.number || github.run_id }}",
                concurrency["group"],
            )
            self.assertEqual(
                concurrency["cancel-in-progress"],
                "${{ github.event_name == 'pull_request' }}",
            )

    def assert_native_contract(self, native):
        steps = {step["name"]: step for step in native["steps"]}
        for name in POST_MERGE_STEPS:
            self.assertEqual(steps[name]["if"], "github.event_name != 'pull_request'")
        for name in LINUX_POST_MERGE_STEPS:
            self.assertEqual(
                steps[name]["if"],
                "runner.os == 'Linux' && github.event_name != 'pull_request'",
            )
        self.assertEqual(
            steps["Report patience capability and skips"]["if"],
            "always() && github.event_name != 'pull_request'",
        )
        self.assertEqual(
            steps["Check out PineForge HPO"]["with"]["submodules"],
            "${{ github.event_name != 'pull_request' }}",
        )
        for name in PR_CORE_STEPS:
            self.assertEqual(steps[name]["if"], "github.event_name == 'pull_request'")
        configure = steps["Configure PR native core"]["run"]
        for option in (
            "BUILD_ENGINE_ADAPTER=OFF",
            "BUILD_NATIVE_CLI=OFF",
            "BUILD_TESTS=ON",
            "BUILD_EXAMPLES=ON",
            "REQUIRE_SERIAL_GOLDEN=ON",
            "REQUIRE_MPFR=ON",
        ):
            self.assertIn("-DPINEFORGE_HPO_" + option, configure)
        # The default target of the core-only tree, never a --target subset.
        build = commands(steps["Build PR native core"]["run"])
        self.assertEqual(build, ["cmake --build build -j4"])
        # Every CTest test of the core-only tree: the verbose named run, then the
        # unfiltered run, which is main's full run plus the empty-selection error.
        main_test = commands(steps["Test"]["run"])
        pr_test = commands(steps["Test PR native core"]["run"])
        self.assertEqual(tuple(pr_test), PR_CORE_TEST)
        self.assertEqual(pr_test[1], main_test[1] + " --no-tests=error")
        self.assertEqual(steps["Build"]["run"].strip(), "cmake --build build -j4")
        planner = steps["Build PineForge window planner helper"]["run"]
        self.assertIn(
            "cmake --build external/pineforge-engine/build --target pineforge_window_plan",
            planner,
        )
        self.assertIn("Linux) planner_suffix=so", planner)
        self.assertIn("macOS) planner_suffix=dylib", planner)
        self.assertIn(
            "${{ github.workspace }}/external/pineforge-engine/build/lib/"
            "libpineforge_window_plan.${planner_suffix}",
            planner,
        )
        self.assertIn('test -f "$planner_helper"', planner)
        self.assertIn(
            'echo "PFH_WINDOW_PLAN_LIB=$planner_helper" >> "$GITHUB_ENV"',
            planner,
        )
        self.assertEqual(steps["Test"]["env"]["PFH_REQUIRE_WINDOW_PLAN"], "1")
        self.assertIn(
            "ctest --test-dir build --output-on-failure", steps["Test"]["run"]
        )
        self.assertNotIn("if", steps["Run public C++ API example"])

    def mutated_native(self, name, key, value):
        mutated = copy.deepcopy(self.workflows["ci-post.yml"]["jobs"]["native"])
        step = next(step for step in mutated["steps"] if step["name"] == name)
        step[key] = value
        return mutated

    def test_full_proof_is_post_merge_and_pr_core_runs_every_core_test(self):
        native = self.workflows["ci-post.yml"]["jobs"]["native"]
        self.assert_native_contract(native)
        for name in POST_MERGE_STEPS + LINUX_POST_MERGE_STEPS:
            with self.subTest(step=name):
                mutated = copy.deepcopy(native)
                step = next(step for step in mutated["steps"] if step["name"] == name)
                step["if"] = "github.event_name == 'pull_request'"
                with self.assertRaises(AssertionError):
                    self.assert_native_contract(mutated)
        on_pushes = "github.event_name != 'pull_request'"
        for name in PR_CORE_STEPS:
            with self.subTest(step=name, mutation="runs on pushes instead"):
                mutated = self.mutated_native(name, "if", on_pushes)
                with self.assertRaises(AssertionError):
                    self.assert_native_contract(mutated)
        for name in ("Build PR native core", "Test PR native core"):
            with self.subTest(step=name, mutation="verification skipped"):
                mutated = self.mutated_native(name, "run", "echo verification skipped")
                with self.assertRaises(AssertionError):
                    self.assert_native_contract(mutated)

    def test_pr_core_configure_keeps_the_engine_adapter_and_cli_off(self):
        native = self.workflows["ci-post.yml"]["jobs"]["native"]
        steps = {step["name"]: step for step in native["steps"]}
        name = "Configure PR native core"
        configure = steps[name]["run"]
        for option in ("BUILD_ENGINE_ADAPTER", "BUILD_NATIVE_CLI"):
            with self.subTest(option=option):
                switched = configure.replace(option + "=OFF", option + "=ON")
                self.assertNotEqual(switched, configure)
                mutated = self.mutated_native(name, "run", switched)
                with self.assertRaises(AssertionError):
                    self.assert_native_contract(mutated)

    def test_pr_core_build_and_test_reject_every_narrowed_form(self):
        first, full = PR_CORE_TEST
        old_targets = (
            "pineforge_hpo_test_core",
            "pineforge_hpo_test_warm_start",
            "pineforge_hpo_test_portable_math",
            "pineforge_hpo_test_portable_math_canary",
            "pineforge_hpo_example_ask_tell",
        )
        old_build = "cmake --build build -j4 --target " + " ".join(old_targets)
        old_filter = first.replace("(serial_golden", "(core|serial_golden")
        forms = {
            "Build PR native core": (
                old_build,
                "cmake --build build -j4 --target pineforge_hpo_test_core",
            ),
            "Test PR native core": (
                old_filter,
                first,
                full,
                "\n".join((first, full + " -R '^pineforge_hpo_core$'")),
                "\n".join((first, full + " -E '^pineforge_hpo_tpe_stress$'")),
                "\n".join((first, full.replace(" --no-tests=error", ""))),
            ),
        }
        for name, runs in forms.items():
            for run in runs:
                with self.subTest(step=name, run=run):
                    mutated = self.mutated_native(name, "run", run)
                    with self.assertRaises(AssertionError):
                        self.assert_native_contract(mutated)

    def test_only_the_engine_adapter_block_is_left_to_pushes(self):
        cmake = (ROOT / "tests/CMakeLists.txt").read_text()
        self.assertEqual(engine_references_outside_block(cmake), [])
        extra = "add_test(NAME pineforge_hpo_leaked COMMAND pineforge-hpo-native)\n"
        leaked = cmake + extra
        self.assertEqual(len(engine_references_outside_block(leaked)), 1)
        moved = cmake.replace(ADAPTER_BLOCK_OPEN, "if(UNIX)")
        with self.assertRaises(ValueError):
            engine_references_outside_block(moved)

    def test_docs_only_skips_remain_fail_closed(self):
        for job_id in (
            "release-notification",
            "native",
            "native-macos",
            "python",
            "memory-gate",
            "optuna",
        ):
            job = self.admitted["jobs"][job_id]
            self.assertEqual(job["needs"], ["admit"])
            for result in ("success", "failure", "cancelled", "skipped"):
                for allowed in ("true", "false", ""):
                    for docs_only in ("true", "false", ""):
                        with self.subTest(
                            job=job_id, result=result, allowed=allowed, docs=docs_only
                        ):
                            run = evaluate(
                                job["if"],
                                needs=SimpleNamespace(
                                    admit=SimpleNamespace(
                                        result=result,
                                        outputs=SimpleNamespace(
                                            allowed=allowed, docs_only=docs_only
                                        ),
                                    )
                                ),
                            )
                            self.assertEqual(
                                run, allowed == "true" and docs_only != "true"
                            )

    def test_post_merge_proofs_have_no_classification_or_job_skip_conditions(self):
        for name, workflow in self.workflows.items():
            jobs = workflow["jobs"]
            aggregate = "build-gate" if name == "ci-post.yml" else "benchmark-check"
            self.assertNotIn("changes", jobs)
            for job_id, job in jobs.items():
                with self.subTest(workflow=name, job=job_id):
                    self.assertNotIn("outputs", job)
                    if job_id == aggregate:
                        self.assertEqual(job["if"], "${{ always() }}")
                    elif job_id != "authz":
                        self.assertEqual(job["needs"], ["authz"])
                        self.assertNotIn("if", job)
            text = (ROOT / ".github/workflows" / name).read_text()
            self.assertNotIn("needs.changes", text)
            self.assertNotIn("steps.classify", text)
            self.assertNotIn("HEAVY_JOBS", text)

    def test_draft_ci_pull_requests_keep_the_current_suite_condition(self):
        workflow = self.pr_workflows["ci.yml"]
        self.assertIn("ready_for_review", workflow["on"]["pull_request"]["types"])
        for job_id in ("release-notification", "native", "python", "memory-gate"):
            job = self.admitted["jobs"][job_id]
            self.assertEqual(job["needs"], ["admit"])
            for draft in (False, True):
                for admitted in ("true", "false", ""):
                    for docs_only in ("true", "false", ""):
                        with self.subTest(
                            job=job_id, draft=draft, admitted=admitted, docs=docs_only
                        ):
                            allowed = evaluate(
                                job["if"],
                                github=SimpleNamespace(
                                    event_name="pull_request_target",
                                    event=SimpleNamespace(
                                        pull_request=SimpleNamespace(draft=draft)
                                    ),
                                ),
                                needs=SimpleNamespace(
                                    admit=SimpleNamespace(
                                        outputs=SimpleNamespace(
                                            allowed=admitted, docs_only=docs_only
                                        )
                                    )
                                ),
                            )
                            self.assertEqual(
                                allowed, admitted == "true" and docs_only != "true"
                            )


if __name__ == "__main__":
    unittest.main()
