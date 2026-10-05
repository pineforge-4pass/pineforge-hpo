"""Dependency-free reference writer for concatenable warm-start v2 blocks."""

from __future__ import annotations

from array import array
import json
import math
import struct
import sys
from typing import Any, BinaryIO, Mapping, Sequence

from .continuation import WarmStartError, _parameters, space_hash
from .study_spec import StudySpec, _finite_parameter_cardinality, _fused_grid_value

MAGIC = b"PFHWARM\0"
STATE_MAGIC = b"PFHSTATE"
VERSION = 2
HEADER = struct.Struct("<8sHHIQQIIII32s")
NULL_BITS = 0x7FF8000000000000
STATES = (
    "ok",
    "constraint_violation",
    "engine_error",
    "objective_error",
    "constraint_error",
    "trial_error",
    "trial_timeout",
    "pruned",
    "partial",
)


def _column(typecode: str, values: Sequence[Any]) -> bytes:
    column = array(typecode, values)
    if column.itemsize != {"Q": 8, "i": 4, "d": 8}[typecode]:
        raise ValueError("host array width is not supported")
    if sys.byteorder != "little":
        column.byteswap()
    return column.tobytes()


def _score(value: Any) -> bytes:
    if value is None:
        return struct.pack("<Q", NULL_BITS)
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError("objective/constraint must be finite or null")
    return struct.pack("<d", value)


def encode_warm_block(
    study: StudySpec,
    trials: Sequence[Mapping[str, Any]],
    *,
    sampler_state: str | None = None,
    symbol_feeds_record: Mapping[str, Any] | None = None,
) -> bytes:
    """Encode every attempted trial in one chunk, sorting rows by uint64 trial ID.

    Parameters use lexicographic name order. Missing v0.5 constraint values become
    canonical nulls; feasibility continues to come from state, exactly as in v0.5.
    Concatenate independent returned blocks without adding separators or a footer.
    """
    try:
        if not trials:
            raise ValueError("binary block must contain at least one trial")
        for trial in trials:
            identifier = trial["trial_id"]
            if type(identifier) is not int or not 0 <= identifier < (1 << 64) - 1:
                raise ValueError("expected uint64 trial_id with a continuation ID")
        ordered = sorted(trials, key=lambda trial: trial["trial_id"])
        identifiers = [trial["trial_id"] for trial in ordered]
        if any(left == right for left, right in zip(identifiers, identifiers[1:])):
            raise ValueError("duplicate trial_id")
        parameters = [_parameters(study, trial["parameters"]) for trial in ordered]
        states = []
        for trial in ordered:
            state = STATES.index(trial["status"])
            if type(trial["feasible"]) is not bool or trial["feasible"] != (state == 0):
                raise ValueError("inconsistent status/feasibility/objective")
            states.append(state)
        descriptors = bytearray()
        columns = [_column("Q", identifiers), bytes(states)]
        for name, parameter in sorted(study.search_space.items()):
            kind = {"integer": 1, "real": 2, "boolean": 3, "categorical": 4}[
                parameter.kind
            ]
            encoding = 2 if parameter.kind == "real" and parameter.step is None else 1
            descriptors.extend(struct.pack("<BBH", kind, encoding, 0))
            values = [row[name] for row in parameters]
            if encoding == 2:
                columns.append(_column("d", values))
                continue
            count = _finite_parameter_cardinality(parameter, name, [])
            if count is None or count > 1 << 31:
                raise ValueError(f"binary parameter grid index exceeds int32: {name}")
            if parameter.kind == "integer":
                indices = [
                    (value - parameter.low) // parameter.step for value in values
                ]
            elif parameter.kind == "real":
                indices = [
                    round((value - parameter.low) / parameter.step) for value in values
                ]
                for trial, index in zip(ordered, indices):
                    decoded = (
                        float(parameter.low)
                        if parameter.low == parameter.high
                        else _fused_grid_value(index, parameter.step, parameter.low)
                    )
                    if index + 1 == count and decoded > parameter.high:
                        decoded = float(parameter.high)
                    if struct.pack("<d", decoded) != struct.pack(
                        "<d", trial["parameters"][name]
                    ):
                        raise ValueError(f"noncanonical binary grid parameter: {name}")
            elif parameter.kind == "boolean":
                indices = [int(value) for value in values]
            else:
                indices = [
                    next(
                        index
                        for index, choice in enumerate(parameter.choices)
                        if type(value) is type(choice) and value == choice
                    )
                    for value in values
                ]
                for value, index in zip(values, indices):
                    if type(value) is float and struct.pack("<d", value) != struct.pack(
                        "<d", parameter.choices[index]
                    ):
                        raise ValueError(
                            f"noncanonical binary choice parameter: {name}"
                        )
            if any(not 0 <= index < count for index in indices):
                raise ValueError(f"invalid binary grid index: {name}")
            columns.append(_column("i", indices))
        columns.append(b"".join(_score(trial["objective"]) for trial in ordered))
        constraint_count = len(study.objective.constraints)
        constraint_rows = []
        for trial in ordered:
            values = trial.get("constraint_values", [None] * constraint_count)
            if not isinstance(values, (list, tuple)) or len(values) != constraint_count:
                raise ValueError("constraint column counts do not match study")
            constraint_rows.append(values)
        constraint_order = sorted(
            range(constraint_count),
            key=lambda index: study.objective.constraints[index],
        )
        for column in constraint_order:
            columns.append(
                b"".join(_score(values[column]) for values in constraint_rows)
            )
        records = [
            trial["space"].get("symbol_feeds") for trial in ordered if "space" in trial
        ]
        if symbol_feeds_record is None and records:
            symbol_feeds_record = records[0]
        if any(record != symbol_feeds_record for record in records):
            raise ValueError("symbol feeds differ between trial records")
        empty_index = (
            isinstance(study.symbol_feeds, Mapping)
            and study.symbol_feeds.get("symbols") == {}
        )
        if (
            study.symbol_feeds is not None
            and not empty_index
            and symbol_feeds_record is None
        ):
            raise ValueError(
                "symbol feeds record is required for this study's warm header"
            )
        runtime = b""
        if symbol_feeds_record is not None and not isinstance(
            symbol_feeds_record, Mapping
        ):
            raise ValueError("symbol feeds record must be an object")
        if symbol_feeds_record is not None and symbol_feeds_record.get("symbols") != {}:
            encoded = json.dumps(
                symbol_feeds_record,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            if len(encoded) > 8 * 1024 * 1024:
                raise ValueError("symbol feeds header exceeds 8 MiB")
            runtime = struct.pack("<I", len(encoded)) + encoded
        header_bytes = HEADER.size + len(descriptors) + len(runtime)
        payload = b"".join(columns)
        header = HEADER.pack(
            MAGIC,
            VERSION,
            int(bool(runtime)),
            header_bytes,
            header_bytes + len(payload),
            len(ordered),
            len(study.search_space),
            1,
            constraint_count,
            0,
            bytes.fromhex(space_hash(study)),
        )
        block = header + descriptors + runtime + payload
        if sampler_state is not None:
            if not isinstance(sampler_state, str) or not sampler_state:
                raise ValueError("sampler_state must be a nonempty string")
            state = sampler_state.encode("utf-8")
            if len(state) > 16 * 1024 * 1024:
                raise ValueError("sampler_state exceeds 16 MiB")
            block += STATE_MAGIC + struct.pack("<Q", len(state)) + state
        return block
    except (ValueError, TypeError, KeyError, OverflowError, StopIteration) as error:
        raise WarmStartError(f"warm-start incompatible: {error}") from error


def write_warm_block(
    output: BinaryIO,
    study: StudySpec,
    trials: Sequence[Mapping[str, Any]],
    *,
    sampler_state: str | None = None,
    symbol_feeds_record: Mapping[str, Any] | None = None,
) -> int:
    """Write one validated chunk to a binary stream; return its byte count."""
    block = encode_warm_block(
        study,
        trials,
        sampler_state=sampler_state,
        symbol_feeds_record=symbol_feeds_record,
    )
    written = output.write(block)
    if written != len(block):
        raise OSError("short write of binary warm block")
    return written
