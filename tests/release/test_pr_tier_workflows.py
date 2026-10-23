"""Keep verification hosted and preserve the complete post-merge native suite."""

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
AGGREGATE_STEP_NAME = "Require success or independently proven cheap skips"


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
        self.workflows = {
            name: yaml.load(
                (ROOT / ".github/workflows" / name).read_text(),
                Loader=yaml.BaseLoader,
            )
            for name in ("ci.yml", "benchmark-smoke.yml")
        }

    def assert_routing(self, workflows):
        ci = workflows["ci.yml"]["jobs"]
        benchmark = workflows["benchmark-smoke.yml"]["jobs"]
        routes = (
            ci["release-notification"]["runs-on"],
            ci["python"]["runs-on"],
            ci["native"]["strategy"]["matrix"]["include"][0]["runner"],
            benchmark["optuna"]["runs-on"],
        )
        for route in routes:
            self.assertEqual(route, "ubuntu-24.04")
        self.assertEqual(ci["native"]["runs-on"], "${{ matrix.runner }}")
        self.assertEqual(
            ci["native"]["strategy"]["matrix"]["include"][1],
            {"os": "macos-latest", "runner": "macos-latest"},
        )
        for jobs, aggregate in ((ci, "build-gate"), (benchmark, "benchmark-check")):
            for job_id in ("authz", "changes", "workflow-lint", aggregate):
                self.assertEqual(jobs[job_id]["runs-on"], "ubuntu-24.04")

    def test_no_route_is_a_conditional_expression(self):
        # Every routed site is now a literal hosted label, so routing can no longer
        # vary by event, actor or fork. Guard that invariant directly: a `${{ }}`
        # expression creeping back into any of the four routed sites is a regression.
        ci = self.workflows["ci.yml"]["jobs"]
        benchmark = self.workflows["benchmark-smoke.yml"]["jobs"]
        routes = (
            ci["release-notification"]["runs-on"],
            ci["python"]["runs-on"],
            ci["native"]["strategy"]["matrix"]["include"][0]["runner"],
            benchmark["optuna"]["runs-on"],
        )
        for route in routes:
            with self.subTest(route=route):
                self.assertNotIn("${{", route)
                self.assertEqual(route, "ubuntu-24.04")
        self.assert_routing(self.workflows)

    def test_old_pool_routing_and_pool_fallback_are_rejected(self):
        for bad_route in (
            OLD_ROUTE,
            POOL_ROUTE,
            POOL_ROUTE.replace("'ubuntu-24.04'", f"fromJSON('{json.dumps(POOL)}')"),
        ):
            for job_id in ("release-notification", "python"):
                with self.subTest(job=job_id, route=bad_route):
                    mutated = copy.deepcopy(self.workflows)
                    mutated["ci.yml"]["jobs"][job_id]["runs-on"] = bad_route
                    with self.assertRaises(AssertionError):
                        self.assert_routing(mutated)
            mutated = copy.deepcopy(self.workflows)
            mutated["ci.yml"]["jobs"]["native"]["strategy"]["matrix"]["include"][0][
                "runner"
            ] = bad_route
            with self.assertRaises(AssertionError):
                self.assert_routing(mutated)
            mutated = copy.deepcopy(self.workflows)
            mutated["benchmark-smoke.yml"]["jobs"]["optuna"]["runs-on"] = bad_route
            with self.assertRaises(AssertionError):
                self.assert_routing(mutated)

    def test_check_names_permissions_and_unfiltered_pr_triggers_are_preserved(self):
        expected_names = {
            "ci.yml": (
                "CI",
                {
                    "authz": "authz",
                    "changes": "changes",
                    "workflow-lint": "workflow-lint",
                    "light": "Light",
                    "release-notification": "Release notification contract",
                    "native": "Native (${{ matrix.os }})",
                    "python": "Python ${{ matrix.python }}",
                    "build-gate": "build-gate",
                },
            ),
            "benchmark-smoke.yml": (
                "Benchmark smoke",
                {
                    "authz": "authz",
                    "changes": "changes",
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
                trigger = workflow["on"]["pull_request"] or {}
                self.assertNotIn("paths", trigger)
                self.assertNotIn("paths-ignore", trigger)
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
        matrix = self.workflows["ci.yml"]["jobs"]["native"]["strategy"]["matrix"]
        self.assertEqual(matrix["os"], ["ubuntu-latest", "macos-latest"])
        self.assertEqual(
            self.workflows["ci.yml"]["jobs"]["python"]["strategy"]["matrix"]["python"],
            ["3.11", "3.14"],
        )

    def test_license_entity_is_an_unconditional_light_check(self):
        jobs = self.workflows["ci.yml"]["jobs"]
        light = jobs["light"]
        self.assertEqual(light["needs"], ["authz"])
        self.assertEqual(light["if"], "${{ needs.authz.result == 'success' }}")
        self.assertEqual(light["runs-on"], "ubuntu-24.04")
        self.assertEqual(
            job_step(light, "Check license entity")["run"],
            "python3 -m unittest tests/python/test_license_entity.py -v",
        )
        self.assertEqual(
            jobs["build-gate"]["needs"],
            [
                "authz",
                "changes",
                "workflow-lint",
                "light",
                "release-notification",
                "native",
                "python",
            ],
        )

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
        script = workflow_step(
            self.workflows["ci.yml"], "authz", AUTHZ_STEP_NAME
        )["run"]
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
        script = workflow_step(
            self.workflows["ci.yml"], "authz", AUTHZ_STEP_NAME
        )["run"]
        fail_function = (
            'fail() { echo "::error title=authz::$1"; echo "authz: REFUSED: $1" '
            '>> "$GITHUB_STEP_SUMMARY"; exit 1; }'
        )
        self.assertIn(fail_function, script)
        mutated = script.replace(fail_function, "fail() { :; }", 1)
        result = self.run_workflow_script(
            mutated,
            self.authz_environment(
                EVENT="pull_request",
                HEAD_REPO="attacker/example",
                HEAD_IS_FORK="true",
                ASSOCIATION="OWNER",
            ),
        )
        self.assertNotEqual(result.returncode, 0)

    def aggregate_environment(self, **overrides):
        aggregate = self.workflows["ci.yml"]["jobs"]["build-gate"]
        step = workflow_step(
            self.workflows["ci.yml"], "build-gate", AGGREGATE_STEP_NAME
        )
        needs = {job_id: {"result": "success"} for job_id in aggregate["needs"]}
        for job_id, result in overrides.items():
            needs[job_id] = {"result": result}
        return {
            "NEEDS": json.dumps(needs),
            "HEAVY_JOBS": step["env"]["HEAVY_JOBS"],
            "DOCS_ONLY": "false",
            "WORKFLOWS_ONLY": "false",
        }

    def test_aggregate_script_rejects_a_failed_native_result(self):
        script = workflow_step(
            self.workflows["ci.yml"], "build-gate", AGGREGATE_STEP_NAME
        )["run"]
        result = self.run_workflow_script(
            script, self.aggregate_environment(native="failure")
        )
        self.assertNotEqual(result.returncode, 0)

    def test_aggregate_script_rejects_a_noop_run_mutation(self):
        mutated = copy.deepcopy(self.workflows["ci.yml"])
        step = workflow_step(mutated, "build-gate", AGGREGATE_STEP_NAME)
        step["run"] = "true"
        result = self.run_workflow_script(
            step["run"], self.aggregate_environment(native="failure")
        )
        self.assertNotEqual(result.returncode, 0)

    def test_concurrency_cancels_only_runs_of_the_same_pull_request(self):
        for workflow in self.workflows.values():
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
        self.assertIn(
            "ctest --test-dir build --output-on-failure", steps["Test"]["run"]
        )
        self.assertNotIn("if", steps["Run public C++ API example"])

    def mutated_native(self, name, key, value):
        mutated = copy.deepcopy(self.workflows["ci.yml"]["jobs"]["native"])
        step = next(step for step in mutated["steps"] if step["name"] == name)
        step[key] = value
        return mutated

    def test_full_proof_is_post_merge_and_pr_core_runs_every_core_test(self):
        native = self.workflows["ci.yml"]["jobs"]["native"]
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
        native = self.workflows["ci.yml"]["jobs"]["native"]
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
        for name, workflow in self.workflows.items():
            jobs = workflow["jobs"]
            aggregate = "build-gate" if name == "ci.yml" else "benchmark-check"
            self.assertEqual(jobs[aggregate]["if"], "${{ always() }}")
            for job_id, job in jobs.items():
                if job_id in ("authz", "changes", "workflow-lint", "light", aggregate):
                    continue
                self.assertEqual(job["needs"], ["authz", "changes"])
                for result in ("success", "failure", "cancelled", "skipped"):
                    for code in ("true", "false", ""):
                        with self.subTest(job=job_id, result=result, code=code):
                            allowed = evaluate(
                                job["if"],
                                github=SimpleNamespace(
                                    event_name="pull_request",
                                    event=SimpleNamespace(
                                        pull_request=SimpleNamespace(draft=False)
                                    ),
                                ),
                                needs=SimpleNamespace(
                                    changes=SimpleNamespace(
                                        result=result,
                                        outputs=SimpleNamespace(code=code),
                                    )
                                ),
                                cancelled=lambda: False,
                            )
                            self.assertEqual(allowed, code == "true")


if __name__ == "__main__":
    unittest.main()
