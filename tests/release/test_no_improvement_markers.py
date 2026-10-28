"""Keep the patience CI report synchronized with the test's printed markers."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
STEP = "Report patience capability and skips"


def required_markers(workflow):
    """Read the marker loop from this step's embedded Python, using only stdlib."""
    step = re.search(
        rf"^      - name: {re.escape(STEP)}\n"
        r"(?P<body>(?:(?!      - name: ).*\n)*)",
        workflow,
        re.MULTILINE,
    )
    if step is None:
        raise AssertionError(f"missing CI step: {STEP}")
    script = re.search(
        r"^          python3 - <<'PY'\n(?P<code>.*?)^          PY$",
        step["body"],
        re.MULTILINE | re.DOTALL,
    )
    if script is None:
        raise AssertionError("patience report has no embedded Python script")
    tree = ast.parse(textwrap.dedent(script["code"]))
    loops = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "marker"
    ]
    if len(loops) != 1:
        raise AssertionError("expected one required-marker loop in the patience report")
    markers = ast.literal_eval(loops[0].iter)
    if not markers or not all(isinstance(marker, str) and marker for marker in markers):
        raise AssertionError("the required-marker loop must contain nonempty strings")
    return markers


def printed_markers(source):
    """Only a literal prefix passed to print counts as a marker printer."""
    markers = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
            and node.args
        ):
            prefix = node.args[0]
            while isinstance(prefix, ast.BinOp) and isinstance(prefix.op, ast.Add):
                prefix = prefix.left
            if isinstance(prefix, ast.Constant) and isinstance(prefix.value, str):
                markers.add(prefix.value)
    return markers


class NoImprovementMarkerTests(unittest.TestCase):
    def test_ci_required_markers_have_printers(self):
        source = (ROOT / "tests/test_no_improvement.py").read_text(encoding="utf-8")
        printers = printed_markers(source)
        workflows = {
            name: (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
            for name in ("ci.yml", "ci-post.yml", "gke-pr.yml")
        }
        self.assertNotIn(f"- name: {STEP}", workflows["ci.yml"])
        self.assertNotIn(f"- name: {STEP}", workflows["gke-pr.yml"])
        self.assertIn(f"- name: {STEP}", workflows["ci-post.yml"])
        for marker in required_markers(workflows["ci-post.yml"]):
            with self.subTest(marker=marker):
                self.assertIn(
                    marker, printers, "CI requires a marker the test never prints"
                )

    def test_a_mention_without_printing_does_not_satisfy_the_contract(self):
        source = 'marker = "NO_IMPROVEMENT_CAPABILITY "\nprint("other " + marker)\n'
        self.assertNotIn("NO_IMPROVEMENT_CAPABILITY ", printed_markers(source))


if __name__ == "__main__":
    unittest.main()
