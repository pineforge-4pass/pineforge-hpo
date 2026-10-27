"""Pure selected-study admission of producer-bound request-feed dependencies.

Only ``read_request_feed_inventory`` reads artifact files. Neither entry point
loads a plugin, reads feeds, creates a strategy, nor enumerates trial products.
The optional codegen inventory module supplies the wire-token canonicalizer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Literal, Mapping

from .error import HpoError
from .study_spec import ParameterSpec
from .transpile import REQUEST_FEED_INVENTORY_SCHEMA

# report-v2.md section 2 [SOURCE]: E/docker/selected_window_report.py:78-90.
# These candidate implementation constants are checked again by the engine,
# which remains authoritative. Lane 5 re-proves them against its support_policy().
SUPPORT_POLICY = {
    "primary_chart_timeframes": ("1", "3", "5", "15", "30", "60", "120", "240"),
    "preroll": "supported",
    "request_feed_timeframes": ("1", "D"),
}

RequestInventoryReason = Literal[
    "missing",
    "invalid",
    "unbound",
    "artifact_mismatch",
    "chart_mismatch",
    "unknown",
    "input_unresolved",
    "producer_unavailable",
]
RequestTimeframeCapability = Literal[
    "selected_window_chart_timeframe",
    "selected_window_request_feed_timeframe",
]

_INVENTORY_KEYS = {
    "schema",
    "source_sha256",
    "artifact_sha256",
    "primary_chart_timeframe",
    "entries",
}
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _diagnostic(message: str) -> str:
    return message.encode("utf-8", errors="replace")[:4096].decode(
        "utf-8",
        errors="ignore",
    )


class RequestInventoryError(HpoError, RuntimeError):
    """An inventory refusal with closed arguments and a bounded diagnostic."""

    def __init__(self, reason: RequestInventoryReason, message: str) -> None:
        super().__init__(_diagnostic(message))
        self.with_failure(
            "hpo_walkforward_inventory_invalid",
            {
                "capability": "selected_window_request_feed_inventory",
                "reason": reason,
            },
        )


class RequestTimeframeError(HpoError, RuntimeError):
    """A policy refusal; offending tokens belong in text, not catalog args."""

    def __init__(self, capability: RequestTimeframeCapability, message: str) -> None:
        super().__init__(_diagnostic(message))
        self.with_failure(
            "hpo_walkforward_timeframe_unsupported", {"capability": capability}
        )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except FileNotFoundError as error:
        raise RequestInventoryError(
            "missing", f"inventory metadata missing: {path}"
        ) from error
    except (OSError, UnicodeError, ValueError) as error:
        raise RequestInventoryError(
            "invalid", f"cannot read inventory metadata {path}: {error}"
        ) from error


def _json_bytes(value: Any) -> bytes:
    # Compare parsed JSON without Python's True == 1 or 1 == 1.0 equivalence.
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _manifest_inventory(manifest: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(manifest, Mapping):
        raise RequestInventoryError("invalid", f"{location} must be a JSON object")
    if "request_feed_inventory" not in manifest:
        identity = manifest.get("request_identity")
        unavailable = (
            isinstance(identity, Mapping)
            and "request_feed_inventory" in identity
            and identity["request_feed_inventory"] is None
        )
        reason = "producer_unavailable" if unavailable else "missing"
        raise RequestInventoryError(
            reason,
            f"{location}.request_feed_inventory is missing"
            + (
                "; request_identity.request_feed_inventory is null: "
                "the build had no inventory producer"
                if unavailable
                else "; selected execution requires a producer-bound inventory"
            ),
        )
    inventory = manifest["request_feed_inventory"]
    if not isinstance(inventory, Mapping):
        raise RequestInventoryError(
            "invalid", f"{location}.request_feed_inventory must be a JSON object"
        )
    return inventory


def read_request_feed_inventory(
    artifact_dir_or_manifest: str | os.PathLike[str] | Mapping[str, Any],
) -> Mapping[str, Any]:
    """Read the manifest block and compare an optional adjacent JSON sidecar.

    Pass an artifact directory, a manifest path, or an already parsed manifest
    (the last form performs no I/O). The block is required; a sidecar alone never
    supplies it. When present, the sidecar must have identical parsed JSON bytes.
    Duplicate keys at every JSON depth are invalid, as required by
    E/docker/request_feed_inventory.py:160-221 (report-v2 section 2).

    A missing block with ``request_identity.request_feed_inventory: null`` is
    ``producer_unavailable``: the build explicitly lacked the producer. An older
    identity without the key is ``missing``. A present null block is ``invalid``.
    Ordinary artifact loading never calls this selected-only helper.
    """

    if isinstance(artifact_dir_or_manifest, Mapping):
        return _manifest_inventory(artifact_dir_or_manifest, "manifest")
    path = Path(artifact_dir_or_manifest)
    manifest_path = path / "manifest.json" if path.is_dir() else path
    inventory = _manifest_inventory(_read_json(manifest_path), str(manifest_path))
    sidecar = manifest_path.parent / "request_feed_inventory.json"
    if sidecar.exists():
        companion = _read_json(sidecar)
        if _json_bytes(companion) != _json_bytes(inventory):
            raise RequestInventoryError(
                "invalid",
                f"{sidecar} disagrees with {manifest_path}.request_feed_inventory",
            )
    return inventory


def _validate_shape(
    inventory: Any,
    canonical: Callable[[Any], str | None],
) -> Mapping[str, Any]:
    """Exact five keys/shapes: E/docker/request_feed_inventory.py:190-221.

    Token entries and non-null ``primary_chart_timeframe`` must already be
    canonical. This is stricter than the engine resolver, which accepts any
    spelling that canonicalizes, and matches codegen's binder ``_is_wire_token``
    (canonical == value). Codegen never emits aliases here, so this rule only
    refuses hand-edited inventories.
    """

    def invalid(message: str) -> None:
        raise RequestInventoryError("invalid", f"request_feed_inventory.{message}")

    if not isinstance(inventory, Mapping) or set(inventory) != _INVENTORY_KEYS:
        invalid(
            "keys must be exactly schema, source_sha256, artifact_sha256, "
            "primary_chart_timeframe, entries"
        )
    if (
        type(inventory["schema"]) is not str
        or inventory["schema"] != REQUEST_FEED_INVENTORY_SCHEMA
    ):
        invalid(f"schema must equal {REQUEST_FEED_INVENTORY_SCHEMA!r}")
    for name in ("source_sha256", "artifact_sha256"):
        value = inventory[name]
        if name == "artifact_sha256" and value is None:
            continue
        if type(value) is not str or _HEX64.fullmatch(value) is None:
            invalid(f"{name} must be 64 lowercase hex digits")
    primary = inventory["primary_chart_timeframe"]
    if primary is not None and (
        type(primary) is not str or canonical(primary) != primary
    ):
        invalid(
            f"primary_chart_timeframe {primary!r} must be null or a canonical token"
        )
    entries = inventory["entries"]
    if not isinstance(entries, list):
        invalid("entries must be a list")
    for position, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            invalid(f"entries[{position}] must be an object")
        kind = entry.get("kind")
        if kind == "token":
            token = entry.get("timeframe")
            if (
                set(entry) != {"kind", "timeframe"}
                or type(token) is not str
                or canonical(token) != token
            ):
                invalid(f"entries[{position}] has an invalid token {token!r}")
        elif kind == "input":
            if (
                set(entry) != {"kind", "key", "default"}
                or type(entry["key"]) is not str
                or type(entry["default"]) is not str
            ):
                invalid(f"entries[{position}] must contain string key and default")
        elif kind != "unknown" or set(entry) != {"kind"}:
            invalid(f"entries[{position}] must be exactly token, input or unknown")
    return inventory


def _input_values(
    entry: Mapping[str, Any],
    fixed_inputs: Mapping[str, Any],
    input_domains: Mapping[str, ParameterSpec],
) -> tuple[Any, ...]:
    key = entry["key"]
    if key in input_domains:
        domain = input_domains[key]
        # StudySpec ParameterSpec (study_spec.py:38-46, 348-382): categorical
        # choices are the only domain that can contain string tokens. Integer,
        # real and boolean dimensions contain non-strings even when finite;
        # rejecting their type needs neither a lattice nor a Cartesian product.
        if (
            not isinstance(domain, ParameterSpec)
            or domain.kind != "categorical"
            or not isinstance(domain.choices, tuple)
            or not domain.choices
        ):
            raise RequestInventoryError(
                "input_unresolved",
                f"input_domains[{key!r}] must enumerate a nonempty finite "
                "categorical domain of string timeframe values; numeric, boolean "
                "and continuous domains cannot resolve request tokens",
            )
        return domain.choices
    if key in fixed_inputs:
        return (fixed_inputs[key],)
    return (entry["default"],)


def admit_request_feed_inventory(
    inventory: Mapping[str, Any] | None,
    *,
    plugin_sha256: str,
    generated_cpp_sha256: str,
    chart_timeframe: str,
    fixed_inputs: Mapping[str, Any],
    input_domains: Mapping[str, ParameterSpec],
) -> tuple[str, ...]:
    """Return every reachable request token, sorted, unique and canonical.

    Rules from report-v2 section 2 [SOURCE], E/docker/request_feed_inventory.py:
    :190-221 requires exactly five schema keys and exact token/input/unknown
    entry shapes, with no extras, missing or duplicate JSON keys (duplicates
    must be rejected while parsing; ``read_request_feed_inventory`` does so).
    :258-264 requires lowercase 64-hex digests and an artifact binding. HPO also
    compares the recorded source digest against supplied generated C++ identity.
    Null artifact binding is ``unbound``; either differing digest is
    ``artifact_mismatch``. :239-280 accepts a null primary (chart-unbound),
    otherwise requires its canonical token to match the supplied canonical chart.
    An ``unknown`` entry always refuses; empty entries is complete and admitted.
    Token entries and non-null ``primary_chart_timeframe`` must already equal
    codegen's ``canonical_wire_timeframe`` result (:99-157). This is stricter than
    the engine resolver, which accepts any spelling that canonicalizes, and
    matches codegen's binder ``_is_wire_token`` (canonical == value). Codegen
    never emits aliases here, so this rule only refuses hand-edited inventories.
    Input values may use aliases, resolve from effective fixed values or declared
    defaults, and invalid present values never fall back (:239-280).

    Study admission additionally checks every choice of each referenced finite
    ParameterSpec domain, independently, before trials. A swept input takes
    precedence over any fixed value; no non-enumerable or non-string domain can
    prove admission. No Cartesian products are constructed. :283-321 checks the
    primary chart policy first, then resolved feed policy; the engine's own
    admission remains authoritative. No artifact/feed I/O or library load occurs.

    Callers must pass the verified SHA-256 of the plugin file that will be loaded
    as ``plugin_sha256`` and of its generated C++ as ``generated_cpp_sha256``
    (manifest values verified against the files by the artifact loader).
    Admission performs no I/O and compares inventory hashes only with these
    supplied values, so its identity check is only as good as those arguments.
    """

    try:
        from pineforge_codegen.request_feed_inventory import canonical_wire_timeframe
    except ImportError as error:
        raise RequestInventoryError(
            "producer_unavailable",
            "cannot import pineforge_codegen.request_feed_inventory; "
            "admission needs pineforge-codegen with request_feed_inventory",
        ) from error

    chart = canonical_wire_timeframe(chart_timeframe)
    if chart not in SUPPORT_POLICY["primary_chart_timeframes"]:
        raise RequestTimeframeError(
            "selected_window_chart_timeframe",
            f"selected-window chart timeframe {chart_timeframe!r} "
            f"(canonical {chart!r}) is unsupported",
        )
    if inventory is None:
        raise RequestInventoryError(
            "missing", "selected execution requires request_feed_inventory"
        )
    checked = _validate_shape(inventory, canonical_wire_timeframe)
    if checked["artifact_sha256"] is None:
        raise RequestInventoryError(
            "unbound", "request_feed_inventory.artifact_sha256 is null"
        )
    for name, expected in (
        ("source_sha256", generated_cpp_sha256),
        ("artifact_sha256", plugin_sha256),
    ):
        if checked[name] != expected:
            raise RequestInventoryError(
                "artifact_mismatch",
                f"request_feed_inventory.{name} {checked[name]!r} differs "
                f"from the supplied artifact identity {expected!r}",
            )
    primary = checked["primary_chart_timeframe"]
    if primary is not None and primary != chart:
        raise RequestInventoryError(
            "chart_mismatch",
            f"request_feed_inventory.primary_chart_timeframe {primary!r} "
            f"differs from chart timeframe {chart!r}",
        )
    tokens: set[str] = set()
    for position, entry in enumerate(checked["entries"]):
        if entry["kind"] == "unknown":
            raise RequestInventoryError(
                "unknown", f"request_feed_inventory.entries[{position}] is unknown"
            )
        if entry["kind"] == "token":
            tokens.add(entry["timeframe"])
            continue
        for value in _input_values(entry, fixed_inputs, input_domains):
            token = canonical_wire_timeframe(value)
            if token is None:
                raise RequestInventoryError(
                    "input_unresolved",
                    f"request_feed_inventory.entries[{position}] input "
                    f"{entry['key']!r} cannot resolve timeframe value {value!r}",
                )
            tokens.add(token)
    unsupported = sorted(tokens - set(SUPPORT_POLICY["request_feed_timeframes"]))
    if unsupported:
        raise RequestTimeframeError(
            "selected_window_request_feed_timeframe",
            f"selected-window request-feed timeframes {unsupported!r} are unsupported",
        )
    return tuple(sorted(tokens))


__all__ = [
    "RequestInventoryError",
    "RequestInventoryReason",
    "RequestTimeframeCapability",
    "RequestTimeframeError",
    "SUPPORT_POLICY",
    "admit_request_feed_inventory",
    "read_request_feed_inventory",
]
