#!/usr/bin/env python3
"""Shipped-CLI contracts for `--sampler candidates --candidates FILE` (format
`pineforge_candidates_v1`), driven only through the native binary and the fake strategy
plugin. Usage: test_candidate_list_cli.py NATIVE PLUGIN [unittest arguments].

Written from the AR pin (methods-c-contract.pin.md) before the wiring existed. These tests
are UNEXECUTED until the spot phase. Independent oracles: Python recomputes the list digest
and the canonical identity from the documented byte layout, and compares row bytes across
worker counts; nothing imports the product.

For the literal off-path comparison supply both PFH_CANDIDATE_LIST_REFERENCE (a native binary
built at d2f83326, before this feature) and PFH_CANDIDATE_LIST_REFERENCE_SHA256. Without them
that test is skipped and says so; version and build fields are never normalized.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest


NATIVE = Path(sys.argv.pop(1)).resolve()
PLUGIN = Path(sys.argv.pop(1)).resolve()

FORMAT = "pineforge_candidates_v1"
BARS = 64

# Length (int lattice), Mult (stepped real), Fast (bool) and Mode (string choice, declared last
# by the CLI). The whole space is finite (100 x 5 x 2 x 2 = 2000 points).
SPACE = ("--int-dim", "Length", "1", "100", "1", "--real-dim", "Mult", "0", "1", "0.25",
         "--bool-dim", "Fast", "--categorical-choice", "Mode", "fast",
         "--categorical-choice", "Mode", "slow")
# An integer-only space needs no input-kind manifest, so a bogus plugin path cannot fail first.
INTEGER_SPACE = ("--int-dim", "Length", "1", "100", "1")
# SequenceScore picks values[Length % 4]; "error" makes the fake engine fail that trial.
SEQUENCE = "1,2,error,4"
CONSTRAINT = "metrics.all.num_trades >= 10"


def vector(length, mult=0.25, fast=True, mode="fast"):
    return {"Length": length, "Mult": mult, "Fast": fast, "Mode": mode}


def line(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def real_bits(value: float) -> str:
    if value == 0.0:
        value = 0.0  # candidate_key does not distinguish the sign of a zero
    return struct.pack(">d", value).hex()


def canonical_key(value) -> str:
    """The importer's candidate_key, rebuilt from its documented layout."""
    members = {}
    for name, item in value.items():
        if name == "Length":
            members[name] = ["integer", str(int(item))]
        elif name == "Mult":
            members[name] = ["real", real_bits(float(item))]
        elif name == "Fast":
            members[name] = ["boolean", bool(item)]
        else:
            members[name] = ["string", item]
    return json.dumps(members, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def list_sha256(vectors) -> str:
    material = FORMAT + "\n" + "".join(canonical_key(item) + "\n" for item in vectors)
    return hashlib.sha256(material.encode()).hexdigest()


def missing_ranges(count: int, present) -> list[list[int]]:
    present = set(present)
    ranges: list[list[int]] = []
    position = 0
    while position < count:
        if position in present:
            position += 1
            continue
        first = position
        while position < count and position not in present:
            position += 1
        ranges.append([first, position - 1])
    return ranges


def expected_status(length: int) -> str:
    """Status the fake engine, SEQUENCE and CONSTRAINT give a trial of this Length."""
    if SEQUENCE.split(",")[length % 4] == "error":
        return "engine_error"
    return "ok" if length >= 10 else "constraint_violation"


def supplied_reference():
    path = os.environ.get("PFH_CANDIDATE_LIST_REFERENCE")
    expected = os.environ.get("PFH_CANDIDATE_LIST_REFERENCE_SHA256")
    if not path or not expected:
        return None
    reference = Path(path).resolve()
    actual = hashlib.sha256(reference.read_bytes()).hexdigest()
    if actual != expected.lower():
        raise AssertionError(f"reference binary digest {actual} is not the supplied {expected}")
    return reference


class CandidateListCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pf_cand_cli_")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.csv = self.directory / "bars.csv"
        self.csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            + "".join(f"{1700000000000 + index * 60000},100,102,99,101,10\n"
                      for index in range(BARS)),
            encoding="utf-8",
        )

    # ---- helpers ---------------------------------------------------------------------

    def write_list(self, vectors, name="list.jsonl") -> Path:
        path = self.directory / name
        path.write_bytes("".join(line(item) + "\n" for item in vectors).encode())
        return path

    def argv(self, candidates, *extra, native=None, strategy=None, ohlcv=None, space=SPACE,
             sampler="candidates"):
        command = [
            str(native or NATIVE), "run", "--strategy", str(strategy or PLUGIN),
            "--ohlcv", str(ohlcv or self.csv), "--objective", "metrics.all.net_profit",
            "--input-tf", "1", "--script-tf", "5", "--chart-timezone", "Asia/Taipei",
            "--bar-magnifier", "true", "--magnifier-samples", "6",
            "--magnifier-distribution", "triangle", "--fixed-input", "BatchPrefixTest", "1",
            *space,
        ]
        if sampler:
            command += ["--sampler", sampler]
        if candidates is not None:
            command += ["--candidates", str(candidates)]
        return command + [str(item) for item in extra]

    def run_cli(self, candidates, *extra, expected=0, pass_fds=(), **options):
        completed = subprocess.run(
            self.argv(candidates, *extra, **options), text=True, capture_output=True,
            check=False, pass_fds=pass_fds, timeout=600,
        )
        if expected is not None:
            self.assertEqual(completed.returncode, expected, completed.stderr)
        return completed

    def trial_bytes(self, completed) -> bytes:
        start = completed.stdout.index('  "trials": [') + len('  "trials": ')
        return completed.stdout[start:completed.stdout.rfind("\n}")].encode()

    def rows_of(self, completed) -> str:
        """The trial rows alone: a parent's source digest legitimately differs by encoding."""
        return json.dumps(json.loads(completed.stdout)["trials"], sort_keys=True)

    def assert_block(self, result, vectors, *, evaluated=None, scored=None, ranges=None,
                     source: Path | None = None) -> dict:
        block = result["candidate_list"]
        count = len(vectors)
        self.assertEqual(block["format"], FORMAT)
        self.assertEqual(block["count"], count)
        self.assertEqual(block["list_sha256"], list_sha256(vectors))
        if source is not None:
            self.assertEqual(block["source_sha256"],
                             hashlib.sha256(source.read_bytes()).hexdigest())
        if evaluated is not None:
            self.assertEqual(block["evaluated"], evaluated)
        if scored is not None:
            self.assertEqual(block["scored"], scored)
        if ranges is not None:
            self.assertEqual(block["unevaluated_ranges"], ranges)
        self.assertEqual(block["complete"], block["evaluated"] == count)
        covered = {trial["trial_id"] for trial in result["trials"]}
        if result["trials_out"] == "all":
            self.assertEqual(block["unevaluated_ranges"], missing_ranges(count, covered))
            self.assertEqual(block["evaluated"], len(covered))
        return block

    def assert_refusal(self, completed, code, reason, exit_code, leaks=()):
        self.assertEqual(completed.returncode, exit_code, completed.stderr)
        document = json.loads(completed.stdout)
        self.assertFalse(document["ok"])
        self.assertNotIn("trials", document)
        failure = document["failure"]
        self.assertEqual(failure["code"], code, completed.stderr)
        self.assertEqual(failure["args"], {"reason": reason} if reason else {}, completed.stderr)
        self.assertEqual(failure["exit_code"], exit_code)
        for token in ("PFSENTINEL", str(self.directory), self.directory.name, *leaks):
            self.assertNotIn(token, completed.stdout + completed.stderr)

    # ---- order, ids, duplicates, hashes ---------------------------------------------------

    def test_order_ids_duplicates_and_hashes(self) -> None:
        vectors = [vector(40, 0.5, False, "slow"), vector(7), vector(40, 0.5, False, "slow"),
                   vector(100, 1, True, "fast"), vector(1, 0, False, "slow"), vector(7)]
        path = self.write_list(vectors)
        result = json.loads(self.run_cli(path, "--workers", "3").stdout)
        self.assertEqual(result["sampler"], "candidates")
        self.assertEqual(result["sampler_implementation"], "pineforge_candidate_list_v1")
        self.assertEqual(result["trials_requested"], 6)
        self.assertEqual(result["trials_completed"], 6)
        self.assertEqual(result["stop_reason"], "trial_budget_reached")
        self.assertEqual([trial["trial_id"] for trial in result["trials"]], list(range(6)))
        for position, trial in enumerate(result["trials"]):
            self.assertEqual(trial["parameters"], vectors[position])
            self.assertEqual(trial["status"], "ok")
        # Duplicates are separate occurrences with their own ids and identical outcomes.
        for first, second in ((0, 2), (1, 5)):
            self.assertEqual(result["trials"][first]["objective"],
                             result["trials"][second]["objective"])
            self.assertNotEqual(result["trials"][first]["trial_id"],
                                result["trials"][second]["trial_id"])
        self.assertEqual(result["unique_candidates_attempted"], 4)
        self.assertFalse(result["search_space_exhausted"])
        self.assert_block(result, vectors, evaluated=6, scored=6, ranges=[], source=path)
        self.assertTrue(result["candidate_list"]["complete"])
        self.assertNotIn("early_stop", result)
        self.assertEqual(result["candidate_policy"], "sampler_default")

    def test_spelling_and_order_change_source_digest_only(self) -> None:
        vectors = [vector(12, 0.5), vector(30, 1.0, False, "slow")]
        canonical = self.write_list(vectors, "canonical.jsonl")
        respelled = self.directory / "respelled.jsonl"
        respelled.write_bytes(
            b'{ "Mode":"fast" , "Fast":true,"Mult":5E-1,"Length":12 }\r\n'
            b'{"Length":30,"Mult":1,"Fast":false,"Mode":"slow"}'
        )
        first = json.loads(self.run_cli(canonical).stdout)
        second = json.loads(self.run_cli(respelled).stdout)
        self.assertEqual(first["candidate_list"]["list_sha256"],
                         second["candidate_list"]["list_sha256"])
        self.assertNotEqual(first["candidate_list"]["source_sha256"],
                            second["candidate_list"]["source_sha256"])
        self.assertEqual(second["candidate_list"]["source_sha256"],
                         hashlib.sha256(respelled.read_bytes()).hexdigest())
        self.assertEqual(json.dumps(first["trials"]), json.dumps(second["trials"]))
        reordered = self.write_list(list(reversed(vectors)), "reordered.jsonl")
        third = json.loads(self.run_cli(reordered).stdout)
        self.assertNotEqual(first["candidate_list"]["list_sha256"],
                            third["candidate_list"]["list_sha256"])
        self.assertEqual(third["trials"][0]["parameters"], vectors[1])

    def test_budget_is_absent_zero_or_exactly_the_list_length(self) -> None:
        vectors = [vector(length) for length in (11, 12, 13, 14)]
        path = self.write_list(vectors)
        absent = self.run_cli(path)
        self.assertEqual(json.loads(absent.stdout)["trials_requested"], 4)
        self.assertEqual(self.run_cli(path, "--max-trials", "0").stdout, absent.stdout)
        self.assertEqual(self.run_cli(path, "--max-trials", "4").stdout, absent.stdout)
        for wrong in ("3", "5", "1000"):
            self.assert_refusal(self.run_cli(path, "--max-trials", wrong, expected=1),
                                "hpo_study_spec_invalid", "sampler", 1)
        # The seed is recorded and changes no value.
        seeded = self.run_cli(path, "--seed", "999")
        self.assertEqual(json.loads(seeded.stdout)["seed"], 999)
        self.assertEqual(self.trial_bytes(seeded), self.trial_bytes(absent))

    # ---- admission: whole file first, before any trial -------------------------------------

    def refuse_bytes(self, label, data: bytes, code, reason, *extra, exit_code=1):
        path = self.directory / "bad.jsonl"
        path.write_bytes(data)
        trials_file = self.directory / "never-created.ndjson"
        with self.subTest(label):
            completed = self.run_cli(path, "--trials-file", trials_file, *extra,
                                     expected=exit_code)
            self.assert_refusal(completed, code, reason, exit_code)
            # RunState opens (truncates) --trials-file as soon as trial work starts.
            self.assertFalse(trials_file.exists(), "trial work started before admission failed")

    def test_admission_refusals_are_typed_and_leak_nothing(self) -> None:
        good = line(vector(10))

        def with_member(**changes):
            item = vector(10)
            item.update(changes)
            return line(item)

        invalid_input = "hpo_input_file_invalid"
        invalid_study = "hpo_study_spec_invalid"
        table = [
            ("empty file", b"", invalid_study, "sampler"),
            ("single LF", b"\n", invalid_input, None),
            ("blank line between", f"{good}\n\n{good}\n".encode(), invalid_input, None),
            ("trailing blank line", f"{good}\n\n".encode(), invalid_input, None),
            ("byte order mark", b"\xef\xbb\xbf" + good.encode() + b"\n", invalid_input, None),
            ("trailing garbage", (good + " x\n").encode(), invalid_input, None),
            ("NaN token", b'{"Length":10,"Mult":NaN,"Fast":true,"Mode":"fast"}\n',
             invalid_input, None),
            ("Infinity token", b'{"Length":10,"Mult":Infinity,"Fast":true,"Mode":"fast"}\n',
             invalid_input, None),
            ("duplicate JSON key",
             b'{"Length":10,"Length":11,"Mult":0.25,"Fast":true,"Mode":"fast"}\n',
             invalid_input, None),
            ("duplicate unknown JSON key", b'{"PFSENTINEL_KEY":1,"PFSENTINEL_KEY":2}\n',
             invalid_input, None),
            ("invalid UTF-8 in a value",
             b'{"Length":10,"Mult":0.25,"Fast":true,"Mode":"\xff\xfePFSENTINEL"}\n',
             invalid_input, None),
            ("overlong UTF-8", b'{"Length":10,"Mult":0.25,"Fast":true,"Mode":"\xc0\x80"}\n',
             invalid_input, None),
            ("line over 64 KiB",
             (good + " " * (65537 - len(good))).encode() + b"\n", invalid_input, None),
            ("file over 32 MiB", b" " * (32 * 1024 * 1024 + 1), invalid_input, None),
            ("50,001 occurrences", ((good + "\n") * 50001).encode(), invalid_study, "sampler"),
            ("not an object", b"[1,2,3]\n", invalid_study, "search_space"),
            ("missing key", line({"Length": 10, "Mult": 0.25, "Fast": True}).encode() + b"\n",
             invalid_study, "search_space"),
            ("unknown key", with_member(PFSENTINEL_KEY=1).encode() + b"\n",
             invalid_study, "search_space"),
            ("key naming only a fixed input", with_member(BatchPrefixTest=1).encode() + b"\n",
             invalid_study, "search_space"),
            ("string for int", with_member(Length="10").encode() + b"\n",
             invalid_study, "search_space"),
            ("real spelling for int",
             b'{"Length":10.0,"Mult":0.25,"Fast":true,"Mode":"fast"}\n',
             invalid_study, "search_space"),
            ("bool for int", with_member(Length=True).encode() + b"\n",
             invalid_study, "search_space"),
            ("int for bool", with_member(Fast=1).encode() + b"\n",
             invalid_study, "search_space"),
            ("string for real", with_member(Mult="0.25").encode() + b"\n",
             invalid_study, "search_space"),
            ("unknown choice", with_member(Mode="PFSENTINEL_VALUE").encode() + b"\n",
             invalid_study, "search_space"),
            ("number for string choice", with_member(Mode=1).encode() + b"\n",
             invalid_study, "search_space"),
            ("int below low", with_member(Length=0).encode() + b"\n",
             invalid_study, "search_space"),
            ("int above high", with_member(Length=101).encode() + b"\n",
             invalid_study, "search_space"),
            ("real off the lattice", with_member(Mult=0.3).encode() + b"\n",
             invalid_study, "search_space"),
            ("real above high", with_member(Mult=1.25).encode() + b"\n",
             invalid_study, "search_space"),
            ("bad vector after good ones",
             f"{good}\n{good}\n".encode() + with_member(Length=0).encode() + b"\n",
             invalid_study, "search_space"),
        ]
        for label, data, code, reason in table:
            self.refuse_bytes(label, data, code, reason)

    def test_paths_and_file_types_are_refused(self) -> None:
        trials_file = self.directory / "never-created.ndjson"
        fifo = self.directory / "list.fifo"
        os.mkfifo(fifo)
        link_target = self.write_list([vector(10)], "target.jsonl")
        link = self.directory / "link.jsonl"
        link.symlink_to(link_target)
        for label, path in (("missing file", self.directory / "missing.jsonl"),
                            ("directory", self.directory), ("FIFO", fifo),
                            ("character device", Path("/dev/null"))):
            with self.subTest(label):
                completed = self.run_cli(path, "--trials-file", trials_file, expected=1)
                self.assert_refusal(completed, "hpo_input_file_invalid", None, 1)
                self.assertFalse(trials_file.exists())
        # A symlink to a regular file resolves to the same bytes and the same digests.
        direct = json.loads(self.run_cli(link_target).stdout)
        through = json.loads(self.run_cli(link).stdout)
        self.assertEqual(direct["candidate_list"], through["candidate_list"])

    def test_exact_limits_are_accepted(self) -> None:
        good = line(vector(10))
        exact_line = self.directory / "exact-line.jsonl"
        exact_line.write_bytes((good + " " * (65536 - len(good))).encode() + b"\n")
        result = json.loads(self.run_cli(exact_line).stdout)
        self.assertEqual(result["candidate_list"]["count"], 1)
        self.assertTrue(result["candidate_list"]["complete"])
        no_final_lf = self.directory / "no-final-lf.jsonl"
        no_final_lf.write_bytes(f"{good}\n{good}".encode())
        self.assertEqual(json.loads(self.run_cli(no_final_lf).stdout)["trials_completed"], 2)

    def test_flag_conflicts_are_usage_errors_and_warm_start_is_exit_four(self) -> None:
        path = self.write_list([vector(10), vector(11)])
        for label, extra in (("exhaustive policy", ("--candidate-policy", "exhaustive")),
                             ("without_replacement", ("--candidate-policy", "without_replacement")),
                             ("median pruner", ("--pruner", "median")),
                             ("halving pruner", ("--pruner", "halving")),
                             ("positive patience", ("--no-improvement-trials", "1"))):
            with self.subTest(label):
                self.assert_refusal(self.run_cli(path, *extra, expected=1),
                                    "hpo_cli_usage", None, 1)
        with self.subTest("sampler without file"):
            self.assert_refusal(self.run_cli(None, expected=1), "hpo_cli_usage", None, 1)
        with self.subTest("file without sampler"):
            self.assert_refusal(self.run_cli(path, sampler="grid", expected=1),
                                "hpo_cli_usage", None, 1)
        with self.subTest("patience zero is accepted"):
            result = json.loads(self.run_cli(path, "--no-improvement-trials", "0").stdout)
            self.assertNotIn("early_stop", result)
        with self.subTest("explicit default policy and pruner none"):
            self.run_cli(path, "--candidate-policy", "sampler_default", "--pruner", "none")
        with self.subTest("warm start is refused before anything is read"):
            self.assert_refusal(
                self.run_cli(path, "--warm-start", self.directory / "no-parent.json",
                             expected=4), "hpo_warm_start_rejected", None, 4)

    def test_fixed_input_conflict_is_refused_at_admission(self) -> None:
        path = self.write_list([vector(10)])
        trials_file = self.directory / "never-created.ndjson"
        completed = self.run_cli(path, "--fixed-input", "Length", "5", "--trials-file",
                                 trials_file, expected=1)
        self.assert_refusal(completed, "hpo_study_spec_invalid", "input", 1)
        self.assertFalse(trials_file.exists())

    def test_admission_precedes_plugin_and_dataset_loading(self) -> None:
        bogus = self.directory / "no-such-plugin"
        missing_data = self.directory / "no-such-data.csv"
        bad = self.directory / "bad.jsonl"
        bad.write_bytes(b'{"Length":0}\n')
        refused = self.run_cli(bad, expected=1, strategy=bogus, ohlcv=missing_data,
                               space=INTEGER_SPACE)
        self.assert_refusal(refused, "hpo_study_spec_invalid", "search_space", 1)
        good = self.write_list([{"Length": 10}], "good.jsonl")
        loaded = self.run_cli(good, expected=1, strategy=bogus, ohlcv=missing_data,
                              space=INTEGER_SPACE)
        self.assertEqual(json.loads(loaded.stdout)["failure"]["code"], "hpo_plugin_invalid")
        empty = self.directory / "empty.jsonl"
        empty.write_bytes(b"")
        self.assert_refusal(self.run_cli(empty, expected=1, strategy=bogus, ohlcv=missing_data,
                                         space=INTEGER_SPACE),
                            "hpo_study_spec_invalid", "sampler", 1)

    # ---- determinism across workers, fixed batch and lag ----------------------------------

    def test_fixed_batch_rows_are_identical_across_workers_and_lag(self) -> None:
        vectors = [vector(length, mult, fast, mode) for length, mult, fast, mode in (
            (61, 0.5, True, "fast"), (3, 0.0, False, "slow"), (61, 0.5, True, "fast"),
            (44, 1.0, True, "slow"), (17, 0.75, False, "fast"), (3, 0.0, False, "slow"),
            (90, 0.25, True, "fast"), (8, 0.5, False, "slow"), (52, 0.25, True, "slow"),
            (17, 0.75, False, "fast"), (29, 1.0, False, "fast"), (73, 0.0, True, "slow"))]
        path = self.write_list(vectors)
        reference = None
        for lag in ("0", "1"):
            for workers in (1, 2, 4, 8):
                with self.subTest(lag=lag, workers=workers):
                    completed = self.run_cli(path, "--workers", workers, "--batch-size", "4",
                                             "--batch-lag", lag)
                    encoded = self.trial_bytes(completed)
                    if reference is None:
                        reference = encoded
                    self.assertEqual(encoded, reference)
                    result = json.loads(completed.stdout)
                    self.assert_block(result, vectors, evaluated=12, scored=12, ranges=[],
                                      source=path)
        for workers in (1, 8):
            with self.subTest(default_batch_workers=workers):
                self.assertEqual(self.trial_bytes(self.run_cli(path, "--workers", workers)),
                                 reference)

    def test_progress_and_trials_file_equal_final_rows(self) -> None:
        vectors = [vector(length) for length in (20, 21, 22, 23, 24, 25, 26, 27)]
        path = self.write_list(vectors)
        trials_file = self.directory / "trials.ndjson"
        read_fd, write_fd = os.pipe()
        chunks: list[bytes] = []

        def drain() -> None:
            with os.fdopen(read_fd, "rb") as stream:
                chunks.append(stream.read())

        reader = threading.Thread(target=drain)
        reader.start()
        try:
            completed = self.run_cli(path, "--workers", 4, "--progress-fd", write_fd,
                                     "--trials-file", trials_file, pass_fds=(write_fd,))
        finally:
            os.close(write_fd)
            reader.join(timeout=30)
        result = json.loads(completed.stdout)
        rows = {trial["trial_id"]: trial for trial in result["trials"]}
        for source in (b"".join(chunks).decode().splitlines(),
                       trials_file.read_text().splitlines()):
            lines = [json.loads(item) for item in source]
            self.assertEqual([item["trial_id"] for item in lines], list(range(8)))
            self.assertTrue(all(rows[item["trial_id"]] == item for item in lines))
        self.assertEqual(result["candidate_list"]["evaluated"], 8)
        self.assertTrue(all("candidate_list" not in item for item in rows.values()))

    def test_best_k_and_none_keep_coverage(self) -> None:
        lengths = (11, 55, 23, 70, 31, 12)
        vectors = [vector(length) for length in lengths]
        path = self.write_list(vectors)
        for mode, expected_rows in (("best-k", 2), ("none", 0), ("all", 6)):
            with self.subTest(trials_out=mode):
                completed = self.run_cli(path, "--trials-out", mode, "--best-k", "2",
                                         "--workers", 3)
                result = json.loads(completed.stdout)
                self.assertEqual(len(result["trials"]), expected_rows)
                self.assertEqual(result["trials_completed"], 6)
                self.assert_block(result, vectors, evaluated=6, scored=6, ranges=[])
                self.assertTrue(result["candidate_list"]["complete"])

    # ---- row outcomes: constraints and engine errors are ordinary rows ----------------------

    def test_constraint_and_engine_errors_are_ordinary_terminal_rows(self) -> None:
        lengths = [3, 12, 14, 5, 20, 22, 9, 30, 11, 13]
        vectors = [vector(length) for length in lengths]
        path = self.write_list(vectors)
        completed = self.run_cli(path, "--workers", 3, "--constraint", CONSTRAINT,
                                 "--fixed-input", "SequenceScore", SEQUENCE)
        result = json.loads(completed.stdout)
        statuses = [trial["status"] for trial in result["trials"]]
        self.assertEqual(statuses, [expected_status(length) for length in lengths])
        self.assertEqual(statuses.count("engine_error"), 3)
        self.assertTrue(any(item == "constraint_violation" for item in statuses))
        scored = sum(item in ("ok", "constraint_violation") for item in statuses)
        self.assert_block(result, vectors, evaluated=10, scored=scored, ranges=[])
        self.assertTrue(result["candidate_list"]["complete"])
        self.assertIsNotNone(result["best_trial_id"])

    def test_complete_means_terminal_not_scored(self) -> None:
        infeasible = [vector(length) for length in (3, 5, 9)]
        completed = self.run_cli(self.write_list(infeasible), "--constraint", CONSTRAINT,
                                 "--fixed-input", "SequenceScore", SEQUENCE, expected=2)
        result = json.loads(completed.stdout)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["best_trial_id"])
        self.assert_block(result, infeasible, evaluated=3, scored=3, ranges=[])
        self.assertTrue(result["candidate_list"]["complete"])
        failing = [vector(length) for length in (2, 6, 10)]
        completed = self.run_cli(self.write_list(failing, "failing.jsonl"), "--constraint",
                                 CONSTRAINT, "--fixed-input", "SequenceScore", SEQUENCE,
                                 expected=2)
        result = json.loads(completed.stdout)
        self.assertEqual({trial["status"] for trial in result["trials"]}, {"engine_error"})
        block = self.assert_block(result, failing, evaluated=3, scored=0, ranges=[])
        self.assertTrue(block["complete"])

    # ---- external stops and the trial watchdog ------------------------------------------------

    def test_sigterm_reports_missing_positions(self) -> None:
        vectors = [vector(length) for length in (10, 11, 12, 13, 14)]
        path = self.write_list(vectors)
        entered_read, entered_write = os.pipe()
        release_read, release_write = os.pipe()
        child = subprocess.Popen(
            self.argv(path, "--workers", 1, "--fixed-input", "GateAtLength", "12",
                      "--fixed-input", "GateEnteredFd", entered_write,
                      "--fixed-input", "GateReleaseFd", release_read),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(entered_write, release_read),
        )
        os.close(entered_write)
        os.close(release_read)
        try:
            self.assertTrue(select.select([entered_read], [], [], 30)[0], "gate never entered")
            self.assertEqual(os.read(entered_read, 1), b"G")
            child.send_signal(signal.SIGTERM)
            time.sleep(0.4)
            os.write(release_write, b"R")
            stdout, stderr = child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            for descriptor in (entered_read, release_write):
                os.close(descriptor)
        self.assertEqual(child.returncode, 0, stderr)
        result = json.loads(stdout)
        self.assertEqual(result["stop_reason"], "cancelled")
        # The started row finishes; nothing after it is claimed.
        self.assertEqual([trial["trial_id"] for trial in result["trials"]], [0, 1, 2])
        block = self.assert_block(result, vectors, evaluated=3, scored=3, ranges=[[3, 4]])
        self.assertFalse(block["complete"])

    def test_deadline_reports_missing_positions(self) -> None:
        vectors = [vector(length) for length in range(10, 40)]
        path = self.write_list(vectors)
        completed = self.run_cli(path, "--workers", 1, "--max-wall-seconds", "0.8",
                                 "--fixed-input", "DelayMs", "120")
        result = json.loads(completed.stdout)
        self.assertEqual(result["stop_reason"], "deadline")
        rows = [trial["trial_id"] for trial in result["trials"]]
        self.assertEqual(rows, list(range(len(rows))))
        self.assertTrue(0 < len(rows) < 30)
        block = self.assert_block(result, vectors, evaluated=len(rows), scored=len(rows),
                                  ranges=[[len(rows), 29]])
        self.assertFalse(block["complete"])

    def test_trial_timeout_publishes_coverage_and_exits_three(self) -> None:
        lengths = [10, 11, 15, 12, 13, 14, 16, 17, 18, 19]
        vectors = [vector(length) for length in lengths]
        path = self.write_list(vectors)
        final = self.directory / "timeout.json"
        completed = self.run_cli(path, "--workers", 2, "--fixed-input", "HangAtLength", "15",
                                 "--fixed-input", "DelayMs", "40", "--trial-timeout-seconds",
                                 "0.5", "--output", final, expected=3)
        result = json.loads(completed.stdout)
        self.assertEqual(result, json.loads(final.read_text()))
        self.assertEqual(result["stop_reason"], "trial_timeout")
        timeouts = [trial for trial in result["trials"] if trial["status"] == "trial_timeout"]
        self.assertEqual([trial["trial_id"] for trial in timeouts], [2])
        present = [trial["trial_id"] for trial in result["trials"]]
        block = self.assert_block(result, vectors, evaluated=len(present),
                                  scored=len(present) - 1)
        self.assertFalse(block["complete"])
        self.assertNotIn(2, {item for first, last in block["unevaluated_ranges"]
                             for item in range(first, last + 1)})

    def test_fatal_progress_failure_keeps_coverage(self) -> None:
        vectors = [vector(length) for length in range(10, 40)]
        path = self.write_list(vectors)
        read_fd, write_fd = os.pipe()
        os.close(read_fd)  # the reader is gone: the first progress write fails
        try:
            completed = self.run_cli(path, "--workers", 2, "--progress-fd", write_fd,
                                     "--fixed-input", "DelayMs", "30", pass_fds=(write_fd,),
                                     expected=1)
        finally:
            os.close(write_fd)
        result = json.loads(completed.stdout)
        self.assertEqual(result["failure"]["code"], "hpo_output_io_failed")
        present = [trial["trial_id"] for trial in result["trials"]]
        self.assertEqual(present, sorted(present))
        # Whatever finished before the stop was observed is reported, and nothing else.
        self.assert_block(result, vectors, evaluated=len(present), scored=len(present))

    # ---- C -> TPE: history import through the current importer ----------------------------------

    def tpe_child(self, parent: Path, *extra, expected=0):
        return self.run_cli(None, "--sampler", "tpe", "--max-trials", "6", "--seed", "5",
                            "--warm-start", parent, "--batch-size", "2",
                            "--constraint", CONSTRAINT, *extra, expected=expected,
                            sampler=None)

    def test_candidate_result_seeds_tpe_as_rebuilt_history(self) -> None:
        lengths = [3, 12, 14, 5, 20, 22, 9, 30, 11, 13, 40, 41]
        vectors = [vector(length) for length in lengths]
        path = self.write_list(vectors)
        parent = self.directory / "parent.json"
        stream = self.directory / "parent.ndjson"
        completed = self.run_cli(path, "--constraint", CONSTRAINT, "--fixed-input",
                                 "SequenceScore", SEQUENCE, "--output", parent,
                                 "--trials-file", stream)
        document = json.loads(completed.stdout)
        self.assertNotIn("tpe_sampler_state", document)
        completed_rows = sum(trial["status"] in ("ok", "constraint_violation")
                             for trial in document["trials"])
        feasible_rows = sum(trial["status"] == "ok" for trial in document["trials"])
        reference = None
        for workers in (1, 2, 4, 8):
            with self.subTest(workers=workers):
                child = self.tpe_child(parent, "--workers", workers, expected=None)
                self.assertIn(child.returncode, (0, 2), child.stderr)
                result = json.loads(child.stdout)
                self.assertEqual(result["warm_start"]["trials"], 12)
                self.assertEqual(result["warm_start"]["completed"], completed_rows)
                self.assertEqual(result["warm_start"]["feasible"], feasible_rows)
                self.assertEqual(result["warm_start_model"], "rebuilt_history")
                self.assertEqual([trial["trial_id"] for trial in result["trials"]],
                                 list(range(12, 18)))
                self.assertEqual(result["trials_completed"], 6)
                self.assertNotIn("candidate_list", result)
                encoded = (child.returncode, self.rows_of(child))
                if reference is None:
                    reference = encoded
                self.assertEqual(encoded, reference)
        with self.subTest("trials-file parent"):
            child = self.tpe_child(stream, "--workers", 2, expected=None)
            self.assertEqual((child.returncode, self.rows_of(child)), reference)

    def test_partial_candidate_result_is_a_valid_history_and_summary_is_not(self) -> None:
        vectors = [vector(length) for length in range(10, 24)]
        path = self.write_list(vectors)
        partial = self.directory / "partial.json"
        self.run_cli(path, "--workers", 1, "--max-wall-seconds", "0.5", "--fixed-input",
                     "DelayMs", "150", "--constraint", CONSTRAINT, "--output", partial)
        rows = json.loads(partial.read_text())["trials"]
        self.assertTrue(0 < len(rows) < 14)
        child = self.tpe_child(partial, "--workers", 2, expected=None)
        self.assertIn(child.returncode, (0, 2), child.stderr)
        self.assertEqual(json.loads(child.stdout)["warm_start"]["trials"], len(rows))
        summary = self.directory / "summary.json"
        self.run_cli(path, "--trials-out", "best-k", "--best-k", "2", "--constraint",
                     CONSTRAINT, "--output", summary)
        refused = self.tpe_child(summary, "--workers", 2, expected=4)
        self.assertEqual(json.loads(refused.stdout)["failure"]["code"],
                         "hpo_warm_start_rejected")

    # ---- signed zero propagation (bit sensitive: read the raw text) -----------------------------

    def test_signed_zero_reaches_the_row_unchanged(self) -> None:
        space = (*INTEGER_SPACE, "--real-dim", "Level", "-1", "1", "continuous")
        path = self.directory / "zeros.jsonl"
        path.write_bytes(b'{"Length":5,"Level":-0.0}\n{"Length":5,"Level":0.0}\n'
                         b'{"Length":5,"Level":-0}\n')
        completed = self.run_cli(path, space=space)
        text = completed.stdout
        negative = re.findall(r'"Level": -0(?=[,}])', text)
        positive = re.findall(r'"Level": 0(?=[,}])', text)
        self.assertEqual((len(negative), len(positive)), (2, 1), text[:2000])
        result = json.loads(text)
        # One canonical identity for +0.0 and -0.0, but the original bytes differ.
        swapped = self.directory / "zeros-swapped.jsonl"
        swapped.write_bytes(b'{"Length":5,"Level":0.0}\n{"Length":5,"Level":-0.0}\n'
                            b'{"Length":5,"Level":0}\n')
        other = json.loads(self.run_cli(swapped, space=space).stdout)
        self.assertEqual(result["candidate_list"]["list_sha256"],
                         other["candidate_list"]["list_sha256"])
        self.assertNotEqual(result["candidate_list"]["source_sha256"],
                            other["candidate_list"]["source_sha256"])

    # ---- off path: nothing changes for the other samplers --------------------------------------

    def test_other_samplers_carry_no_candidate_metadata(self) -> None:
        for sampler in ("grid", "random", "tpe", "dlib_global"):
            with self.subTest(sampler=sampler):
                completed = self.run_cli(None, "--max-trials", "6", "--seed", "3", sampler=sampler,
                                         space=INTEGER_SPACE)
                result = json.loads(completed.stdout)
                self.assertNotIn("candidate_list", result)
                self.assertEqual(result["sampler"], sampler)
                self.assertNotEqual(result["sampler_implementation"],
                                    "pineforge_candidate_list_v1")

    def test_supplied_reference_off_bytes(self) -> None:
        reference = supplied_reference()
        if reference is None:
            self.skipTest("reference binary not supplied: no literal off-byte comparison made")
        (self.directory / "reference.json").write_text(json.dumps({
            "path": str(reference),
            "sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
            "contract": "literal stdout bytes; no normalization of version or build fields",
        }) + "\n")
        variants = (
            ("grid", ()), ("random", ("--max-trials", "12", "--seed", "4")),
            ("tpe", ("--max-trials", "12", "--seed", "4", "--batch-size", "4")),
            ("dlib_global", ("--max-trials", "12", "--seed", "4")),
            ("random", ("--max-trials", "12", "--seed", "4", "--no-improvement-trials", "3")),
            ("tpe", ("--max-trials", "12", "--seed", "4", "--pruner", "median")),
        )
        for sampler, extra in variants:
            for workers in (1, 4):
                with self.subTest(sampler=sampler, extra=extra, workers=workers):
                    flags = ("--workers", workers, *extra)
                    if sampler == "grid":
                        flags += ("--max-trials", "8")
                    old = self.run_cli(None, *flags, sampler=sampler, space=INTEGER_SPACE,
                                       native=reference, expected=None)
                    new = self.run_cli(None, *flags, sampler=sampler, space=INTEGER_SPACE,
                                       expected=None)
                    self.assertEqual(old.returncode, new.returncode)
                    self.assertEqual(old.stdout, new.stdout)

    def test_help_documents_the_sampler_and_flag(self) -> None:
        completed = subprocess.run([str(NATIVE), "--help"], text=True, capture_output=True,
                                   check=False, timeout=60)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--candidates", completed.stdout)
        self.assertIn("candidates", completed.stdout.split("--sampler", 1)[1].splitlines()[0])


if __name__ == "__main__":
    unittest.main()
