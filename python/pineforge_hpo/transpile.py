"""One-pass PineScript transpiler bridge with structured diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import importlib
from importlib import metadata
from pathlib import Path
import re
from typing import Any, Callable, Mapping

try:
    import pineforge_codegen
    from pineforge_codegen import transpile_full
    from pineforge_codegen.errors import CompileError
except ModuleNotFoundError:
    pineforge_codegen = None
    transpile_full = None
    CompileError = None


@dataclass(frozen=True)
class TranspileDiagnostic:
    """A stable diagnostic envelope independent of codegen's Python classes."""

    severity: str
    phase: str
    message: str
    filename: str | None = None
    line: int | None = None
    column: int | None = None
    end_column: int | None = None
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "phase": self.phase,
            "message": self.message,
            "filename": self.filename,
            "line": self.line,
            "column": self.column,
            "end_column": self.end_column,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class TranspileResult:
    """Result of the strategy pipeline, with a separately cached capability canary."""

    generated_cpp: str | None
    inputs: tuple[dict[str, Any], ...] = ()
    strategy_params: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: tuple[TranspileDiagnostic, ...] = ()
    input_kind_schema: int | None = None

    @property
    def ok(self) -> bool:
        return self.generated_cpp is not None and not any(
            diagnostic.severity == "error" for diagnostic in self.diagnostics
        )

    def require_success(self) -> str:
        if not self.ok or self.generated_cpp is None:
            raise TranspileFailure(self)
        return self.generated_cpp


class TranspileFailure(RuntimeError):
    """Raised when an artifact build cannot proceed past transpilation."""

    def __init__(self, result: TranspileResult):
        self.result = result
        summary = "; ".join(d.message for d in result.diagnostics) or "transpile failed"
        super().__init__(summary)


@dataclass(frozen=True)
class CodegenIdentity:
    """Installed codegen version, source hash, and resolved module location."""

    version: str
    implementation_sha256: str
    module_path: str

    def to_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "implementation_sha256": self.implementation_sha256,
            "module_path": self.module_path,
        }


def _diagnostic_from_codegen(diagnostic: Any) -> TranspileDiagnostic:
    location = getattr(diagnostic, "location", None)
    level = getattr(diagnostic, "level", "error")
    phase = getattr(diagnostic, "phase", "TRANSPILE")
    return TranspileDiagnostic(
        severity=str(getattr(level, "value", level)).lower(),
        phase=str(getattr(phase, "value", phase)),
        message=str(getattr(diagnostic, "message", diagnostic)),
        filename=getattr(location, "file", None),
        line=getattr(location, "line", None),
        column=getattr(location, "col", None),
        end_column=getattr(location, "end_col", None),
        hint=getattr(diagnostic, "hint", None),
    )


def _diagnostics_from_payload(
    payload: Mapping[str, Any],
) -> tuple[TranspileDiagnostic, ...]:
    output: list[TranspileDiagnostic] = []
    raw_diagnostics = payload.get("diagnostics", ())
    if not isinstance(raw_diagnostics, (list, tuple)):
        return ()
    for raw in raw_diagnostics:
        if isinstance(raw, Mapping):
            output.append(
                TranspileDiagnostic(
                    severity=str(
                        raw.get("severity", raw.get("level", "warning"))
                    ).lower(),
                    phase=str(raw.get("phase", "TRANSPILE")),
                    message=str(raw.get("message", "")),
                    filename=raw.get("filename", raw.get("file")),
                    line=raw.get("line"),
                    column=raw.get("column", raw.get("col")),
                    end_column=raw.get("end_column", raw.get("end_col")),
                    hint=raw.get("hint"),
                )
            )
        else:
            output.append(_diagnostic_from_codegen(raw))
    return tuple(output)


def legacy_input_kind_version(version: str | None) -> bool:
    """Whether a recorded, parseable codegen version predates input-kind metadata."""
    match = re.fullmatch(
        r"[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?([-+a-zA-Z.].*)?",
        (version or "").strip(),
    )
    if not match:
        return False
    core = tuple(int(component or "0") for component in match.groups()[:3])
    suffix = match.group(4) or ""
    return core < (1, 1, 0) or (
        core == (1, 1, 0) and suffix.startswith(("-", "a", "b", "rc", "dev", ".dev"))
    )


@lru_cache(maxsize=8)
def _input_kind_canary(transpiler: Callable[..., Any]) -> bool:
    source = (
        '//@version=6\nstrategy("kind canary")\nprobe = input.symbol("NASDAQ:AAPL")'
    )
    try:
        payload = transpiler(source, filename="<input-kind-canary>")
        return isinstance(payload, Mapping) and any(
            isinstance(item, Mapping) and item.get("kind") == "symbol"
            for item in payload.get("inputs", [])
        )
    except Exception:
        return False


def transpile_source(pine_source: str, *, filename: str = "<input>") -> TranspileResult:
    """Transpile source once and normalize all expected diagnostics.

    Pine language errors are returned in ``TranspileResult`` rather than being
    conflated with native compiler failures. Unexpected bridge failures are also
    represented as an ``INTERNAL`` diagnostic so a CLI can emit one stable JSON
    envelope.
    """

    try:
        transpiler, compile_error = _load_codegen()
    except ModuleNotFoundError:
        return TranspileResult(
            generated_cpp=None,
            diagnostics=(
                TranspileDiagnostic(
                    severity="error",
                    phase="CONFIGURATION",
                    message=(
                        "pineforge-codegen is required to compile PineScript; "
                        "install pineforge-hpo[transpile] or provide a precompiled artifact"
                    ),
                    filename=filename,
                ),
            ),
        )

    try:
        payload = transpiler(pine_source, filename=filename)
    except compile_error as error:
        return TranspileResult(
            generated_cpp=None,
            diagnostics=tuple(_diagnostic_from_codegen(d) for d in error.diagnostics),
        )
    except Exception as error:  # keep the initialization error JSON-serializable
        return TranspileResult(
            generated_cpp=None,
            diagnostics=(
                TranspileDiagnostic(
                    severity="error",
                    phase="INTERNAL",
                    message=f"{type(error).__name__}: {error}",
                    filename=filename,
                ),
            ),
        )

    if not isinstance(payload, Mapping):
        return TranspileResult(
            generated_cpp=None,
            diagnostics=(
                TranspileDiagnostic(
                    severity="error",
                    phase="INTERNAL",
                    message="transpile_full returned a non-object result",
                    filename=filename,
                ),
            ),
        )

    cpp = payload.get("cpp")
    inputs = payload.get("inputs", [])
    strategy_params = payload.get("strategyParams", {})
    if (
        not isinstance(cpp, str)
        or not isinstance(inputs, list)
        or not isinstance(strategy_params, Mapping)
    ):
        return TranspileResult(
            generated_cpp=None,
            diagnostics=(
                TranspileDiagnostic(
                    severity="error",
                    phase="INTERNAL",
                    message="transpile_full returned an invalid result shape",
                    filename=filename,
                ),
            ),
        )

    normalized_inputs: list[dict[str, Any]] = []
    for item in inputs:
        if not isinstance(item, Mapping):
            return TranspileResult(
                generated_cpp=None,
                diagnostics=(
                    TranspileDiagnostic(
                        severity="error",
                        phase="INTERNAL",
                        message="transpile_full input manifest contains a non-object entry",
                        filename=filename,
                    ),
                ),
            )
        normalized_inputs.append(dict(item))

    return TranspileResult(
        generated_cpp=cpp,
        inputs=tuple(normalized_inputs),
        strategy_params=dict(strategy_params),
        diagnostics=_diagnostics_from_payload(payload),
        input_kind_schema=(
            1
            if isinstance(payload.get("requests"), list)
            and all(isinstance(item, Mapping) for item in payload["requests"])
            and _input_kind_canary(transpiler)
            else None
        ),
    )


def codegen_identity() -> CodegenIdentity:
    """Return version plus a hash that detects dirty/editable codegen installs."""

    _transpiler, _compile_error = _load_codegen()
    assert pineforge_codegen is not None

    try:
        version = metadata.version("pineforge-codegen")
    except metadata.PackageNotFoundError:
        version = str(getattr(pineforge_codegen, "__version__", "unknown"))

    module_file = Path(pineforge_codegen.__file__ or "").resolve()
    package_root = module_file.parent
    digest = hashlib.sha256()
    files = (
        sorted(package_root.rglob("*.py")) if package_root.is_dir() else [module_file]
    )
    for path in files:
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(package_root).as_posix()
        except ValueError:
            relative = path.name
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return CodegenIdentity(
        version=version,
        implementation_sha256=digest.hexdigest(),
        module_path=str(module_file),
    )


def _load_codegen() -> tuple[Any, type[Exception]]:
    """Load the optional transpiler only when a Pine source must be compiled."""

    global pineforge_codegen, transpile_full, CompileError
    if transpile_full is not None and CompileError is not None:
        return transpile_full, CompileError
    try:
        pineforge_codegen = importlib.import_module("pineforge_codegen")
        transpile_full = getattr(pineforge_codegen, "transpile_full")
        errors = importlib.import_module("pineforge_codegen.errors")
        CompileError = getattr(errors, "CompileError")
    except (ModuleNotFoundError, AttributeError) as error:
        raise ModuleNotFoundError(
            "pineforge_codegen is unavailable or incomplete"
        ) from error
    return transpile_full, CompileError


__all__ = [
    "CodegenIdentity",
    "TranspileDiagnostic",
    "TranspileFailure",
    "TranspileResult",
    "codegen_identity",
    "transpile_source",
]
