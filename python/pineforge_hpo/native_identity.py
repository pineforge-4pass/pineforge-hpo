"""Selected producer evidence contract and local value preparation.

The closed record describes local observations, not an admitted nativeCard.
Recorded build validation is neither a fresh plugin load nor resource admission.
Decoding validates data without authenticating custody or reading its paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from .artifact import (
    INPUT_METADATA_REVISION,
    MANIFEST_SCHEMA_VERSION,
    StrategyArtifact,
    _REQUIRED_PLUGIN_SYMBOLS,
    _canonical_json,
    _json_sha256,
)
from .study_spec import (
    StudySpec,
    _reject_duplicate_keys,
    _reject_json_constant,
)


class NativeIdentityError(ValueError):
    """Invalid local producer relationship or closed evidence record."""


PRODUCER_NAME = "pineforge-hpo/native-card-producer"
PRODUCER_VERSION = 1
PRODUCER_TYPE = "pineforge.hpo.native-producer-evidence/1"
PRODUCER_ISSUER = "pineforge-core"


class EvidenceState(Enum):
    RECORDED = auto()
    DEFERRED = auto()
    MISSING = auto()
    UNQUALIFIED = auto()
    UNSUPPORTED = auto()
    UNBOUND = auto()


class FileRole(Enum):
    PINE_SOURCE = auto()
    GENERATED_CPP = auto()
    PLUGIN = auto()
    BUILD_MANIFEST = auto()
    BUILD_PROVENANCE = auto()
    DATASET_CSV = auto()
    DATASET_BINARY = auto()


class ProducerRole(Enum):
    SOURCE_BUILD_RELATION = auto()
    PRODUCER_CONTRACT = auto()
    FOREIGN_CODEC = auto()
    RETAINED_AUTHORITY = auto()
    RESOURCE_ADMISSION_POLICY = auto()
    EXECUTED_ARTIFACT_ADMISSION = auto()
    PRIMARY_EVALUATION_COUNT = auto()
    TOTAL_PHYSICAL_RECORDS = auto()
    TRUSTED_DATASET_CSV_RECEIPTS = auto()
    FOREIGN_FEED_COUNTS = auto()
    SELECTED_PAIR_ORDINALS = auto()
    RETENTION = auto()
    TERMINAL_BUDGET = auto()
    TIMEOUT_MILLISECONDS = auto()
    RECORD_REPORT_OUTPUT_BYTES = auto()


@dataclass(frozen=True)
class FileObservation:
    """Observed file bytes; a path/digest is not retained content authority."""

    role: FileRole
    path: Path
    sha256: str
    byte_count: int


@dataclass(frozen=True)
class SourceValues:
    file: FileObservation
    # The builder hashes read_text's UTF8 encoding, which can differ from raw
    # file bytes because Python translates CRLF/CR to LF before compilation.
    compile_input_sha256: str | None


@dataclass(frozen=True)
class PluginEvidence:
    state: EvidenceState
    abi_version: int | None
    required_symbols: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class BuildValues:
    artifact_key: str
    request_key: str
    manifest_schema_version: int
    input_metadata_revision: int
    # Preserve the complete existing identity (codegen, engine, compiler,
    # target, flags, etc.) without assigning it a new authority or wire type.
    request_identity_json: str
    generated_cpp: FileObservation
    manifest: FileObservation
    provenance: FileObservation
    plugin_validation: PluginEvidence


@dataclass(frozen=True)
class DatasetValues:
    id: str
    declared_ordinal: int
    strategy_association_ordinals: tuple[int, ...]
    file: FileObservation
    input_tf: str
    script_tf: str
    chart_timezone: str


@dataclass(frozen=True)
class UnavailableValue:
    role: ProducerRole
    state: EvidenceState
    reason: str


@dataclass(frozen=True)
class ProducerValues:
    # The typed declaration and actual argv remain separate, including any
    # prepare_run symbol-feed/warm-start overrides. Neither is a foreign codec.
    study: StudySpec
    native_command: tuple[str, ...] | None
    source: SourceValues | None
    plugin: FileObservation | None
    build: BuildValues | None
    datasets: tuple[DatasetValues, ...]
    unavailable: tuple[UnavailableValue, ...]


def _observation(role: FileRole, path: Path, data: bytes) -> FileObservation:
    return FileObservation(role, path, hashlib.sha256(data).hexdigest(), len(data))


def _observe_file(role: FileRole, path: Path) -> FileObservation:
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                byte_count += len(chunk)
    except OSError as error:
        raise NativeIdentityError(f"cannot observe {role.name}: {path}") from error
    return FileObservation(role, path, digest.hexdigest(), byte_count)


def _read_document(
    role: FileRole, path: Path
) -> tuple[dict[str, Any], FileObservation]:
    try:
        raw = path.read_bytes()
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise NativeIdentityError(f"cannot read build record: {path}") from error
    if not isinstance(document, dict):
        raise NativeIdentityError(f"build record must be an object: {path}")
    return document, _observation(role, path, raw)


def _plugin_evidence(value: Any, identity: dict[str, Any]) -> PluginEvidence:
    if value is None:
        return PluginEvidence(
            EvidenceState.MISSING, None, (), "no recorded plugin validation"
        )
    if value == {"status": "deferred"}:
        return PluginEvidence(
            EvidenceState.DEFERRED, None, (), "plugin loading remains pending"
        )
    engine = identity.get("engine")
    expected_abi = engine.get("abi_version") if isinstance(engine, dict) else None
    if isinstance(value, dict):
        abi = value.get("abi_version")
        symbols = value.get("required_symbols")
        if (
            "status" not in value
            and type(abi) is int
            and type(expected_abi) is int
            and abi == expected_abi
            and isinstance(symbols, list)
            and all(isinstance(symbol, str) for symbol in symbols)
            and set(_REQUIRED_PLUGIN_SYMBOLS).issubset(symbols)
        ):
            return PluginEvidence(
                EvidenceState.RECORDED,
                abi,
                tuple(symbols),
                "ABI/symbol validation recorded by the builder; no fresh load",
            )
    return PluginEvidence(
        EvidenceState.UNQUALIFIED, None, (), "incomplete or conflicting validation"
    )


def _build_values(
    artifact: StrategyArtifact, source: SourceValues
) -> tuple[BuildValues, FileObservation]:
    manifest, manifest_file = _read_document(
        FileRole.BUILD_MANIFEST, artifact.manifest_path
    )
    provenance, provenance_file = _read_document(
        FileRole.BUILD_PROVENANCE, artifact.provenance_path
    )
    shared = (
        "schema_version",
        "artifact_key",
        "request_key",
        "request_identity",
        "generated_cpp_sha256",
        "plugin_sha256",
    )
    for field in shared:
        if (
            field not in manifest
            or field not in provenance
            or _canonical_json(manifest[field]) != _canonical_json(provenance[field])
        ):
            raise NativeIdentityError(f"build records disagree on {field}")
    if _canonical_json(manifest.get("plugin_validation")) != _canonical_json(
        provenance.get("plugin_validation")
    ):
        raise NativeIdentityError("build records disagree on plugin_validation")
    schema = manifest["schema_version"]
    identity = manifest["request_identity"]
    if type(schema) is not int or schema != MANIFEST_SCHEMA_VERSION:
        raise NativeIdentityError("unsupported internal manifest schema")
    if not isinstance(identity, dict):
        raise NativeIdentityError("missing build request identity")
    revision = identity.get("input_metadata_revision")
    if type(revision) is not int or revision != INPUT_METADATA_REVISION:
        raise NativeIdentityError("unsupported internal input metadata revision")
    if "source_kind" in identity:
        raise NativeIdentityError("this record does not establish a Pine source build")
    if (
        source.compile_input_sha256 is None
        or identity.get("source_sha256") != source.compile_input_sha256
    ):
        raise NativeIdentityError("Pine compile input differs from build record")
    if (
        manifest["request_key"] != artifact.request_key
        or _json_sha256(identity) != artifact.request_key
    ):
        raise NativeIdentityError("request key differs from build identity")
    generated = _observe_file(FileRole.GENERATED_CPP, artifact.generated_cpp_path)
    plugin = _observe_file(FileRole.PLUGIN, artifact.plugin_path)
    if (
        artifact.plugin_path.resolve() == source.file.path.resolve()
        or manifest.get("plugin_filename") != artifact.plugin_path.name
    ):
        raise NativeIdentityError("Pine source and plugin roles must remain distinct")
    if generated.sha256 != manifest["generated_cpp_sha256"]:
        raise NativeIdentityError("generated C++ bytes differ from build record")
    if plugin.sha256 != manifest["plugin_sha256"]:
        raise NativeIdentityError("plugin bytes differ from build record")
    if (
        manifest["artifact_key"] != artifact.artifact_key
        or _json_sha256(
            {
                "request_identity": identity,
                "generated_cpp_sha256": generated.sha256,
            }
        )
        != artifact.artifact_key
    ):
        raise NativeIdentityError("artifact key differs from build identity")
    return (
        BuildValues(
            artifact.artifact_key,
            artifact.request_key,
            schema,
            revision,
            _canonical_json(identity),
            generated,
            manifest_file,
            provenance_file,
            _plugin_evidence(manifest.get("plugin_validation"), identity),
        ),
        plugin,
    )


def _unavailable_values(
    *, source_present: bool, build_present: bool
) -> tuple[UnavailableValue, ...]:
    unbound = (
        (ProducerRole.FOREIGN_CODEC, "selected Compute codec is externally owned"),
        (ProducerRole.RETAINED_AUTHORITY, "local bytes do not authenticate custody"),
        (
            ProducerRole.RESOURCE_ADMISSION_POLICY,
            "no selected policy identity supplied",
        ),
        (ProducerRole.EXECUTED_ARTIFACT_ADMISSION, "build records are not admission"),
    )
    unsupported = (
        (ProducerRole.PRIMARY_EVALUATION_COUNT, "no selected primary evaluation rule"),
        (ProducerRole.TOTAL_PHYSICAL_RECORDS, "no trusted whole-file record receipt"),
        (
            ProducerRole.TRUSTED_DATASET_CSV_RECEIPTS,
            "local byte observations have no retained owner/receipt binding",
        ),
        (ProducerRole.FOREIGN_FEED_COUNTS, "dataset declarations are not F/F+1/F+2"),
        (
            ProducerRole.SELECTED_PAIR_ORDINALS,
            "no retained R10 pair association supplied",
        ),
        (ProducerRole.RETENTION, "StudySpec has no retention member"),
        (ProducerRole.TERMINAL_BUDGET, "trials are not a terminal/resource budget"),
        (ProducerRole.TIMEOUT_MILLISECONDS, "wrapper refuses timeout_seconds"),
        (ProducerRole.RECORD_REPORT_OUTPUT_BYTES, "no corresponding encoded records"),
    )
    values = tuple(
        UnavailableValue(role, state, reason)
        for entries, state in (
            (unbound, EvidenceState.UNBOUND),
            (unsupported, EvidenceState.UNSUPPORTED),
        )
        for role, reason in entries
    )
    if not build_present:
        values += (
            UnavailableValue(
                ProducerRole.SOURCE_BUILD_RELATION,
                (
                    EvidenceState.MISSING
                    if source_present
                    else EvidenceState.UNSUPPORTED
                ),
                (
                    "no source-build record supplied"
                    if source_present
                    else "precompiled input does not establish a Pine build chain"
                ),
            ),
        )
    return values


def prepare_producer_values(
    study: StudySpec,
    artifact: StrategyArtifact | None = None,
    *,
    compiled_source: str | None = None,
    wrapper_launched: bool = False,
    native_command: Sequence[str] | None = None,
) -> ProducerValues:
    """Observe actual inputs and corroborate a supplied source-build record.

    No compilation, plugin load, metadata write or external-policy decoding
    occurs here. The optional argv is the actual prepare_run output, not a
    reconstruction of effective controls from the StudySpec declaration.
    """
    if not isinstance(study, StudySpec):
        raise TypeError("study must be a StudySpec")
    if type(wrapper_launched) is not bool:
        raise TypeError("wrapper_launched must be a bool")
    for name, value, minimum in (
        ("seed", study.sampler.seed, 0),
        ("trials", study.sampler.trials, 1),
        ("workers", study.execution.workers, 1),
    ):
        if type(value) is not int or value < minimum:
            raise NativeIdentityError(f"{name} must be an integer >= {minimum}")
    batch_size = study.execution.batch_size
    if batch_size is not None and (type(batch_size) is not int or batch_size < 1):
        raise NativeIdentityError("batch_size must be a positive integer when supplied")
    strategy = study.strategy
    if (strategy.source is None) == (strategy.artifact is None):
        raise NativeIdentityError("exactly one source or artifact role is required")
    source = None
    plugin = None
    build = None
    if strategy.source is not None:
        try:
            raw = strategy.source.read_bytes()
            text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except (OSError, UnicodeError) as error:
            raise NativeIdentityError("cannot read UTF8 Pine source") from error
        if compiled_source is not None and (
            not isinstance(compiled_source, str) or compiled_source != text
        ):
            raise NativeIdentityError("Pine file differs from actual compiler input")
        source = SourceValues(
            _observation(FileRole.PINE_SOURCE, strategy.source, raw),
            (
                hashlib.sha256(compiled_source.encode("utf-8")).hexdigest()
                if compiled_source is not None
                else None
            ),
        )
        if artifact is not None:
            if not isinstance(artifact, StrategyArtifact):
                raise TypeError("artifact must be a StrategyArtifact")
            build, plugin = _build_values(artifact, source)
    else:
        if wrapper_launched:
            raise NativeIdentityError("wrapper precompiled-artifact bypass is refused")
        if artifact is not None or compiled_source is not None:
            raise NativeIdentityError("a precompiled input is not a Pine build receipt")
        assert strategy.artifact is not None
        plugin = _observe_file(FileRole.PLUGIN, strategy.artifact)
    dataset_ids = tuple(dataset.id for dataset in study.datasets)
    if len(set(dataset_ids)) != len(dataset_ids) or not set(
        strategy.dataset_ids
    ).issubset(dataset_ids):
        raise NativeIdentityError("dataset associations do not match declared IDs")
    datasets = []
    for ordinal, dataset in enumerate(study.datasets):
        if (dataset.ohlcv is None) == (dataset.ohlcv_bin is None):
            raise NativeIdentityError(
                "exactly one CSV or binary dataset role is required"
            )
        path = dataset.ohlcv_bin if dataset.ohlcv_bin is not None else dataset.ohlcv
        assert path is not None
        role = (
            FileRole.DATASET_BINARY
            if dataset.ohlcv_bin is not None
            else FileRole.DATASET_CSV
        )
        datasets.append(
            DatasetValues(
                dataset.id,
                ordinal,
                tuple(
                    index
                    for index, dataset_id in enumerate(strategy.dataset_ids)
                    if dataset_id == dataset.id
                ),
                _observe_file(role, path),
                dataset.input_tf,
                dataset.script_tf,
                dataset.chart_timezone,
            )
        )
    command = None if native_command is None else tuple(native_command)
    if command is not None:
        if plugin is None or "--strategy" not in command:
            raise NativeIdentityError("native argv requires the resulting plugin")
        offset = command.index("--strategy") + 1
        if (
            offset >= len(command)
            or Path(command[offset]).resolve() != plugin.path.resolve()
        ):
            raise NativeIdentityError("native argv strategy differs from plugin role")
    return ProducerValues(
        study,
        command,
        source,
        plugin,
        build,
        tuple(datasets),
        _unavailable_values(
            source_present=source is not None, build_present=build is not None
        ),
    )


def _file_record(value: FileObservation) -> dict[str, Any]:
    return {
        "role": value.role.name,
        "path": str(value.path),
        "sha256": value.sha256,
        "byte_count": value.byte_count,
    }


def producer_identity_record(values: ProducerValues) -> dict[str, Any]:
    """Produce the selected closed evidence data record, without admission."""
    if not isinstance(values, ProducerValues):
        raise TypeError("values must be ProducerValues")
    study = values.study
    source = values.source
    build = values.build
    record = {
        "producer_identity": {
            "name": PRODUCER_NAME,
            "version": PRODUCER_VERSION,
            "type": PRODUCER_TYPE,
            "issuer": PRODUCER_ISSUER,
        },
        "study": {
            "schema_version": study.schema_version,
            "mode": study.mode,
            "strategy_id": study.strategy.id,
            "strategy_dataset_ids": list(study.strategy.dataset_ids),
            "seed": study.sampler.seed,
            "trials": study.sampler.trials,
            "workers": study.execution.workers,
            "batch_size": study.execution.batch_size,
        },
        "source": (
            {
                "file": _file_record(source.file),
                "compile_input_sha256": source.compile_input_sha256,
            }
            if source is not None
            else None
        ),
        "plugin": _file_record(values.plugin) if values.plugin is not None else None,
        "build": (
            {
                "artifact_key": build.artifact_key,
                "request_key": build.request_key,
                "manifest_schema_version": build.manifest_schema_version,
                "input_metadata_revision": build.input_metadata_revision,
                "generated_cpp": _file_record(build.generated_cpp),
                "manifest": _file_record(build.manifest),
                "provenance": _file_record(build.provenance),
                "plugin_validation": {
                    "state": build.plugin_validation.state.name,
                    "abi_version": build.plugin_validation.abi_version,
                    "required_symbols": list(build.plugin_validation.required_symbols),
                    "reason": build.plugin_validation.reason,
                },
            }
            if build is not None
            else None
        ),
        "datasets": [
            {
                "id": dataset.id,
                "declared_ordinal": dataset.declared_ordinal,
                "strategy_association_ordinals": list(
                    dataset.strategy_association_ordinals
                ),
                "file": _file_record(dataset.file),
                "input_tf": dataset.input_tf,
                "script_tf": dataset.script_tf,
                "chart_timezone": dataset.chart_timezone,
            }
            for dataset in values.datasets
        ],
        "native_command": (
            list(values.native_command) if values.native_command is not None else None
        ),
        "unavailable": [
            {"role": value.role.name, "state": value.state.name, "reason": value.reason}
            for value in values.unavailable
        ],
    }
    _validate_identity_record(record)
    return record


def _identity_bytes(record: dict[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (ValueError, UnicodeError) as error:
        raise NativeIdentityError(
            "producer record must contain finite UTF8 JSON"
        ) from error


def serialize_producer_identity(values: ProducerValues) -> bytes:
    """Encode the selected evidence record as canonical UTF8 JSON plus LF."""
    return _identity_bytes(producer_identity_record(values))


def _closed(value: Any, keys: tuple[str, ...], where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise NativeIdentityError(
            f"{where}: missing, unknown or invalid object members"
        )
    return value


def _string(value: Any, where: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise NativeIdentityError(f"{where}: expected a UTF8 string")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise NativeIdentityError(f"{where}: invalid UTF8 string") from error
    return value


def _integer(value: Any, where: str, *, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise NativeIdentityError(f"{where}: invalid true integer")
    return value


def _sha256(value: Any, where: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise NativeIdentityError(f"{where}: expected lowercase SHA256")
    return value


def _array(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise NativeIdentityError(f"{where}: expected an array")
    return value


def _strings(value: Any, where: str, *, nonempty: bool = False) -> list[str]:
    return [_string(item, where, nonempty=nonempty) for item in _array(value, where)]


def _check_file(value: Any, roles: tuple[FileRole, ...], where: str) -> dict[str, Any]:
    file = _closed(value, ("role", "path", "sha256", "byte_count"), where)
    if _string(file["role"], where) not in {role.name for role in roles}:
        raise NativeIdentityError(f"{where}: incorrect file role")
    _string(file["path"], f"{where}.path")
    _sha256(file["sha256"], f"{where}.sha256")
    _integer(file["byte_count"], f"{where}.byte_count", minimum=0)
    return file


def _check_plugin_evidence(value: Any) -> None:
    evidence = _closed(
        value,
        ("state", "abi_version", "required_symbols", "reason"),
        "plugin_validation",
    )
    state = _string(evidence["state"], "plugin_validation.state")
    symbols = _strings(
        evidence["required_symbols"], "plugin_validation.required_symbols"
    )
    _string(evidence["reason"], "plugin_validation.reason")
    if state == EvidenceState.RECORDED.name:
        _integer(evidence["abi_version"], "plugin_validation.abi_version")
        if not set(_REQUIRED_PLUGIN_SYMBOLS).issubset(symbols):
            raise NativeIdentityError("recorded validation lacks required symbols")
    elif state in {
        EvidenceState.DEFERRED.name,
        EvidenceState.MISSING.name,
        EvidenceState.UNQUALIFIED.name,
    }:
        if evidence["abi_version"] is not None or symbols:
            raise NativeIdentityError(
                "pending validation cannot confirm ABI or symbols"
            )
    else:
        raise NativeIdentityError("unsupported plugin validation state")


def _validate_identity_record(record: Any) -> None:
    root = _closed(
        record,
        (
            "producer_identity",
            "study",
            "source",
            "plugin",
            "build",
            "datasets",
            "native_command",
            "unavailable",
        ),
        "record",
    )
    identity = _closed(
        root["producer_identity"],
        ("name", "version", "type", "issuer"),
        "producer_identity",
    )
    _integer(identity["version"], "producer_identity.version")
    for member, expected in (
        ("name", PRODUCER_NAME),
        ("version", PRODUCER_VERSION),
        ("type", PRODUCER_TYPE),
        ("issuer", PRODUCER_ISSUER),
    ):
        if identity[member] != expected:
            raise NativeIdentityError(f"unsupported producer identity {member}")
    study = _closed(
        root["study"],
        (
            "schema_version",
            "mode",
            "strategy_id",
            "strategy_dataset_ids",
            "seed",
            "trials",
            "workers",
            "batch_size",
        ),
        "study",
    )
    if _integer(study["schema_version"], "study.schema_version") != 1:
        raise NativeIdentityError("unsupported StudySpec version")
    if study["mode"] != "single_strategy":
        raise NativeIdentityError("unsupported study mode")
    _string(study["strategy_id"], "study.strategy_id", nonempty=True)
    association = _strings(
        study["strategy_dataset_ids"], "study.strategy_dataset_ids", nonempty=True
    )
    if not association or len(set(association)) != len(association):
        raise NativeIdentityError("study associations must be nonempty and unique")
    for member, minimum in (("seed", 0), ("trials", 1), ("workers", 1)):
        _integer(study[member], f"study.{member}", minimum=minimum)
    if study["batch_size"] is not None:
        _integer(study["batch_size"], "study.batch_size", minimum=1)

    source = root["source"]
    if source is not None:
        source = _closed(source, ("file", "compile_input_sha256"), "source")
        _check_file(source["file"], (FileRole.PINE_SOURCE,), "source.file")
        if source["compile_input_sha256"] is not None:
            _sha256(source["compile_input_sha256"], "source.compile_input_sha256")
    plugin = root["plugin"]
    if plugin is not None:
        _check_file(plugin, (FileRole.PLUGIN,), "plugin")
        if source is not None and plugin["path"] == source["file"]["path"]:
            raise NativeIdentityError("source and plugin paths must remain distinct")
    build = root["build"]
    if build is not None:
        build = _closed(
            build,
            (
                "artifact_key",
                "request_key",
                "manifest_schema_version",
                "input_metadata_revision",
                "generated_cpp",
                "manifest",
                "provenance",
                "plugin_validation",
            ),
            "build",
        )
        if source is None or plugin is None or source["compile_input_sha256"] is None:
            raise NativeIdentityError(
                "build evidence requires Pine input and plugin roles"
            )
        _sha256(build["artifact_key"], "build.artifact_key")
        _sha256(build["request_key"], "build.request_key")
        if (
            _integer(build["manifest_schema_version"], "build.manifest_schema_version")
            != 1
        ):
            raise NativeIdentityError("unsupported internal manifest version")
        if (
            _integer(build["input_metadata_revision"], "build.input_metadata_revision")
            != 2
        ):
            raise NativeIdentityError("unsupported internal input metadata revision")
        for member, role in (
            ("generated_cpp", FileRole.GENERATED_CPP),
            ("manifest", FileRole.BUILD_MANIFEST),
            ("provenance", FileRole.BUILD_PROVENANCE),
        ):
            _check_file(build[member], (role,), f"build.{member}")
        _check_plugin_evidence(build["plugin_validation"])
    elif (source is None) == (plugin is None):
        raise NativeIdentityError(
            "unbuilt source and precompiled plugin are distinct states"
        )

    datasets = _array(root["datasets"], "datasets")
    ids = []
    for ordinal, item in enumerate(datasets):
        dataset = _closed(
            item,
            (
                "id",
                "declared_ordinal",
                "strategy_association_ordinals",
                "file",
                "input_tf",
                "script_tf",
                "chart_timezone",
            ),
            "dataset",
        )
        dataset_id = _string(dataset["id"], "dataset.id", nonempty=True)
        ids.append(dataset_id)
        if _integer(dataset["declared_ordinal"], "dataset.declared_ordinal") != ordinal:
            raise NativeIdentityError(
                "dataset declaration ordinal differs from array order"
            )
        positions = _array(
            dataset["strategy_association_ordinals"],
            "dataset.strategy_association_ordinals",
        )
        for position in positions:
            _integer(position, "dataset.strategy_association_ordinals", minimum=0)
        if positions != [
            index
            for index, associated_id in enumerate(association)
            if associated_id == dataset_id
        ]:
            raise NativeIdentityError(
                "dataset association ordinals differ from study IDs"
            )
        _check_file(
            dataset["file"],
            (FileRole.DATASET_CSV, FileRole.DATASET_BINARY),
            "dataset.file",
        )
        for member in ("input_tf", "script_tf", "chart_timezone"):
            _string(dataset[member], f"dataset.{member}")
    if not ids or len(set(ids)) != len(ids) or not set(association).issubset(ids):
        raise NativeIdentityError("dataset IDs do not match the study associations")

    command = root["native_command"]
    if command is not None:
        command = _strings(command, "native_command")
        if plugin is None or "--strategy" not in command:
            raise NativeIdentityError("native command requires a plugin argument")
        offset = command.index("--strategy") + 1
        if offset >= len(command) or command[offset] != plugin["path"]:
            raise NativeIdentityError("native command must name the observed plugin")

    # This producer supplies an explicit disposition for every unsupported or
    # externally unbound role. Selection resolves PRODUCER_CONTRACT itself;
    # it never resolves any of the other authorities by default.
    expected = _unavailable_values(
        source_present=source is not None, build_present=build is not None
    )
    unavailable = _array(root["unavailable"], "unavailable")
    if len(unavailable) != len(expected):
        raise NativeIdentityError("missing or additional unavailable roles")
    for row, disposition in zip(unavailable, expected):
        row = _closed(row, ("role", "state", "reason"), "unavailable row")
        if (
            row["role"] != disposition.role.name
            or row["state"] != disposition.state.name
        ):
            raise NativeIdentityError(
                "unavailable role/state/order differs from producer"
            )
        _string(row["reason"], "unavailable.reason", nonempty=True)


def _reject_float(value: str) -> None:
    raise NativeIdentityError("producer record numeric fields require true integers")


def decode_producer_identity(raw: bytes) -> dict[str, Any]:
    """Decode canonical evidence DATA only; never authenticate or admit a card."""
    if not isinstance(raw, bytes):
        raise TypeError("raw must be UTF8 bytes")
    try:
        record = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
            parse_float=_reject_float,
        )
    except (UnicodeError, ValueError) as error:
        raise NativeIdentityError("invalid producer record JSON") from error
    _validate_identity_record(record)
    if raw != _identity_bytes(record):
        raise NativeIdentityError(
            "producer record bytes are not canonical UTF8 JSON plus LF"
        )
    return record
