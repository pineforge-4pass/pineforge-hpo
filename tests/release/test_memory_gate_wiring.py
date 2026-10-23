"""Exercise CI/release refusal paths with stubs only; never invoke HPO."""

import copy
import json
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts/run_ci_memory_gate.sh"


def load_yaml(path):
    return yaml.load((ROOT / path).read_text(), Loader=yaml.BaseLoader)


class MemoryGateWiringTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "build/memory-gate-wiring-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.ci = load_yaml(".github/workflows/ci.yml")["jobs"]
        self.release = load_yaml(".github/workflows/release.yml")["jobs"]

    def shell(self, command, **environment):
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", command],
            cwd=self.directory,
            env={**os.environ, **environment},
            capture_output=True,
            text=True,
        )

    def stub(self, output, status=0):
        scripts = self.directory / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / "ci_memory_gate.py").write_text(
            f"print({output!r})\nraise SystemExit({status})\n"
        )

    def guard(self):
        return subprocess.run(
            ["bash", str(GUARD)],
            cwd=self.directory,
            capture_output=True,
            text=True,
        )

    def test_absent_script_is_incomplete(self):
        result = self.guard()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("INCOMPLETE", result.stdout)
        self.assertIn("scripts/ci_memory_gate.py is missing", result.stdout)

    def test_zero_missing_or_invalid_count_cannot_pass_on_exit_zero(self):
        for output in (
            "0 rows",
            "0 cases judged",
            "rows=0",
            "cases_judged: 0",
            "clean",
            "",
            "-1 rows",
            "rows=-1",
            "1.5 rows",
            "rows=1.5",
            "4 rows\n0 cases",
        ):
            with self.subTest(output=output):
                self.stub(output)
                result = self.guard()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("INCOMPLETE", result.stdout)

    def test_measurer_failure_is_preserved_even_with_positive_rows(self):
        for status in (1, 2):
            with self.subTest(status=status):
                self.stub("4 rows judged", status)
                result = self.guard()
                self.assertEqual(
                    result.returncode, status, result.stdout + result.stderr
                )
                self.assertIn(f"failed (exit {status})", result.stdout)

    def test_clean_nonempty_stub_passes_and_retains_output(self):
        for summary in (
            "4 rows",
            "4 cases judged",
            "rows=4",
            "cases: 4",
            "rows_judged=4",
        ):
            with self.subTest(summary=summary):
                output = "small rung: stub\nlarge rung: stub\n" + summary
                self.stub(output)
                result = self.guard()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(output, result.stdout)
                self.assertEqual(
                    (self.directory / "build/memory-gate.log").read_text(),
                    output + "\n",
                )

    def test_both_jobs_use_hosted_runners_and_refuse_absent_script_before_build(self):
        for job in (self.ci["memory-gate"], self.release["memory-gate"]):
            with self.subTest(job=job["name"]):
                self.assertEqual(job["runs-on"], "ubuntu-24.04")
                steps = job["steps"]
                precheck_index = next(
                    i
                    for i, step in enumerate(steps)
                    if "test -f scripts/ci_memory_gate.py" in step.get("run", "")
                )
                build_index = next(
                    i
                    for i, step in enumerate(steps)
                    if step.get("uses") == "./.github/actions/memory-gate"
                )
                self.assertLess(precheck_index, build_index)
                missing = self.shell(steps[precheck_index]["run"])
                self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
                self.assertIn("INCOMPLETE", missing.stdout)
                self.assertNotIn("continue-on-error", job)
                for step in steps:
                    self.assertNotIn("continue-on-error", step)

    def assert_aggregate_contract(self, ci):
        gate = ci["build-gate"]
        self.assertEqual(set(gate["needs"]), set(ci) - {"build-gate"})
        self.assertEqual(gate["needs"], sorted(gate["needs"]))
        self.assertEqual(gate.get("name", "build-gate"), "build-gate")
        self.assertEqual(gate["if"], "${{ always() }}")
        self.assertEqual(gate["runs-on"], "ubuntu-24.04")
        environment = gate["steps"][-1]["env"]
        self.assertEqual(environment["NEEDS"], "${{ toJSON(needs) }}")
        self.assertEqual(
            environment["DOCS_ONLY"], "${{ steps.classify.outputs.docs_only }}"
        )
        self.assertEqual(
            environment["WORKFLOWS_ONLY"],
            "${{ steps.classify.outputs.workflows_only }}",
        )
        self.assertEqual(
            json.loads(environment["HEAVY_JOBS"]),
            sorted(
                job_id
                for job_id, job in ci.items()
                if job_id != "build-gate" and "changes" in job.get("needs", [])
            ),
        )

    def aggregate(self, docs_only="false", workflows_only="false", ci=None, **results):
        ci = self.ci if ci is None else ci
        gate = ci["build-gate"]
        step = gate["steps"][-1]
        needs = {job_id: {"result": "success"} for job_id in gate["needs"]}
        self.assertLessEqual(results.keys(), needs.keys())
        for job_id, result in results.items():
            needs[job_id]["result"] = result
        environment = {
            **step["env"],
            "NEEDS": json.dumps(needs),
            "DOCS_ONLY": docs_only,
            "WORKFLOWS_ONLY": workflows_only,
            "RUNNER_TEMP": str(self.directory),
        }
        return self.shell(step["run"], **environment)

    def test_aggregate_covers_all_jobs_and_refuses_every_failed_cancelled_or_skipped_job(
        self,
    ):
        gate = self.ci["build-gate"]
        self.assert_aggregate_contract(self.ci)
        clean = self.aggregate()
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        for job_id in gate["needs"]:
            for status in ("failure", "cancelled", "skipped"):
                with self.subTest(job=job_id, status=status):
                    result = self.aggregate(**{job_id: status})
                    self.assertNotEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )

    def test_aggregate_only_accepts_independently_proven_cheap_skips(self):
        skipped = dict.fromkeys(
            ("release-notification", "native", "python", "memory-gate"), "skipped"
        )
        for docs_only, workflows_only in (("true", "false"), ("false", "true")):
            with self.subTest(docs_only=docs_only, workflows_only=workflows_only):
                result = self.aggregate(docs_only, workflows_only, **skipped)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for job_id in ("authz", "changes", "workflow-lint"):
                    for status in ("failure", "cancelled", "skipped"):
                        with self.subTest(job=job_id, status=status):
                            result = self.aggregate(
                                docs_only,
                                workflows_only,
                                **{**skipped, job_id: status},
                            )
                            self.assertNotEqual(
                                result.returncode, 0, result.stdout + result.stderr
                            )
                for job_id in skipped:
                    for status in ("failure", "cancelled"):
                        with self.subTest(job=job_id, status=status):
                            result = self.aggregate(
                                docs_only,
                                workflows_only,
                                **{**skipped, job_id: status},
                            )
                            self.assertNotEqual(
                                result.returncode, 0, result.stdout + result.stderr
                            )
        for docs_only, workflows_only in (
            ("false", "false"),
            ("", "false"),
            ("false", ""),
            ("true", "true"),
            ("true", ""),
            ("", "true"),
        ):
            with self.subTest(docs_only=docs_only, workflows_only=workflows_only):
                result = self.aggregate(docs_only, workflows_only, **skipped)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_push_classification_cannot_authorize_cheap_skips(self):
        classify = next(
            step
            for step in self.ci["build-gate"]["steps"]
            if step.get("id") == "classify"
        )
        output = self.directory / "classification"
        result = self.shell(
            classify["run"],
            EVENT="push",
            PR_BASE="",
            PR_HEAD="",
            GITHUB_OUTPUT=str(output),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        proof = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(
            proof, {"docs_only": "false", "workflows_only": "false", "code": "true"}
        )
        result = self.aggregate(
            proof["docs_only"],
            proof["workflows_only"],
            **dict.fromkeys(
                ("release-notification", "native", "python", "memory-gate"), "skipped"
            ),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_memory_gate_dependency_is_rejected(self):
        mutated = copy.deepcopy(self.ci)
        mutated["build-gate"]["needs"].remove("memory-gate")
        with self.assertRaisesRegex(AssertionError, "memory-gate"):
            self.assert_aggregate_contract(mutated)
        result = self.aggregate(ci=mutated)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_memory_gate_heavy_job_is_rejected(self):
        mutated = copy.deepcopy(self.ci)
        environment = mutated["build-gate"]["steps"][-1]["env"]
        heavy = json.loads(environment["HEAVY_JOBS"])
        heavy.remove("memory-gate")
        environment["HEAVY_JOBS"] = json.dumps(heavy)
        with self.assertRaisesRegex(AssertionError, "memory-gate"):
            self.assert_aggregate_contract(mutated)

    def test_release_package_depends_on_the_gate_and_uses_its_exact_commit(self):
        package = self.release["package"]
        self.assertEqual(package["needs"], "memory-gate")
        self.assertEqual(package["if"], "github.event_name == 'release'")
        self.assertEqual(
            package["steps"][0]["with"]["ref"],
            "${{ needs.memory-gate.outputs.source_sha }}",
        )
        self.assertEqual(
            self.release["memory-gate"]["outputs"]["source_sha"],
            "${{ steps.source.outputs.sha }}",
        )
        action = load_yaml(".github/actions/memory-gate/action.yml")
        self.assertEqual(
            action["runs"]["steps"][-1]["run"], "bash scripts/run_ci_memory_gate.sh"
        )


if __name__ == "__main__":
    unittest.main()
