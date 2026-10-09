#!/usr/bin/env python3
"""Shipped-CLI contracts for `--sampler sobol` (pineforge_sobol_gray64_joe_kuo_d6_v1), driven only
through the native binary and the fake strategy plugin.
Usage: test_sobol_cli.py NATIVE PLUGIN [unittest arguments].

Written from the AR pin (methods-sobol-contract.pin.md) and the amendment before the wiring
existed. UNEXECUTED until the proof phase. Independent oracle: this file implements the Gray-code
Sobol recurrence, the Joe-Kuo table recurrence (equation 2), the SplitMix64 digital shift and the
discrete and linear-real mappings with Python integers and IEEE floats, importing nothing from the
product; the direction-number table is read from the vendored bytes. The published 32-bit prefix
comes from the checked-in evidence TSV. Logarithmic dimensions are checked by range and by a
relative bound only: their bit-exact reference is the mapper's own fixture
(pineforge_hpo_sobol_mapper), because Python's exp is not correctly rounded.

A floating-point Sobol space (real, stepped-real, log-real or log-integer column) needs a build
whose Sobol numeric identity is bound. On an unbound build those tests assert the registered
refusal instead; with PFH_REQUIRE_SOBOL_IDENTITY=1 (proof configuration) an unbound build is a
failure. The literal off-byte comparison against the pinned release binary is mandatory when
PFH_SOBOL_PROOF=1: PFH_SOBOL_REFERENCE and PFH_SOBOL_REFERENCE_SHA256.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


NATIVE = Path(sys.argv.pop(1)).resolve()
PLUGIN = Path(sys.argv.pop(1)).resolve()
ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "third_party" / "sobol_joe_kuo" / "new-joe-kuo-6.21201.first1024"
GRAY_TSV = ROOT / "tests" / "fixtures" / "sobol" / "engine" / "vectors-gray-n0-31-d1-8.tsv"

MASK = (1 << 64) - 1
UINT64_MAX = MASK
IMPLEMENTATION = "pineforge_sobol_gray64_joe_kuo_d6_v1"
CONTRACT = "pineforge_sobol_v1"
MAPPER_CONTRACT = "pineforge_sobol_mapper_v1"
TABLE_SUBSET_SHA256 = "52eeb57738dd69f5e1f593f2085c4efb3fb410c3af241d5396d7a2bef60b9257"
TABLE_UPSTREAM_SHA256 = "68eedd2a4e3b659b9695e7aff0f8ac68718bcf620730fc3d3a8c65df2a067441"
NUMERIC_PREFIX = "portable-sobol-v1"
REQUIRE_IDENTITY = os.environ.get("PFH_REQUIRE_SOBOL_IDENTITY") == "1"
PROOF = os.environ.get("PFH_SOBOL_PROOF") == "1"
EXPECTED_BLOCK_KEYS = {"contract", "table", "word_bits", "scramble", "seed", "columns",
                       "numeric_build_identity", "mapper", "identity", "first_index",
                       "next_index", "exact_stream"}


# ---------------------------------------------------------------------------------------------
# Independent oracle
# ---------------------------------------------------------------------------------------------

def _load_table():
    rows = {}
    for line in TABLE.read_text().splitlines()[1:]:
        parts = line.split()
        dimension, degree, polynomial = int(parts[0]), int(parts[1]), int(parts[2])
        rows[dimension] = (degree, polynomial, [int(item) for item in parts[3:3 + degree]])
    return rows


TABLE_ROWS = _load_table()
_DIRECTIONS: dict[int, list[int]] = {}


def directions(column: int) -> list[int]:
    """V_1..V_64 of Sobol dimension column + 1 (equations (2) of the authors' notes)."""
    if column not in _DIRECTIONS:
        dimension = column + 1
        if dimension == 1:
            m = [1] * 64
        else:
            degree, polynomial, initial = TABLE_ROWS[dimension]
            m = list(initial)
            for k in range(degree + 1, 65):
                value = (m[k - degree - 1] << degree) ^ m[k - degree - 1]
                for j in range(1, degree):
                    if (polynomial >> (degree - 1 - j)) & 1:
                        value ^= m[k - j - 1] << j
                m.append(value)
        _DIRECTIONS[column] = [m[k - 1] << (64 - k) for k in range(1, 65)]
    return _DIRECTIONS[column]


def gray_word(index: int, column: int) -> int:
    gray = index ^ (index >> 1)
    word = 0
    for k, direction in enumerate(directions(column)):
        if (gray >> k) & 1:
            word ^= direction
    return word


def shift_word(seed: int, column: int) -> int:
    z = (seed + (column + 1) * 0x9E3779B97F4A7C15) & MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
    return z ^ (z >> 31)


def coordinate(index: int, column: int, seed: int, scramble: str) -> int:
    word = gray_word(index, column)
    return word ^ shift_word(seed, column) if scramble == "digital_shift" else word


class Dim:
    """One search dimension: its CLI flags and its independent mapping."""

    def __init__(self, name, kind, *args):
        self.name, self.kind, self.args = name, kind, args

    @property
    def varying(self) -> bool:
        return True

    def flags(self) -> list[str]:
        if self.kind == "bool":
            return ["--bool-dim", self.name]
        if self.kind == "int":
            low, high, step = self.args
            return ["--int-dim", self.name, str(low), str(high), str(step)]
        if self.kind == "cat":
            return [item for choice in self.args[0]
                    for item in ("--categorical-choice", self.name, choice)]
        if self.kind == "linear":
            low, high = self.args
            return ["--real-dim", self.name, str(low), str(high), "continuous"]
        if self.kind == "stepped":
            low, high, step = self.args
            return ["--real-dim", self.name, str(low), str(high), str(step)]
        if self.kind == "log":
            low, high = self.args
            return ["--log-real-dim", self.name, str(low), str(high)]
        raise AssertionError(self.kind)

    def floating(self) -> bool:
        return self.kind in ("linear", "stepped", "log")

    def value(self, word: int):
        if self.kind == "bool":
            return ((word * 2) >> 64) == 1
        if self.kind == "int":
            low, high, step = self.args
            count = (high - low) // step + 1
            return low + ((word * count) >> 64) * step
        if self.kind == "cat":
            return self.args[0][(word * len(self.args[0])) >> 64]
        unit = (word >> 11) * (2.0 ** -53)
        if self.kind == "linear":
            low, high = self.args
            return min(max(low + (high - low) * unit, low), high)
        if self.kind == "stepped":
            low, high, step = self.args
            count = int((high - low) / step) + 1
            return ((word * count) >> 64) * step + low
        return None  # log: range-checked only


class Space:
    def __init__(self, *dims: Dim):
        self.dims = sorted(dims, key=lambda item: item.name.encode())

    def flags(self) -> list[str]:
        # Declaration order is irrelevant to Sobol; keep one fixed order for the CLI.
        return [flag for dim in self.dims for flag in dim.flags()]

    @property
    def columns(self) -> list[str]:
        return [dim.name for dim in self.dims]

    @property
    def floating(self) -> bool:
        return any(dim.floating() for dim in self.dims)

    def candidate(self, index: int, seed: int, scramble: str) -> dict:
        values = {}
        for column, dim in enumerate(self.dims):
            values[dim.name] = dim.value(coordinate(index, column, seed, scramble))
        return values


DISCRETE = Space(Dim("Length", "int", 1, 1000, 1), Dim("Fast", "bool"),
                 Dim("Mode", "cat", ["fast", "slow", "medium"]))
NO_STRING = Space(Dim("Length", "int", 1, 1000, 1), Dim("Fast", "bool"),
                  Dim("Window", "int", 3, 50, 3))
TINY = Space(Dim("Fast", "bool"), Dim("Slow", "bool"))
FLOAT = Space(Dim("Length", "int", 1, 1000, 1), Dim("Fast", "bool"),
              Dim("Mode", "cat", ["fast", "slow", "medium"]), Dim("Level", "linear", 0, 10),
              Dim("Mult", "stepped", 0, 1, 0.25), Dim("Scale", "log", 0.001, 1000))
EMPTY = Space()
FLOAT_NO_STRING = Space(Dim("Level", "linear", 0, 10), Dim("Fast", "bool"))
PUBLISHED = Space(*[Dim(f"d{index}", "linear", 0, 1) for index in range(1, 9)])
PUBLISHED_BOOL = Space(*[Dim(f"d{index}", "bool") for index in range(1, 9)])


def canonical_identity(block, space_hash, numeric) -> str:
    """The documented identity hash, recomputed from the result's own fields."""
    document = {
        "columns": block["columns"], "contract": CONTRACT, "implementation": IMPLEMENTATION,
        "mapper_contract": MAPPER_CONTRACT, "mapper_revision": block["mapper"]["revision"],
        "numeric_build_identity": numeric, "scramble": block["scramble"], "seed": block["seed"],
        "space_hash": space_hash, "table_subset_sha256": TABLE_SUBSET_SHA256, "word_bits": 64,
    }
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def supplied_reference(required: bool):
    path = os.environ.get("PFH_SOBOL_REFERENCE")
    expected = os.environ.get("PFH_SOBOL_REFERENCE_SHA256")
    if not path or not expected:
        if required:
            raise AssertionError("proof mode needs PFH_SOBOL_REFERENCE and "
                                 "PFH_SOBOL_REFERENCE_SHA256 (the pinned release binary)")
        return None
    reference = Path(path).resolve()
    actual = hashlib.sha256(reference.read_bytes()).hexdigest()
    if actual != expected.lower():
        raise AssertionError(f"reference binary digest {actual} is not the supplied {expected}")
    return reference


_IDENTITY_STATE: dict[str, object] = {}


def identity_bound() -> bool:
    """Whether this build's Sobol numeric identity is bound (probed once, black box)."""
    if "bound" not in _IDENTITY_STATE:
        with tempfile.TemporaryDirectory(prefix="pf_sobol_probe_") as directory:
            csv = Path(directory) / "bars.csv"
            csv.write_text("timestamp,open,high,low,close,volume\n"
                           + "".join(f"{1700000000000 + i * 60000},100,102,99,101,10\n"
                                     for i in range(4)), encoding="utf-8")
            probe = subprocess.run(
                [str(NATIVE), "run", "--strategy", str(PLUGIN), "--ohlcv", str(csv), "--objective",
                 "metrics.all.net_profit", "--input-tf", "1", "--script-tf", "5",
                 "--chart-timezone", "UTC", "--fixed-input", "BatchPrefixTest", "1",
                 "--real-dim", "Level", "0", "1", "continuous", "--sampler", "sobol",
                 "--max-trials", "1"], text=True, capture_output=True, check=False, timeout=120)
        try:
            failure = json.loads(probe.stdout).get("failure") or {}
        except ValueError:
            failure = {}
        _IDENTITY_STATE["bound"] = probe.returncode == 0
        _IDENTITY_STATE["refusal"] = failure
        _IDENTITY_STATE["stderr"] = probe.stderr
    return bool(_IDENTITY_STATE["bound"])


class SobolCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pf_sobol_cli_")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.csv = self.directory / "bars.csv"
        self.csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            + "".join(f"{1700000000000 + index * 60000},100,102,99,101,10\n"
                      for index in range(64)),
            encoding="utf-8")

    # ---- helpers ---------------------------------------------------------------------------

    def argv(self, space, *extra, sampler="sobol", native=None, strategy=None, jitter=True):
        command = [
            str(native or NATIVE), "run", "--strategy", str(strategy or PLUGIN),
            "--ohlcv", str(self.csv), "--objective", "metrics.all.net_profit",
            "--input-tf", "1", "--script-tf", "5", "--chart-timezone", "Asia/Taipei",
            "--bar-magnifier", "true", "--magnifier-samples", "6",
            "--magnifier-distribution", "triangle", "--fixed-input", "BatchPrefixTest", "1",
            *( ["--fixed-input", "BatchPrefixJitter", "0"] if jitter else []),
            *space.flags(), "--sampler", sampler,
        ]
        return command + [str(item) for item in extra]

    def run_cli(self, space, *extra, expected=0, pass_fds=(), **options):
        completed = subprocess.run(self.argv(space, *extra, **options), text=True,
                                   capture_output=True, check=False, pass_fds=pass_fds,
                                   timeout=1800)
        if expected is not None:
            self.assertEqual(completed.returncode, expected, completed.stderr)
        return completed

    def needs_identity(self, space) -> None:
        if space.floating and not identity_bound():
            if REQUIRE_IDENTITY:
                self.fail("the Sobol numeric identity is unbound but is required: "
                          + str(_IDENTITY_STATE.get("stderr")))
            self.skipTest("the Sobol numeric identity is unbound in this build; the refusal is "
                          "asserted by test_unbound_identity_refuses_floating_spaces")

    def result(self, space, *extra, **options):
        self.needs_identity(space)
        return json.loads(self.run_cli(space, *extra, **options).stdout)

    def write_json(self, name, document) -> Path:
        path = self.directory / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def assert_refusal(self, completed, code, exit_code, reason=None):
        self.assertEqual(completed.returncode, exit_code, completed.stderr)
        document = json.loads(completed.stdout)
        self.assertFalse(document["ok"])
        self.assertNotIn("trials", document)
        failure = document["failure"]
        self.assertEqual(failure["code"], code, completed.stderr)
        if reason is not None:
            self.assertEqual(failure["args"], {"reason": reason}, completed.stderr)
        self.assertEqual(failure["exit_code"], exit_code)
        for token in (str(self.directory), self.directory.name):
            self.assertNotIn(token, completed.stdout + completed.stderr)
        return failure

    def check_rows(self, result, space, seed, scramble, first=0, count=None):
        rows = result["trials"]
        count = len(rows) if count is None else count
        self.assertEqual([row["trial_id"] for row in rows], list(range(first, first + count)))
        for row in rows:
            expected = space.candidate(row["trial_id"], seed, scramble)
            for dim in space.dims:
                if dim.kind == "log":
                    low, high = dim.args
                    self.assertTrue(low <= row["parameters"][dim.name] <= high)
                    continue
                self.assertEqual(row["parameters"][dim.name], expected[dim.name],
                                 f"id {row['trial_id']} {dim.name}")

    def check_block(self, result, space, seed, scramble, first, next_index, exact):
        block = result["sobol"]
        self.assertEqual(set(block), EXPECTED_BLOCK_KEYS)
        self.assertEqual(block["contract"], CONTRACT)
        self.assertEqual(block["table"], {"name": "new-joe-kuo-6.21201",
                                          "subset_sha256": TABLE_SUBSET_SHA256,
                                          "upstream_sha256": TABLE_UPSTREAM_SHA256})
        self.assertEqual(block["word_bits"], 64)
        self.assertEqual(block["scramble"], scramble)
        self.assertEqual(block["seed"], str(seed) if scramble == "digital_shift" else None)
        self.assertEqual(block["columns"], space.columns)
        self.assertEqual(block["mapper"]["contract"], MAPPER_CONTRACT)
        self.assertIsInstance(block["mapper"]["revision"], int)
        self.assertEqual((block["first_index"], block["next_index"]),
                         (str(first), str(next_index)))
        self.assertIs(block["exact_stream"], exact)
        numeric = block["numeric_build_identity"]
        if space.floating:
            self.assertTrue(numeric.startswith(NUMERIC_PREFIX), numeric)
            self.assertNotIn("flags_sha256:unavailable", numeric)
        else:
            self.assertIsNone(numeric)
        self.assertEqual(block["identity"],
                         canonical_identity(block, result["space_hash"], numeric))
        self.assertEqual(result["sampler"], "sobol")
        self.assertEqual(result["sampler_implementation"], IMPLEMENTATION)

    # ---- published vectors and the independent oracle ------------------------------------------

    def test_published_prefix_through_the_shipped_cli(self) -> None:
        published = []
        for line in GRAY_TSV.read_text().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            fields = line.split()
            published.append([int(word, 16) for word in fields[1:]])
        self.assertEqual(len(published), 32)
        # Discrete columns: a Boolean is the top bit of the word (no floating-point identity).
        result = json.loads(self.run_cli(PUBLISHED_BOOL, "--sobol-scramble", "none",
                                         "--max-trials", "32").stdout)
        for row in result["trials"]:
            words = published[row["trial_id"]]
            for column in range(8):
                self.assertEqual(row["parameters"][f"d{column + 1}"], words[column] >= 2**31)
        self.check_block(result, PUBLISHED_BOOL, 0, "none", 0, 32, True)
        # Continuous columns: u = X / 2^32 exactly, the published 32-bit words shifted by 32.
        self.needs_identity(PUBLISHED)
        result = json.loads(self.run_cli(PUBLISHED, "--sobol-scramble", "none",
                                         "--max-trials", "32").stdout)
        for row in result["trials"]:
            words = published[row["trial_id"]]
            for column in range(8):
                self.assertEqual(row["parameters"][f"d{column + 1}"], words[column] / 2**32)
        self.check_block(result, PUBLISHED, 0, "none", 0, 32, True)
        # First rows by hand: dimension 1 is 0, 1/2, 3/4, 1/4, 3/8, 7/8, 5/8, 1/8.
        self.assertEqual([row["parameters"]["d1"] for row in result["trials"][:8]],
                         [0.0, 0.5, 0.75, 0.25, 0.375, 0.875, 0.625, 0.125])

    def test_rows_equal_the_independent_oracle_for_every_scramble_and_seed(self) -> None:
        for seed in (0, 1, 7, 2**53 + 1, 2**63, UINT64_MAX):
            for scramble in ("digital_shift", "none"):
                with self.subTest(seed=seed, scramble=scramble):
                    result = self.result(DISCRETE, "--seed", seed, "--sobol-scramble", scramble,
                                         "--max-trials", 40)
                    self.check_rows(result, DISCRETE, seed, scramble)
                    self.check_block(result, DISCRETE, seed, scramble, 0, 40, True)
                    self.assertEqual(result["seed"], seed)

    def test_the_none_scramble_ignores_the_seed(self) -> None:
        first = self.result(DISCRETE, "--seed", 1, "--sobol-scramble", "none", "--max-trials", 12)
        second = self.result(DISCRETE, "--seed", 999, "--sobol-scramble", "none",
                             "--max-trials", 12)
        self.assertEqual(first["trials"], second["trials"])
        self.assertEqual(first["sobol"]["identity"], second["sobol"]["identity"])
        self.assertIsNone(first["sobol"]["seed"])
        shifted = self.result(DISCRETE, "--seed", 999, "--max-trials", 12)
        self.assertNotEqual(shifted["trials"], first["trials"])
        self.assertNotEqual(shifted["sobol"]["identity"], first["sobol"]["identity"])

    def test_declaration_order_is_irrelevant_and_constants_take_no_column(self) -> None:
        forward = self.result(DISCRETE, "--seed", 5, "--max-trials", 16)
        # The same dimensions declared in the opposite order give the same stream.
        backward = json.loads(subprocess.run(
            self._reversed_command("--seed", 5, "--max-trials", 16),
            text=True, capture_output=True, check=False, timeout=600).stdout)
        self.assertEqual(forward["trials"], backward["trials"])
        self.assertEqual(forward["sobol"]["identity"], backward["sobol"]["identity"])
        constant = self.result(DISCRETE, "--seed", 5, "--max-trials", 16,
                               "--int-dim", "Fixed", "4", "4", "1")
        self.assertEqual(constant["sobol"]["columns"], ["Fast", "Length", "Mode"])
        for left, right in zip(constant["trials"], forward["trials"]):
            self.assertEqual(left["parameters"].pop("Fixed"), 4)
            self.assertEqual(left["parameters"], right["parameters"])

    def _reversed_command(self, *extra):
        command = self.argv(DISCRETE, *extra)
        flags_start = command.index("--int-dim")
        flags_end = command.index("--sampler")
        flags = [flag for dim in reversed(DISCRETE.dims) for flag in dim.flags()]
        return command[:flags_start] + flags + command[flags_end:]

    def test_floating_columns_match_the_oracle_where_it_is_exact(self) -> None:
        for scramble in ("digital_shift", "none"):
            with self.subTest(scramble=scramble):
                result = self.result(FLOAT, "--seed", 3, "--sobol-scramble", scramble,
                                     "--max-trials", 64)
                self.check_rows(result, FLOAT, 3, scramble)
                self.check_block(result, FLOAT, 3, scramble, 0, 64, True)
                for row in result["trials"]:
                    self.assertIn(row["parameters"]["Mult"], (0.0, 0.25, 0.5, 0.75, 1.0))

    # ---- determinism: repeats, workers, lag ---------------------------------------------------

    def replay_identical(self, space) -> None:
        self.needs_identity(space)
        reference: dict[int, tuple] = {}
        for workers, lag in ((1, 0), (1, 0), (4, 0), (16, 0), (1, 1), (4, 1), (16, 1)):
            with self.subTest(space=len(space.dims), workers=workers, lag=lag):
                trials_file = self.directory / f"trials-{workers}-{lag}.ndjson"
                completed = self.run_cli(space, "--seed", 11, "--max-trials", 64,
                                         "--workers", workers, "--batch-size", 16,
                                         "--batch-lag", lag, "--trials-file", trials_file)
                encoded = (completed.stdout, trials_file.read_bytes())
                reference.setdefault(lag, encoded)
                self.assertEqual(encoded, reference[lag])
        # The parameter stream itself does not depend on the lag either.
        first = json.loads(reference[0][0])["trials"]
        second = json.loads(reference[1][0])["trials"]
        self.assertEqual([r["parameters"] for r in first], [r["parameters"] for r in second])

    def test_repeats_and_workers_at_a_fixed_batch_are_byte_identical_discrete(self) -> None:
        self.replay_identical(DISCRETE)

    def test_repeats_and_workers_at_a_fixed_batch_are_byte_identical_floating(self) -> None:
        self.replay_identical(FLOAT)

    def test_progress_stream_equals_the_final_rows(self) -> None:
        read_fd, write_fd = os.pipe()
        chunks: list[bytes] = []

        def drain() -> None:
            with os.fdopen(read_fd, "rb") as stream:
                chunks.append(stream.read())

        reader = threading.Thread(target=drain)
        reader.start()
        try:
            completed = self.run_cli(DISCRETE, "--seed", 2, "--max-trials", 24, "--workers", 4,
                                     "--batch-size", 8, "--progress-fd", write_fd,
                                     pass_fds=(write_fd,))
        finally:
            os.close(write_fd)
            reader.join(timeout=60)
        result = json.loads(completed.stdout)
        lines = [json.loads(item) for item in b"".join(chunks).decode().splitlines()]
        self.assertEqual(lines, result["trials"])
        self.assertTrue(all("sobol" not in line for line in lines))

    # ---- complete-parent continuation ---------------------------------------------------------

    def part(self, space, count, seed, scramble, parent=None, name="part", **extra):
        output = self.directory / f"{name}.json"
        arguments = ["--seed", seed, "--sobol-scramble", scramble, "--max-trials", count,
                     "--output", output, "--batch-size", 4]
        if parent is not None:
            arguments += ["--warm-start", parent]
        for key, value in extra.items():
            arguments += [key, value]
        self.needs_identity(space)
        completed = self.run_cli(space, *arguments)
        self.assertEqual(json.loads(completed.stdout), json.loads(output.read_text()))
        return output, json.loads(completed.stdout)

    def test_complete_parents_continue_the_shifted_parameter_stream(self) -> None:
        for space in (DISCRETE, FLOAT):
            for k in (3, 10):
                total = 2 ** k + 3
                whole = self.part(space, total, 9, "digital_shift", name="whole")[1]
                self.check_rows(whole, space, 9, "digital_shift")
                for first in (2 ** k - 1, 2 ** k, 2 ** k + 1):
                    with self.subTest(space=len(space.dims), k=k, first=first):
                        path_a, a = self.part(space, first, 9, "digital_shift", name="a")
                        path_b, b = self.part(space, total - first, 9, "digital_shift",
                                              parent=path_a, name="b")
                        rows = a["trials"] + b["trials"]
                        self.assertEqual([r["trial_id"] for r in rows], list(range(total)))
                        self.assertEqual([r["parameters"] for r in rows],
                                         [r["parameters"] for r in whole["trials"]])
                        self.check_block(a, space, 9, "digital_shift", 0, first, True)
                        self.check_block(b, space, 9, "digital_shift", first, total, True)
                        self.assertEqual(a["sobol"]["identity"], b["sobol"]["identity"])
                        self.assertEqual(b["warm_start"]["trials"], first)
                        self.assertEqual(b["trials_completed"], total - first)
                        self.assertNotIn("warm_start_model", b)  # TPE-only field
                        # No whole-result claim: the parts' counters are their own.
                        self.assertEqual(a["trials_completed"], first)

    def test_a_three_part_chain_and_the_none_scramble(self) -> None:
        for scramble in ("digital_shift", "none"):
            with self.subTest(scramble=scramble):
                whole = self.part(DISCRETE, 40, 4, scramble, name="whole")[1]
                p1, a = self.part(DISCRETE, 7, 4, scramble, name="p1")
                p2, b = self.part(DISCRETE, 13, 4, scramble, parent=p1, name="p2")
                p3, c = self.part(DISCRETE, 20, 4, scramble, parent=p2, name="p3")
                rows = a["trials"] + b["trials"] + c["trials"]
                self.assertEqual([r["parameters"] for r in rows],
                                 [r["parameters"] for r in whole["trials"]])
                self.check_block(c, DISCRETE, 4, scramble, 20, 40, True)
                # The grandchild carries every ancestor row verbatim: admission can recompute it.
                self.assertEqual(len(c["warm_start_trials"]), 20)
                self.assertEqual(c["warm_start"]["trials"], 20)

    def test_gaps_and_cancelled_parents_keep_point_by_id_meaning_only(self) -> None:
        whole = self.part(DISCRETE, 7, 6, "digital_shift", name="seven")[1]
        document = json.loads(json.dumps(whole))
        document["trials"] = [row for row in document["trials"] if row["trial_id"] not in (3, 4)]
        document["trials_completed"] = len(document["trials"])
        document["sobol"]["next_index"] = "7"
        document["sobol"]["exact_stream"] = False
        holes = self.write_json("holes.json", document)
        child = json.loads(self.run_cli(DISCRETE, "--seed", 6, "--max-trials", 4,
                                        "--warm-start", holes).stdout)
        self.check_rows(child, DISCRETE, 6, "digital_shift", first=7, count=4)
        self.check_block(child, DISCRETE, 6, "digital_shift", 7, 11, False)
        self.assertEqual(child["warm_start"]["trials"], 5)
        # An honest claim of exactness over a gapped parent is a forged claim.
        document["sobol"]["exact_stream"] = True
        forged = self.write_json("forged-exact.json", document)
        self.assert_refusal(self.run_cli(DISCRETE, "--seed", 6, "--max-trials", 4,
                                         "--warm-start", forged, expected=4),
                            "hpo_warm_start_rejected", 4)
        # And the converse: an exact parent that claims to have a gap.
        document = json.loads(json.dumps(whole))
        document["sobol"]["exact_stream"] = False
        converse = self.write_json("forged-inexact.json", document)
        self.assert_refusal(self.run_cli(DISCRETE, "--seed", 6, "--max-trials", 4,
                                         "--warm-start", converse, expected=4),
                            "hpo_warm_start_rejected", 4)

    def high_id_parent(self, last_id: int, seed: int) -> Path:
        base = self.part(DISCRETE, 1, seed, "digital_shift", name="one")[1]
        document = json.loads(json.dumps(base))
        row = document["trials"][0]
        row["trial_id"] = last_id
        row["parameters"] = DISCRETE.candidate(last_id, seed, "digital_shift")
        document["sobol"]["first_index"] = "0"
        document["sobol"]["next_index"] = str(last_id + 1)
        document["sobol"]["exact_stream"] = last_id == 0
        return self.write_json(f"high-{last_id}.json", document)

    def test_high_ids_continue_from_the_largest_recorded_id(self) -> None:
        seed = 12
        for next_id in (2 ** 53 - 1, 2 ** 53, 2 ** 53 + 1, 2 ** 63, 2 ** 64 - 3):
            with self.subTest(next_id=next_id):
                parent = self.high_id_parent(next_id - 1, seed)
                budget = 2
                child = json.loads(self.run_cli(DISCRETE, "--seed", seed, "--max-trials", budget,
                                                "--warm-start", parent).stdout)
                self.assertEqual([r["trial_id"] for r in child["trials"]],
                                 [next_id, next_id + 1])
                for row in child["trials"]:
                    self.assertEqual(row["parameters"],
                                     DISCRETE.candidate(row["trial_id"], seed, "digital_shift"))
                self.check_block(child, DISCRETE, seed, "digital_shift", next_id,
                                 next_id + budget, False)
                # Decimal strings keep the 64-bit values exact where a JSON number could not.
                self.assertIsInstance(child["sobol"]["first_index"], str)

    def test_trial_id_overflow_is_refused_before_any_trial(self) -> None:
        seed = 12
        never = self.directory / "never-created.ndjson"
        cases = (
            ("budget three at 2^64-3", 2 ** 64 - 4, 3),
            ("budget above the remainder", 2 ** 63, 2 ** 63 + 5),
            ("next ID 2^64-1", 2 ** 64 - 2, 1),
        )
        for label, last_id, budget in cases:
            with self.subTest(label):
                parent = self.high_id_parent(last_id, seed)
                self.assert_refusal(
                    self.run_cli(DISCRETE, "--seed", seed, "--max-trials", budget,
                                 "--warm-start", parent, "--trials-file", never, expected=4),
                    "hpo_warm_start_rejected", 4)
                self.assertFalse(never.exists())
        # A parent row with the reserved ID 2^64-1 cannot be a parent at all.
        document = json.loads(self.high_id_parent(5, seed).read_text())
        document["trials"][0]["trial_id"] = UINT64_MAX
        document["sobol"]["next_index"] = str(UINT64_MAX)
        reserved = self.write_json("reserved.json", document)
        self.assert_refusal(self.run_cli(DISCRETE, "--seed", seed, "--max-trials", 1,
                                         "--warm-start", reserved, expected=4),
                            "hpo_warm_start_rejected", 4)

    # ---- parent refusals ---------------------------------------------------------------------

    def test_foreign_and_lossy_parents_are_refused_without_a_fallback(self) -> None:
        sobol_path, sobol = self.part(DISCRETE, 6, 3, "digital_shift", name="parent")
        trials_file = self.directory / "rows.ndjson"
        self.run_cli(DISCRETE, "--seed", 3, "--max-trials", 6, "--trials-file", trials_file)
        parents = {
            "jsonl rows": trials_file,
            "rows array": self.write_json("array.json", sobol["trials"]),
            "binary magic": self._write_bytes("binary.bin", b"PFHWARM\x00" + bytes(64)),
            "garbage": self._write_bytes("garbage.json", b"\xff\xfe not json"),
            "empty file": self._write_bytes("empty.json", b""),
        }
        for foreign in ("tpe", "random", "grid"):
            result = self.directory / f"{foreign}.json"
            self.run_cli(NO_STRING, "--max-trials", 6, "--seed", 3, "--output", result,
                         "--batch-size", 2, sampler=foreign)
            parents[f"{foreign} result"] = result
        for mode in ("best-k", "none"):
            lossy = self.directory / f"{mode}.json"
            self.run_cli(DISCRETE, "--seed", 3, "--max-trials", 6, "--trials-out", mode,
                         "--best-k", 2, "--output", lossy)
            parents[f"trials_out {mode}"] = lossy
        missing_block = json.loads(json.dumps(sobol))
        del missing_block["sobol"]
        parents["no sobol block"] = self.write_json("noblock.json", missing_block)
        for label, parent in parents.items():
            with self.subTest(label):
                space = NO_STRING if "tpe" in label or "random" in label or "grid" in label \
                    else DISCRETE
                self.assert_refusal(self.run_cli(space, "--seed", 3, "--max-trials", 4,
                                                 "--warm-start", parent, expected=4),
                                    "hpo_warm_start_rejected", 4)

    def test_mismatching_descriptors_are_refused(self) -> None:
        parent, _ = self.part(DISCRETE, 6, 3, "digital_shift", name="parent")
        wider = Space(Dim("Length", "int", 1, 2000, 1), Dim("Fast", "bool"),
                      Dim("Mode", "cat", ["fast", "slow", "medium"]))
        extra = Space(Dim("Length", "int", 1, 1000, 1), Dim("Fast", "bool"),
                      Dim("Mode", "cat", ["fast", "slow", "medium"]), Dim("Window", "int", 3, 9, 3))
        cases = (
            ("another seed", DISCRETE, ("--seed", 4)),
            ("another scramble", DISCRETE, ("--seed", 3, "--sobol-scramble", "none")),
            ("another range", wider, ("--seed", 3)),
            ("another column set", extra, ("--seed", 3)),
        )
        for label, space, arguments in cases:
            with self.subTest(label):
                self.assert_refusal(self.run_cli(space, *arguments, "--max-trials", 4,
                                                 "--warm-start", parent, expected=4),
                                    "hpo_warm_start_rejected", 4)
        # Another objective changes the space hash, hence the identity.
        command = self.argv(DISCRETE, "--seed", 3, "--max-trials", 4, "--warm-start", parent)
        command[command.index("--objective") + 1] = "metrics.all.net_profit * 2"
        refused = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assert_refusal(refused, "hpo_warm_start_rejected", 4)

    def test_forged_descriptors_rows_and_ids_are_refused(self) -> None:
        _, base = self.part(DISCRETE, 8, 3, "digital_shift", name="base")

        def forged(label, mutate):
            document = json.loads(json.dumps(base))
            mutate(document)
            return label, self.write_json(f"forged-{label}.json", document)

        def flip_parameter(document):
            row = document["trials"][2]
            row["parameters"]["Length"] = (row["parameters"]["Length"] % 1000) + 1

        def swap_ids(document):
            document["trials"][0]["trial_id"], document["trials"][1]["trial_id"] = 1, 0

        def duplicate_id(document):
            document["trials"][1]["trial_id"] = 0

        cases = [
            forged("row", flip_parameter),
            forged("swapped-ids", swap_ids),
            forged("duplicate-id", duplicate_id),
            forged("identity", lambda d: d["sobol"].__setitem__("identity", "0" * 64)),
            forged("seed-text", lambda d: d["sobol"].__setitem__("seed", "03")),
            forged("columns", lambda d: d["sobol"]["columns"].reverse()),
            forged("mapper-revision", lambda d: d["sobol"]["mapper"].__setitem__("revision", 1)),
            forged("extra-key", lambda d: d["sobol"].__setitem__("extra", 1)),
            forged("next-index", lambda d: d["sobol"].__setitem__("next_index", "9")),
            forged("count", lambda d: d.__setitem__("trials_completed", 99)),
            forged("word-bits", lambda d: d["sobol"].__setitem__("word_bits", 32)),
            forged("numeric", lambda d: d["sobol"].__setitem__("numeric_build_identity",
                                                               NUMERIC_PREFIX + ";x")),
            forged("sampler", lambda d: d.__setitem__("sampler", "random")),
        ]
        for label, parent in cases:
            with self.subTest(label):
                self.assert_refusal(self.run_cli(DISCRETE, "--seed", 3, "--max-trials", 2,
                                                 "--warm-start", parent, expected=4),
                                    "hpo_warm_start_rejected", 4)

    def test_unsorted_parent_rows_are_canonicalized(self) -> None:
        _, base = self.part(DISCRETE, 8, 3, "digital_shift", name="base")
        document = json.loads(json.dumps(base))
        document["trials"].reverse()
        parent = self.write_json("unsorted.json", document)
        child = json.loads(self.run_cli(DISCRETE, "--seed", 3, "--max-trials", 4,
                                        "--warm-start", parent).stdout)
        self.check_rows(child, DISCRETE, 3, "digital_shift", first=8, count=4)
        self.check_block(child, DISCRETE, 3, "digital_shift", 8, 12, True)

    def test_an_empty_parent_is_refused_explicitly(self) -> None:
        _, base = self.part(DISCRETE, 2, 3, "digital_shift", name="base")
        document = json.loads(json.dumps(base))
        document["trials"] = []
        document["trials_completed"] = 0
        document["sobol"]["next_index"] = "0"
        parent = self.write_json("empty-rows.json", document)
        self.assert_refusal(self.run_cli(DISCRETE, "--seed", 3, "--max-trials", 2,
                                         "--warm-start", parent, expected=4),
                            "hpo_warm_start_rejected", 4)

    def _write_bytes(self, name, data: bytes) -> Path:
        path = self.directory / name
        path.write_bytes(data)
        return path

    # ---- with-replacement semantics ----------------------------------------------------------

    def test_a_finite_space_keeps_duplicates_and_never_stops_early(self) -> None:
        result = self.result(TINY, "--seed", 5, "--max-trials", 64)
        self.assertEqual(result["trials_completed"], 64)
        self.assertEqual(result["trials_requested"], 64)
        self.assertEqual(result["stop_reason"], "trial_budget_reached")
        self.check_rows(result, TINY, 5, "digital_shift")
        vectors = [json.dumps(row["parameters"], sort_keys=True) for row in result["trials"]]
        self.assertLessEqual(len(set(vectors)), 4)
        self.assertGreater(len(vectors), len(set(vectors)))  # duplicate occurrences exist
        self.assertLessEqual(result["unique_candidates_attempted"], 4)
        self.check_block(result, TINY, 5, "digital_shift", 0, 64, True)
        # A budget beyond the cardinality is not an error, and wall-only runs are bounded by time.
        timed = self.result(TINY, "--seed", 5, "--max-wall-seconds", 1.0, "--workers", 2,
                            "--fixed-input", "DelayMs", 15)
        self.assertEqual(timed["stop_reason"], "deadline")
        self.assertGreater(timed["trials_completed"], 4)
        self.check_rows(timed, TINY, 5, "digital_shift")

    def test_a_parent_that_covers_the_whole_finite_space_still_continues(self) -> None:
        parent, first = self.part(TINY, 64, 8, "digital_shift", name="cover")
        self.assertEqual(first["unique_candidates_attempted"], 4)
        completed = self.run_cli(TINY, "--seed", 8, "--max-trials", 5, "--warm-start", parent)
        child = json.loads(completed.stdout)  # exit 0: no space-exhausted exit 5, no rejection
        self.check_rows(child, TINY, 8, "digital_shift", first=64, count=5)
        self.check_block(child, TINY, 8, "digital_shift", 64, 69, True)

    def test_early_stop_keeps_point_by_id_meaning_and_replays_at_a_fixed_batch(self) -> None:
        reference = None
        for workers, lag in ((1, 0), (4, 0), (16, 0), (1, 1), (4, 1), (16, 1)):
            with self.subTest(workers=workers, lag=lag):
                result = self.result(NO_STRING, "--seed", 2, "--max-trials", 200, "--workers",
                                     workers, "--batch-size", 4, "--batch-lag", lag,
                                     "--no-improvement-trials", 3, "--fixed-input",
                                     "SequenceScore", "5")
                self.assertEqual(result["stop_reason"], "no_improvement")
                rows = result["trials"]
                self.assertGreaterEqual(len(rows), 4)
                self.assertLess(len(rows), 200)
                self.check_rows(result, NO_STRING, 2, "digital_shift")
                self.check_block(result, NO_STRING, 2, "digital_shift", 0, len(rows), True)
                self.assertEqual(result["early_stop"]["patience_trials"], 3)
                if lag == 0:
                    key = json.dumps(result, sort_keys=True)
                    reference = reference or key
                    self.assertEqual(key, reference)

    def test_pruned_rows_are_ordinary_rows_with_their_own_ids(self) -> None:
        result = self.result(NO_STRING, "--seed", 2, "--max-trials", 24, "--pruner", "median",
                             "--batch-size", 4)
        self.check_rows(result, NO_STRING, 2, "digital_shift")
        self.check_block(result, NO_STRING, 2, "digital_shift", 0, 24, True)

    # ---- accounting: retention modes, deadline, timeout ----------------------------------------

    def test_best_k_and_none_report_the_same_block_as_all(self) -> None:
        blocks = {}
        for mode in ("all", "best-k", "none"):
            result = self.result(DISCRETE, "--seed", 1, "--max-trials", 30, "--trials-out", mode,
                                 "--best-k", 3)
            blocks[mode] = result["sobol"]
            self.assertEqual(result["trials_completed"], 30)
        self.assertEqual(blocks["all"], blocks["best-k"])
        self.assertEqual(blocks["all"], blocks["none"])
        self.assertEqual(blocks["all"]["next_index"], "30")

    def test_a_deadline_stop_reports_the_terminal_prefix(self) -> None:
        result = self.result(DISCRETE, "--seed", 1, "--workers", 1, "--max-wall-seconds", 0.8,
                             "--fixed-input", "DelayMs", 120)
        self.assertEqual(result["stop_reason"], "deadline")
        count = len(result["trials"])
        self.assertTrue(0 < count < 100)
        self.check_rows(result, DISCRETE, 1, "digital_shift")
        self.check_block(result, DISCRETE, 1, "digital_shift", 0, count, True)

    def test_a_trial_timeout_publishes_the_block_in_every_retention_mode(self) -> None:
        # One worker runs the IDs in order, so the first ID whose Length hangs is the timed-out
        # trial and the terminal set is exactly 0..that ID.
        target = DISCRETE.candidate(2, 1, "digital_shift")["Length"]
        hung = min(index for index in range(3)
                   if DISCRETE.candidate(index, 1, "digital_shift")["Length"] == target)
        blocks = []
        for mode in ("all", "none"):
            final = self.directory / f"timeout-{mode}.json"
            completed = self.run_cli(DISCRETE, "--seed", 1, "--workers", 1, "--max-trials", 20,
                                     "--fixed-input", "HangAtLength", target,
                                     "--trial-timeout-seconds", 0.6, "--trials-out", mode,
                                     "--output", final, expected=3)
            result = json.loads(completed.stdout)
            self.assertEqual(result, json.loads(final.read_text()))
            self.assertEqual(result["stop_reason"], "trial_timeout")
            self.check_block(result, DISCRETE, 1, "digital_shift", 0, hung + 1, True)
            blocks.append(result["sobol"])
            if mode == "all":
                self.assertEqual([r["trial_id"] for r in result["trials"]],
                                 list(range(hung + 1)))
                self.assertEqual(result["trials"][-1]["status"], "trial_timeout")
        self.assertEqual(blocks[0], blocks[1])

    # ---- refusals before any trial work ----------------------------------------------------------

    def refuse_early(self, space_flags, *extra, code="hpo_study_spec_invalid", reason="sampler",
                     exit_code=1):
        """The refusal must precede the plugin load and every output file."""
        never = self.directory / "never-created.ndjson"
        command = self.argv(EMPTY, *extra, "--trials-file", never,
                            strategy=self.directory / "no-such-plugin")
        at = command.index("--sampler")
        command[at:at] = list(space_flags)
        completed = subprocess.run(command, text=True, capture_output=True, check=False,
                                   timeout=600)
        self.assert_refusal(completed, code, exit_code, reason)
        self.assertFalse(never.exists(), "an output file was opened before the refusal")
        return completed

    def test_refusals_before_billing(self) -> None:
        self.refuse_early([item for i in range(1025) for item in ("--bool-dim", f"b{i:04d}")],
                          "--max-trials", 4)
        self.refuse_early(["--int-dim", "Huge", "-9223372036854775808", "9223372036854775807",
                           "1"], "--max-trials", 4)
        self.refuse_early(["--log-int-dim", "Wide", "1", str(2 ** 53 + 1)], "--max-trials", 4)
        self.refuse_early(["--log-int-dim", "High", str(2 ** 52 + 1), str(2 ** 52 + 100)],
                          "--max-trials", 4)
        self.refuse_early(["--bool-dim", "Fast"], "--sobol-scramble", "owen", "--max-trials", 4)
        # Usage errors keep their own registered code.
        self.refuse_early(["--bool-dim", "Fast"], code="hpo_cli_usage", reason=None)  # no budget
        self.refuse_early(["--bool-dim", "Fast"], "--max-trials", 4, "--candidate-policy",
                          "exhaustive", code="hpo_cli_usage", reason=None)
        usage = subprocess.run(self.argv(DISCRETE, "--max-trials", 4, sampler="grid") +
                               ["--sobol-scramble", "none"], text=True, capture_output=True,
                               check=False, timeout=60)
        self.assert_refusal(usage, "hpo_cli_usage", 1)
        usage = subprocess.run(self.argv(DISCRETE, "--max-trials", 4) +
                               ["--candidates", str(self.directory / "list.jsonl")],
                               text=True, capture_output=True, check=False, timeout=60)
        self.assert_refusal(usage, "hpo_cli_usage", 1)

    def test_the_boundary_column_counts_are_accepted(self) -> None:
        many = Space(*[Dim(f"b{i:04d}", "bool") for i in range(1024)])
        result = json.loads(self.run_cli(many, "--max-trials", 3).stdout)
        self.assertEqual(len(result["sobol"]["columns"]), 1024)
        self.check_rows(result, many, 0, "digital_shift")
        # A one-value dimension and a degenerate real take no column.
        result = self.result(DISCRETE, "--max-trials", 2, "--int-dim", "One", "7", "7", "1")
        self.assertEqual(result["sobol"]["columns"], ["Fast", "Length", "Mode"])

    def test_discrete_log_integer_limits_are_exact(self) -> None:
        self.needs_identity(Space(Dim("L", "log", 1, 2)))
        for low, high in ((1, 2 ** 53), (2 ** 52, 2 ** 52 + 2 ** 53 - 1)):
            with self.subTest(low=low, high=high):
                completed = subprocess.run(
                    self.argv(NO_STRING, "--max-trials", 2) + ["--log-int-dim", "Lg", str(low),
                                                              str(high)],
                    text=True, capture_output=True, check=False, timeout=600)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                for row in json.loads(completed.stdout)["trials"]:
                    self.assertTrue(low <= row["parameters"]["Lg"] <= high)

    # ---- numeric identity capability -------------------------------------------------------------

    def test_unbound_identity_refuses_floating_spaces_only(self) -> None:
        discrete = json.loads(self.run_cli(DISCRETE, "--max-trials", 2).stdout)
        self.assertIsNone(discrete["sobol"]["numeric_build_identity"])  # always available
        if identity_bound():
            result = json.loads(self.run_cli(FLOAT, "--max-trials", 2).stdout)
            self.assertTrue(result["sobol"]["numeric_build_identity"].startswith(NUMERIC_PREFIX))
            return
        if REQUIRE_IDENTITY:
            self.fail("the Sobol numeric identity is unbound but is required")
        never = self.directory / "never-created.ndjson"
        for plugin in (PLUGIN, self.directory / "no-such-plugin"):
            refused = self.run_cli(FLOAT_NO_STRING, "--max-trials", 2, "--trials-file", never,
                                   expected=1, strategy=plugin)
            self.assert_refusal(refused, "hpo_toolchain_unavailable", 1, "native_runner")
            self.assertIn("unbound", refused.stderr)
            self.assertFalse(never.exists())

    def test_a_floating_identity_is_not_the_tpe_or_statistics_identity(self) -> None:
        self.needs_identity(FLOAT)
        result = self.result(FLOAT, "--max-trials", 3)
        numeric = result["sobol"]["numeric_build_identity"]
        self.assertTrue(numeric.startswith(NUMERIC_PREFIX))
        self.assertNotIn("portable-tpe", numeric)
        self.assertNotIn("return-stats", numeric)
        self.assertNotIn("numeric_build_identity", result)  # the TPE field is absent for sobol

    # ---- C/X interop and untouched bytes ---------------------------------------------------------

    def test_return_statistics_are_sampler_independent(self) -> None:
        plain = self.result(DISCRETE, "--seed", 4, "--max-trials", 12)
        probe = self.run_cli(DISCRETE, "--seed", 4, "--max-trials", 2, "--record-metric",
                             "returns.bar.count", expected=None)
        if probe.returncode != 0:
            if json.loads(probe.stdout).get("failure", {}).get("code") == \
                    "hpo_toolchain_unavailable":
                self.skipTest("the return-statistics identity is unbound in this build")
            self.fail(probe.stderr)
        with_stats = json.loads(self.run_cli(
            DISCRETE, "--seed", 4, "--max-trials", 12, "--record-metric", "returns.bar.count",
            "--record-metric", "returns.monthly.status").stdout)
        self.assertEqual([r["parameters"] for r in plain["trials"]],
                         [r["parameters"] for r in with_stats["trials"]])
        self.assertEqual(plain["sobol"], with_stats["sobol"])
        stats = with_stats["return_stats"]
        self.assertNotEqual(stats["numeric_build_identity"], plain["sobol"]["identity"])
        self.assertTrue(all("returns.bar.count" in row["metrics"] for row in with_stats["trials"]))
        self.assertNotIn("return_stats", plain)

    def test_other_samplers_carry_no_sobol_block(self) -> None:
        for sampler in ("grid", "random", "tpe", "dlib_global"):
            with self.subTest(sampler=sampler):
                completed = self.run_cli(NO_STRING, "--max-trials", 6, "--seed", 3,
                                         sampler=sampler)
                result = json.loads(completed.stdout)
                self.assertNotIn("sobol", result)
                self.assertNotEqual(result["sampler_implementation"], IMPLEMENTATION)

    def test_help_documents_the_sampler_and_flag(self) -> None:
        completed = subprocess.run([str(NATIVE), "--help"], text=True, capture_output=True,
                                   check=False, timeout=60)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--sobol-scramble", completed.stdout)
        self.assertIn("sobol", completed.stdout.split("--sampler", 1)[1].splitlines()[0])

    def test_supplied_reference_off_bytes(self) -> None:
        reference = supplied_reference(required=PROOF)
        if reference is None:
            self.skipTest("pinned reference binary not supplied: no literal comparison made")
        (self.directory / "reference.json").write_text(json.dumps({
            "path": str(reference), "sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
            "contract": "literal stdout and trials-file bytes; no normalization",
        }) + "\n")
        variants = (
            ("grid", ("--max-trials", "12")), ("random", ("--max-trials", "12", "--seed", "4")),
            ("tpe", ("--max-trials", "12", "--seed", "4", "--batch-size", "4")),
            ("dlib_global", ("--max-trials", "12", "--seed", "4")),
            ("random", ("--max-trials", "12", "--seed", "4", "--no-improvement-trials", "3")),
            ("tpe", ("--max-trials", "12", "--seed", "4", "--pruner", "median")),
        )
        parents = {}
        for sampler in ("random", "tpe"):
            parent = self.directory / f"parent-{sampler}.json"
            self.run_cli(NO_STRING, "--max-trials", "10", "--seed", "4", "--batch-size", "2",
                         "--output", parent, sampler=sampler, native=reference)
            parents[sampler] = parent
        for sampler, extra in variants:
            for workers in (1, 4):
                with self.subTest(sampler=sampler, extra=extra, workers=workers):
                    flags = ("--workers", workers, "--batch-size", "4", *extra)
                    old_trials, new_trials = (self.directory / "old.ndjson",
                                              self.directory / "new.ndjson")
                    old = self.run_cli(NO_STRING, *flags, "--trials-file", old_trials,
                                       sampler=sampler, native=reference, expected=None)
                    new = self.run_cli(NO_STRING, *flags, "--trials-file", new_trials,
                                       sampler=sampler, expected=None)
                    self.assertEqual(old.returncode, new.returncode)
                    self.assertEqual(old.stdout, new.stdout)
                    self.assertEqual(old_trials.read_bytes(), new_trials.read_bytes())
        for sampler, parent in parents.items():
            with self.subTest(warm=sampler):
                flags = ("--max-trials", "6", "--seed", "4", "--batch-size", "2", "--warm-start",
                         parent, "--workers", "2")
                old = self.run_cli(NO_STRING, *flags, sampler=sampler, native=reference,
                                   expected=None)
                new = self.run_cli(NO_STRING, *flags, sampler=sampler, expected=None)
                self.assertEqual((old.returncode, old.stdout), (new.returncode, new.stdout))
        # The release binary does not know the sampler: it must refuse, never ignore it.
        refused = self.run_cli(NO_STRING, "--max-trials", "2", native=reference, expected=1)
        self.assertEqual(json.loads(refused.stdout)["failure"]["code"], "hpo_cli_usage")


if __name__ == "__main__":
    unittest.main()
