#!/usr/bin/env python3
"""CR-220 acceptance: real child RSS, slope-checker controls and raw trial bytes.

Usage: test_streamed_trial_retention.py NATIVE PLUGIN ARTIFACT_ROOT [unittest arguments]
ARTIFACT_ROOT must be an explicit build/scratch directory. Every run gets a new
subdirectory; artifacts are retained, never deleted. Run this unchanged against
parent and head binaries. Linux cannot skip the memory row.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import traceback
import unittest


COUNTS = (10, 1_000, 100_000)
SLOPE_LIMITS = (256, 32)  # Bytes/trial for 10->1,000 and 1,000->100,000.


def slopes(samples: list[tuple[int, int]]) -> list[float]:
    return [(right[1] - left[1]) / (right[0] - left[0])
            for left, right in zip(samples, samples[1:])]


def check_slopes(samples: list[tuple[int, int]]) -> None:
    if tuple(count for count, _ in samples) != COUNTS:
        raise AssertionError(f"RSS acceptance requires all three trial counts: {samples!r}")
    if any(peak <= 0 for _, peak in samples):
        raise AssertionError(f"missing child peak RSS: {samples!r}")
    for left, right, limit in zip(samples, samples[1:], SLOPE_LIMITS):
        delta = right[1] - left[1]
        trials = right[0] - left[0]
        # Compare integers so exactly the limit passes and a single extra byte fails.
        if delta > limit * trials:
            raise AssertionError(
                f"{left[0]:,}->{right[0]:,} trials: measured {delta / trials:.9f} "
                f"bytes/trial exceeds {limit}; peaks {left[1]} -> {right[1]} bytes"
            )


def peak_bytes(ru_maxrss: int) -> int:
    if sys.platform == "darwin":
        return int(ru_maxrss)
    if sys.platform.startswith("linux"):
        return int(ru_maxrss) * 1024
    raise AssertionError(f"ru_maxrss units are not defined for {sys.platform}")


def allowed_path(path: Path) -> Path:
    """Refuse protected paths by name before resolving, then check symlink targets."""
    def check(candidate: Path) -> None:
        parts = candidate.parts
        forbidden = {".ssh", ".aws", ".codex"}
        if any(part in forbidden or part.endswith(".env") for part in parts):
            raise AssertionError(f"refused protected path by name: {candidate}")
        for index in range(len(parts)):
            if parts[index:index + 3] == (".config", "zsh", "keys") or \
                    parts[index:index + 2] == (".config", "pineforge"):
                raise AssertionError(f"refused protected path by name: {candidate}")

    absolute = Path(os.path.abspath(path))
    check(absolute)
    resolved = absolute.resolve()
    check(resolved)
    return resolved


def list_paths(label: str, paths: list[Path]) -> list[Path]:
    checked = [allowed_path(path) for path in paths]
    print(f"FILES {label}:", flush=True)
    for path in checked:
        print(f"  {path}", flush=True)
    return checked


def write_json(path: Path, value: dict) -> None:
    path, = list_paths("result write", [path])
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def append_memory_summary(evidence: dict) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    path, = list_paths("GitHub step summary append", [Path(summary)])
    lines = [
        "\n### Streamed trial retention RSS (after)\n\n",
        "| Trials | Peak RSS bytes | Slope bytes/trial | Limit bytes/trial | Verdict |\n",
        "| ---: | ---: | ---: | ---: | :--- |\n",
    ]
    samples = evidence["samples"]
    for index, sample in enumerate(samples):
        slope = limit = "-"
        verdict = "BASELINE"
        if index:
            previous = samples[index - 1]
            delta = sample["peak_rss_bytes"] - previous["peak_rss_bytes"]
            trials = sample["trials"] - previous["trials"]
            limit = evidence["limits_bytes_per_trial"][index - 1]
            slope = f"{evidence['slopes_bytes_per_trial'][index - 1]:.9f}"
            verdict = "PASS" if delta <= limit * trials else "FAIL"
        lines.append(
            f"| {sample['trials']} | {sample['peak_rss_bytes']} | {slope} | {limit} | {verdict} |\n")
    lines.append(f"\nRSS verdict: **{evidence['verdict']}**\n")
    with path.open("a", encoding="utf-8") as output:
        output.write("".join(lines))


def run_child(label: str, command: list[str], inputs: list[Path], *,
              outputs: list[Path] | None = None, tmpdir: Path | None = None) -> dict:
    """wait4 reaps exactly this child; no wrapper/parent usage or exit code is sampled."""
    stdout = ROOT / f"{label}.stdout"
    stderr = ROOT / f"{label}.stderr"
    exit_file = ROOT / f"{label}.exit"
    result_file = ROOT / f"{label}.result.json"
    paths = [Path(command[0]), *inputs, *(outputs or []), stdout, stderr,
             exit_file, result_file, tmpdir or ROOT]
    list_paths(f"{label} (including the anonymous spool directory)", paths)
    print("COMMAND " + json.dumps(command), flush=True)
    environment = os.environ.copy()
    environment["TMPDIR"] = str(allowed_path(tmpdir or ROOT))
    with stdout.open("wb") as output, stderr.open("wb") as errors:
        child = subprocess.Popen(command, stdout=output, stderr=errors,
                                 cwd=ROOT, env=environment)
        while True:
            try:
                waited_pid, status, usage = os.wait4(child.pid, 0)
                break
            except InterruptedError:
                continue
        if waited_pid != child.pid:
            raise AssertionError(f"wait4 returned {waited_pid}, expected {child.pid}")
        child.returncode = os.waitstatus_to_exitcode(status)
        # A signalled work process records its negative signal exit, never an echo/wrapper code.
        exit_file.write_text(f"{child.returncode}\n", encoding="ascii")
    row = {
        "command": command,
        "pid": child.pid,
        "waited_pid": waited_pid,
        "exit_code": child.returncode,
        "ru_maxrss": usage.ru_maxrss,
        "peak_rss_bytes": peak_bytes(usage.ru_maxrss),
        "stdout": str(stdout),
        "stderr": str(stderr),
        "exit_file": str(exit_file),
        "result_file": str(result_file),
        "verdict": "FAIL",
        "reason": "work output has not passed its assertions",
    }
    write_json(result_file, row)
    return row


def pass_row(row: dict, **evidence: object) -> None:
    row.update(verdict="PASS", reason="work output passed its assertions", **evidence)
    write_json(Path(row["result_file"]), row)


def document_diagnostics(document: dict, first_trial: dict | None) -> str:
    first_fields = None
    if first_trial is not None:
        first_fields = {key: first_trial[key] for key in ("status", "error") if key in first_trial}
    return (
        f"CLI document reports ok={document.get('ok')!r}: "
        f"best_trial_id={document.get('best_trial_id')!r}, "
        f"trials_completed={document.get('trials_completed')!r}, first trial={first_fields!r}"
    )


def child_diagnostics(row: dict, *, include_document: bool = False) -> str:
    names = ("stdout", "stderr")
    paths = list_paths("child output diagnostics read", [Path(row[name]) for name in names])
    messages = [f"child exit code: {row['exit_code']}"]
    for name, path in zip(names, paths):
        with path.open("rb") as source:
            messages.append(f"{name} (first 2,000 bytes): {source.read(2_000)!r}")
    if include_document:
        with paths[0].open("rb") as source:
            header = bytearray()
            for line in source:
                if line == b'  "trials": [\n':
                    try:
                        document = json.loads(bytes(header) + b'  "trials": []\n}')
                        payload = source.readline().strip().removesuffix(b",")
                        first_trial = json.loads(payload) if payload.startswith(b"{") else None
                    except (json.JSONDecodeError, UnicodeDecodeError) as error:
                        # Keep the excerpts and the original nonzero-exit assertion on bad JSON.
                        messages.append(f"CLI document diagnostic could not be parsed: {error}")
                    else:
                        if document.get("ok") is False:
                            messages.append(document_diagnostics(document, first_trial))
                    break
                header.extend(line)
    return "\n".join(messages)


class StreamedTrialRetentionTests(unittest.TestCase):
    def command(self, trials: int, *extra: str) -> list[str]:
        return [
            str(NATIVE), "run", "--strategy", str(PLUGIN), "--ohlcv", str(CSV),
            "--objective", "metrics.all.net_profit", "--sampler", "random",
            "--seed", "17", "--workers", "1", "--batch-size", "1",
            "--schedule", "batches", "--pruner", "none",
            "--max-trials", str(trials), "--trials-out", "all",
            "--input-tf", "1", "--script-tf", "5",
            "--bar-magnifier", "true", "--magnifier-samples", "6",
            "--magnifier-distribution", "triangle",
            "--real-dim", "Length", "1", "100", "continuous", *extra,
        ]

    def completed_document(self, row: dict, trials: int) -> dict:
        stdout = Path(row["stdout"])
        stderr = Path(row["stderr"])
        list_paths("CLI document read", [stdout, stderr])
        self.assertEqual(
            row["exit_code"], 0,
            child_diagnostics(row, include_document=True) if row["exit_code"] != 0 else None)
        # Parse one row at a time. A large Python document retained between children could
        # contaminate a forked child's initial RSS on platforms that account for that memory.
        header = bytearray()
        observed = 0
        trial_ids = set() if trials == 1_000 else None
        smallest_row_bytes = None
        first_trial = None
        with stdout.open("rb") as source:
            for line in source:
                if line == b'  "trials": [\n':
                    break
                header.extend(line)
            else:
                self.fail("CLI document has no trials array")
            # Check ok before a failed first trial can mask the document-level diagnostic.
            document = json.loads(bytes(header) + b'  "trials": []\n}')
            for line in source:
                if line.startswith(b"  ]"):
                    closing = line
                    break
                self.assertTrue(line.startswith(b"    ") and line.endswith(b"\n"))
                payload = line[4:-1]
                if observed + 1 < trials:
                    self.assertTrue(payload.endswith(b","), "missing trial-array separator")
                    payload = payload[:-1]
                trial = json.loads(payload)
                if observed == 0:
                    first_trial = {key: trial[key] for key in ("status", "error") if key in trial}
                    self.assertTrue(document["ok"], document_diagnostics(document, first_trial))
                self.assertEqual(trial["trial_id"], observed)
                self.assertEqual(trial["status"], "ok")
                self.assertTrue(trial["feasible"])
                if trial_ids is not None:
                    trial_ids.add(trial["trial_id"])
                    size = len(payload)
                    smallest_row_bytes = (
                        size if smallest_row_bytes is None else min(smallest_row_bytes, size))
                observed += 1
            else:
                self.fail("CLI document has an incomplete trials array")
            metadata = bytes(header) + b'  "trials": []' + closing[3:] + source.read()
        document = json.loads(metadata)
        self.assertTrue(document["ok"], document_diagnostics(document, first_trial))
        self.assertNotIn("failure", document)
        self.assertEqual(document["trials_out"], "all")
        self.assertEqual(document["sampler"], "random")
        self.assertEqual(document["trials_requested"], trials)
        self.assertEqual(document["trials_completed"], trials)
        self.assertEqual(document["stop_reason"], "trial_budget_reached")
        # This also catches clipping, missing/duplicate IDs and an early stop hidden by a count.
        self.assertEqual(observed, trials)
        row["trial_rows"] = observed
        if trial_ids is not None:
            self.assertEqual(observed, 1_000, "stdout must contain exactly 1,000 trial rows")
            self.assertEqual(len(trial_ids), 1_000, "stdout must contain 1,000 distinct trial IDs")
            self.assertIsNotNone(smallest_row_bytes, "stdout has no trial row size")
            self.assertGreater(smallest_row_bytes, 0)
            row.update(distinct_trial_ids=len(trial_ids),
                       smallest_trial_row_bytes=smallest_row_bytes)
        return document

    def test_00_slope_checker_controls(self) -> None:
        base = 16 * 1024 * 1024
        check_slopes([(count, base) for count in COUNTS])
        with self.assertRaises(AssertionError):
            check_slopes([(count, base + 512 * count) for count in COUNTS])
        exact = [
            (COUNTS[0], base),
            (COUNTS[1], base + (COUNTS[1] - COUNTS[0]) * SLOPE_LIMITS[0]),
        ]
        exact.append((COUNTS[2], exact[1][1] +
                      (COUNTS[2] - COUNTS[1]) * SLOPE_LIMITS[1]))
        check_slopes(exact)
        # Independently exercise both edges, including one byte beyond the total RSS allowance.
        for edge in (1, 2):
            over = [(count, peak + (1 if index >= edge else 0))
                    for index, (count, peak) in enumerate(exact)]
            with self.assertRaises(AssertionError):
                check_slopes(over)
        with self.assertRaises(AssertionError):
            check_slopes(exact[:2])
        write_json(ROOT / "slope-controls.result.json", {
            "verdict": "PASS",
            "checks": ["flat", "linear growth rejected", "exact limits",
                       "one byte over each limit rejected", "missing 100000 row rejected"],
        })

    def test_01_crash_records_the_work_exit(self) -> None:
        row = run_child("deliberate-crash", [
            sys.executable, "-I", "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
        ], [])
        self.assertEqual(row["exit_code"], -signal.SIGKILL, child_diagnostics(row))
        exit_file = Path(row["exit_file"])
        list_paths("deliberate crash marker read", [exit_file])
        self.assertEqual(int(exit_file.read_text(encoding="ascii")), -signal.SIGKILL)
        self.assertEqual(row["pid"], row["waited_pid"])
        pass_row(row, check="wait4 observed the deliberate SIGKILL and wrote its nonzero exit")

    def test_02_peak_rss_is_bounded(self) -> None:
        if sys.platform == "darwin":
            reason = "Linux CI owns the RSS slope gate; macOS allocator RSS is not this gate"
            print("SKIP memory row: " + reason, flush=True)
            self.skipTest(reason)
        self.assertTrue(sys.platform.startswith("linux"), "RSS gate requires Linux CI")
        self.assertTrue(hasattr(os, "wait4"), "Linux RSS gate must run with os.wait4")
        samples = []
        smallest_row_bytes = None
        retention_guard = {}
        for trials in COUNTS:
            row = run_child(f"rss-{trials}", self.command(trials), [PLUGIN, MANIFEST, CSV])
            self.completed_document(row, trials)
            if trials == 1_000:
                smallest_row_bytes = row["smallest_trial_row_bytes"]
                retention_guard.update(
                    trial_rows_at_1000=row["trial_rows"],
                    distinct_trial_ids_at_1000=row["distinct_trial_ids"],
                    smallest_trial_row_bytes_at_1000=smallest_row_bytes)
            elif trials == 100_000:
                self.assertIsNotNone(smallest_row_bytes, "missing 1,000-trial row-size evidence")
                stdout, = list_paths("retention stdout size read", [Path(row["stdout"])])
                stdout_bytes = stdout.stat().st_size
                # Integer comparison: stdout must exceed N times half the smallest 1,000-run row.
                self.assertGreater(
                    2 * stdout_bytes, trials * smallest_row_bytes,
                    f"{stdout_bytes} stdout bytes must exceed {trials} times half of "
                    f"the smallest 1,000-trial row ({smallest_row_bytes} bytes)")
                row.update(stdout_size_bytes=stdout_bytes,
                           smallest_trial_row_bytes_at_1000=smallest_row_bytes)
                retention_guard["stdout_size_bytes_at_100000"] = stdout_bytes
            samples.append((trials, row["peak_rss_bytes"]))
            pass_row(row, trials_completed=trials, check="complete CLI document and ordered rows")
        values = slopes(samples)
        print("\ntrials\tpeak RSS (bytes)\tslope (bytes/trial)", flush=True)
        for index, (trials, peak) in enumerate(samples):
            slope = "-" if index == 0 else f"{values[index - 1]:.9f}"
            print(f"{trials}\t{peak}\t{slope}", flush=True)
        evidence = {
            "samples": [{"trials": trials, "peak_rss_bytes": peak} for trials, peak in samples],
            "slopes_bytes_per_trial": values,
            "limits_bytes_per_trial": list(SLOPE_LIMITS),
            "retention_guard": retention_guard,
            "verdict": "FAIL",
        }
        write_json(ROOT / "memory.result.json", evidence)
        try:
            check_slopes(samples)
            evidence["verdict"] = "PASS"
            write_json(ROOT / "memory.result.json", evidence)
        finally:
            append_memory_summary(evidence)

    def test_03_trials_file_bytes_match_result(self) -> None:
        trials_file = ROOT / "equality.trials.jsonl"
        final = ROOT / "equality.final.json"
        row = run_child("equality-1000", self.command(
            1_000, "--trials-file", str(trials_file), "--output", str(final)),
            [PLUGIN, MANIFEST, CSV], outputs=[trials_file, final])
        self.completed_document(row, 1_000)
        stdout = Path(row["stdout"])
        list_paths("byte equality read", [trials_file, final, stdout])
        result_bytes = stdout.read_bytes()
        self.assertEqual(final.read_bytes(), result_bytes)
        ndjson = trials_file.read_bytes()
        self.assertTrue(ndjson.endswith(b"\n"))
        rows = ndjson.splitlines(keepends=True)
        self.assertEqual(len(rows), 1_000)
        expected = b"".join(
            b"    " + line[:-1] + (b",\n" if index + 1 < len(rows) else b"\n")
            for index, line in enumerate(rows)
        )
        start = result_bytes.index(b'  "trials": [\n') + len(b'  "trials": [\n')
        end = result_bytes.index(b"  ]", start)
        # No JSON decoding or reserialization in this comparison: every row byte is observed.
        self.assertEqual(result_bytes[start:end], expected)
        pass_row(row, trials_completed=1_000,
                 check="raw trials-file rows equal the final array bytes; stdout equals output")

    def test_04_output_alias_preserves_document(self) -> None:
        shared = ROOT / "shared-output-and-trials.json"
        row = run_child("output-alias-1000", self.command(
            1_000, "--trials-file", str(shared), "--output", str(shared)),
            [PLUGIN, MANIFEST, CSV], outputs=[shared])
        self.completed_document(row, 1_000)
        stdout = Path(row["stdout"])
        list_paths("output alias read", [shared, stdout])
        self.assertEqual(shared.read_bytes(), stdout.read_bytes())
        pass_row(row, trials_completed=1_000,
                 check="output overwrites its trials-file path with the complete JSON")

    def test_05_spool_failures_are_typed_refusals(self) -> None:
        missing = ROOT / "missing-spool-directory"
        row = run_child("spool-create-refused", self.command(10),
                        [PLUGIN, MANIFEST, CSV], tmpdir=missing)
        self.assert_output_refusal(row)
        if sys.platform.startswith("linux"):
            full = Path("/dev/full")
            row = run_child("trials-file-write-refused",
                            self.command(10, "--trials-file", str(full)),
                            [PLUGIN, MANIFEST, CSV], outputs=[full])
            stdout, stderr = list_paths(
                "ordered trials-file failure read", [Path(row["stdout"]), Path(row["stderr"])])
            self.assertEqual(row["exit_code"], 1, child_diagnostics(row))
            document = json.loads(stdout.read_bytes())
            self.assertEqual(document["failure"], {
                "origin": "hpo", "code": "hpo_output_io_failed", "args": {}, "exit_code": 1,
            })
            self.assertIn("failed writing terminal trial to --trials-file",
                          stderr.read_text(encoding="utf-8"))
            # The parent still published the archived terminal table after an ordered stream
            # failed. This is independent of a refusal to create or complete the all-mode spool.
            self.assertTrue(document["ok"])
            self.assertEqual(document["stop_reason"], "cancelled")
            self.assertEqual(document["trials_out"], "all")
            self.assertGreater(document["trials_completed"], 0)
            self.assertLessEqual(document["trials_completed"], 10)
            self.assertEqual(len(document["trials"]), document["trials_completed"])
            self.assertEqual([trial["trial_id"] for trial in document["trials"]],
                             list(range(document["trials_completed"])))
            self.assertTrue(all(trial["status"] == "ok" for trial in document["trials"]))
            pass_row(row, trials_completed=document["trials_completed"],
                     check="parent ordered-stream failure keeps the complete terminal array")

    def test_06_default_all_does_not_backpressure_completions(self) -> None:
        """Mutation control: a default all-mode run must refill the free rolling worker."""
        lengths = [2, 1, 3, 5, 7, 9, 11, 13]
        candidates, bars = list_paths(
            "rolling guard fixture write", [ROOT / "rolling.candidates.jsonl",
                                           ROOT / "rolling.bars.csv"])
        candidates.write_text(
            "".join(json.dumps({"Length": length}, separators=(",", ":")) + "\n"
                    for length in lengths), encoding="utf-8")
        bars.write_text(
            "timestamp,open,high,low,close,volume\n"
            + "".join(f"{1700000000000 + index * 60000},100,102,99,101,10\n"
                      for index in range(64)), encoding="utf-8")
        command = [
            str(NATIVE), "run", "--strategy", str(PLUGIN), "--ohlcv", str(bars),
            "--objective", "metrics.all.net_profit", "--sampler", "candidates",
            "--max-trials", str(len(lengths)), "--seed", "90210", "--workers", "2",
            "--input-tf", "1", "--script-tf", "5", "--chart-timezone", "Asia/Taipei",
            "--bar-magnifier", "true", "--magnifier-samples", "6",
            "--magnifier-distribution", "triangle", "--fixed-input", "BatchPrefixTest", "1",
            "--int-dim", "Length", "1", "100", "1", "--trials-out", "all",
            "--schedule", "rolling", "--candidates", str(candidates),
            "--fixed-input", "BatchPrefixJitter", "0",
            "--fixed-input", "SequenceDelayMs", "1000,15",
        ]
        self.assertNotIn("--trials-file", command)
        self.assertNotIn("--progress-fd", command)
        row = run_child("default-all-rolling", command, [PLUGIN, MANIFEST, bars, candidates])
        stdout, = list_paths("rolling guard document read", [Path(row["stdout"])])
        self.assertEqual(row["exit_code"], 0, child_diagnostics(row))
        document = json.loads(stdout.read_bytes())
        self.assertTrue(document["ok"])
        self.assertNotIn("failure", document)
        self.assertEqual(document["trials_out"], "all")
        self.assertEqual(document["trials_completed"], len(lengths))
        self.assertEqual(document["completion_order"], [1, 2, 3, 4, 5, 6, 7, 0])
        self.assertEqual(document["completed_before"], [0, 0, 1, 2, 3, 4, 5, 6])
        self.assertEqual([trial["trial_id"] for trial in document["trials"]],
                         list(range(len(lengths))))
        self.assertEqual([trial["parameters"]["Length"] for trial in document["trials"]], lengths)
        self.assertEqual(document["candidate_list"]["evaluated"], len(lengths))
        self.assertTrue(all(trial["status"] == "ok" for trial in document["trials"]))
        pass_row(row, trials_completed=len(lengths),
                 completion_order=document["completion_order"],
                 completed_before=document["completed_before"],
                 check="all-mode spool never blocks rolling completion behind trial zero")

    def test_07_default_all_keeps_candidate_list_id_gaps(self) -> None:
        # Two occurrences hang; the watchdog records one timeout while the other has no row.
        # The quick first/last positions leave an interior hole whichever hang times out first.
        lengths = [1, 2, 2, 3]
        candidates, = list_paths(
            "candidate gap list write", [ROOT / "gaps.candidates.jsonl"])
        candidates.write_text(
            "".join(json.dumps({"Length": length}, separators=(",", ":")) + "\n"
                    for length in lengths), encoding="utf-8")
        command = [
            str(NATIVE), "run", "--strategy", str(PLUGIN), "--ohlcv", str(CSV),
            "--objective", "metrics.all.net_profit", "--sampler", "candidates",
            "--candidates", str(candidates), "--workers", "4",
            "--input-tf", "1", "--script-tf", "5",
            "--bar-magnifier", "true", "--magnifier-samples", "6",
            "--magnifier-distribution", "triangle",
            "--int-dim", "Length", "1", "100", "1",
            "--fixed-input", "HangAtLength", "2", "--trial-timeout-seconds", "0.5",
        ]
        row = run_child("default-all-candidate-gaps", command, [PLUGIN, MANIFEST, CSV, candidates])
        stdout, = list_paths("candidate gap document read", [Path(row["stdout"])])
        self.assertEqual(row["exit_code"], 3, child_diagnostics(row))
        document = json.loads(stdout.read_bytes())
        self.assertTrue(document["ok"])
        self.assertNotIn("failure", document)
        self.assertEqual(document["trials_out"], "all")
        self.assertEqual(document["stop_reason"], "trial_timeout")
        self.assertEqual(document["trials_completed"], 3)
        block = document["candidate_list"]
        self.assertEqual(block["count"], len(lengths))
        self.assertEqual(block["evaluated"], 3)
        self.assertEqual(block["scored"], 2)
        self.assertFalse(block["complete"])
        self.assertEqual(len(block["unevaluated_ranges"]), 1)
        missing, last = block["unevaluated_ranges"][0]
        self.assertEqual(missing, last)
        self.assertIn(missing, (1, 2))
        evaluated_ids = [position for position in range(len(lengths)) if position != missing]
        trials = document["trials"]
        self.assertEqual([trial["trial_id"] for trial in trials], evaluated_ids)
        self.assertEqual([trial["trial_id"] for trial in trials if trial["status"] == "ok"], [0, 3])
        self.assertEqual(
            [trial["trial_id"] for trial in trials if trial["status"] == "trial_timeout"],
            [position for position in (1, 2) if position != missing])
        self.assertEqual([trial["parameters"]["Length"] for trial in trials],
                         [lengths[position] for position in evaluated_ids])
        pass_row(row, trials_completed=3, evaluated_ids=evaluated_ids,
                 unevaluated_ranges=block["unevaluated_ranges"],
                 check="default all-mode rows equal evaluated candidate positions with an ID gap")

    def assert_output_refusal(self, row: dict) -> None:
        stdout = Path(row["stdout"])
        list_paths("spool refusal document read", [stdout])
        self.assertEqual(row["exit_code"], 1, child_diagnostics(row))
        document = json.loads(stdout.read_bytes())
        self.assertEqual(document["failure"]["code"], "hpo_output_io_failed")
        self.assertNotIn("trials", document, "a partial spool was published as a trial array")
        pass_row(row, check="work failure document reports hpo_output_io_failed without trials")


def main() -> int:
    global NATIVE, PLUGIN, MANIFEST, ROOT, CSV
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    NATIVE, PLUGIN, artifact_root = list_paths(
        "test arguments", [Path(value) for value in sys.argv[1:4]])
    del sys.argv[1:4]
    MANIFEST = allowed_path(PLUGIN.parent / "manifest.json")
    list_paths("fixture manifest and artifact directory", [MANIFEST, artifact_root])
    artifact_root.mkdir(parents=True, exist_ok=True)
    ROOT = allowed_path(Path(tempfile.mkdtemp(prefix="run-", dir=artifact_root)))
    CSV = ROOT / "bars.csv"
    complete = ROOT / "RUN.COMPLETE"
    own_exit = ROOT / "suite.exit"
    list_paths("fixture and suite result files", [CSV, complete, own_exit])
    result = None
    code = 1
    try:
        CSV.write_text("timestamp,open,high,low,close,volume\n"
                       "1700000000000,100,102,99,101,10\n"
                       "1700000060000,100,102,99,101,10\n", encoding="utf-8")
        program = unittest.main(verbosity=2, failfast=True, exit=False)
        result = program.result
        # A Linux skip is a failed gate even if unittest considers the suite successful.
        linux_skips = sys.platform.startswith("linux") and bool(result.skipped)
        code = 0 if result.wasSuccessful() and not linux_skips else 1
    except BaseException:
        code = 1
        traceback.print_exc()
    finally:
        if sys.exc_info()[0] is not None:
            code = 1
        try:
            write_json(complete, {
                "verdict": "PASS" if code == 0 else "FAIL",
                "tests_run": result.testsRun if result is not None else 0,
                "failures": len(result.failures) if result is not None else 0,
                "errors": len(result.errors) if result is not None else 0,
                "skips": [(str(test), reason) for test, reason in result.skipped] if result else [],
            })
            print(f"VERDICT FILE {complete}", flush=True)
        except BaseException:
            code = 1
            raise
        finally:
            # This is this test program's own exit, including an unexpected Python failure.
            own_exit.write_text(f"{code}\n", encoding="ascii")
    return code


if __name__ == "__main__":
    sys.exit(main())
