from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))

from pineforge_hpo.error import HpoError  # noqa: E402
from pineforge_hpo.request_inventory import (  # noqa: E402
    RequestInventoryError,
    RequestTimeframeError,
    admit_request_feed_inventory,
    read_request_feed_inventory,
)
from pineforge_hpo.study_spec import ParameterSpec  # noqa: E402


class RequestInventoryAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_hash = hashlib.sha256(b"generated source").hexdigest()
        self.plugin_hash = hashlib.sha256(b"linked plugin").hexdigest()
        self.inventory = {
            "schema": "pineforge-request-feed-inventory/v1",
            "source_sha256": self.source_hash,
            "artifact_sha256": self.plugin_hash,
            "primary_chart_timeframe": None,
            "entries": [{"kind": "token", "timeframe": "1"}],
        }

    def admit(self, inventory, **options) -> tuple[str, ...]:
        arguments = {
            "plugin_sha256": self.plugin_hash,
            "generated_cpp_sha256": self.source_hash,
            "chart_timeframe": "15",
            "fixed_inputs": {},
            "input_domains": {},
        }
        arguments.update(options)
        return admit_request_feed_inventory(inventory, **arguments)

    def assert_reason(self, error: RequestInventoryError, reason: str) -> None:
        self.assertIsInstance(error, HpoError)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(error.code, "hpo_walkforward_inventory_invalid")
        self.assertEqual(
            error.failure_args,
            {
                "capability": "selected_window_request_feed_inventory",
                "reason": reason,
            },
        )
        self.assertEqual(error.args, (str(error),))

    def test_missing_inventory_is_not_an_empty_assertion(self) -> None:
        # Proves absence refuses despite a valid chart and supplied digests.
        # False pass: treating None as [] admits a strategy with no declaration.
        with self.assertRaises(RequestInventoryError) as caught:
            self.admit(None)
        self.assert_reason(caught.exception, "missing")

    def test_empty_entries_are_complete_and_all_primary_tokens_are_admitted(
        self,
    ) -> None:
        # Proves empty means complete, and pins each primary policy member.
        # False pass: using truthiness rejects empty or only the default chart works.
        self.inventory["entries"] = []
        for chart in ("1", "3", "5", "15", "30", "60", "120", "240"):
            with self.subTest(chart=chart):
                self.assertEqual(self.admit(self.inventory, chart_timeframe=chart), ())

    def test_tokens_are_sorted_unique_and_admission_performs_no_io(self) -> None:
        # Proves request tokens are computed, canonical, sorted and deduplicated.
        # False pass: a constant token list, entry order, or filesystem lookup passes.
        self.inventory["entries"] = [
            {"kind": "token", "timeframe": "D"},
            {"kind": "token", "timeframe": "1"},
            {"kind": "token", "timeframe": "D"},
        ]
        original = copy.deepcopy(self.inventory)
        # Load the token module before trapping I/O; imports are not admission work.
        from pineforge_codegen.request_feed_inventory import canonical_wire_timeframe

        self.assertEqual(canonical_wire_timeframe("1D"), "D")
        with (
            patch("builtins.open", side_effect=AssertionError("admission read a file")),
            patch.object(
                Path,
                "open",
                side_effect=AssertionError("admission read a path"),
            ),
            patch(
                "ctypes.CDLL",
                side_effect=AssertionError("admission loaded a plugin"),
            ),
        ):
            self.assertEqual(self.admit(self.inventory), ("1", "D"))
        self.assertEqual(self.inventory, original)
        self.inventory["entries"] = [{"kind": "token", "timeframe": "D"}]
        self.assertEqual(self.admit(self.inventory), ("D",))

    def test_exact_five_keys_schema_and_entry_shapes(self) -> None:
        # Proves each invalid row isolates one schema/shape rule from valid bindings.
        # False pass: a malformed digest or unrelated refusal hides a missing check.
        cases = []
        missing = copy.deepcopy(self.inventory)
        missing.pop("primary_chart_timeframe")
        cases.append(("missing key", missing))
        extra = copy.deepcopy(self.inventory)
        extra["extra"] = 1
        cases.append(("extra key", extra))
        for key, value in (
            ("schema", "pineforge-request-feed-inventory/v2"),
            ("source_sha256", "A" * 64),
            ("artifact_sha256", "0" * 63),
            ("primary_chart_timeframe", "1D"),
            ("entries", {}),
            ("entries", [None]),
            ("entries", [{"kind": "token", "timeframe": "1", "extra": 0}]),
            ("entries", [{"kind": "token"}]),
            ("entries", [{"kind": "input", "key": 1, "default": "1"}]),
            ("entries", [{"kind": "input", "key": "Feed", "default": 1}]),
            (
                "entries",
                [{"kind": "input", "key": "Feed", "default": "1", "extra": 0}],
            ),
            ("entries", [{"kind": "unknown", "extra": 0}]),
            ("entries", [{"kind": "other"}]),
        ):
            inventory = copy.deepcopy(self.inventory)
            inventory[key] = value
            cases.append((f"{key}: {value!r}", inventory))
        for label, inventory in cases:
            with self.subTest(clause=label):
                with self.assertRaises(RequestInventoryError) as caught:
                    self.admit(inventory)
                self.assert_reason(caught.exception, "invalid")

    def test_unbound_artifact_is_refused(self) -> None:
        # Proves null binding refuses even with a correctly bound generated source.
        # False pass: skipping plugin binding admits the valid-looking schema.
        self.inventory["artifact_sha256"] = None
        with self.assertRaises(RequestInventoryError) as caught:
            self.admit(self.inventory)
        self.assert_reason(caught.exception, "unbound")
        self.assertIn("artifact_sha256", str(caught.exception))

    def test_each_digest_must_match_the_supplied_artifact_identity(self) -> None:
        # Proves source and plugin binding independently; other digest stays valid.
        # False pass: validating just hex shape or only the plugin digest passes.
        for field in ("source_sha256", "artifact_sha256"):
            with self.subTest(field=field):
                inventory = copy.deepcopy(self.inventory)
                inventory[field] = "0" * 64
                with self.assertRaises(RequestInventoryError) as caught:
                    self.admit(inventory)
                self.assert_reason(caught.exception, "artifact_mismatch")
                self.assertIn(field, str(caught.exception))

    def test_chart_binding_accepts_match_and_refuses_mismatch(self) -> None:
        # Proves primary binding is enforced independently of the primary policy.
        # False pass: both supported charts are admitted despite a different binding.
        self.inventory["primary_chart_timeframe"] = "15"
        self.assertEqual(self.admit(self.inventory), ("1",))
        with self.assertRaises(RequestInventoryError) as caught:
            self.admit(self.inventory, chart_timeframe="30")
        self.assert_reason(caught.exception, "chart_mismatch")
        self.assertIn("'15'", str(caught.exception))
        self.assertIn("'30'", str(caught.exception))

    def test_unknown_entry_always_refuses(self) -> None:
        # Proves one unknown poisons an otherwise valid complete-looking inventory.
        # False pass: ignoring unknown retains the admitted token from another entry.
        self.inventory["entries"].append({"kind": "unknown"})
        with self.assertRaises(RequestInventoryError) as caught:
            self.admit(self.inventory)
        self.assert_reason(caught.exception, "unknown")
        self.assertIn("entries[1]", str(caught.exception))

    def test_input_default_and_fixed_override_resolve_aliases(self) -> None:
        # Proves default/fixed precedence and alias normalization to a token set.
        # False pass: ignoring the fixed value returns the default in both rows.
        self.inventory["entries"] = [
            {"kind": "input", "key": "Feed", "default": "1D"},
        ]
        self.assertEqual(self.admit(self.inventory), ("D",))
        self.assertEqual(
            self.admit(self.inventory, fixed_inputs={"Feed": "1"}),
            ("1",),
        )

    def test_invalid_present_input_never_falls_back_to_default(self) -> None:
        # Proves each non-string/non-token present value refuses with a valid default.
        # False pass: falsy values or parse failures fall back and admit D.
        self.inventory["entries"] = [
            {"kind": "input", "key": "Feed", "default": "D"},
        ]
        for value in (None, False, 1, 1.0, "", "01", " 1", "1H", "d", "1\n"):
            with self.subTest(value=value):
                with self.assertRaises(RequestInventoryError) as caught:
                    self.admit(self.inventory, fixed_inputs={"Feed": value})
                self.assert_reason(caught.exception, "input_unresolved")
                self.assertIn("'Feed'", str(caught.exception))
                self.assertIn(repr(value), str(caught.exception))

    def test_invalid_default_is_refused_when_no_effective_value_exists(self) -> None:
        # Proves defaults are resolved rather than assumed to be canonical tokens.
        # False pass: a string-shaped default is accepted without token validation.
        self.inventory["entries"] = [
            {"kind": "input", "key": "Feed", "default": "invalid"},
        ]
        with self.assertRaises(RequestInventoryError) as caught:
            self.admit(self.inventory)
        self.assert_reason(caught.exception, "input_unresolved")
        self.assertEqual(
            self.admit(self.inventory, fixed_inputs={"Feed": "1D"}),
            ("D",),
        )

    def test_finite_sweeps_check_every_choice_without_cartesian_products(self) -> None:
        # Proves all reachable strings, not just a fixed/default value, contribute.
        # False pass: first-choice-only admission or inspecting an unrelated domain.
        self.inventory["entries"] = [
            {"kind": "input", "key": "Fast", "default": "invalid"},
            {"kind": "input", "key": "Slow", "default": "invalid"},
        ]
        domains = {
            "Fast": ParameterSpec(kind="categorical", choices=("1", "1D", "D")),
            "Slow": ParameterSpec(kind="categorical", choices=("D", "1")),
            "Unrelated": ParameterSpec(kind="real", low=0.0, high=1.0),
        }
        self.assertEqual(
            self.admit(
                self.inventory,
                fixed_inputs={"Fast": None},
                input_domains=domains,
            ),
            ("1", "D"),
        )
        for choices in (("1", None), ("1D", "not-a-token")):
            with self.subTest(choices=choices):
                changed = dict(domains)
                changed["Slow"] = ParameterSpec(kind="categorical", choices=choices)
                with self.assertRaises(RequestInventoryError) as caught:
                    self.admit(self.inventory, input_domains=changed)
                self.assert_reason(caught.exception, "input_unresolved")
                self.assertIn("'Slow'", str(caught.exception))
        changed = dict(domains)
        changed["Slow"] = ParameterSpec(kind="categorical", choices=("1", "240"))
        with self.assertRaises(RequestTimeframeError) as caught:
            self.admit(self.inventory, input_domains=changed)
        self.assertEqual(
            caught.exception.failure_args,
            {"capability": "selected_window_request_feed_timeframe"},
        )
        self.assertIn("'240'", str(caught.exception))

    def test_non_enumerable_empty_and_non_string_domains_are_unresolved(self) -> None:
        # Proves a swept domain cannot fall back to its admissible default/fixed value.
        # False pass: coercing numeric values or admitting an unenumerable real passes.
        self.inventory["entries"] = [
            {"kind": "input", "key": "Feed", "default": "1"},
        ]
        for domain in (
            ParameterSpec(kind="real", low=1.0, high=2.0),
            ParameterSpec(kind="real", low=1.0, high=1.0),
            ParameterSpec(kind="integer", low=1, high=3, step=1),
            ParameterSpec(kind="boolean"),
            ParameterSpec(kind="categorical"),
            ParameterSpec(kind="categorical", choices=("1", 1)),
        ):
            with self.subTest(domain=domain):
                with self.assertRaises(RequestInventoryError) as caught:
                    self.admit(
                        self.inventory,
                        fixed_inputs={"Feed": "D"},
                        input_domains={"Feed": domain},
                    )
                self.assert_reason(caught.exception, "input_unresolved")
                self.assertIn("'Feed'", str(caught.exception))

    def test_literal_tokens_must_already_be_canonical(self) -> None:
        # Proves token entries are stricter than input-value aliases.
        # False pass: silently canonicalizing a malformed producer token admits 1D.
        for token in ("1D", "1W", "M", "S", "01", "1H", "d", " 1", "1\n"):
            with self.subTest(token=token):
                self.inventory["entries"] = [{"kind": "token", "timeframe": token}]
                with self.assertRaises(RequestInventoryError) as caught:
                    self.admit(self.inventory)
                self.assert_reason(caught.exception, "invalid")
                self.assertIn(repr(token), str(caught.exception))

    def test_canonical_input_aliases_reach_policy_as_canonical_tokens(self) -> None:
        # Proves 1D is accepted, while other valid wire aliases reach policy refusal.
        # False pass: treating W/M/S aliases as malformed instead of unsupported.
        self.inventory["entries"] = [
            {"kind": "input", "key": "Feed", "default": "1"},
        ]
        for value, canonical in (("1W", "W"), ("M", "1M"), ("S", "1S")):
            with self.subTest(value=value):
                with self.assertRaises(RequestTimeframeError) as caught:
                    self.admit(self.inventory, fixed_inputs={"Feed": value})
                self.assertEqual(
                    caught.exception.failure_args,
                    {"capability": "selected_window_request_feed_timeframe"},
                )
                self.assertIn(repr(canonical), str(caught.exception))

    def test_chart_policy_refuses_outside_the_primary_list(self) -> None:
        # Proves the primary capability alone refuses, even with a valid inventory.
        # False pass: checking only feeds admits unsupported primary charts.
        for chart in ("2", "D", "1D", "W", "01"):
            with self.subTest(chart=chart):
                with self.assertRaises(RequestTimeframeError) as caught:
                    self.admit(self.inventory, chart_timeframe=chart)
                self.assertIsInstance(caught.exception, HpoError)
                self.assertEqual(
                    caught.exception.code, "hpo_walkforward_timeframe_unsupported"
                )
                self.assertEqual(
                    caught.exception.failure_args,
                    {"capability": "selected_window_chart_timeframe"},
                )
                self.assertIn(repr(chart), str(caught.exception))

    def test_feed_policy_refuses_canonical_tokens_in_sorted_diagnostics(self) -> None:
        # Proves feed policy runs after valid shape/bindings with a supported primary.
        # False pass: permitting any canonical token or checking only the first entry.
        self.inventory["entries"] = [
            {"kind": "token", "timeframe": "W"},
            {"kind": "token", "timeframe": "1"},
            {"kind": "token", "timeframe": "240"},
        ]
        with self.assertRaises(RequestTimeframeError) as caught:
            self.admit(self.inventory)
        self.assertEqual(caught.exception.code, "hpo_walkforward_timeframe_unsupported")
        self.assertEqual(
            caught.exception.failure_args,
            {"capability": "selected_window_request_feed_timeframe"},
        )
        self.assertIn("['240', 'W']", str(caught.exception))


class RequestInventoryReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest_path = self.root / "manifest.json"
        self.sidecar = self.root / "request_feed_inventory.json"
        self.inventory = {
            "schema": "pineforge-request-feed-inventory/v1",
            "source_sha256": "a" * 64,
            "artifact_sha256": "b" * 64,
            "primary_chart_timeframe": None,
            "entries": [],
        }
        self.manifest = {"request_feed_inventory": self.inventory}

    def write_manifest(self) -> None:
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def assert_reason(self, caught, reason: str) -> None:
        self.assertEqual(caught.exception.code, "hpo_walkforward_inventory_invalid")
        self.assertEqual(
            caught.exception.failure_args,
            {
                "capability": "selected_window_request_feed_inventory",
                "reason": reason,
            },
        )

    def test_manifest_only_and_equal_sidecar_accept_different_json_layouts(
        self,
    ) -> None:
        # Proves sidecar is optional, but equivalent parsed bytes are accepted.
        # False pass: comparing file text rejects formatting or key-order differences.
        self.write_manifest()
        self.assertEqual(read_request_feed_inventory(self.root), self.inventory)
        self.sidecar.write_text(
            json.dumps(dict(reversed(list(self.inventory.items()))), indent=2),
            encoding="utf-8",
        )
        self.assertEqual(
            read_request_feed_inventory(self.manifest_path),
            self.inventory,
        )
        self.assertEqual(read_request_feed_inventory(self.manifest), self.inventory)

    def test_missing_block_and_producer_unavailable_are_distinct(self) -> None:
        # Proves explicit producer absence differs from a legacy missing key.
        # False pass: reading only a sidecar manufactures a block for either state.
        self.sidecar.write_text(json.dumps(self.inventory), encoding="utf-8")
        for identity, reason in (
            ({}, "missing"),
            ({"request_feed_inventory": None}, "producer_unavailable"),
            (
                {"request_feed_inventory": "pineforge-request-feed-inventory/v1"},
                "missing",
            ),
        ):
            with self.subTest(identity=identity):
                self.manifest = {"request_identity": identity}
                self.write_manifest()
                with self.assertRaises(RequestInventoryError) as caught:
                    read_request_feed_inventory(self.root)
                self.assert_reason(caught, reason)
                self.assertIn(str(self.manifest_path), str(caught.exception))
                self.assertIn("request_feed_inventory", str(caught.exception))

    def test_missing_manifest_is_missing(self) -> None:
        # Proves the file boundary preserves the missing reason and names the path.
        # False pass: JSON errors or an implicit empty inventory replace absence.
        with self.assertRaises(RequestInventoryError) as caught:
            read_request_feed_inventory(self.manifest_path)
        self.assert_reason(caught, "missing")
        self.assertIn(str(self.manifest_path), str(caught.exception))

    def test_present_null_block_is_invalid_even_with_null_producer_identity(
        self,
    ) -> None:
        # Proves explicit corrupt publication cannot be mistaken for no producer.
        # False pass: get() conflates an absent block with a present null block.
        self.manifest = {
            "request_identity": {"request_feed_inventory": None},
            "request_feed_inventory": None,
        }
        self.write_manifest()
        with self.assertRaises(RequestInventoryError) as caught:
            read_request_feed_inventory(self.root)
        self.assert_reason(caught, "invalid")

    def test_sidecar_disagreement_is_invalid_and_names_both_paths(self) -> None:
        # Proves a present second copy cannot silently override the manifest block.
        # False pass: reading only one copy admits inconsistent artifact publication.
        self.write_manifest()
        changed = copy.deepcopy(self.inventory)
        changed["entries"] = [{"kind": "token", "timeframe": "1"}]
        self.sidecar.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(RequestInventoryError) as caught:
            read_request_feed_inventory(self.root)
        self.assert_reason(caught, "invalid")
        self.assertIn(str(self.sidecar), str(caught.exception))
        self.assertIn(str(self.manifest_path), str(caught.exception))

    def test_corrupt_sidecar_is_invalid(self) -> None:
        # Proves invalid JSON at the optional sidecar is checked when present.
        # False pass: a parse failure is treated as sidecar absence.
        self.write_manifest()
        for raw in ("{", '{"source_sha256":NaN}'):
            with self.subTest(raw=raw):
                self.sidecar.write_text(raw, encoding="utf-8")
                with self.assertRaises(RequestInventoryError) as caught:
                    read_request_feed_inventory(self.root)
                self.assert_reason(caught, "invalid")
                self.assertIn(str(self.sidecar), str(caught.exception))

    def test_duplicate_keys_are_invalid_at_each_json_boundary(self) -> None:
        # Proves duplicates in the manifest, inventory and an entry cannot overwrite.
        # False pass: normal json.loads keeps the last equal value and admits it.
        encoded = json.dumps(self.inventory)
        duplicate_schema = encoded.replace(
            '"schema":', '"schema":"pineforge-request-feed-inventory/v1","schema":', 1
        )
        duplicate_entry = encoded.replace(
            '"entries": []', '"entries":[{"kind":"unknown","kind":"unknown"}]'
        )
        duplicate_block = (
            f'{{"request_feed_inventory":{encoded},'
            f'"request_feed_inventory":{encoded}}}'
        )
        for raw in (
            duplicate_block,
            f'{{"request_feed_inventory":{duplicate_schema}}}',
            f'{{"request_feed_inventory":{duplicate_entry}}}',
        ):
            with self.subTest(raw=raw):
                self.manifest_path.write_text(raw, encoding="utf-8")
                with self.assertRaises(RequestInventoryError) as caught:
                    read_request_feed_inventory(self.root)
                self.assert_reason(caught, "invalid")
                self.assertIn("duplicate JSON key", str(caught.exception))
        self.write_manifest()
        self.sidecar.write_text(duplicate_schema, encoding="utf-8")
        with self.assertRaises(RequestInventoryError) as caught:
            read_request_feed_inventory(self.root)
        self.assert_reason(caught, "invalid")
        self.assertIn(str(self.sidecar), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
