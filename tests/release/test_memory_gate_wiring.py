"""Exercise CI/release refusal paths with stubs only; never invoke HPO."""

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

    def test_both_jobs_refuse_hosted_work_and_absent_script_before_build(self):
        for job in (self.ci["memory-gate"], self.release["memory-gate"]):
            with self.subTest(job=job["name"]):
                steps = job["steps"]
                hosted = self.shell(steps[0]["run"], RUNNER_ENVIRONMENT="github-hosted")
                self.assertEqual(hosted.returncode, 2, hosted.stdout + hosted.stderr)
                own = self.shell(steps[0]["run"], RUNNER_ENVIRONMENT="self-hosted")
                self.assertEqual(own.returncode, 0, own.stdout + own.stderr)
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

    def aggregate(self, docs_only="false", event="push", **results):
        step = self.ci["build-gate"]["steps"][0]
        environment = {key: "success" for key in step["env"] if key.endswith("_RESULT")}
        environment.update(EVENT_NAME=event, DOCS_ONLY=docs_only, **results)
        return self.shell(step["run"], **environment)

    def test_aggregate_covers_all_jobs_and_refuses_every_failed_cancelled_or_skipped_job(
        self,
    ):
        gate = self.ci["build-gate"]
        self.assertEqual(set(gate["needs"]), set(self.ci) - {"build-gate"})
        self.assertEqual(gate["name"], "build")
        self.assertEqual(gate["if"], "always()")
        self.assertEqual(gate["runs-on"], "ubuntu-24.04")
        clean = self.aggregate()
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        for variable in gate["steps"][0]["env"]:
            if not variable.endswith("_RESULT"):
                continue
            for status in ("failure", "cancelled", "skipped"):
                with self.subTest(variable=variable, status=status):
                    result = self.aggregate(**{variable: status})
                    self.assertNotEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )

    def test_aggregate_only_accepts_docs_skips_after_successful_pr_classification(self):
        skipped = dict(
            RELEASE_RESULT="skipped",
            NATIVE_RESULT="skipped",
            PYTHON_RESULT="skipped",
            MEMORY_RESULT="skipped",
        )
        result = self.aggregate("true", "pull_request", **skipped)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for docs_only, event, changes in (
            ("true", "push", "success"),
            ("false", "pull_request", "success"),
            ("", "pull_request", "success"),
            ("true", "pull_request", "failure"),
            ("true", "pull_request", "cancelled"),
            ("true", "pull_request", "skipped"),
        ):
            with self.subTest(docs_only=docs_only, event=event, changes=changes):
                result = self.aggregate(
                    docs_only, event, CHANGES_RESULT=changes, **skipped
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        for status in ("failure", "cancelled"):
            result = self.aggregate("true", "pull_request", MEMORY_RESULT=status)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

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
