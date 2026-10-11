"""Probability of Backtest Overfitting (CSCV) over per-block moments: the exact stdlib reference.

Post hoc and pure. The input is an N x S table of block moments, one record per trial
(``{trial_id, status, block_returns: {status, count[S], sum[S], m2[S]}}``), never the T x N
return matrix. ``count`` is the number of intervals in a block, ``sum`` the sum of the
per-interval returns and ``m2`` the sum of squared deviations from the block mean. A row with
status ``ok`` or ``constraint_violation`` is computed (default D5); a failed, pruned or partial
row, or a payload that reports a non-positive equity, refuses with a code of its own. The rows
come in parts, each part with the result-level ``return_blocks`` object its rows were reduced
under plus a ``dataset_identity`` that the caller states (a call with one part passes a list of
one pair), and the caller declares the roster (the declared trial set). The module refuses
unless the parts declare one and the same object, every row's counts equal the ``n_b`` of its
own part's object, and the rows of all the parts together are exactly the roster. It checks what
it is handed: the grouping of the rows into parts and the object declared for each part are the
caller's. The module enumerates all C(S, S/2) ordered train/test splits of the S blocks, ranks
every trial on the training blocks, and reports how the training winner ranks on the
complementary blocks.

Every number is plain binary64 arithmetic in a fixed operation order, with no fused
operations and no tolerance. Counts and the sign of the logit are exact integer arithmetic; only
the logarithm of a logit value depends on the platform math library. Contract text and defaults:
``docs/pbo.md``. UNEXECUTED until the proof phase: neither this module nor its tests have been
run.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
import hashlib
from itertools import combinations
import json
import math
from typing import Any, Iterable, Iterator, Sequence

CONTRACT = "pineforge-hpo-pbo/v1"
ALGORITHM = "cscv-all-ordered-splits/v1"
STATISTIC = "pooled-mean-over-sample-sigma/v1"
OK = "ok"
CONSTRAINT_VIOLATION = "constraint_violation"
NON_POSITIVE_EQUITY = "non_positive_equity"

# Row statuses (default D5). A constraint-violating trial was tried, so it belongs to the roster
# and is computed like an ok trial. A failed, pruned or partial trial refuses, each with its own
# code; so does a payload whose status says an equity at or below zero skipped an interval.
ROSTER_ROW_STATUSES = (OK, CONSTRAINT_VIOLATION)
ERROR_ROW_STATUSES = (
    "error",
    "objective_error",
    "constraint_error",
    "engine_error",
    "trial_error",
    "trial_timeout",
)
PRUNED_ROW_STATUS = "pruned"
PARTIAL_ROW_STATUS = "partial"

# The result-level ``return_blocks`` object of the pinned wire (dossier clause 3.3) declares the
# layout the rows of a part were reduced under. These are its keys.
RETURN_BLOCKS_CONTRACT = "pineforge-hpo-return-blocks/v1"
RETURN_BLOCKS_SERIES = "bar"
RETURN_BLOCKS_KEYS = (
    "contract",
    "series",
    "S",
    "K",
    "n_b",
    "boundary_time_ms",
    "numeric_build_identity",
)
# The declared object of a part is that object plus one key of this API, not of the wire: the
# caller states a text that names the dataset the rows of the part were reduced over. Contract
# clause 3.5 refuses a dataset that differs between parts, and the interval count and the time
# axis alone cannot tell two datasets of the same length apart. Exactly the keys of
# ``DECLARED_OBJECT_KEYS`` are read; any other key refuses.
DATASET_IDENTITY_KEY = "dataset_identity"
DECLARED_OBJECT_KEYS = RETURN_BLOCKS_KEYS + (DATASET_IDENTITY_KEY,)

TIE_RULES = {
    "equality": "exact_binary64",
    "train_best": "mixture_weight_1_over_k",
    "oos_rank": "mid_rank",
}

INTERPRETATION = (
    "A deterministic frequency over the re-labellings of one dataset into training and testing "
    "halves. The composition of the trial roster moves it. For adaptive or correlated trials it "
    "is a diagnostic, not a probability under independent strategy draws."
)

PAPER_CONFORMANT_SAMPLERS = ("grid", "random", "sobol")
DIAGNOSTIC_SAMPLERS = ("tpe", "dlib_global", "candidates")

# A required keyword of ``pbo`` that was not passed (None is a value: no early stop).
_UNSET = object()

CODES = (
    "pbo_input_invalid",
    "pbo_parameter_missing",
    "pbo_roster_mismatch",
    "pbo_return_blocks_mismatch",
    "pbo_block_sizes_uneven",
    "pbo_dataset_mismatch",
    "pbo_window_mismatch",
    "pbo_identity_mismatch",
    "pbo_rows_incomplete",
    "pbo_status_error",
    "pbo_status_pruned",
    "pbo_status_partial",
    "pbo_non_positive_equity",
    "pbo_odd_s",
    "pbo_too_few_trials",
    "pbo_zero_variance",
    "pbo_block_mismatch",
    "pbo_resource_exceeded",
    "pbo_sampler_unknown",
)

# When several kinds of row problems occur at once, the first kind in this order is reported
# (with every offending id of that kind), so the refusal does not depend on the row order.
# Every code that a row, or the comparison of the rows with the roster, can raise is listed.
_PRIORITY = (
    "pbo_input_invalid",
    "pbo_roster_mismatch",
    "pbo_status_error",
    "pbo_status_pruned",
    "pbo_status_partial",
    "pbo_non_positive_equity",
    "pbo_rows_incomplete",
    "pbo_block_mismatch",
)

# The fields in which the declared objects of two parts can differ (contract clause 3.5), with the
# code of each difference. K comes first: a different interval count is a different dataset (and
# changes n_b with it), and so is a different dataset identity; the same K with other counts is a
# different block layout. The contract, the series and S are not listed: every declared object
# must carry the fixed contract and series and the S of the call, so they cannot differ between
# parts that were accepted one by one.
_CONTEXT_FIELDS = (
    ("K", "pbo_dataset_mismatch"),
    (DATASET_IDENTITY_KEY, "pbo_dataset_mismatch"),
    ("n_b", "pbo_return_blocks_mismatch"),
    ("boundary_time_ms", "pbo_window_mismatch"),
    ("numeric_build_identity", "pbo_identity_mismatch"),
)
_COUNT_LIMIT = 1 << 53

Triple = tuple  # (count: int, sum: float, m2: float)


def _id_key(value: Any) -> tuple[bool, Any]:
    return (isinstance(value, str), value)


class PboError(Exception):
    """A refusal: a module-local ``pbo_`` code, one plain sentence, the offending trial ids and,
    when a call parameter is the offender, its name.

    ``code`` and ``ids`` are those of the first kind of problem in the fixed priority order.
    ``refusals`` names every kind that was found, in that order, each with its ``count`` and its
    offending ``ids`` (contract clause 3.5: counts per reason and offending ids), and
    ``coverage`` says how much of the declared roster the rows cover (``declared`` trials,
    ``supplied`` rows, ``usable`` declared trials whose row had no problem) when the rows were
    examined, else None; a refusal raised after the rows were accepted, such as a zero variance
    or a work figure above ``max_work``, carries it too. A refusal that was not found among rows
    has the one entry of its own code, counting its ids (or 1 when it has none).
    """

    def __init__(
        self,
        code: str,
        detail: str,
        ids: Iterable[Any] = (),
        names: Iterable[str] = (),
        *,
        coverage: dict[str, int] | None = None,
        refusals: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.ids = tuple(sorted(ids, key=_id_key))
        self.names = tuple(names)
        self.coverage = None if coverage is None else dict(coverage)
        if refusals is None:
            refusals = [{"code": code, "count": len(self.ids) or 1, "ids": self.ids}]
        self.refusals = tuple(
            {"code": item["code"], "count": item["count"], "ids": tuple(item["ids"])}
            for item in refusals
        )

    def document(self) -> dict[str, Any]:
        """The refusal as a JSON-ready document; it carries no number of the analysis."""
        return {
            "contract": CONTRACT,
            "status": "refused",
            "code": self.code,
            "detail": self.detail,
            "ids": list(self.ids),
            "names": list(self.names),
            "coverage": None if self.coverage is None else dict(self.coverage),
            "refusals": [
                {"code": item["code"], "count": item["count"], "ids": list(item["ids"])}
                for item in self.refusals
            ],
        }


class _RowProblem(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class MomentTable:
    """A validated N x S table of block moments; rows ascend by trial id.

    ``counts`` is common to every row and equals ``n_b`` of the declared object. ``sums[i][b]``
    and ``m2[i][b]`` belong to trial ``trial_ids[i]`` and block ``b``. ``return_blocks_json`` is
    the text of the declared object that every part declared.

    The table checks itself whenever it is built, whoever builds it: at least 2 trials, unique
    ascending ids of one type, arrays of the right lengths, counts equal to ``n_b``, finite
    moments, and the declared object itself (including the half-size rule). ``input_sha256`` is
    not an argument: it is computed from the content, so it cannot disagree with it. A refusal is
    a ``PboError``.
    """

    S: int
    trial_ids: tuple[Any, ...]
    counts: tuple[int, ...]
    sums: tuple[tuple[float, ...], ...]
    m2: tuple[tuple[float, ...], ...]
    return_blocks_json: str
    input_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        size = _check_s(self.S)
        context = _table_context(self.return_blocks_json, size)
        ids = _table_ids(self.trial_ids)
        counts = self.counts
        if not isinstance(counts, (list, tuple)):
            raise PboError("pbo_input_invalid", "counts must be an array")
        if len(counts) != size:
            raise PboError(
                "pbo_block_mismatch",
                f"counts has {len(counts)} entries, expected S={size}",
            )
        if any(type(count) is not int for count in counts):
            raise PboError("pbo_input_invalid", "every count must be an integer")
        if list(counts) != context["n_b"]:
            raise PboError(
                "pbo_block_mismatch", "the counts differ from return_blocks.n_b"
            )
        raw_sums = _table_matrix("sums", self.sums, len(ids), size)
        raw_m2 = _table_matrix("m2", self.m2, len(ids), size)
        sums: list[tuple[float, ...]] = []
        m2s: list[tuple[float, ...]] = []
        for position, trial_id in enumerate(ids):
            try:
                blocks = [
                    _check_block(
                        index,
                        counts[index],
                        raw_sums[position][index],
                        raw_m2[position][index],
                    )
                    for index in range(size)
                ]
            except _RowProblem as problem:
                raise PboError(
                    problem.code, f"trial {trial_id!r}: {problem.detail}", [trial_id]
                ) from None
            sums.append(tuple(total for total, _ in blocks))
            m2s.append(tuple(centred for _, centred in blocks))
        object.__setattr__(self, "S", size)
        object.__setattr__(self, "trial_ids", ids)
        object.__setattr__(self, "counts", tuple(counts))
        object.__setattr__(self, "sums", tuple(sums))
        object.__setattr__(self, "m2", tuple(m2s))
        object.__setattr__(
            self, "return_blocks_json", _canonical(context).decode("ascii")
        )
        object.__setattr__(self, "input_sha256", _input_digest(ids, counts, sums, m2s))


def _check_s(S: Any) -> int:
    if type(S) is not int:
        raise PboError("pbo_input_invalid", "S must be an integer")
    if S % 2 == 1:
        raise PboError("pbo_odd_s", f"S must be even, got {S}")
    if S < 2:
        raise PboError("pbo_input_invalid", f"S must be at least 2, got {S}")
    return S


def _check_early_stop(early_stop: Any) -> bool:
    """Whether the study had a no-improvement early stop (default D6); the caller must say.

    Pass the ``early_stop`` object of the result when it has one (or ``True``), and ``None`` (or
    ``False``) when it has none: the label depends on it, so there is no silent default.
    """
    if early_stop is _UNSET:
        raise PboError(
            "pbo_parameter_missing",
            "early_stop was not given: pass the result's early_stop object, or None when the "
            "result has none",
            names=("early_stop",),
        )
    if early_stop is None or early_stop is False:
        return False
    if early_stop is True or isinstance(early_stop, dict):
        return True
    raise PboError(
        "pbo_input_invalid",
        "early_stop must be None, a bool or the early_stop object of the result",
    )


def _check_budget(max_work: Any) -> None:
    if max_work is None:
        return
    if (
        type(max_work) not in (int, float)
        or (type(max_work) is float and math.isnan(max_work))
        or max_work < 0
    ):
        raise PboError(
            "pbo_input_invalid",
            "max_work must be None or a number that is not negative",
        )


def _finite_float(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _clip(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _declared_ids(roster: Any) -> tuple[Any, ...]:
    """The declared trial set (contract clause 3.5), validated and sorted.

    It is a required input: PBO decides from it whether the rows are the whole roster.
    """
    if roster is None:
        raise PboError(
            "pbo_parameter_missing",
            "no roster was given: pass the declared trial set of the lineage",
            names=("roster",),
        )
    if not isinstance(roster, (list, tuple, set, frozenset)):
        raise PboError("pbo_input_invalid", "roster must be a list of trial ids")
    ids = list(roster)
    for position, item in enumerate(ids):
        if type(item) is not int and type(item) is not str:
            raise PboError(
                "pbo_input_invalid",
                f"roster entry {position} is neither an integer nor a string",
            )
    if len({type(item) for item in ids}) > 1:
        raise PboError(
            "pbo_input_invalid", "the roster mixes integer and string trial ids"
        )
    repeated = [item for item, seen in Counter(ids).items() if seen > 1]
    if repeated:
        raise PboError(
            "pbo_input_invalid",
            f"{len(repeated)} trial id(s) occur more than once in the roster",
            repeated,
        )
    if len(ids) < 2:
        raise PboError(
            "pbo_too_few_trials",
            f"PBO needs at least 2 trials, the roster declares {len(ids)}",
        )
    return tuple(sorted(ids))


def _check_return_blocks(declared: Any, S: int) -> dict[str, Any]:
    """Validate the declared object of a part and return a normalised copy.

    The object is the result-level ``return_blocks`` object plus the dataset identity the caller
    states. It declares the layout the rows were reduced under: the contract, the series, the
    block count ``S``, the interval count ``K``, the interval count of every block ``n_b``, the
    ``S + 1`` boundary times in milliseconds, the numeric identity of the reducer build and the
    dataset identity. It is a required input (contract clause 3.5).
    """
    if declared is None:
        raise PboError(
            "pbo_parameter_missing",
            "no return_blocks object was given: pass the result-level object of the rows",
            names=("return_blocks",),
        )
    if not isinstance(declared, dict):
        raise PboError(
            "pbo_input_invalid", "return_blocks must be the result-level object"
        )
    absent = sorted(set(DECLARED_OBJECT_KEYS) - set(declared))
    unknown = sorted(str(key) for key in set(declared) - set(DECLARED_OBJECT_KEYS))
    if absent or unknown:
        raise PboError(
            "pbo_input_invalid",
            f"return_blocks lacks the keys {absent} and has the unknown keys {unknown}",
            names=absent + unknown,
        )
    if declared["contract"] != RETURN_BLOCKS_CONTRACT:
        raise PboError(
            "pbo_return_blocks_mismatch",
            f"return_blocks.contract is {_clip(declared['contract'])}, "
            f"not {RETURN_BLOCKS_CONTRACT!r}",
        )
    if declared["series"] != RETURN_BLOCKS_SERIES:
        raise PboError(
            "pbo_return_blocks_mismatch",
            f"return_blocks.series is {_clip(declared['series'])}; this version reads the "
            f"{RETURN_BLOCKS_SERIES!r} series only",
        )
    blocks = declared["S"]
    if type(blocks) is not int:
        raise PboError("pbo_input_invalid", "return_blocks.S must be an integer")
    if blocks != S:
        raise PboError(
            "pbo_return_blocks_mismatch",
            f"return_blocks declares S={blocks}, the call asks for S={S}",
        )
    intervals = declared["K"]
    if type(intervals) is not int or intervals < S:
        raise PboError(
            "pbo_input_invalid", f"return_blocks.K must be an integer of at least S={S}"
        )
    n_b = declared["n_b"]
    if not isinstance(n_b, (list, tuple)):
        raise PboError("pbo_input_invalid", "return_blocks.n_b is not an array")
    if len(n_b) != S:
        raise PboError(
            "pbo_block_mismatch",
            f"return_blocks.n_b has {len(n_b)} entries, expected S={S}",
        )
    for index, count in enumerate(n_b):
        if type(count) is not int or count < 1 or count >= _COUNT_LIMIT:
            raise PboError(
                "pbo_input_invalid",
                f"return_blocks.n_b[{index}] must be an integer from 1 to 2**53 - 1",
            )
    if sum(n_b) != intervals:
        raise PboError(
            "pbo_input_invalid",
            f"return_blocks.K is {intervals} but n_b adds up to {sum(n_b)}",
        )
    if sum(sorted(n_b)[: S // 2]) < 2:
        raise PboError(
            "pbo_input_invalid",
            "a half of the blocks could hold fewer than 2 intervals, so its sample deviation "
            "is undefined",
        )
    if max(n_b) - min(n_b) > 1:
        raise PboError(
            "pbo_block_sizes_uneven",
            f"the blocks hold from {min(n_b)} to {max(n_b)} intervals; default D2 accepts block "
            "sizes that differ by at most one interval",
        )
    times = declared["boundary_time_ms"]
    if not isinstance(times, (list, tuple)):
        raise PboError(
            "pbo_input_invalid", "return_blocks.boundary_time_ms is not an array"
        )
    if len(times) != S + 1:
        raise PboError(
            "pbo_block_mismatch",
            f"return_blocks.boundary_time_ms has {len(times)} entries, expected S + 1 = {S + 1}",
        )
    for index, moment in enumerate(times):
        if type(moment) is not int:
            raise PboError(
                "pbo_input_invalid",
                f"return_blocks.boundary_time_ms[{index}] must be an integer",
            )
        if index and moment < times[index - 1]:
            raise PboError(
                "pbo_input_invalid", "return_blocks.boundary_time_ms must not decrease"
            )
    identity = declared["numeric_build_identity"]
    if type(identity) is not str or not identity:
        raise PboError(
            "pbo_input_invalid",
            "return_blocks.numeric_build_identity must be a non-empty text",
        )
    dataset = declared[DATASET_IDENTITY_KEY]
    if type(dataset) is not str or not dataset:
        raise PboError(
            "pbo_input_invalid",
            f"return_blocks.{DATASET_IDENTITY_KEY} must be a non-empty text",
        )
    return {
        "contract": RETURN_BLOCKS_CONTRACT,
        "series": RETURN_BLOCKS_SERIES,
        "S": S,
        "K": intervals,
        "n_b": list(n_b),
        "boundary_time_ms": list(times),
        "numeric_build_identity": identity,
        DATASET_IDENTITY_KEY: dataset,
    }


def _part_differences(objects: Sequence[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """The fields in which the declared objects of the parts differ, as ``(key, code, detail)``.

    Every field of ``_CONTEXT_FIELDS`` is compared across all the parts, in that order. A field
    that is not the same in every part is reported once, naming part 0 and the first part that
    differs from it. Which fields differ does not depend on the order of the parts; only the
    detail, which names part positions, does.
    """
    found = []
    for key, code in _CONTEXT_FIELDS:
        for position in range(1, len(objects)):
            if objects[position][key] != objects[0][key]:
                found.append(
                    (
                        key,
                        code,
                        f"return_blocks.{key} differs between the parts: part 0 declares "
                        f"{_clip(objects[0][key])}, part {position} declares "
                        f"{_clip(objects[position][key])}",
                    )
                )
                break
    return found


def _check_parts(parts: Any, S: int) -> list[tuple[dict[str, Any], Any]]:
    """The parts as ``(validated declared object, rows)`` pairs, in the order given.

    A part is a ``(declared object, rows)`` pair: the result-level ``return_blocks`` object the
    rows of the part were reduced under, and the rows. A malformed object refuses at once (the
    first part, in the order given, whose object is malformed). Then the objects are compared
    with each other (contract clause 3.5: ``return_blocks``, dataset, window or identity must not
    differ between parts): a difference refuses before any row is read, with the code of the
    first field that differs, every field that differs in ``names`` and one entry per code in
    ``refusals``. It names no trial id, because no trial is the offender, and no coverage,
    because no row has been examined.
    """
    if parts is None:
        raise PboError(
            "pbo_parameter_missing",
            "no parts were given: pass a list of (declared object, rows) pairs, one for each part",
            names=("parts",),
        )
    if not isinstance(parts, (list, tuple)):
        raise PboError(
            "pbo_input_invalid", "parts must be a list of (declared object, rows) pairs"
        )
    checked: list[tuple[dict[str, Any], Any]] = []
    for position, part in enumerate(parts):
        if not isinstance(part, (list, tuple)) or len(part) != 2:
            raise PboError(
                "pbo_input_invalid",
                f"part {position} is not a (declared object, rows) pair",
            )
        try:
            declared = _check_return_blocks(part[0], S)
        except PboError as error:
            raise PboError(
                error.code, f"part {position}: {error.detail}", error.ids, error.names
            ) from None
        checked.append((declared, part[1]))
    differences = _part_differences([declared for declared, _ in checked])
    if differences:
        codes = Counter(code for _, code, _ in differences)
        raise PboError(
            differences[0][1],
            differences[0][2],
            names=[key for key, _, _ in differences],
            refusals=[
                {"code": code, "count": count, "ids": ()}
                for code, count in codes.items()
            ],
        )
    return checked


def _roster_difference(
    row_ids: Iterable[Any], declared: Iterable[Any]
) -> tuple[list[Any], list[Any]]:
    """The declared ids that have no row, and the row ids that are not declared (sorted)."""
    present = set(row_ids)
    expected = set(declared)
    return (
        sorted(expected - present, key=_id_key),
        sorted(present - expected, key=_id_key),
    )


def _roster_detail(
    missing: list[Any], unexpected: list[Any], repeated: list[Any]
) -> str:
    """The roster reasons: a gap, an id that is not declared and a duplicate (clause 3.5)."""
    parts = []
    if missing:
        parts.append(
            f"{len(missing)} declared trial(s) have no row (the first is {missing[0]!r})"
        )
    if unexpected:
        parts.append(
            f"{len(unexpected)} trial id(s) have a row but are not in the roster "
            f"(the first is {unexpected[0]!r})"
        )
    if repeated:
        parts.append(
            f"{len(repeated)} trial id(s) occur in more than one row "
            f"(the first is {repeated[0]!r})"
        )
    return "the rows are not the declared roster: " + "; ".join(parts)


def _check_block(
    index: int, count: Any, raw_sum: Any, raw_m2: Any
) -> tuple[float, float]:
    """One block's moments: an integer count from 1 to 2**53 - 1, a finite sum, and a finite
    ``m2`` that is not negative and is 0 for a block of one interval. Returns ``(sum, m2)``."""
    if type(count) is not int or count < 1 or count >= _COUNT_LIMIT:
        raise _RowProblem(
            "pbo_input_invalid",
            f"count[{index}] must be an integer from 1 to 2**53 - 1",
        )
    total = _finite_float(raw_sum)
    if total is None:
        raise _RowProblem("pbo_input_invalid", f"sum[{index}] must be a finite number")
    centred = _finite_float(raw_m2)
    if centred is None or centred < 0.0:
        raise _RowProblem(
            "pbo_input_invalid",
            f"m2[{index}] must be a finite number that is not negative",
        )
    if count == 1 and centred != 0.0:
        raise _RowProblem(
            "pbo_input_invalid", f"m2[{index}] must be 0 for a block of one interval"
        )
    return total, centred


def _input_digest(
    ids: Sequence[Any],
    counts: Sequence[int],
    sums: Sequence[Sequence[float]],
    m2s: Sequence[Sequence[float]],
) -> str:
    """The canonical input digest: SHA-256 over the sorted raw digests of the per-row JSON."""
    digests = [
        hashlib.sha256(
            _canonical([item, list(counts), list(row_sums), list(row_m2)])
        ).digest()
        for item, row_sums, row_m2 in zip(ids, sums, m2s)
    ]
    return hashlib.sha256(b"".join(sorted(digests))).hexdigest()


def _table_context(text: Any, S: int) -> dict[str, Any]:
    if not isinstance(text, str):
        raise PboError(
            "pbo_input_invalid",
            "return_blocks_json must be the JSON text of the object",
        )
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        raise PboError(
            "pbo_input_invalid", "return_blocks_json is not valid JSON"
        ) from None
    if not isinstance(document, dict):
        raise PboError(
            "pbo_input_invalid", "return_blocks_json is not the text of an object"
        )
    return _check_return_blocks(document, S)


def _table_ids(ids: Any) -> tuple[Any, ...]:
    if not isinstance(ids, (list, tuple)):
        raise PboError("pbo_input_invalid", "trial_ids must be an array of trial ids")
    if len(ids) < 2:
        raise PboError(
            "pbo_too_few_trials",
            f"PBO needs at least 2 trials, the table holds {len(ids)}",
        )
    for position, item in enumerate(ids):
        if type(item) is not int and type(item) is not str:
            raise PboError(
                "pbo_input_invalid",
                f"trial_ids[{position}] is neither an integer nor a string",
            )
    if len({type(item) for item in ids}) > 1:
        raise PboError("pbo_input_invalid", "the trial ids mix integers and strings")
    if any(not left < right for left, right in zip(ids, ids[1:])):
        raise PboError(
            "pbo_input_invalid", "the trial ids must be unique and ascending"
        )
    return tuple(ids)


def _table_matrix(name: str, matrix: Any, trials: int, size: int) -> list[Any]:
    if not isinstance(matrix, (list, tuple)):
        raise PboError("pbo_input_invalid", f"{name} must be an array of rows")
    if len(matrix) != trials:
        raise PboError(
            "pbo_block_mismatch", f"{name} has {len(matrix)} rows, expected N={trials}"
        )
    for position, row in enumerate(matrix):
        if not isinstance(row, (list, tuple)):
            raise PboError("pbo_input_invalid", f"{name}[{position}] is not an array")
        if len(row) != size:
            raise PboError(
                "pbo_block_mismatch",
                f"{name}[{position}] has {len(row)} entries, expected S={size}",
            )
    return list(matrix)


def _parse_row(
    row: dict[str, Any], S: int, declared: dict[str, Any]
) -> tuple[tuple[int, ...], tuple[float, ...], tuple[float, ...]]:
    status = row.get("status")
    if status in ERROR_ROW_STATUSES:
        raise _RowProblem(
            "pbo_status_error", f"the row status is {status!r}: the trial failed"
        )
    if status == PRUNED_ROW_STATUS:
        raise _RowProblem(
            "pbo_status_pruned",
            "the row status is 'pruned': the trial was stopped early",
        )
    if status == PARTIAL_ROW_STATUS:
        raise _RowProblem(
            "pbo_status_partial",
            "the row status is 'partial': the trial did not run to its end",
        )
    if status not in ROSTER_ROW_STATUSES:
        raise _RowProblem(
            "pbo_rows_incomplete",
            f"the row status is {status!r}, not 'ok' or 'constraint_violation'",
        )
    block = row.get("block_returns")
    if not isinstance(block, dict):
        raise _RowProblem(
            "pbo_rows_incomplete", "the row carries no block_returns payload"
        )
    block_status = block.get("status")
    if block_status == NON_POSITIVE_EQUITY:
        raise _RowProblem(
            "pbo_non_positive_equity",
            "the block_returns status is 'non_positive_equity': an equity at or below zero "
            "skipped an interval",
        )
    if block_status != OK:
        raise _RowProblem(
            "pbo_rows_incomplete",
            f"the block_returns status is {block_status!r}, not 'ok'",
        )
    arrays = []
    for name in ("count", "sum", "m2"):
        values = block.get(name)
        if not isinstance(values, (list, tuple)):
            raise _RowProblem(
                "pbo_input_invalid", f"block_returns.{name} is not an array"
            )
        if len(values) != S:
            raise _RowProblem(
                "pbo_block_mismatch",
                f"block_returns.{name} has {len(values)} entries, expected S={S}",
            )
        arrays.append(values)
    raw_counts, raw_sums, raw_m2 = arrays
    counts = []
    sums = []
    m2s = []
    for index in range(S):
        count = raw_counts[index]
        total, centred = _check_block(index, count, raw_sums[index], raw_m2[index])
        counts.append(count)
        sums.append(total)
        m2s.append(centred)
    if counts != declared["n_b"]:
        index = next(item for item in range(S) if counts[item] != declared["n_b"][item])
        raise _RowProblem(
            "pbo_block_mismatch",
            f"count[{index}] is {counts[index]} where return_blocks.n_b[{index}] is "
            f"{declared['n_b'][index]}",
        )
    return tuple(counts), tuple(sums), tuple(m2s)


def load_moment_table(parts: Any, *, S: int, roster: Any = None) -> MomentTable:
    """Validate the parts (contract clause 3.5) and return the merged table sorted by trial id.

    ``parts`` is a list of ``(declared object, rows)`` pairs, one for each part of the lineage (a
    call with one part passes a list of one pair): the declared object is the result-level
    ``return_blocks`` object the rows of that part were reduced under, plus the
    ``dataset_identity`` that the caller states. ``roster`` is the declared trial set of the
    lineage. Both are required. The declared objects of all the parts must be
    one and the same, every row's counts must equal the ``n_b`` of its own part's object, and the
    rows of all the parts together must be exactly the roster. Refuses with ``PboError`` and
    never subsets: one bad row refuses the whole roster.
    """
    S = _check_s(S)
    declared_ids = _declared_ids(roster)
    return _load_parts(parts, S, declared_ids)


def _load_parts(parts: Any, S: int, declared_ids: tuple[Any, ...]) -> MomentTable:
    checked = _check_parts(parts, S)
    entries: list[tuple[int, Any]] = []
    ids: list[Any] = []
    for part, (_, rows) in enumerate(checked):
        if not isinstance(rows, (list, tuple)):
            raise PboError(
                "pbo_input_invalid",
                f"the rows of part {part} must be a list of row objects",
            )
        for position, row in enumerate(rows):
            if not isinstance(row, dict) or "trial_id" not in row:
                raise PboError(
                    "pbo_input_invalid",
                    f"row {position} of part {part} is not an object with a trial_id",
                )
            trial_id = row["trial_id"]
            if type(trial_id) is not int and type(trial_id) is not str:
                raise PboError(
                    "pbo_input_invalid",
                    f"row {position} of part {part} has a trial_id that is neither an integer "
                    "nor a string",
                )
            entries.append((part, row))
            ids.append(trial_id)
    if len({type(item) for item in ids}) > 1:
        raise PboError("pbo_input_invalid", "the trial ids mix integers and strings")
    repeated = sorted(
        (item for item, seen in Counter(ids).items() if seen > 1), key=_id_key
    )
    order = sorted(range(len(entries)), key=ids.__getitem__)

    failures: dict[str, list[tuple[Any, str]]] = {}
    parsed: list[tuple[Any, tuple[int, ...], tuple[float, ...], tuple[float, ...]]] = []
    for index in order:
        part, row = entries[index]
        try:
            counts, sums, m2s = _parse_row(row, S, checked[part][0])
        except _RowProblem as problem:
            detail = (
                problem.detail
                if len(checked) == 1
                else f"{problem.detail} (part {part})"
            )
            failures.setdefault(problem.code, []).append((ids[index], detail))
            continue
        parsed.append((ids[index], counts, sums, m2s))
    # The roster reasons (contract clause 3.5: gap, duplicate, absent or unknown part) are one
    # code, with one entry for every trial id that has any of them.
    missing, unexpected = _roster_difference(ids, declared_ids)
    roster_ids = sorted({*missing, *unexpected, *repeated}, key=_id_key)
    if roster_ids:
        failures["pbo_roster_mismatch"] = [
            (item, "differs from the roster") for item in roster_ids
        ]
    stray = set(failures) - set(_PRIORITY)
    if stray:
        raise RuntimeError(
            f"internal accounting error: unlisted refusal codes {sorted(stray)}"
        )
    refusals = [
        {
            "code": code,
            "count": len(failures[code]),
            "ids": tuple(sorted((item for item, _ in failures[code]), key=_id_key)),
        }
        for code in _PRIORITY
        if failures.get(code)
    ]
    if refusals:
        code = refusals[0]["code"]
        found = failures[code]
        if code == "pbo_roster_mismatch":
            detail = _roster_detail(missing, unexpected, repeated)
        else:
            detail = (
                f"{len(found)} of {len(entries)} rows refused; the first is trial "
                f"{found[0][0]!r}: {found[0][1]}"
            )
        declared_set = set(declared_ids)
        repeated_set = set(repeated)
        coverage = {
            "declared": len(declared_ids),
            "supplied": len(entries),
            "usable": sum(
                1
                for item, _, _, _ in parsed
                if item in declared_set and item not in repeated_set
            ),
        }
        raise PboError(
            code,
            detail,
            [item for item, _ in found],
            coverage=coverage,
            refusals=refusals,
        )

    # With no refusal above there is at least one part: no part at all leaves every declared
    # trial (the roster holds at least 2) without a row, which is a roster mismatch.
    declared = checked[0][0]
    return MomentTable(
        S=S,
        trial_ids=tuple(item for item, _, _, _ in parsed),
        counts=tuple(declared["n_b"]),
        sums=tuple(sums for _, _, sums, _ in parsed),
        m2=tuple(m2s for _, _, _, m2s in parsed),
        return_blocks_json=_canonical(declared).decode("ascii"),
    )


def merge_moments(a: Triple, b: Triple) -> Triple:
    """Merge two ``(count, sum, m2)`` triples, ``a`` before ``b``, by the Chan/Pebay rule.

    ``M2 = M2a + M2b + d*d*na*nb/n`` with ``d = sum_b/nb - sum_a/na``. The operation order is
    pinned: ``(((d*d)*na)*nb)/n`` is formed first and added to ``(M2a + M2b)``; each operation
    is a separate binary64 step, so nothing is fused. The term added is never negative, hence
    neither is the result.
    """
    na, sa, ma = a
    nb, sb, mb = b
    n = na + nb
    d = sb / nb - sa / na
    t = d * d
    t = t * na
    t = t * nb
    t = t / n
    return n, sa + sb, (ma + mb) + t


def pooled_moments(blocks: Iterable[Triple]) -> Triple:
    """Merge the triples left to right in the order given (block order); the fold is pinned."""
    iterator = iter(blocks)
    try:
        n, s, m2 = next(iterator)
    except StopIteration:
        raise PboError("pbo_input_invalid", "there are no blocks to pool") from None
    pooled = (n, s, m2)
    for block in iterator:
        pooled = merge_moments(pooled, block)
    return pooled


def ranking_statistic(n: int, s: float, m2: float) -> float | None:
    """Pooled mean over sample sigma: ``(s/n) / sqrt(m2/(n-1))``; risk-free 0, unannualised.

    Returns None when the variance is exactly zero in binary64 (the caller refuses; a flat
    trial is never imputed). Refuses a count below 2 or a non-finite value.
    """
    if n < 2:
        raise PboError(
            "pbo_input_invalid", "the sample deviation needs at least 2 intervals"
        )
    if not (math.isfinite(s) and math.isfinite(m2)):
        raise PboError("pbo_input_invalid", "the pooled moments are not finite")
    if not m2 > 0.0:
        return None
    sigma = math.sqrt(m2 / (n - 1))
    if not sigma > 0.0:
        return None
    statistic = (s / n) / sigma
    if not math.isfinite(statistic):
        raise PboError("pbo_input_invalid", "the ranking statistic is not finite")
    return statistic


def estimate_work(S: int, N: int) -> dict[str, int]:
    """The admission figure of contract clause 3.7, exact integers, before any split is run.

    ``n_splits = C(S, S/2)``. Each complementary pair is evaluated once, so the merges are
    ``(n_splits/2) * N * (S - 2)``; the comparisons are ``2 * n_splits * N`` (a count of the
    selected trial's rank, not a sort). ``work`` is their sum.
    """
    S = _check_s(S)
    if type(N) is not int or N < 0:
        raise PboError("pbo_input_invalid", "N must be an integer that is not negative")
    n_splits = math.comb(S, S // 2)
    merges = (n_splits // 2) * N * (S - 2)
    comparisons = 2 * n_splits * N
    return {
        "n_splits": n_splits,
        "merges": merges,
        "comparisons": comparisons,
        "work": merges + comparisons,
    }


def roster_label(sampler: Any, *, early_stop: Any = _UNSET) -> str:
    """The roster label of default D6; an unknown or missing sampler refuses.

    ``diagnostic_adaptive_roster`` for an adaptive sampler and for any sampler whose study had a
    no-improvement early stop; ``paper_conformant`` for ``grid``, ``random`` and ``sobol`` without
    one. The early stop is not a sampler: it is a separate object of the result, so it is its own
    argument and ``"early_stop"`` is not a sampler name. As for ``pbo``, the caller must say
    whether the study had one (the result's ``early_stop`` object or ``True``; ``None`` or
    ``False`` when it had none): leaving it out refuses with ``pbo_parameter_missing``, so a study
    with an early stop cannot get ``paper_conformant`` by a default.
    """
    stopped = _check_early_stop(early_stop)
    if isinstance(sampler, str):
        if sampler in DIAGNOSTIC_SAMPLERS:
            return "diagnostic_adaptive_roster"
        if sampler in PAPER_CONFORMANT_SAMPLERS:
            return "diagnostic_adaptive_roster" if stopped else "paper_conformant"
    raise PboError(
        "pbo_sampler_unknown",
        f"sampler {sampler!r} is not one of "
        f"{', '.join(PAPER_CONFORMANT_SAMPLERS + DIAGNOSTIC_SAMPLERS)}",
    )


def _partitions(S: int) -> Iterator[tuple[tuple[int, ...], tuple[int, ...]]]:
    """Every unordered split of the blocks into two halves, once: first holds block 0."""
    for rest in combinations(range(1, S), S // 2 - 1):
        first = (0, *rest)
        taken = set(first)
        yield first, tuple(block for block in range(S) if block not in taken)


def _ordered_splits(S: int) -> Iterator[tuple[tuple[int, ...], tuple[int, ...]]]:
    for first, second in _partitions(S):
        yield first, second
        yield second, first


def iter_ordered_splits(S: int) -> Iterator[tuple[tuple[int, ...], tuple[int, ...]]]:
    """Yield every ordered ``(train, test)`` split: C(S, S/2) of them.

    Train is a size-S/2 subset of the blocks in original order and test is its complement.
    Order: the half that holds block 0, in lexicographic order, as train first and then as test.
    """
    return _ordered_splits(_check_s(S))


def _half_statistics(
    table: MomentTable, blocks: tuple[int, ...], coverage: dict[str, int]
) -> list[float]:
    """The ranking statistic of every trial over the union of ``blocks``, in trial order.

    A refusal here is raised after every row was accepted, so it carries ``coverage``.
    """
    counts = table.counts
    head = blocks[0]
    flat: list[Any] = []
    values: list[float] = []
    for index, trial_id in enumerate(table.trial_ids):
        sums = table.sums[index]
        m2s = table.m2[index]
        pooled = (counts[head], sums[head], m2s[head])
        for block in blocks[1:]:
            pooled = merge_moments(pooled, (counts[block], sums[block], m2s[block]))
        try:
            value = ranking_statistic(*pooled)
        except PboError as error:
            raise PboError(
                error.code,
                f"{error.detail} (over blocks {list(blocks)})",
                (trial_id,),
                coverage=coverage,
            ) from None
        if value is None:
            flat.append(trial_id)
        else:
            values.append(value)
    if flat:
        raise PboError(
            "pbo_zero_variance",
            f"{len(flat)} trial(s) have zero sample variance over blocks {list(blocks)}",
            flat,
            coverage=coverage,
        )
    return values


def _exact_count(weights: dict[int, int]) -> Fraction:
    return sum((Fraction(count, k) for k, count in weights.items()), Fraction(0))


def _count_value(count: Fraction) -> int | float:
    return int(count) if count.denominator == 1 else float(count)


def pbo(
    parts: Any,
    *,
    S: int,
    roster: Any = None,
    max_work: int | float | None = None,
    sampler: str | None = None,
    early_stop: Any = _UNSET,
    with_lambdas: bool = False,
) -> dict[str, Any]:
    """Run CSCV over every ordered split and return the result object (contract clause 3.6).

    ``parts`` is a list of ``(declared object, rows)`` pairs, one for each part of the lineage (a
    call with one part passes a list of one pair): the declared object is the result-level
    ``return_blocks`` object the rows of that part were reduced under, plus the
    ``dataset_identity`` that the caller states, and the rows are those that
    ``load_moment_table`` takes. A table is not accepted: it has lost its parts.
    ``roster`` (the declared trial set of the lineage) is required: the call refuses unless the
    declared objects of all the parts are one and the same, every row's counts equal the ``n_b``
    of its own part's object, and the rows of all the parts together are exactly the roster
    (contract clause 3.5). Block sizes may differ by at most one interval (default D2); the
    result then says ``equal_blocks: false`` and names ``block_counts``, and a larger spread
    refuses.
    ``sampler`` and ``early_stop`` (the result's ``early_stop`` object, or None when it has none)
    are both required and decide the roster label of default D6.
    Per split, the training winner is the trial with the largest ranking statistic; k tied
    winners (exact binary64 equality) each carry weight 1/k. Its out-of-sample rank is the
    mid-rank among the N trials, ``omega = rank / (N + 1)`` and
    ``lambda = ln(omega / (1 - omega))``. The primary rate ``pbo`` is the share of splits with
    lambda <= 0 (default D3); the share with lambda < 0 is reported next to it. A refusal raises
    ``PboError``; nothing partial is returned.
    """
    S = _check_s(S)
    _check_budget(max_work)
    stopped = _check_early_stop(early_stop)
    label = roster_label(sampler, early_stop=stopped)
    declared_ids = _declared_ids(roster)
    table = _load_parts(parts, S, declared_ids)
    n_trials = len(table.trial_ids)
    # Every row was accepted and the rows are the roster, so the three numbers are N. A refusal
    # from here on carries them: its rows were examined.
    coverage = {"declared": n_trials, "supplied": n_trials, "usable": n_trials}
    work = estimate_work(S, n_trials)
    if max_work is not None and work["work"] > max_work:
        raise PboError(
            "pbo_resource_exceeded",
            f"the estimated work {work['work']} (n_splits {work['n_splits']}, N {n_trials}, "
            f"S {S}) exceeds max_work {max_work}",
            coverage=coverage,
        )
    n_splits = work["n_splits"]

    below: dict[int, int] = {}
    atom: dict[int, int] = {}
    above: dict[int, int] = {}
    lambdas: list[float] = []
    tie_sizes: list[int] = []
    total = n_trials + 1
    for first, second in _partitions(S):
        stats_first = _half_statistics(table, first, coverage)
        stats_second = _half_statistics(table, second, coverage)
        for train, test in ((stats_first, stats_second), (stats_second, stats_first)):
            best = max(train)
            winners = [index for index, value in enumerate(train) if value == best]
            k = len(winners)
            for index in winners:
                reference = test[index]
                less = 0
                equal = 0
                for value in test:
                    if value < reference:
                        less += 1
                    elif value == reference:
                        equal += 1
                # twice the mid-rank: omega = rank / (N + 1), odds = omega / (1 - omega).
                twice_rank = 2 * less + equal + 1
                if twice_rank < total:
                    bucket = below
                elif twice_rank == total:
                    bucket = atom
                else:
                    bucket = above
                bucket[k] = bucket.get(k, 0) + 1
                if with_lambdas:
                    lambdas.append(math.log(twice_rank / (2 * total - twice_rank)))
                    tie_sizes.append(k)

    n_lt0 = _exact_count(below)
    n_eq0 = _exact_count(atom)
    n_gt0 = _exact_count(above)
    if n_lt0 + n_eq0 + n_gt0 != n_splits:
        raise RuntimeError(
            "internal accounting error: the split counts do not add up to n_splits"
        )
    rate_le0 = float((n_lt0 + n_eq0) / n_splits)
    result: dict[str, Any] = {
        "contract": CONTRACT,
        "algorithm": ALGORITHM,
        "status": OK,
        "S": S,
        "N": n_trials,
        "n_splits": n_splits,
        "equal_blocks": len(set(table.counts)) == 1,
        "block_counts": list(table.counts),
        "statistic": STATISTIC,
        "tie_rules": dict(TIE_RULES),
        "n_lt0": _count_value(n_lt0),
        "n_eq0": _count_value(n_eq0),
        "n_gt0": _count_value(n_gt0),
        "pbo": rate_le0,
        "rate_le0": rate_le0,
        "rate_lt0": float(n_lt0 / n_splits),
        "calibrated": False,
        "interpretation": INTERPRETATION,
        "sampler": sampler,
        "early_stop": stopped,
        "roster_label": label,
        "work_estimate": work["work"],
        "input_sha256": table.input_sha256,
        "return_blocks_sha256": hashlib.sha256(
            table.return_blocks_json.encode("ascii")
        ).hexdigest(),
        "coverage": dict(coverage),
        "refusals": [],
    }
    if with_lambdas:
        result["lambdas"] = lambdas
        result["lambda_tie_sizes"] = tie_sizes
    return result


__all__ = [
    "ALGORITHM",
    "CODES",
    "CONSTRAINT_VIOLATION",
    "CONTRACT",
    "DATASET_IDENTITY_KEY",
    "DECLARED_OBJECT_KEYS",
    "DIAGNOSTIC_SAMPLERS",
    "ERROR_ROW_STATUSES",
    "INTERPRETATION",
    "MomentTable",
    "NON_POSITIVE_EQUITY",
    "PAPER_CONFORMANT_SAMPLERS",
    "PARTIAL_ROW_STATUS",
    "PRUNED_ROW_STATUS",
    "PboError",
    "RETURN_BLOCKS_CONTRACT",
    "RETURN_BLOCKS_KEYS",
    "RETURN_BLOCKS_SERIES",
    "ROSTER_ROW_STATUSES",
    "STATISTIC",
    "TIE_RULES",
    "estimate_work",
    "iter_ordered_splits",
    "load_moment_table",
    "merge_moments",
    "pbo",
    "pooled_moments",
    "ranking_statistic",
    "roster_label",
]
