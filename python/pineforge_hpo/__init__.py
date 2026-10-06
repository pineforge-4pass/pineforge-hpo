"""Python initialization path for PineForge HPO."""

from importlib import metadata
from pathlib import Path

from .artifact import (
    ArtifactBuildError,
    ArtifactBuilder,
    CANONICAL_COMPILE_FLAGS,
    StrategyArtifact,
    build_strategy_artifact,
)
from .study_spec import (
    DatasetSpec,
    ExecutionSpec,
    ObjectiveSpec,
    ParameterSpec,
    SamplerSpec,
    StrategySpec,
    StudySpec,
    StudySpecError,
    TpeSamplerConfig,
    ValidationIssue,
    load_study_spec,
)
from .transpile import (
    CodegenIdentity,
    TranspileDiagnostic,
    TranspileFailure,
    TranspileResult,
    codegen_identity,
    transpile_source,
)

from .error import HpoError


def _distribution_version() -> str:
    try:
        return metadata.version("pineforge-hpo")
    except metadata.PackageNotFoundError:
        version_file = Path(__file__).resolve().parents[2] / "VERSION"
        try:
            return version_file.read_text(encoding="utf-8").strip()
        except OSError:
            return "0+unknown"


__version__ = _distribution_version()


def prepare_run(study_path, engine_root=None, cache_dir=None, **options):
    """Return native argv and artifact JSON; do not launch the native process.

    Optional keyword arguments are native, compiler, and eigen_include, matching
    the existing CLI build/executable overrides.
    """
    from .cli import prepare_run as prepare

    return prepare(study_path, engine_root, cache_dir, **options)


__all__ = [
    "__version__",
    "ArtifactBuildError",
    "ArtifactBuilder",
    "CANONICAL_COMPILE_FLAGS",
    "CodegenIdentity",
    "DatasetSpec",
    "ExecutionSpec",
    "HpoError",
    "ObjectiveSpec",
    "ParameterSpec",
    "SamplerSpec",
    "StrategyArtifact",
    "StrategySpec",
    "StudySpec",
    "StudySpecError",
    "TpeSamplerConfig",
    "TranspileDiagnostic",
    "TranspileFailure",
    "TranspileResult",
    "ValidationIssue",
    "build_strategy_artifact",
    "codegen_identity",
    "load_study_spec",
    "prepare_run",
    "transpile_source",
]
