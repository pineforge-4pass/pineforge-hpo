"""Reject time limits; a measured exact CD job is the sole policy exception.

Run the scanner controls and repository check separately when reviewing a
refusing repository. A controls success never implies a clean repository.
CTest still invokes both through this file, without an execution time cap.
"""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from _no_time_limits import (
    Finding,
    POLICY_PATH,
    parse_policy,
    scan_limits,
    scan_repository,
    select_population,
    unique_object,
    validate_findings,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "tests/fixtures/no_time_limits/catalog.json"
EXPECTED_CASES = 162
EXPECTED_POSITIVE_CASES = 109
EXPECTED_FINDINGS = 110
EXPECTED_CLEAN_CASES = 53


class NoTimeLimitsTest(unittest.TestCase):
    def test_repository_has_no_time_limits(self) -> None:
        population, findings = scan_repository(ROOT)
        self.assertGreater(len(population), 0, "no source files scanned")
        for name, language in population:
            print(f"SCAN {language} {name}", flush=True)
        print(f"POPULATION {len(population)} files", flush=True)
        for finding in findings:
            print(f"REFUSE {finding}", flush=True)
        self.assertEqual(
            findings, [], "Prohibited time limits:\n" + "\n".join(map(str, findings))
        )

    def test_scanner_controls(self) -> None:
        catalog = json.loads(
            CATALOG.read_text(encoding="utf-8"), object_pairs_hook=unique_object
        )
        self.assertEqual(catalog["version"], 1)
        self.assertEqual(catalog["expected_cases"], EXPECTED_CASES)
        self.assertEqual(catalog["expected_positive_cases"], EXPECTED_POSITIVE_CASES)
        self.assertEqual(catalog["expected_findings"], EXPECTED_FINDINGS)
        self.assertEqual(catalog["expected_clean_cases"], EXPECTED_CLEAN_CASES)
        cases = catalog["cases"]
        self.assertEqual(len(cases), EXPECTED_CASES)
        self.assertEqual(len({case["id"] for case in cases}), EXPECTED_CASES)
        self.assertEqual(
            sum(bool(case["expected"]) for case in cases), EXPECTED_POSITIVE_CASES
        )
        self.assertEqual(
            sum(not case["expected"] for case in cases), EXPECTED_CLEAN_CASES
        )
        self.assertEqual(
            sum(len(case["expected"]) for case in cases), EXPECTED_FINDINGS
        )
        policies = {
            name: parse_policy(json.dumps(data), allow_control=True)
            for name, data in catalog["policies"].items()
        }
        observed = 0
        for case in cases:
            with self.subTest(control=case["id"]):
                source = "\n".join(case["source"]) + "\n"
                findings = scan_limits(
                    case["name"], source, policies[case.get("policy", "empty")]
                )
                self.assertEqual(
                    [[item.line, item.kind] for item in findings], case["expected"]
                )
                observed += len(findings)
        self.assertGreater(observed, 0)
        self.assertEqual(observed, EXPECTED_FINDINGS)
        self._policy_controls(catalog["policies"])
        self._finding_controls()
        self._population_controls()

    def _policy_controls(self, policies: dict) -> None:
        valid = policies["cd"]
        measured = policies["cd"]["jobs"][0]
        rows = [({}, "policy keys differ")]
        row = deepcopy(valid)
        row["authority"] = "unreviewed"
        rows.append((row, "policy authority differs"))
        row = deepcopy(valid)
        row["reviewed_on"] = "undated"
        rows.append((row, "Invalid isoformat"))
        row = deepcopy(valid)
        row["unknown"] = True
        rows.append((row, "policy keys differ"))
        for change, error in (
            ({"purpose": "test"}, "policy purpose must be CD"),
            ({"workflow": ".github/workflows/*.yml"}, "exact workflow and job"),
            ({"job": "*"}, "exact workflow and job"),
            ({"measured_max_seconds": 0}, "positive integer measurement"),
            ({"margin_seconds": 0}, "positive integer measurement"),
            ({"timeout_minutes": 15}, "measured maximum plus margin"),
            ({"evidence": ""}, "measurement evidence required"),
        ):
            row = deepcopy(valid)
            row["jobs"] = [{**measured, **change}]
            rows.append((row, error))
        row = deepcopy(valid)
        row["jobs"] = [
            {key: value for key, value in measured.items() if key != "measured_on"}
        ]
        rows.append((row, "measured job keys differ"))
        self.assertEqual(len(rows), 12)
        for index, (row, error) in enumerate(rows):
            with self.subTest(policy=index), self.assertRaisesRegex(ValueError, error):
                parse_policy(json.dumps(row), allow_control=True)
        with self.assertRaises(ValueError):
            parse_policy("{malformed")
        with self.assertRaises(ValueError):
            parse_policy(json.dumps(policies["cd"]))
        with self.assertRaisesRegex(ValueError, "synthetic measurements"):
            parse_policy(
                json.dumps({**valid, "data": "production", "jobs": [measured]})
            )
        with self.assertRaises(ValueError):
            parse_policy(
                json.dumps({**policies["cd"], "jobs": [measured, measured]}),
                allow_control=True,
            )
        with self.assertRaisesRegex(ValueError, "missing or malformed policy"):
            scan_limits("tests/control.py", "child.wait()\n")
        with self.assertRaisesRegex(ValueError, "empty or malformed source"):
            scan_limits(
                "tests/control.py", "", parse_policy(json.dumps(policies["empty"]))
            )

    def _finding_controls(self) -> None:
        name, source = "tests/control.py", "child.wait(3)\n"
        invalid = [
            None,
            (),
            [{}],
            [Finding(name, 0, "python-wait-timeout", "child.wait(3)")],
            [Finding(name, 1, "unknown", "child.wait(3)")],
            [Finding(name, 1, "python-wait-timeout", "different source")],
            [Finding("other.py", 1, "python-wait-timeout", "child.wait(3)")],
        ]
        self.assertEqual(len(invalid), 7)
        for data in invalid:
            with self.subTest(finding_data=data), self.assertRaises(ValueError):
                validate_findings(name, source, data)

    def _population_controls(self) -> None:
        names = {
            "CMakeLists.txt": "cmake",
            "pyproject.toml": "config",
            POLICY_PATH: "policy",
            "tests/test_no_time_limits.py": "python",
            "tests/_no_time_limits.py": "python",
            "tests/deep/helper.py": "python",
            "tests/fixtures/worker.py": "python",
            "scripts/deep/setup.sh": "shell",
            ".github/workflows/control.yaml": "yaml",
            "cmake/deep/control.cmake": "cmake",
            "setup.py": "python",
            "python/package/build.py": "python",
            "benchmarks/nested/proof.py": "python",
            "tests/control.cpp": "native",
        }
        records = [("100644", name) for name in names]
        records.extend(
            [
                ("100644", "tests/fixtures/strings.json"),
                ("100644", "README.md"),
                ("160000", "external/pineforge-engine"),
                ("100644", "third_party/vendor.py"),
            ]
        )
        self.assertEqual(select_population(records), sorted(names.items()))
        for data in (
            [],
            [("100644", "README.md")],
            [*records, ("100644", "tests/deep/helper.py")],
            [*records, ("120000", "scripts/link.py")],
            [*records, ("100644", "../outside.py")],
        ):
            with self.subTest(population=data), self.assertRaises(ValueError):
                select_population(data)


if __name__ == "__main__":
    unittest.main()
