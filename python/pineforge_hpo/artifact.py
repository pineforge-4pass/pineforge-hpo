"""Content-addressed PineScript strategy artifact builder."""

from __future__ import annotations

from contextlib import AbstractContextManager
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

from .transpile import (
    TranspileDiagnostic,
    TranspileFailure,
    codegen_identity,
    legacy_input_kind_version,
    transpile_source,
)

CACHE_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
INPUT_METADATA_REVISION = 2
CANONICAL_COMPILE_FLAGS = (
    "-std=c++17",
    "-O2",
    "-ffp-contract=off",
    "-fPIC",
    "-shared",
)
# Engine 1.0 requires Clang builds outside CMake to lift Clang's 256-level bracket
# limit: generated C++ for long Pine chains nests deeper. GCC has no such flag.
_CLANG_COMPILE_FLAGS = ("-fbracket-depth=1024",)
_COMPILER_ENV_KEYS = (
    "CPATH",
    "CPLUS_INCLUDE_PATH",
    "LIBRARY_PATH",
    "SDKROOT",
    "MACOSX_DEPLOYMENT_TARGET",
)
_REQUIRED_PLUGIN_SYMBOLS = (
    "pf_abi_version",
    "strategy_create",
    "strategy_free",
    "strategy_set_input",
    "strategy_set_override",
    "strategy_get_last_error",
    "run_backtest_full",
    "report_free",
)


class ArtifactBuildError(RuntimeError):
    """A configuration, transpile, compile, or cache publication failure."""

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        command: Sequence[str] = (),
        stdout: str = "",
        stderr: str = "",
        diagnostics: Sequence[TranspileDiagnostic] = (),
    ) -> None:
        self.stage = stage
        self.command = tuple(command)
        self.stdout = stdout
        self.stderr = stderr
        self.diagnostics = tuple(diagnostics)
        super().__init__(message)


@dataclass(frozen=True)
class StrategyArtifact:
    """Paths and metadata consumed by the native strategy-plugin loader."""

    artifact_key: str
    request_key: str
    plugin_path: Path
    generated_cpp_path: Path
    manifest_path: Path
    provenance_path: Path
    cache_hit: bool
    inputs: tuple[dict[str, Any], ...]
    strategy_params: Mapping[str, Any]

    @property
    def library_path(self) -> Path:
        """Compatibility alias for callers that call plugins libraries."""

        return self.plugin_path


@dataclass(frozen=True)
class _EngineLayout:
    root: Path
    include_dirs: tuple[Path, ...]
    static_library: Path
    version: str
    abi_version: int
    headers_sha256: str
    library_sha256: str

    def identity_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "version": self.version,
            "abi_version": self.abi_version,
            "include_dirs": [str(path) for path in self.include_dirs],
            "headers_sha256": self.headers_sha256,
            "static_library": str(self.static_library),
            "static_library_sha256": self.library_sha256,
        }


@dataclass(frozen=True)
class _CompilerIdentity:
    path: Path
    version_output: str
    target: str

    @property
    def compile_flags(self) -> tuple[str, ...]:
        if "clang" in self.version_output.lower():
            return (*CANONICAL_COMPILE_FLAGS, *_CLANG_COMPILE_FLAGS)
        return CANONICAL_COMPILE_FLAGS

    def to_dict(self) -> dict[str, str]:
        return {
            "path": str(self.path),
            "version": self.version_output,
            "target": self.target,
        }


@dataclass(frozen=True)
class _EigenIdentity:
    include_dir: Path
    headers_sha256: str
    version: str

    def to_dict(self) -> dict[str, str]:
        return {
            "include_dir": str(self.include_dir),
            "headers_sha256": self.headers_sha256,
            "version": self.version,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(roots: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    seen: set[Path] = set()
    for root_index, root in enumerate(roots):
        resolved = root.resolve()
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        for path in sorted(item for item in resolved.rglob("*") if item.is_file()):
            relative = path.relative_to(resolved).as_posix()
            digest.update(str(root_index).encode("ascii"))
            digest.update(b":")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _read_text(path: Path, description: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise ArtifactBuildError(
            "configuration", f"cannot read {description}: {path}: {error}"
        ) from error


def _read_define(text: str, name: str) -> str | None:
    prefix = f"#define {name} "
    for line in text.splitlines():
        normalized = " ".join(line.strip().split())
        if normalized.startswith(prefix):
            return normalized[len(prefix) :].strip().strip('"')
    return None


def _default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base).expanduser() / "pineforge" / "hpo"
    return Path.home() / ".cache" / "pineforge" / "hpo"


class _KeyLock(AbstractContextManager["_KeyLock"]):
    """Small cross-process advisory lock used only around one request key."""

    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._handle: Any = None

    def __enter__(self) -> "_KeyLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise ArtifactBuildError(
                        "cache", f"timed out waiting for artifact lock: {self.path}"
                    )
                time.sleep(0.05)

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class ArtifactBuilder:
    """Transpile and compile a Pine strategy once per compatible request identity."""

    def __init__(
        self,
        *,
        engine_root: str | os.PathLike[str],
        cache_dir: str | os.PathLike[str] | None = None,
        compiler: str | os.PathLike[str] | None = None,
        eigen_include: str | os.PathLike[str] | None = None,
        plugin_validator: Callable[[Path, int], Mapping[str, Any]] | None = None,
        lock_timeout_seconds: float = 120.0,
        compile_timeout_seconds: float = 300.0,
    ) -> None:
        self.engine_root = Path(engine_root).expanduser().resolve()
        self.cache_dir = (
            Path(cache_dir).expanduser().resolve()
            if cache_dir
            else _default_cache_dir()
        )
        self.compiler = os.fspath(compiler) if compiler is not None else None
        self.eigen_include = (
            Path(eigen_include).expanduser().resolve()
            if eigen_include is not None
            else None
        )
        self.plugin_validator = plugin_validator or self._validate_plugin
        self.lock_timeout_seconds = lock_timeout_seconds
        self.compile_timeout_seconds = compile_timeout_seconds

    def build(self, pine_source: str, *, filename: str = "<input>") -> StrategyArtifact:
        if not isinstance(pine_source, str):
            raise TypeError("pine_source must be a string")
        if self.lock_timeout_seconds <= 0 or self.compile_timeout_seconds <= 0:
            raise ArtifactBuildError("configuration", "build timeouts must be positive")

        engine = self._resolve_engine_layout()
        compiler = self._resolve_compiler_identity()
        eigen = self._resolve_eigen_identity(engine)
        try:
            codegen = codegen_identity()
        except ModuleNotFoundError as error:
            raise ArtifactBuildError(
                "configuration",
                "pineforge-codegen is required to compile PineScript; install "
                "pineforge-hpo[transpile] or provide a precompiled artifact",
            ) from error
        output_extension, link_mode = self._platform_link_mode()
        compile_spec = {
            "flags": list(compiler.compile_flags),
            "include_dirs": [
                str(path) for path in (*engine.include_dirs, eigen.include_dir)
            ],
            "link_mode": link_mode,
            "output_extension": output_extension,
        }
        request_identity = {
            "input_metadata_revision": INPUT_METADATA_REVISION,
            "source_sha256": hashlib.sha256(pine_source.encode("utf-8")).hexdigest(),
            "codegen": codegen.to_dict(),
            "engine": engine.identity_dict(),
            "compiler": compiler.to_dict(),
            "eigen": eigen.to_dict(),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "sys_platform": sys.platform,
                "python_implementation": platform.python_implementation(),
                "python_version": platform.python_version(),
            },
            "compiler_environment": {
                key: os.environ.get(key, "") for key in _COMPILER_ENV_KEYS
            },
            "compile": compile_spec,
        }
        request_key = _json_sha256(request_identity)

        hit = self._lookup_request(request_key, request_identity, output_extension)
        if hit is not None:
            return hit

        lock_path = self.cache_dir / "locks" / f"{request_key}.lock"
        with _KeyLock(lock_path, self.lock_timeout_seconds):
            hit = self._lookup_request(request_key, request_identity, output_extension)
            if hit is not None:
                return hit

            transpile_result = transpile_source(pine_source, filename=filename)
            try:
                generated_cpp = transpile_result.require_success()
            except TranspileFailure as error:
                raise ArtifactBuildError(
                    "transpile",
                    str(error),
                    diagnostics=transpile_result.diagnostics,
                ) from error

            if transpile_result.input_kind_schema == 1 and legacy_input_kind_version(
                request_identity["codegen"]["version"]
            ):
                raise ArtifactBuildError(
                    "transpile",
                    "input_kind_schema: 1 contradicts recorded codegen version; "
                    "codegen >= 1.1.0 is required",
                )

            generated_cpp_sha256 = hashlib.sha256(
                generated_cpp.encode("utf-8")
            ).hexdigest()
            artifact_identity = {
                "request_identity": request_identity,
                "generated_cpp_sha256": generated_cpp_sha256,
            }
            artifact_key = _json_sha256(artifact_identity)
            existing = self._load_artifact(
                artifact_key=artifact_key,
                request_key=request_key,
                request_identity=request_identity,
                output_extension=output_extension,
                cache_hit=True,
            )
            if existing is not None:
                self._write_request_index(request_key, artifact_key)
                return existing

            artifact = self._compile_and_publish(
                artifact_key=artifact_key,
                request_key=request_key,
                request_identity=request_identity,
                output_extension=output_extension,
                generated_cpp=generated_cpp,
                generated_cpp_sha256=generated_cpp_sha256,
                inputs=transpile_result.inputs,
                input_kind_schema=transpile_result.input_kind_schema,
                strategy_params=transpile_result.strategy_params,
                diagnostics=transpile_result.diagnostics,
                source_name=filename,
                compiler=compiler,
                engine=engine,
                eigen=eigen,
            )
            self._write_request_index(request_key, artifact_key)
            return artifact

    def _resolve_engine_layout(self) -> _EngineLayout:
        root = self.engine_root
        source_include = root / "include"
        build_include = root / "build" / "include"
        include_dirs = tuple(
            path for path in (source_include, build_include) if path.is_dir()
        )
        header = source_include / "pineforge" / "pineforge.h"
        if not include_dirs or not header.is_file():
            raise ArtifactBuildError(
                "configuration",
                f"engine root does not contain include/pineforge/pineforge.h: {root}",
            )

        library_candidates = (
            root / "build" / "lib" / "libpineforge.a",
            root / "lib" / "libpineforge.a",
            root / "build" / "libpineforge.a",
        )
        static_library = next(
            (path for path in library_candidates if path.is_file()), None
        )
        if static_library is None:
            joined = ", ".join(str(path) for path in library_candidates)
            raise ArtifactBuildError(
                "configuration", f"cannot find libpineforge.a; checked: {joined}"
            )

        version_file = root / "VERSION"
        version = (
            version_file.read_text(encoding="utf-8").strip()
            if version_file.is_file()
            else ""
        )
        if not version:
            version_headers = (
                build_include / "pineforge" / "version.h",
                source_include / "pineforge" / "version.h",
            )
            for version_header in version_headers:
                if version_header.is_file():
                    version = (
                        _read_define(
                            _read_text(version_header, "engine version header"),
                            "PINEFORGE_VERSION_STRING",
                        )
                        or ""
                    )
                    if version:
                        break
        if not version:
            raise ArtifactBuildError(
                "configuration", f"cannot determine engine version: {root}"
            )

        abi_text = _read_text(header, "engine ABI header")
        abi_raw = _read_define(abi_text, "PF_ABI_VERSION")
        try:
            abi_version = int(abi_raw or "")
        except ValueError as error:
            raise ArtifactBuildError(
                "configuration", f"cannot determine PF_ABI_VERSION from {header}"
            ) from error

        header_roots = [path / "pineforge" for path in include_dirs]
        return _EngineLayout(
            root=root,
            include_dirs=include_dirs,
            static_library=static_library.resolve(),
            version=version,
            abi_version=abi_version,
            headers_sha256=_tree_sha256(header_roots),
            library_sha256=_file_sha256(static_library),
        )

    def _resolve_compiler_identity(self) -> _CompilerIdentity:
        requested = self.compiler or os.environ.get("CXX") or "c++"
        discovered = shutil.which(requested)
        if discovered is None:
            candidate = Path(requested).expanduser()
            if candidate.is_file():
                discovered = str(candidate)
        if discovered is None:
            raise ArtifactBuildError(
                "configuration", f"C++ compiler not found: {requested}"
            )
        compiler_path = Path(discovered).resolve()

        version = self._run_probe([str(compiler_path), "--version"], "compiler version")
        target = self._run_probe(
            [str(compiler_path), "-dumpmachine"], "compiler target", allow_failure=True
        )
        if not target:
            target = f"{platform.system()}-{platform.machine()}"
        return _CompilerIdentity(
            path=compiler_path,
            version_output=version.strip(),
            target=target.strip(),
        )

    def _run_probe(
        self, command: Sequence[str], description: str, *, allow_failure: bool = False
    ) -> str:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as error:
            if allow_failure:
                return ""
            raise ArtifactBuildError(
                "configuration",
                f"failed to query {description}: {error}",
                command=command,
            ) from error
        if completed.returncode != 0:
            if allow_failure:
                return ""
            raise ArtifactBuildError(
                "configuration",
                f"failed to query {description}",
                command=command,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        return completed.stdout or completed.stderr

    def _resolve_eigen_identity(self, engine: _EngineLayout) -> _EigenIdentity:
        candidates: list[Path] = []
        if self.eigen_include is not None:
            candidates.append(self.eigen_include)
        for variable in ("EIGEN3_INCLUDE_DIR", "EIGEN3_INCLUDE_DIRS"):
            value = os.environ.get(variable)
            if value:
                candidates.extend(
                    Path(item).expanduser() for item in value.split(os.pathsep)
                )
        candidates.extend(
            (
                engine.root / "build" / "_deps" / "eigen-src",
                Path("/opt/homebrew/include/eigen3"),
                Path("/usr/local/include/eigen3"),
                Path("/usr/include/eigen3"),
            )
        )
        include_dir = next(
            (
                candidate.resolve()
                for candidate in candidates
                if (candidate / "Eigen" / "Core").is_file()
            ),
            None,
        )
        if include_dir is None:
            raise ArtifactBuildError(
                "configuration",
                "Eigen include directory not found; pass eigen_include or EIGEN3_INCLUDE_DIR",
            )

        macros = include_dir / "Eigen" / "src" / "Core" / "util" / "Macros.h"
        version = "unknown"
        if macros.is_file():
            text = _read_text(macros, "Eigen version header")
            world = _read_define(text, "EIGEN_WORLD_VERSION")
            major = _read_define(text, "EIGEN_MAJOR_VERSION")
            minor = _read_define(text, "EIGEN_MINOR_VERSION")
            if world and major and minor:
                version = f"{world}.{major}.{minor}"
        return _EigenIdentity(
            include_dir=include_dir,
            headers_sha256=_tree_sha256((include_dir / "Eigen",)),
            version=version,
        )

    def _platform_link_mode(self) -> tuple[str, str]:
        system = platform.system()
        if system == "Darwin":
            return ".dylib", "force_load"
        if system in {"Linux", "FreeBSD"}:
            return ".so", "whole_archive"
        raise ArtifactBuildError(
            "configuration",
            f"generated strategy compilation is not supported on {system}",
        )

    def _lookup_request(
        self,
        request_key: str,
        request_identity: Mapping[str, Any],
        output_extension: str,
    ) -> StrategyArtifact | None:
        index_path = self.cache_dir / "requests" / f"{request_key}.json"
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if (
            not isinstance(index, Mapping)
            or index.get("schema_version") != CACHE_SCHEMA_VERSION
        ):
            return None
        if index.get("request_key") != request_key:
            return None
        artifact_key = index.get("artifact_key")
        if not isinstance(artifact_key, str) or len(artifact_key) != 64:
            return None
        return self._load_artifact(
            artifact_key=artifact_key,
            request_key=request_key,
            request_identity=request_identity,
            output_extension=output_extension,
            cache_hit=True,
        )

    def _load_artifact(
        self,
        *,
        artifact_key: str,
        request_key: str,
        request_identity: Mapping[str, Any],
        output_extension: str,
        cache_hit: bool,
    ) -> StrategyArtifact | None:
        artifact_dir = self.cache_dir / "artifacts" / artifact_key
        manifest_path = artifact_dir / "manifest.json"
        provenance_path = artifact_dir / "provenance.json"
        generated_cpp_path = artifact_dir / "generated.cpp"
        plugin_path = artifact_dir / f"strategy{output_extension}"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, Mapping) or not isinstance(provenance, Mapping):
            return None
        if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            return None
        if (
            manifest.get("artifact_key") != artifact_key
            or manifest.get("request_key") != request_key
        ):
            return None
        if manifest.get("request_identity") != request_identity:
            return None
        generated_hash = manifest.get("generated_cpp_sha256")
        plugin_hash = manifest.get("plugin_sha256")
        plugin_validation = manifest.get("plugin_validation")
        if not isinstance(generated_hash, str) or not isinstance(plugin_hash, str):
            return None
        if not isinstance(plugin_validation, Mapping):
            return None
        expected_abi = request_identity.get("engine", {}).get("abi_version")
        if plugin_validation.get("abi_version") != expected_abi:
            return None
        validated_symbols = plugin_validation.get("required_symbols")
        if not isinstance(validated_symbols, list) or not set(
            _REQUIRED_PLUGIN_SYMBOLS
        ).issubset(validated_symbols):
            return None
        if (
            _json_sha256(
                {
                    "request_identity": request_identity,
                    "generated_cpp_sha256": generated_hash,
                }
            )
            != artifact_key
        ):
            return None
        if not generated_cpp_path.is_file() or not plugin_path.is_file():
            return None
        if _file_sha256(generated_cpp_path) != generated_hash:
            return None
        if _file_sha256(plugin_path) != plugin_hash:
            return None
        if provenance.get("artifact_key") != artifact_key:
            return None
        if provenance.get("request_identity") != request_identity:
            return None
        if provenance.get("plugin_validation") != plugin_validation:
            return None
        if manifest.get("input_kind_schema") != provenance.get("input_kind_schema"):
            return None
        inputs = manifest.get("inputs", [])
        strategy_params = manifest.get("strategy_params", {})
        if not isinstance(inputs, list) or not isinstance(strategy_params, Mapping):
            return None
        if any(not isinstance(item, Mapping) for item in inputs):
            return None
        return StrategyArtifact(
            artifact_key=artifact_key,
            request_key=request_key,
            plugin_path=plugin_path,
            generated_cpp_path=generated_cpp_path,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            cache_hit=cache_hit,
            inputs=tuple(dict(item) for item in inputs),
            strategy_params=dict(strategy_params),
        )

    def _compile_and_publish(
        self,
        *,
        artifact_key: str,
        request_key: str,
        request_identity: Mapping[str, Any],
        output_extension: str,
        generated_cpp: str,
        generated_cpp_sha256: str,
        inputs: Sequence[Mapping[str, Any]],
        input_kind_schema: int | None,
        strategy_params: Mapping[str, Any],
        diagnostics: Sequence[TranspileDiagnostic],
        source_name: str,
        compiler: _CompilerIdentity,
        engine: _EngineLayout,
        eigen: _EigenIdentity,
    ) -> StrategyArtifact:
        artifacts_root = self.cache_dir / "artifacts"
        artifacts_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{artifact_key}.", dir=artifacts_root))
        final_dir = artifacts_root / artifact_key
        generated_path = staging / "generated.cpp"
        plugin_path = staging / f"strategy{output_extension}"
        try:
            generated_path.write_text(generated_cpp, encoding="utf-8")
            command = self._compile_command(
                compiler=compiler,
                engine=engine,
                eigen=eigen,
                generated_path=generated_path,
                plugin_path=plugin_path,
            )
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.compile_timeout_seconds,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise ArtifactBuildError(
                    "compile",
                    f"native strategy compiler failed to run: {error}",
                    command=command,
                ) from error
            if completed.returncode != 0 or not plugin_path.is_file():
                raise ArtifactBuildError(
                    "compile",
                    f"native strategy compilation failed with exit code {completed.returncode}",
                    command=command,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )

            try:
                plugin_validation = dict(
                    self.plugin_validator(plugin_path, engine.abi_version)
                )
            except ArtifactBuildError:
                raise
            except Exception as error:
                raise ArtifactBuildError(
                    "plugin_validation", f"strategy plugin validation failed: {error}"
                ) from error
            if plugin_validation.get("abi_version") != engine.abi_version:
                raise ArtifactBuildError(
                    "plugin_validation",
                    "strategy plugin validator did not confirm the expected engine ABI",
                )

            plugin_hash = _file_sha256(plugin_path)
            created_at = datetime.now(timezone.utc).isoformat()
            manifest = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "artifact_key": artifact_key,
                "request_key": request_key,
                "created_at": created_at,
                "source_name": source_name,
                "request_identity": request_identity,
                "generated_cpp_sha256": generated_cpp_sha256,
                "plugin_filename": plugin_path.name,
                "plugin_sha256": plugin_hash,
                "plugin_validation": plugin_validation,
                "inputs": [dict(item) for item in inputs],
                "strategy_params": dict(strategy_params),
                "diagnostics": [item.to_dict() for item in diagnostics],
            }
            provenance = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "artifact_key": artifact_key,
                "request_key": request_key,
                "created_at": created_at,
                "source_name": source_name,
                "request_identity": request_identity,
                "generated_cpp_sha256": generated_cpp_sha256,
                "plugin_sha256": plugin_hash,
                "plugin_validation": plugin_validation,
                "compile": request_identity["compile"],
            }
            if input_kind_schema == 1:
                manifest["input_kind_schema"] = 1
                provenance["input_kind_schema"] = 1
            (staging / "manifest.json").write_text(
                _canonical_json(manifest) + "\n", encoding="utf-8"
            )
            (staging / "provenance.json").write_text(
                _canonical_json(provenance) + "\n", encoding="utf-8"
            )

            if final_dir.exists():
                cached = self._load_artifact(
                    artifact_key=artifact_key,
                    request_key=request_key,
                    request_identity=request_identity,
                    output_extension=output_extension,
                    cache_hit=True,
                )
                if cached is not None:
                    return cached
                shutil.rmtree(final_dir)
            os.rename(staging, final_dir)
            staging = Path()
        finally:
            if staging != Path() and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

        artifact = self._load_artifact(
            artifact_key=artifact_key,
            request_key=request_key,
            request_identity=request_identity,
            output_extension=output_extension,
            cache_hit=False,
        )
        if artifact is None:
            raise ArtifactBuildError(
                "cache", f"published artifact failed validation: {artifact_key}"
            )
        return artifact

    def _validate_plugin(
        self, plugin_path: Path, expected_abi: int
    ) -> Mapping[str, Any]:
        try:
            mode = getattr(ctypes, "RTLD_LOCAL", 0)
            library = ctypes.CDLL(str(plugin_path), mode=mode)
        except OSError as error:
            raise ArtifactBuildError(
                "plugin_validation", f"cannot load compiled strategy plugin: {error}"
            ) from error

        missing = [
            name for name in _REQUIRED_PLUGIN_SYMBOLS if not hasattr(library, name)
        ]
        if missing:
            raise ArtifactBuildError(
                "plugin_validation",
                "compiled strategy plugin is missing required symbols: "
                + ", ".join(missing),
            )
        abi_function = library.pf_abi_version
        abi_function.argtypes = []
        abi_function.restype = ctypes.c_int
        actual_abi = int(abi_function())
        if actual_abi != expected_abi:
            raise ArtifactBuildError(
                "plugin_validation",
                f"strategy plugin ABI {actual_abi} does not match engine ABI {expected_abi}",
            )
        return {
            "abi_version": actual_abi,
            "required_symbols": list(_REQUIRED_PLUGIN_SYMBOLS),
        }

    def _compile_command(
        self,
        *,
        compiler: _CompilerIdentity,
        engine: _EngineLayout,
        eigen: _EigenIdentity,
        generated_path: Path,
        plugin_path: Path,
    ) -> list[str]:
        command = [str(compiler.path), *compiler.compile_flags]
        for include_dir in (*engine.include_dirs, eigen.include_dir):
            command.extend(("-I", str(include_dir)))
        command.append(str(generated_path))
        if platform.system() == "Darwin":
            command.append(f"-Wl,-force_load,{engine.static_library}")
        else:
            command.extend(
                (
                    "-Wl,--whole-archive",
                    str(engine.static_library),
                    "-Wl,--no-whole-archive",
                )
            )
        command.extend(("-o", str(plugin_path)))
        return command

    def _write_request_index(self, request_key: str, artifact_key: str) -> None:
        requests_root = self.cache_dir / "requests"
        requests_root.mkdir(parents=True, exist_ok=True)
        destination = requests_root / f"{request_key}.json"
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "request_key": request_key,
            "artifact_key": artifact_key,
        }
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{request_key}.", dir=requests_root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(_canonical_json(payload) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def build_strategy_artifact(
    pine_source: str,
    *,
    engine_root: str | os.PathLike[str],
    cache_dir: str | os.PathLike[str] | None = None,
    compiler: str | os.PathLike[str] | None = None,
    eigen_include: str | os.PathLike[str] | None = None,
    filename: str = "<input>",
) -> StrategyArtifact:
    """Convenience entry point for a one-off CLI artifact build."""

    builder = ArtifactBuilder(
        engine_root=engine_root,
        cache_dir=cache_dir,
        compiler=compiler,
        eigen_include=eigen_include,
    )
    return builder.build(pine_source, filename=filename)


__all__ = [
    "ArtifactBuildError",
    "ArtifactBuilder",
    "CANONICAL_COMPILE_FLAGS",
    "StrategyArtifact",
    "build_strategy_artifact",
]
