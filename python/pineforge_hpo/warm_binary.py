"""Dependency-free reference writer for concatenable warm-start v2 blocks."""

from __future__ import annotations

from array import array
import json
import math
import struct
import sys
from typing import Any, BinaryIO, Mapping, Sequence

from .continuation import (
    ContinuationDataContext,
    WarmStartError,
    _dump,
    _loads,
    _parameters,
    _real_bits,
    _validate_data_context,
    _validate_sampler_checkpoint,
    space_hash,
)
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
    data_context: ContinuationDataContext | None = None,
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
        for trial, values in zip(ordered, parameters):
            _validate_data_context(trial, data_context, required=True)
            if data_context is not None:
                data_context.validate_row(trial, values)
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
        records = (
            [
                trial["space"].get("symbol_feeds")
                for trial in ordered
                if "space" in trial
            ]
            if data_context is None
            else []
        )
        if data_context is not None:
            symbol_feeds_record = None
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
            and data_context is None
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
        context = (
            b""
            if data_context is None
            else struct.pack("<I", 1) + bytes.fromhex(data_context.data_context_sha256)
        )
        header_bytes = HEADER.size + len(descriptors) + len(runtime) + len(context)
        if header_bytes >= 1 << 32:
            raise ValueError("binary block header length overflow")
        payload = b"".join(columns)
        header = HEADER.pack(
            MAGIC,
            VERSION,
            int(bool(runtime)) | (2 if data_context is not None else 0),
            header_bytes,
            header_bytes + len(payload),
            len(ordered),
            len(study.search_space),
            1,
            constraint_count,
            0,
            bytes.fromhex(space_hash(study)),
        )
        block = header + descriptors + runtime + context + payload
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
    data_context: ContinuationDataContext | None = None,
) -> int:
    """Write one validated chunk to a binary stream; return its byte count."""
    block = encode_warm_block(
        study,
        trials,
        sampler_state=sampler_state,
        symbol_feeds_record=symbol_feeds_record,
        data_context=data_context,
    )
    written = output.write(block)
    if written != len(block):
        raise OSError("short write of binary warm block")
    return written


def _feed_identity(record: Any) -> str | None:
    if record is None:
        return None
    normalized = _loads(_dump(record))
    symbols = normalized["symbols"]
    if not isinstance(symbols, dict):
        raise ValueError("symbol feeds header symbols must be an object")
    for symbol in symbols.values():
        facts = symbol["facts"]
        if not isinstance(facts, dict) or not isinstance(symbol["feeds"], dict):
            raise ValueError("symbol feeds header facts and feeds must be objects")
        if "mintick" in facts:
            value = facts["mintick"]
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("symbol feed mintick must be positive and finite")
            facts["mintick"] = _real_bits(value)
    return _dump(normalized)


def decode_warm_blocks(
    study: StudySpec,
    content: bytes,
    *,
    data_context: ContinuationDataContext | None = None,
    symbol_feeds_record: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Internal reference reader; public binary preflight still uses the native loader."""
    try:
        rows = []
        offset = 0
        names = sorted(study.search_space)
        constraint_order = sorted(
            range(len(study.objective.constraints)),
            key=lambda index: study.objective.constraints[index],
        )
        while offset < len(content):
            if content[offset : offset + 8] == STATE_MAGIC:
                if len(content) - offset < 16:
                    raise ValueError("truncated sampler-state block header")
                size = struct.unpack_from("<Q", content, offset + 8)[0]
                if (
                    not 0 < size <= 16 * 1024 * 1024
                    or size > len(content) - offset - 16
                ):
                    raise ValueError("invalid sampler-state block length")
                _validate_sampler_checkpoint(
                    content[offset + 16 : offset + 16 + size].decode()
                )
                offset += 16 + size
                continue
            if len(content) - offset < HEADER.size:
                raise ValueError("truncated binary block header")
            (
                magic,
                version,
                flags,
                header_size,
                block_size,
                count,
                parameter_count,
                objectives,
                constraints,
                reserved,
                digest,
            ) = HEADER.unpack_from(content, offset)
            if magic != MAGIC or version != VERSION or flags & ~3 or reserved:
                raise ValueError("unsupported binary block magic, version or flags")
            if digest != bytes.fromhex(space_hash(study)):
                raise ValueError("search space or objective differs (space_hash)")
            if (parameter_count, objectives, constraints) != (
                len(names),
                1,
                len(constraint_order),
            ):
                raise ValueError("binary column counts do not match study")
            base_header = HEADER.size + 4 * parameter_count
            if (
                not count
                or not base_header <= header_size <= block_size <= len(content) - offset
            ):
                raise ValueError("truncated or invalid binary block length")
            cursor = base_header
            feeds = None
            if flags & 1:
                if header_size - cursor < 4:
                    raise ValueError("truncated symbol feeds header")
                size = struct.unpack_from("<I", content, offset + cursor)[0]
                cursor += 4
                if not 0 < size <= 8 * 1024 * 1024 or size > header_size - cursor:
                    raise ValueError("invalid symbol feeds header length")
                feeds = _loads(
                    content[offset + cursor : offset + cursor + size].decode()
                )
                cursor += size
            if flags & 2:
                if header_size - cursor != 36:
                    raise ValueError("invalid data context header length")
                context_version = struct.unpack_from("<I", content, offset + cursor)[0]
                context_digest = content[offset + cursor + 4 : offset + cursor + 36]
                if (
                    context_version != 1
                    or data_context is None
                    or context_digest != bytes.fromhex(data_context.data_context_sha256)
                ):
                    raise ValueError("missing or differing data_context_sha256")
                cursor += 36
            elif data_context is not None:
                raise ValueError("bound parent is missing data context identity")
            if cursor != header_size:
                raise ValueError("invalid binary block header length")
            # Context is checked first: its digest already includes the admitted request feeds.
            if (flags & 1 or data_context is None) and (
                _feed_identity(feeds) != _feed_identity(symbol_feeds_record)
            ):
                raise ValueError(
                    "symbol feeds differ from parent (facts or feed values)"
                )
            codecs = []
            row_size = 17 + 8 * constraints
            for column, name in enumerate(names):
                parameter = study.search_space[name]
                kind, encoding, padding = struct.unpack_from(
                    "<BBH", content, offset + HEADER.size + 4 * column
                )
                expected_kind = {
                    "integer": 1,
                    "real": 2,
                    "boolean": 3,
                    "categorical": 4,
                }[parameter.kind]
                expected_encoding = (
                    2 if parameter.kind == "real" and parameter.step is None else 1
                )
                if (kind, encoding, padding) != (expected_kind, expected_encoding, 0):
                    raise ValueError("binary parameter descriptor differs from study")
                codecs.append(encoding)
                row_size += 8 if encoding == 2 else 4
            if block_size != header_size + count * row_size:
                raise ValueError("invalid binary payload length")
            cursor = offset + header_size
            identifiers = struct.unpack_from(f"<{count}Q", content, cursor)
            cursor += count * 8
            states = content[cursor : cursor + count]
            cursor += count
            values = [{} for _ in range(count)]
            for name, encoding in zip(names, codecs):
                parameter = study.search_space[name]
                column = struct.unpack_from(
                    f"<{count}{'d' if encoding == 2 else 'i'}", content, cursor
                )
                cursor += count * (8 if encoding == 2 else 4)
                cardinality = _finite_parameter_cardinality(parameter, name, [])
                for row, value in zip(values, column):
                    if encoding == 1:
                        if cardinality is None or not 0 <= value < cardinality:
                            raise ValueError("invalid binary parameter column")
                        if parameter.kind == "integer":
                            value = parameter.low + value * parameter.step
                        elif parameter.kind == "real":
                            value = (
                                float(parameter.low)
                                if parameter.low == parameter.high
                                else _fused_grid_value(
                                    value, parameter.step, parameter.low
                                )
                            )
                            value = min(value, float(parameter.high))
                        elif parameter.kind == "boolean":
                            value = bool(value)
                        else:
                            value = parameter.choices[value]
                    row[name] = value

            def scores() -> tuple[float | None, ...]:
                nonlocal cursor
                result = []
                for index in range(count):
                    bits = struct.unpack_from("<Q", content, cursor + index * 8)[0]
                    value = struct.unpack_from("<d", content, cursor + index * 8)[0]
                    if not math.isfinite(value) and bits != NULL_BITS:
                        raise ValueError("invalid binary objective or constraint value")
                    result.append(None if bits == NULL_BITS else value)
                cursor += count * 8
                return tuple(result)

            objectives = scores()
            constraint_rows = [[None] * constraints for _ in range(count)]
            for original_column in constraint_order:
                for row, value in zip(constraint_rows, scores()):
                    row[original_column] = value
            for identifier, state, parameters, objective, constraint_values in zip(
                identifiers, states, values, objectives, constraint_rows
            ):
                if identifier == (1 << 64) - 1 or state >= len(STATES):
                    raise ValueError("invalid binary trial ID or state")
                parameters = _parameters(study, parameters)
                row = {
                    "trial_id": identifier,
                    "status": STATES[state],
                    "feasible": state == 0,
                    "objective": objective,
                    "parameters": parameters,
                    "constraint_values": constraint_values,
                }
                if data_context is not None:
                    row.update(data_context.row_identity(parameters))
                rows.append(row)
            offset += block_size
        if not rows:
            raise ValueError("binary history contains no trials")
        rows.sort(key=lambda row: row["trial_id"])
        if any(
            left["trial_id"] == right["trial_id"] for left, right in zip(rows, rows[1:])
        ):
            raise ValueError("duplicate trial_id")
        return tuple(rows)
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        struct.error,
    ) as error:
        raise WarmStartError(f"warm-start incompatible: {error}") from error
