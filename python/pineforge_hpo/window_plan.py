"""Recorded identity and hash-only admission for the native planner companion.

This module never loads a library. The native consumer must independently verify
and load a private snapshot to close the pathname race, then check the planner
exports, ABI and frozen self-test. Association records bytes, not proof of co-build.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import os
from pathlib import Path
import platform
import re
from typing import TYPE_CHECKING, Any, Literal, Mapping

from .error import HpoError

if TYPE_CHECKING:
    from .artifact import _EngineLayout

WindowPlanReason = Literal[
    "window_plan_hash_mismatch",
    "window_plan_identity",
    "window_plan_manifest_invalid",
    "window_plan_manifest_missing",
    "window_plan_missing",
    "window_plan_path",
]


class WindowPlanError(HpoError, RuntimeError):
    """A selected-execution refusal with a closed, HPO-owned reason."""

    def __init__(self, reason: WindowPlanReason, message: str) -> None:
        diagnostic = message.encode("utf-8", errors="replace")[:4096].decode(
            "utf-8",
            errors="ignore",
        )
        super().__init__(diagnostic)
        self.with_failure("hpo_walkforward_unavailable", {"reason": reason})


@dataclass(frozen=True)
class WindowPlanIdentity:
    """The exact additive v1 block, outside the strategy compilation identity."""

    schema: str
    build_path: str
    sha256: str
    planner_abi_version: int
    engine_version: str
    engine_abi_version: int
    engine_headers_sha256: str
    engine_static_library_sha256: str
    platform_system: str
    platform_machine: str
    self_test_id: str

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            expected_type = (
                int
                if field.name in {"planner_abi_version", "engine_abi_version"}
                else str
            )
            if type(value) is not expected_type or (
                expected_type is str and (not value or "\0" in value)
            ):
                raise WindowPlanError(
                    "window_plan_manifest_invalid",
                    f"manifest.window_plan.{field.name} has an invalid type or value",
                )
        if (
            self.schema != "pineforge-hpo-window-plan/v1"
            or self.planner_abi_version != 1
            or self.self_test_id != "selected-plan-utc-six-hours/v1"
            or not 0 < self.engine_abi_version <= 0xFFFFFFFF
            or not Path(self.build_path).is_absolute()
        ):
            raise WindowPlanError(
                "window_plan_manifest_invalid",
                "manifest.window_plan has an invalid schema, ABI, self-test or path",
            )
        for name in (
            "sha256",
            "engine_headers_sha256",
            "engine_static_library_sha256",
        ):
            if re.fullmatch(r"[0-9a-f]{64}", getattr(self, name)) is None:
                raise WindowPlanError(
                    "window_plan_manifest_invalid",
                    f"manifest.window_plan.{name} must be 64 lowercase hex characters",
                )

    def to_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_dict(cls, value: object) -> WindowPlanIdentity:
        if not isinstance(value, Mapping) or set(value) != {
            field.name for field in fields(cls)
        }:
            raise WindowPlanError(
                "window_plan_manifest_invalid",
                "manifest.window_plan must contain exactly the v1 identity fields",
            )
        return cls(**value)


def _identity(block: WindowPlanIdentity | Mapping[str, Any]) -> WindowPlanIdentity:
    if isinstance(block, WindowPlanIdentity):
        return block
    return WindowPlanIdentity.from_dict(block)


def _check_fields(observed: object, expected: Mapping[str, Any], location: str) -> None:
    if not isinstance(observed, Mapping):
        raise WindowPlanError(
            "window_plan_identity",
            f"{location} is missing the engine/platform identity",
        )
    for name, value in expected.items():
        actual = observed.get(name)
        if type(actual) is not type(value) or actual != value:
            raise WindowPlanError(
                "window_plan_identity",
                f"window_plan association differs at {location}.{name}",
            )


def _check_compile_identity(
    identity: WindowPlanIdentity,
    request_identity: object,
    location: str,
) -> None:
    if not isinstance(request_identity, Mapping):
        raise WindowPlanError(
            "window_plan_identity",
            f"{location}.request_identity is missing",
        )
    _check_fields(
        request_identity.get("engine"),
        {
            "version": identity.engine_version,
            "abi_version": identity.engine_abi_version,
            "headers_sha256": identity.engine_headers_sha256,
            "static_library_sha256": identity.engine_static_library_sha256,
        },
        f"{location}.request_identity.engine",
    )
    _check_fields(
        request_identity.get("platform"),
        {"system": identity.platform_system, "machine": identity.platform_machine},
        f"{location}.request_identity.platform",
    )


def require_window_plan_block(
    manifest: Mapping[str, Any],
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Admit selected metadata, including optional supplied provenance.

    An explicit helper path cannot replace a missing expected identity. Ordinary
    precompiled loading does not call this function; selected callers must do so.
    This checks association metadata only, not plugin bytes or helper exports.
    """

    if not isinstance(manifest, Mapping) or (
        provenance is not None and not isinstance(provenance, Mapping)
    ):
        raise WindowPlanError(
            "window_plan_manifest_invalid",
            "manifest and supplied provenance must be JSON objects",
        )
    if "window_plan" not in manifest:
        if provenance is not None and "window_plan" in provenance:
            WindowPlanIdentity.from_dict(provenance["window_plan"])
            raise WindowPlanError(
                "window_plan_identity",
                "manifest.window_plan is missing but provenance.window_plan is present",
            )
        raise WindowPlanError(
            "window_plan_manifest_missing",
            "selected execution requires manifest.window_plan with the expected helper "
            "identity; an explicit path alone cannot supply it",
        )
    identity = WindowPlanIdentity.from_dict(manifest["window_plan"])
    if provenance is not None:
        if "window_plan" not in provenance:
            raise WindowPlanError(
                "window_plan_identity",
                "provenance.window_plan is missing; the metadata pair is incomplete",
            )
        companion = WindowPlanIdentity.from_dict(provenance["window_plan"])
        if companion != identity:
            raise WindowPlanError(
                "window_plan_identity",
                "manifest.window_plan differs from provenance.window_plan",
            )
        _check_compile_identity(
            companion,
            provenance.get("request_identity"),
            "provenance",
        )
        if manifest.get("request_identity") != provenance.get("request_identity"):
            raise WindowPlanError(
                "window_plan_identity",
                "manifest and provenance request_identity differ",
            )
    _check_compile_identity(identity, manifest.get("request_identity"), "manifest")
    return identity.to_dict()


def derive_window_plan_path(engine_layout: _EngineLayout) -> Path | None:
    """Derive only the sibling of archive candidate 1 or 2; never search."""

    from .artifact import ArtifactBuildError, _platform_link_mode

    candidates = (
        engine_layout.root / "build" / "lib" / "libpineforge.a",
        engine_layout.root / "lib" / "libpineforge.a",
    )
    archive = engine_layout.selected_archive_path or engine_layout.static_library
    if archive not in candidates:
        return None
    try:
        suffix, _ = _platform_link_mode()
    except ArtifactBuildError as error:
        raise WindowPlanError(
            "window_plan_path",
            "window_plan derivation requires a supported platform",
        ) from error
    return archive.with_name(f"libpineforge_window_plan{suffix}")


def _absolute_helper_path(path: str | os.PathLike[str]) -> Path:
    try:
        candidate = Path(path)
    except (TypeError, ValueError) as error:
        raise WindowPlanError(
            "window_plan_path",
            "window_plan requires an explicit absolute file path",
        ) from error
    if not candidate.is_absolute() or "\0" in os.fspath(candidate):
        raise WindowPlanError(
            "window_plan_path",
            f"window_plan path must be absolute before normalization: {candidate}",
        )
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            raise OSError("not a regular helper file")
    except (OSError, RuntimeError, ValueError) as error:
        raise WindowPlanError(
            "window_plan_missing",
            f"cannot read window_plan helper: {candidate}: {error}",
        ) from error
    return resolved


def _associate_window_plan_path(
    engine_layout: _EngineLayout,
    path: str | os.PathLike[str],
) -> dict[str, Any]:
    from .artifact import _file_sha256

    resolved = _absolute_helper_path(path)
    try:
        digest = _file_sha256(resolved)
    except OSError as error:
        raise WindowPlanError(
            "window_plan_missing",
            f"cannot hash window_plan helper: {resolved}: {error}",
        ) from error
    return WindowPlanIdentity(
        schema="pineforge-hpo-window-plan/v1",
        build_path=str(resolved),
        sha256=digest,
        planner_abi_version=1,
        engine_version=engine_layout.version,
        engine_abi_version=engine_layout.abi_version,
        engine_headers_sha256=engine_layout.headers_sha256,
        engine_static_library_sha256=engine_layout.library_sha256,
        platform_system=platform.system(),
        platform_machine=platform.machine(),
        self_test_id="selected-plan-utc-six-hours/v1",
    ).to_dict()


def associate_window_plan(engine_layout: _EngineLayout) -> dict[str, Any] | None:
    """Record a controlled build's optional sibling, without changing compile keys."""

    path = derive_window_plan_path(engine_layout)
    if path is None or not path.is_file():
        return None
    return _associate_window_plan_path(engine_layout, path)


def _require_engine_layout_identity(
    engine_layout: _EngineLayout,
    request_identity: object,
) -> None:
    if not isinstance(request_identity, Mapping):
        raise WindowPlanError(
            "window_plan_identity",
            "manifest.request_identity is missing",
        )
    _check_fields(
        request_identity.get("engine"),
        {
            "version": engine_layout.version,
            "abi_version": engine_layout.abi_version,
            "headers_sha256": engine_layout.headers_sha256,
            "static_library_sha256": engine_layout.library_sha256,
        },
        "manifest.request_identity.engine",
    )
    _check_fields(
        request_identity.get("platform"),
        {"system": platform.system(), "machine": platform.machine()},
        "manifest.request_identity.platform",
    )


def resolve_window_plan_path(
    explicit_path: str | os.PathLike[str] | None,
    engine_root: str | os.PathLike[str] | None,
    recorded_block: WindowPlanIdentity | Mapping[str, Any],
) -> Path:
    """Resolve a run-machine path; call :func:`verify_window_plan` before handoff.

    Explicit paths take precedence with no fallback. Derivation uses only a
    caller-supplied local engine root with matching compile content. The recorded
    build_path is audit data and is never used for lookup.
    """

    from .artifact import ArtifactBuildError, ArtifactBuilder

    identity = _identity(recorded_block)
    explicit = (
        _absolute_helper_path(explicit_path) if explicit_path is not None else None
    )
    _check_fields(
        {"system": platform.system(), "machine": platform.machine()},
        {"system": identity.platform_system, "machine": identity.platform_machine},
        "runtime.platform",
    )
    if explicit is not None:
        return explicit
    if engine_root is None:
        raise WindowPlanError(
            "window_plan_path",
            "an absolute window_plan path is required without a local engine_root",
        )
    try:
        engine = ArtifactBuilder(
            engine_root=engine_root,
            cache_dir=engine_root,
        )._resolve_engine_layout()
    except (ArtifactBuildError, OSError, RuntimeError, ValueError) as error:
        raise WindowPlanError(
            "window_plan_identity",
            f"cannot establish local engine association: {engine_root}: {error}",
        ) from error
    _check_compile_identity(
        identity,
        {
            "engine": engine.identity_dict(),
            "platform": {"system": platform.system(), "machine": platform.machine()},
        },
        "local engine",
    )
    derived = derive_window_plan_path(engine)
    if derived is None:
        raise WindowPlanError(
            "window_plan_path",
            "the selected engine archive has no derivable window_plan sibling; "
            "supply an explicit absolute path",
        )
    return _absolute_helper_path(derived)


def verify_window_plan(
    block: WindowPlanIdentity | Mapping[str, Any],
    path: str | os.PathLike[str],
) -> None:
    """Check only the helper file's SHA-256 against the recorded expected bytes.

    This pathname-based hash check is not race-free loading. Lane 2's native
    consumer must copy/hash an opened file into a private snapshot and load that
    verified snapshot, then validate exports, ABI and the frozen self-test.
    """

    from .artifact import _file_sha256

    identity = _identity(block)
    resolved = _absolute_helper_path(path)
    try:
        actual = _file_sha256(resolved)
    except OSError as error:
        raise WindowPlanError(
            "window_plan_missing",
            f"cannot hash window_plan helper: {resolved}: {error}",
        ) from error
    if actual != identity.sha256:
        raise WindowPlanError(
            "window_plan_hash_mismatch",
            f"window_plan helper SHA-256 differs from manifest.window_plan.sha256: "
            f"{resolved}",
        )


__all__ = [
    "WindowPlanError",
    "WindowPlanIdentity",
    "associate_window_plan",
    "derive_window_plan_path",
    "require_window_plan_block",
    "resolve_window_plan_path",
    "verify_window_plan",
]
