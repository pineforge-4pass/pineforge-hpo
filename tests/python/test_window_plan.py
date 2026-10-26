from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

from pineforge_hpo.window_plan import (  # noqa: E402
    WindowPlanError,
    WindowPlanIdentity,
    require_window_plan_block,
    resolve_window_plan_path,
    verify_window_plan,
)


class WindowPlanIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.helper = self.root / "relocated-helper.so"
        self.helper.write_bytes(b"abc")
        self.block = {
            "schema": "pineforge-hpo-window-plan/v1",
            "build_path": "/build-machine/lib/libpineforge_window_plan.so",
            "sha256": (
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
            ),
            "planner_abi_version": 1,
            "engine_version": "1.7.0",
            "engine_abi_version": 4,
            "engine_headers_sha256": "1" * 64,
            "engine_static_library_sha256": "2" * 64,
            "platform_system": "Linux",
            "platform_machine": "x86_64",
            "self_test_id": "selected-plan-utc-six-hours/v1",
        }
        self.manifest = {
            "schema_version": 1,
            "request_identity": {
                "engine": {
                    "version": "1.7.0",
                    "abi_version": 4,
                    "headers_sha256": "1" * 64,
                    "static_library_sha256": "2" * 64,
                },
                "platform": {"system": "Linux", "machine": "x86_64"},
            },
            "window_plan": copy.deepcopy(self.block),
        }
        self.provenance = copy.deepcopy(self.manifest)
        self.addCleanup(patch.stopall)
        patch("pineforge_hpo.window_plan.platform.system", return_value="Linux").start()
        patch(
            "pineforge_hpo.window_plan.platform.machine",
            return_value="x86_64",
        ).start()

    def assert_reason(self, caught, reason: str) -> None:
        self.assertEqual(caught.exception.code, "hpo_walkforward_unavailable")
        self.assertEqual(caught.exception.origin, "hpo")
        self.assertEqual(caught.exception.failure_args, {"reason": reason})

    def test_identity_round_trip_and_frozen_fields(self) -> None:
        # Proves the complete hand-written block survives JSON and stays immutable.
        # False pass: unknown fields vanish, values coerce, or the identity is mutable.
        identity = WindowPlanIdentity.from_dict(json.loads(json.dumps(self.block)))
        self.assertEqual(identity.to_dict(), self.block)
        self.assertEqual(identity.engine_abi_version, 4)
        with self.assertRaises(FrozenInstanceError):
            identity.sha256 = "0" * 64

    def test_identity_rejects_non_exact_shapes_and_types(self) -> None:
        # Proves shape, literals, absolute audit path, hashes and strict integer types.
        # False pass: a permissive decoder silently repairs any malformed field.
        missing = copy.deepcopy(self.block)
        del missing["sha256"]
        malformed = [
            None,
            [],
            missing,
            {**self.block, "extra": "ignored"},
        ]
        for name, value in (
            ("schema", "pineforge-hpo-window-plan/v2"),
            ("build_path", "relative/helper.so"),
            ("build_path", "/bad\0path"),
            ("sha256", "A" * 64),
            ("sha256", "g" * 64),
            ("sha256", "0" * 63),
            ("planner_abi_version", True),
            ("planner_abi_version", 2),
            ("engine_version", 170),
            ("engine_version", ""),
            ("engine_abi_version", True),
            ("engine_abi_version", 4.0),
            ("engine_headers_sha256", None),
            ("engine_static_library_sha256", "short"),
            ("platform_system", ""),
            ("platform_machine", 64),
            ("self_test_id", "selected-plan-utc-six-hours/v2"),
        ):
            malformed.append({**self.block, name: value})
        for value in malformed:
            with self.subTest(value=value):
                with self.assertRaises(WindowPlanError) as caught:
                    WindowPlanIdentity.from_dict(value)
                self.assert_reason(caught, "window_plan_manifest_invalid")
                with self.assertRaises(WindowPlanError) as caught:
                    require_window_plan_block({**self.manifest, "window_plan": value})
                self.assert_reason(caught, "window_plan_manifest_invalid")

    def test_precompiled_missing_block_cannot_be_supplied_by_a_path(self) -> None:
        # Proves precompiled admission requires recorded expected bytes.
        # False pass: an existing explicit helper manufactures the missing identity.
        del self.manifest["window_plan"]
        del self.provenance["window_plan"]
        self.assertTrue(self.helper.is_file())
        with self.assertRaises(WindowPlanError) as caught:
            require_window_plan_block(self.manifest, self.provenance)
        self.assert_reason(caught, "window_plan_manifest_missing")
        self.assertIn("manifest.window_plan", str(caught.exception))

    def test_provenance_is_optional_but_a_supplied_pair_must_agree(self) -> None:
        # Proves absent provenance is allowed, while a partial pair fails closed.
        # False pass: supplied provenance is ignored or a missing block is accepted.
        self.assertEqual(require_window_plan_block(self.manifest), self.block)
        self.assertEqual(
            require_window_plan_block(self.manifest, self.provenance),
            self.block,
        )
        for absent in ("manifest", "provenance"):
            manifest = copy.deepcopy(self.manifest)
            provenance = copy.deepcopy(self.provenance)
            del (manifest if absent == "manifest" else provenance)["window_plan"]
            with self.subTest(absent=absent):
                with self.assertRaises(WindowPlanError) as caught:
                    require_window_plan_block(manifest, provenance)
                self.assert_reason(caught, "window_plan_identity")
        self.provenance["window_plan"]["sha256"] = "0" * 64
        with self.assertRaises(WindowPlanError) as caught:
            require_window_plan_block(self.manifest, self.provenance)
        self.assert_reason(caught, "window_plan_identity")

    def test_malformed_provenance_block_is_refused(self) -> None:
        # Proves provenance uses the same strict decoder as the manifest.
        # False pass: comparing unvalidated dictionaries masks malformed provenance.
        self.provenance["window_plan"]["planner_abi_version"] = True
        with self.assertRaises(WindowPlanError) as caught:
            require_window_plan_block(self.manifest, self.provenance)
        self.assert_reason(caught, "window_plan_manifest_invalid")

    def test_block_must_match_both_compile_associations(self) -> None:
        # Proves every recorded engine/platform association is independently checked.
        # False pass: equal companion blocks bypass a changed compilation identity.
        for section, field, value in (
            ("engine", "version", "1.6.1"),
            ("engine", "abi_version", True),
            ("engine", "headers_sha256", "3" * 64),
            ("engine", "static_library_sha256", "4" * 64),
            ("platform", "system", "Darwin"),
            ("platform", "machine", "arm64"),
        ):
            for target in ("manifest", "provenance", "both"):
                manifest = copy.deepcopy(self.manifest)
                provenance = copy.deepcopy(self.provenance)
                if target in {"manifest", "both"}:
                    manifest["request_identity"][section][field] = value
                if target in {"provenance", "both"}:
                    provenance["request_identity"][section][field] = value
                with self.subTest(section=section, field=field, target=target):
                    with self.assertRaises(WindowPlanError) as caught:
                        require_window_plan_block(manifest, provenance)
                    self.assert_reason(caught, "window_plan_identity")

    def test_relative_explicit_path_is_refused_before_normalization(self) -> None:
        # Proves relative paths never reach cwd normalization or derivation.
        # False pass: resolving against cwd happens to produce an absolute path.
        for value in ("helper.so", "../helper.so", "~/helper.so", ""):
            with (
                self.subTest(value=value),
                patch.object(
                    Path,
                    "resolve",
                    side_effect=AssertionError("normalized too soon"),
                ),
                patch(
                    "pineforge_hpo.window_plan.derive_window_plan_path",
                    side_effect=AssertionError("explicit path fell back"),
                ),
            ):
                with self.assertRaises(WindowPlanError) as caught:
                    resolve_window_plan_path(value, self.root, self.block)
                self.assert_reason(caught, "window_plan_path")

    def test_invalid_explicit_path_never_falls_back(self) -> None:
        # Proves missing files and directories refuse despite an available root.
        # False pass: a failed explicit lookup triggers a successful sibling search.
        for value in (self.root / "missing.so", self.root):
            with (
                self.subTest(value=value),
                patch(
                    "pineforge_hpo.window_plan.derive_window_plan_path",
                    side_effect=AssertionError("explicit path fell back"),
                ),
            ):
                with self.assertRaises(WindowPlanError) as caught:
                    resolve_window_plan_path(value, self.root, self.block)
                self.assert_reason(caught, "window_plan_missing")

    def test_explicit_path_takes_precedence_over_engine_root(self) -> None:
        # Proves a valid explicit path does not consult an invalid local root.
        # False pass: only root derivation works, hiding the explicit-path branch.
        with patch(
            "pineforge_hpo.window_plan.derive_window_plan_path",
            side_effect=AssertionError("explicit path used derivation"),
        ):
            path = resolve_window_plan_path(
                self.helper,
                self.root / "no-engine",
                self.block,
            )
        self.assertEqual(path, self.helper)
        self.assertIsNone(verify_window_plan(self.block, path))

    def test_precompiled_relocation_accepts_only_the_recorded_bytes(self) -> None:
        # Proves relocation accepts a literal byte copy without a build-machine root.
        # False pass: a helper is accepted by basename or the hash check is skipped.
        block = require_window_plan_block(self.manifest, self.provenance)
        path = resolve_window_plan_path(self.helper, None, block)
        self.assertEqual(path, self.root / "relocated-helper.so")
        self.assertIsNone(verify_window_plan(block, path))
        self.helper.write_bytes(b"abd")
        with self.assertRaises(WindowPlanError) as caught:
            verify_window_plan(block, path)
        self.assert_reason(caught, "window_plan_hash_mismatch")

    def test_build_path_is_audit_only_even_when_it_exists_locally(self) -> None:
        # Proves recorded build_path never becomes a run-machine lookup instruction.
        # False pass: a coincidentally existing build path makes admission succeed.
        self.block["build_path"] = str(self.helper)
        with self.assertRaises(WindowPlanError) as caught:
            resolve_window_plan_path(None, None, self.block)
        self.assert_reason(caught, "window_plan_path")

    def test_resolution_refuses_a_different_runtime_platform(self) -> None:
        # Proves matching helper bytes do not waive the recorded platform association.
        # False pass: cross-platform bytes are accepted without consulting identity.
        for field, value in (
            ("platform_system", "Darwin"),
            ("platform_machine", "arm64"),
        ):
            with self.subTest(field=field):
                block = {**self.block, field: value}
                with self.assertRaises(WindowPlanError) as caught:
                    resolve_window_plan_path(self.helper, None, block)
                self.assert_reason(caught, "window_plan_identity")

    def test_verification_refuses_missing_and_relative_files(self) -> None:
        # Proves direct verification cannot bypass the path checks used by resolution.
        # False pass: only the resolver has typed failures; verification leaks OSError.
        for value, reason in (
            (self.root / "missing.so", "window_plan_missing"),
            (Path("helper.so"), "window_plan_path"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(WindowPlanError) as caught:
                    verify_window_plan(self.block, value)
                self.assert_reason(caught, reason)


if __name__ == "__main__":
    unittest.main()
