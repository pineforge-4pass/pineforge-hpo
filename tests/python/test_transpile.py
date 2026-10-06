from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))

from pineforge_codegen.errors import (  # noqa: E402
    CompileError,
    Diagnostic,
    Level,
    Phase,
    SourceLocation,
)
from pineforge_hpo.transpile import TranspileFailure, transpile_source  # noqa: E402


class TranspileBridgeTest(unittest.TestCase):
    def test_success_normalizes_manifest(self) -> None:
        payload = {
            "cpp": "int strategy = 1;\n",
            "inputs": [{"title": "Length", "type": "int", "default": 14}],
            "strategyParams": {"initial_capital": 10000},
        }
        with patch(
            "pineforge_hpo.transpile.transpile_full", return_value=payload
        ) as call:
            result = transpile_source("strategy('x')", filename="trend.pine")

        self.assertTrue(result.ok)
        self.assertEqual(result.require_success(), payload["cpp"])
        self.assertEqual(result.inputs[0]["title"], "Length")
        self.assertEqual(result.strategy_params["initial_capital"], 10000)
        self.assertIsNone(result.input_kind_schema)
        call.assert_called_once_with("strategy('x')", filename="trend.pine")

    def test_requests_alone_do_not_prove_input_kinds(self) -> None:
        for requests, expected in (
            ([], None),
            ([{"line": 1}], None),
            (None, None),
            ({}, None),
            ([42], None),
        ):
            payload = {
                "cpp": "int strategy = 1;\n",
                "inputs": [{"title": "Mode", "type": "string"}],
                "strategyParams": {},
                "requests": requests,
            }
            with patch("pineforge_hpo.transpile.transpile_full", return_value=payload):
                result = transpile_source("strategy('x')")
            self.assertTrue(result.ok)
            self.assertEqual(result.input_kind_schema, expected)
        payload.pop("requests")
        payload["inputs"].append({"title": "Other", "type": "string", "kind": "symbol"})
        with patch("pineforge_hpo.transpile.transpile_full", return_value=payload):
            result = transpile_source("strategy('x')")
        self.assertIsNone(result.input_kind_schema)

    def test_symbol_canary_proves_input_kind_capability(self) -> None:
        payload = {
            "cpp": "int strategy;",
            "inputs": [],
            "strategyParams": {},
            "requests": [],
        }
        canary = {"inputs": [{"type": "string", "kind": "symbol"}]}
        with patch(
            "pineforge_hpo.transpile.transpile_full", side_effect=[payload, canary]
        ) as call:
            result = transpile_source("strategy('x')")
        self.assertEqual(result.input_kind_schema, 1)
        self.assertEqual(call.call_count, 2)
        self.assertIn('input.symbol("NASDAQ:AAPL")', call.call_args.args[0])

    def test_compile_error_becomes_structured_diagnostic(self) -> None:
        error = CompileError(
            [
                Diagnostic(
                    Level.ERROR,
                    Phase.PARSER,
                    SourceLocation("bad.pine", 7, 4, 9),
                    "expected expression",
                    "remove the trailing operator",
                )
            ]
        )
        with patch("pineforge_hpo.transpile.transpile_full", side_effect=error):
            result = transpile_source("bad", filename="bad.pine")

        self.assertFalse(result.ok)
        diagnostic = result.diagnostics[0]
        self.assertEqual(diagnostic.phase, "PARSER")
        self.assertEqual(diagnostic.filename, "bad.pine")
        self.assertEqual(
            (diagnostic.line, diagnostic.column, diagnostic.end_column), (7, 4, 9)
        )
        self.assertEqual(diagnostic.hint, "remove the trailing operator")
        with self.assertRaises(TranspileFailure):
            result.require_success()

    def test_unexpected_bridge_error_is_distinct_internal_diagnostic(self) -> None:
        with patch(
            "pineforge_hpo.transpile.transpile_full", side_effect=RuntimeError("boom")
        ):
            result = transpile_source("source", filename="x.pine")

        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostics[0].phase, "INTERNAL")
        self.assertIn("RuntimeError: boom", result.diagnostics[0].message)


if __name__ == "__main__":
    unittest.main()
