from __future__ import annotations

from pathlib import Path
import hashlib
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))

import pineforge_codegen  # noqa: E402
from pineforge_codegen.errors import (  # noqa: E402
    CompileError,
    Diagnostic,
    Level,
    Phase,
    SourceLocation,
)
from pineforge_hpo.transpile import (  # noqa: E402
    TranspileFailure,
    CodegenIdentity,
    codegen_identity,
    codegen_supports_request_inventory,
    transpile_source,
    wrapper_codegen_refusal,
)


class TranspileBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        # Existing bridge rows exercise the supported pre-inventory API.
        legacy = patch("pineforge_hpo.transpile.pineforge_codegen", SimpleNamespace())
        legacy.start()
        self.addCleanup(legacy.stop)

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
        self.assertIsNone(result.request_feed_inventory)
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


class WrapperCodegenPolicyTests(unittest.TestCase):
    """Version/source policy units, not release-to-Git attestation."""

    def identity(self, version):
        return CodegenIdentity(version, "c" * 64, str(Path(__file__).resolve()))

    def test_stable_floor_orders_numeric_components_and_rejects_unknown_versions(self):
        for version in (
            "1.10.0",
            "v1.10.0",
            "1.11.0",
            "1.100.0",
            "2.0.0",
            "1.10.0+build.7",
        ):
            with self.subTest(version=version):
                self.assertIsNone(wrapper_codegen_refusal(self.identity(version)))
        for version in ("0.99.99", "1.1.0", "1.9.99", "1.9.99rc1"):
            with self.subTest(version=version):
                self.assertEqual(
                    wrapper_codegen_refusal(self.identity(version)), "below_floor"
                )
        for version in (
            "1.10.0rc1",
            "1.10.0-rc.1",
            "1.10.0.dev1",
            "1.11.0a1",
            "2.0.0-beta.2",
        ):
            with self.subTest(version=version):
                self.assertEqual(
                    wrapper_codegen_refusal(self.identity(version)), "prerelease"
                )
        for version in ("", "unknown", None):
            with self.subTest(version=version):
                self.assertEqual(
                    wrapper_codegen_refusal(self.identity(version)), "version_unknown"
                )
        for version in ("1.10", "1.10.0.1", "1.10.0junk", "01.10.0", " 1.10.0", "main"):
            with self.subTest(version=version):
                self.assertEqual(
                    wrapper_codegen_refusal(self.identity(version)), "version_invalid"
                )

    def test_unmeasured_source_identity_refuses_even_with_supported_version(self):
        for identity in (
            CodegenIdentity("1.10.0", "", "/package/__init__.py"),
            CodegenIdentity("1.10.0", "g" * 64, "/package/__init__.py"),
            CodegenIdentity("1.10.0", "c" * 64, "relative/__init__.py"),
            CodegenIdentity("1.10.0", "c" * 64, ""),
        ):
            with self.subTest(identity=identity):
                self.assertEqual(
                    wrapper_codegen_refusal(identity), "source_unverifiable"
                )

    def test_identity_measures_loaded_package_bytes_and_retains_their_hash(self):
        # A temporary package and version metadata isolate source hashing, not Git ancestry.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "__init__.py"
            module.write_bytes(b"# package\n")
            implementation = root / "impl.py"
            implementation.write_bytes(b"# original implementation\n")
            package = SimpleNamespace(__file__=str(module))
            with (
                patch("pineforge_hpo.transpile.pineforge_codegen", package),
                patch(
                    "pineforge_hpo.transpile.metadata.version", return_value="1.10.0"
                ),
                patch(
                    "pineforge_hpo.transpile._load_codegen",
                    return_value=(None, Exception),
                ),
            ):
                first = codegen_identity()
                expected = hashlib.sha256(
                    b"__init__.py\0"
                    + module.read_bytes()
                    + b"\0"
                    + b"impl.py\0"
                    + implementation.read_bytes()
                    + b"\0"
                ).hexdigest()
                self.assertEqual(first.implementation_sha256, expected)
                self.assertEqual(first.module_path, str(module.resolve()))
                self.assertEqual(first.version, "1.10.0")
                implementation.write_bytes(b"# changed implementation\n")
                second = codegen_identity()
                self.assertNotEqual(
                    first.implementation_sha256, second.implementation_sha256
                )


class RequestInventoryProducerTests(unittest.TestCase):
    def payload(self) -> dict:
        return {
            "cpp": "// generated once: café\n",
            "inputs": [{"title": "Feed", "type": "string", "default": "1D"}],
            "strategyParams": {"initial_capital": 7000},
            "diagnostics": [
                {"severity": "warning", "phase": "ANALYZE", "message": "a note"},
            ],
            "requests": [],
            "request_feed_inventory": {
                "schema": "pineforge-request-feed-inventory/v1",
                "source_sha256": "a" * 64,
                "artifact_sha256": None,
                "primary_chart_timeframe": None,
                "entries": [{"kind": "input", "key": "Feed", "default": "1D"}],
            },
        }

    def test_producer_is_called_once_without_chart_binding(self) -> None:
        # Proves dispatch, chart-unbound kwargs, and all five normalized fields.
        # False pass: a second strategy generation supplies the old metadata.
        producer = Mock(return_value=self.payload())
        with (
            patch(
                "pineforge_hpo.transpile.pineforge_codegen",
                SimpleNamespace(transpile_with_request_inventory=producer),
            ),
            patch("pineforge_hpo.transpile.transpile_full") as full,
            patch("pineforge_hpo.transpile._input_kind_canary", return_value=True),
        ):
            self.assertTrue(codegen_supports_request_inventory())
            result = transpile_source("pine once", filename="inventory.pine")
        producer.assert_called_once_with("pine once", filename="inventory.pine")
        full.assert_not_called()
        self.assertTrue(result.ok)
        self.assertEqual(result.generated_cpp, "// generated once: café\n")
        self.assertEqual(
            result.inputs,
            ({"title": "Feed", "type": "string", "default": "1D"},),
        )
        self.assertEqual(result.strategy_params, {"initial_capital": 7000})
        self.assertEqual(len(result.diagnostics), 1)
        diagnostic = result.diagnostics[0]
        self.assertEqual(
            (diagnostic.severity, diagnostic.phase, diagnostic.message),
            ("warning", "ANALYZE", "a note"),
        )
        self.assertEqual(result.input_kind_schema, 1)
        self.assertEqual(
            result.request_feed_inventory,
            {
                "schema": "pineforge-request-feed-inventory/v1",
                "source_sha256": "a" * 64,
                "artifact_sha256": None,
                "primary_chart_timeframe": None,
                "entries": [{"kind": "input", "key": "Feed", "default": "1D"}],
            },
        )

    def test_old_codegen_uses_only_full_and_carries_no_inventory(self) -> None:
        # Proves detection ignores version strings and never invents an inventory.
        # False pass: falling through to an enriched producer or copying a stray key.
        with (
            patch(
                "pineforge_hpo.transpile.pineforge_codegen",
                SimpleNamespace(__version__="99.0.0"),
            ),
            patch(
                "pineforge_hpo.transpile.transpile_full", return_value=self.payload()
            ) as full,
            patch("pineforge_hpo.transpile._input_kind_canary", return_value=True),
        ):
            self.assertFalse(codegen_supports_request_inventory())
            result = transpile_source("legacy pine", filename="legacy.pine")
        full.assert_called_once_with("legacy pine", filename="legacy.pine")
        self.assertTrue(result.ok)
        self.assertIsNone(result.request_feed_inventory)
        self.assertEqual(result.generated_cpp, "// generated once: café\n")
        self.assertEqual(result.input_kind_schema, 1)

    def test_malformed_producer_inventory_is_an_internal_error(self) -> None:
        # Proves a missing, non-object or wrong-schema inventory cannot be dropped.
        # False pass: success with None makes an unavailable producer look capable.
        for inventory in (None, [], "inventory", {"schema": "other/v1"}):
            with self.subTest(inventory=inventory):
                payload = self.payload()
                payload["request_feed_inventory"] = inventory
                producer = Mock(return_value=payload)
                with (
                    patch(
                        "pineforge_hpo.transpile.pineforge_codegen",
                        SimpleNamespace(transpile_with_request_inventory=producer),
                    ),
                    patch("pineforge_hpo.transpile.transpile_full") as full,
                ):
                    result = transpile_source("bad inventory", filename="bad.pine")
                self.assertFalse(result.ok)
                self.assertIsNone(result.generated_cpp)
                self.assertEqual(result.diagnostics[0].phase, "INTERNAL")
                self.assertEqual(result.diagnostics[0].filename, "bad.pine")
                self.assertIn("request_feed_inventory", result.diagnostics[0].message)
                producer.assert_called_once_with("bad inventory", filename="bad.pine")
                full.assert_not_called()

    def test_real_codegen_preserves_the_five_original_payload_keys(self) -> None:
        # Proves actual codegen equivalence, independently of producer fakes.
        # False pass: only a canned payload is compared or the row silently skips.
        # Like this module's existing errors import, pinned codegen is required.
        source = (
            '//@version=6\nstrategy("inventory parity", initial_capital=7000)\n'
            'length = input.int(7, "Length")\nplot(ta.sma(close, length))\n'
        )
        full = pineforge_codegen.transpile_full(source, filename="parity.pine")
        enriched = pineforge_codegen.transpile_with_request_inventory(
            source, filename="parity.pine"
        )
        for key in ("cpp", "inputs", "strategyParams", "diagnostics", "requests"):
            with self.subTest(key=key):
                self.assertEqual(enriched[key], full[key])
        self.assertEqual(full["inputs"][0]["default"], 7)
        self.assertEqual(full["strategyParams"]["initial_capital"], 7000)
        self.assertIsNone(
            enriched["request_feed_inventory"]["primary_chart_timeframe"],
        )


if __name__ == "__main__":
    unittest.main()
