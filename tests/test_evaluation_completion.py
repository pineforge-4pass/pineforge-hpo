#!/usr/bin/env python3
"""Pin external stops at evaluation completion, separately from real output backpressure.

All evidence stays under the caller's directory, including failed admissions and a deliberate
crash control. No sleeps position events. Real-time timeouts detect a stuck test protocol only;
the study clock advances from zero to exactly its one-second deadline on a pipe command.
"""

from __future__ import annotations

import array
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import resource
import select
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest


PRODUCTION, CONTROLLED, PLUGIN, EVIDENCE = (Path(value).absolute() for value in sys.argv[1:5])
del sys.argv[1:5]
SAMPLERS = ("grid", "random", "dlib_global", "tpe", "sobol")
SCHEDULES = ("batches", "rolling")
PROTOCOL_TIMEOUT = 30
PAYLOAD = "m" * 16384


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def safe_path(path):
    """Refuse forbidden names and symlinks without resolving through credential directories."""
    named = Path(path).absolute()
    path = Path(os.path.abspath(named))
    forbidden = ("/.config/zsh/keys", "/.ssh", "/.aws", "/.codex", "/.config/pineforge")
    for candidate in (named, path):
        text = str(candidate)
        if any(name + "/" in text or text.endswith(name) for name in forbidden):
            raise AssertionError(f"refused credential path by name: {candidate}")
        if candidate.name == ".env" or candidate.suffix == ".env" or ".env." in candidate.name:
            raise AssertionError(f"refused environment path by name: {candidate}")
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise AssertionError(f"refused symlink without following it: {component}")
    return path


def record_process(directory, child):
    # Raw Popen returncode belongs to the work: a signal is negative, never a fabricated zero.
    write_json(directory / "process.json", {"returncode": child.returncode})
    (directory / "work.exit").write_text(str(child.returncode) + "\n", encoding="ascii")


class Pipe:
    def __init__(self):
        self.reader, self.writer = os.pipe()

    def close_reader(self):
        if self.reader >= 0:
            os.close(self.reader)
            self.reader = -1

    def close_writer(self):
        if self.writer >= 0:
            os.close(self.writer)
            self.writer = -1

    def close(self):
        self.close_reader()
        self.close_writer()


class EvaluationCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # No system temporary directory and no automatic evidence deletion.
        for path in (PRODUCTION, CONTROLLED, PLUGIN, EVIDENCE):
            safe_path(path)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        cls.run_directory = Path(tempfile.mkdtemp(prefix="run-", dir=EVIDENCE))
        print(f"EVALUATION_COMPLETION_EVIDENCE {cls.run_directory}", flush=True)

    def setUp(self):
        self.directory = self.run_directory / self._testMethodName
        self.directory.mkdir()
        self.serial = 0
        self.csv = self.directory / "bars.csv"
        self.csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            + "".join(f"{1700000000000 + index * 60000},100,102,99,101,10\n"
                      for index in range(64)), encoding="utf-8")

    def case_directory(self, label):
        self.serial += 1
        directory = self.directory / f"{self.serial:03d}-{label}"
        directory.mkdir()
        return directory

    def arguments(self, directory, sampler, schedule, patience):
        args = [
            "run", "--strategy", str(PLUGIN), "--ohlcv", str(self.csv),
            "--objective", "metrics.all.net_profit", "--sampler", sampler,
            "--seed", "73", "--max-trials", "2", "--workers", "1",
            "--schedule", schedule, "--int-dim", "Length", "0", "255", "1",
            "--categorical-choice", "Payload", PAYLOAD,
            "--input-tf", "1", "--script-tf", "5", "--bar-magnifier", "true",
            "--magnifier-samples", "6", "--magnifier-distribution", "triangle",
            "--fixed-input", "BatchPrefixTest", "1",
            "--fixed-input", "BatchPrefixJitter", "0",
            "--fixed-input", "SequenceScore", "10",
            "--output", str(directory / "final.json"),
            "--trials-file", str(directory / "trials.jsonl"),
        ]
        if schedule == "batches":
            args += ["--batch-size", "2", "--batch-lag", "0"]
        if patience is not None:
            args += ["--no-improvement-trials", str(patience)]
        return args

    def list_files(self, directory, executable, args):
        paths = [safe_path(executable)]
        for index, arg in enumerate(args):
            if arg in {"--strategy", "--ohlcv", "--output", "--trials-file"}:
                paths.append(safe_path(args[index + 1]))
        paths += [safe_path(directory / name) for name in (
            "stdout", "stderr", "progress.jsonl", "engine-completions", "followed-files")]
        write_json(directory / "files.json", [str(path) for path in paths])
        print("EVALUATION_COMPLETION_FILES " + json.dumps([str(path) for path in paths]),
              flush=True)

    def assert_result(self, directory, child, expected_stop, patience, schedule):
        self.assertEqual(child.returncode, 0, (directory / "stderr").read_bytes())
        raw = (directory / "stdout").read_bytes()
        document = json.loads(raw)
        write_json(directory / "result.json", document)
        self.assertNotIn("failure", document)
        self.assertEqual((directory / "final.json").read_bytes(), raw)
        progress = (directory / "progress.jsonl").read_bytes()
        self.assertEqual(progress, (directory / "trials.jsonl").read_bytes())
        self.assertEqual([json.loads(line) for line in progress.splitlines()], document["trials"])
        self.assertEqual(document["trials_completed"], 2)
        self.assertEqual([row["trial_id"] for row in document["trials"]], [0, 1])
        # Cooperative cancellation/deadlines drain the started trial as an ordinary ok record.
        self.assertEqual([row["status"] for row in document["trials"]], ["ok", "ok"])
        self.assertTrue(all(row["feasible"] and row["objective"] == 10
                            for row in document["trials"]))
        self.assertEqual((document["best_trial_id"], document["best_value"]), (0, 10))
        if schedule == "rolling":
            self.assertEqual(document["completion_order"], [0, 1])
        if patience:
            self.assertEqual(document["early_stop"], {
                "patience_trials": patience,
                "trigger_trial_id": 1 if patience == 1 else None,
                "drained_through_trial_id": 1,
                "reference_scope": "part",
            })
        else:
            self.assertNotIn("early_stop", document)
        # Read the verdict from the genuine result, never just the process exit status.
        self.assertEqual(document["stop_reason"], expected_stop,
                         "evaluation-completion observation lost or cleanup relabelled the run")
        return document

    def plain_run(self, executable, sampler, schedule, patience):
        directory = self.case_directory(f"plain-{executable.name}-{sampler}-{schedule}-p{patience}")
        args = self.arguments(directory, sampler, schedule, patience)
        with (directory / "progress.jsonl").open("wb") as progress, (
                directory / "followed-files").open("wb") as manifest:
            args += ["--progress-fd", str(progress.fileno())]
            command = [str(executable)]
            descriptors = [progress.fileno()]
            if executable == CONTROLLED:
                command += ["--test-channels", "-1", "-1", str(manifest.fileno())]
                descriptors.append(manifest.fileno())
            command += args
            self.list_files(directory, executable, args)
            write_json(directory / "command.json", command)
            with (directory / "stdout").open("wb") as out, (
                    directory / "stderr").open("wb") as err:
                child = subprocess.Popen(command, stdout=out, stderr=err,
                                         pass_fds=tuple(descriptors),
                                         env={**os.environ, "TMPDIR": str(directory)})
                try:
                    child.wait(timeout=PROTOCOL_TIMEOUT)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
                    record_process(directory, child)
        expected = "no_improvement" if patience == 1 else "trial_budget_reached"
        self.assert_result(directory, child, expected, patience, schedule)
        return directory

    def wait_token(self, descriptor, token, receipt):
        ready = select.select([descriptor], [], [], PROTOCOL_TIMEOUT)[0]
        self.assertTrue(ready, f"test protocol stalled waiting for {token!r}")
        actual = os.read(descriptor, 1)
        self.assertEqual(actual, token, f"unexpected gate receipt: {receipt}")
        receipt["events"].append({"received": actual.decode("ascii")})

    def send_command(self, descriptor, command, receipt):
        self.assertEqual(os.write(descriptor, command), 1)
        receipt["events"].append({"sent": command.decode("ascii")})

    def intervene(self, child, commands, events, intervention, receipt):
        if intervention == "signal":
            child.send_signal(signal.SIGTERM)
            receipt["events"].append({"signal_sent": "SIGTERM"})
        token = b"S" if intervention == "signal" else b"D"
        self.send_command(commands, token, receipt)
        self.wait_token(events, token, receipt)
        receipt["intervention_acknowledged"] = True
        if intervention == "deadline":
            receipt["study_elapsed_seconds"] = 1

    def queued_bytes(self, descriptor):
        queued = array.array("i", [0])
        fcntl.ioctl(descriptor, termios.FIONREAD, queued, True)
        return queued[0]

    def wait_full_fifo(self, descriptor, capacity):
        # Readiness/byte count establish the event. The real clock bounds protocol failure only.
        limit = time.monotonic() + PROTOCOL_TIMEOUT
        while self.queued_bytes(descriptor) != capacity:
            remaining = limit - time.monotonic()
            self.assertGreater(remaining, 0, "trials-file writer never filled the held FIFO")
            self.assertTrue(select.select([descriptor], [], [], remaining)[0])

    def drain_fifo(self, descriptor, destination):
        with destination.open("wb") as output:
            while True:
                self.assertTrue(select.select([descriptor], [], [], PROTOCOL_TIMEOUT)[0],
                                "trials-file drain stalled")
                try:
                    chunk = os.read(descriptor, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    return
                output.write(chunk)

    def controlled_run(self, sampler, schedule, patience, intervention, phase, *, wall=True):
        directory = self.case_directory(
            f"{phase}-{intervention}-{sampler}-{schedule}-p{patience}-wall{int(wall)}")
        receipt = dict(phase=phase, sampler=sampler, schedule=schedule, patience=patience,
                       intervention=intervention, wall_enabled=wall, events=[],
                       intervention_acknowledged=False)
        ordinary = "no_improvement" if patience == 1 else "trial_budget_reached"
        should_observe = (
            (phase == "evaluation" or (phase == "boundary" and bool(patience)))
            and (intervention == "signal" or wall))
        observed_stop = "cancelled" if intervention == "signal" else "deadline"
        expected = observed_stop if should_observe else ordinary
        receipt["expected_stop_reason"] = expected
        child = None
        verdict = {"verdict": "FAIL", "expected_stop_reason": expected,
                   "reason": "protocol or result assertions incomplete"}
        try:
            with ExitStack() as stack:
                commands, events, entered, release = (Pipe() for _ in range(4))
                for pipe in (commands, events, entered, release):
                    stack.callback(pipe.close)
                progress = stack.enter_context((directory / "progress.jsonl").open("wb"))
                arrivals = stack.enter_context((directory / "engine-completions").open("wb"))
                manifest = stack.enter_context((directory / "followed-files").open("wb"))
                args = self.arguments(directory, sampler, schedule, patience)
                args += ["--progress-fd", str(progress.fileno()),
                         "--fixed-input", "CompletionTraceFd", str(arrivals.fileno())]
                if intervention == "deadline" and wall:
                    args += ["--max-wall-seconds", "1"]
                fifo_fd = None
                if phase == "drain":
                    fifo = directory / "terminal.fifo"
                    os.mkfifo(fifo)
                    fifo_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
                    stack.callback(os.close, fifo_fd)
                    fcntl.fcntl(fifo_fd, fcntl.F_SETPIPE_SZ, 4096)
                    capacity = fcntl.fcntl(fifo_fd, fcntl.F_GETPIPE_SZ)
                    self.assertEqual(capacity, 4096)
                    args[args.index("--trials-file") + 1] = str(fifo)
                    receipt["fifo_capacity"] = capacity
                descriptors = [commands.reader, events.writer, progress.fileno(),
                               arrivals.fileno(), manifest.fileno()]
                if phase == "evaluation":
                    self.assertEqual(sampler, "grid")
                    args += ["--fixed-input", "GateAtLength", "1",
                             "--fixed-input", "GateEnteredFd", str(entered.writer),
                             "--fixed-input", "GateReleaseFd", str(release.reader)]
                    descriptors += [entered.writer, release.reader]
                command = [str(CONTROLLED), "--test-channels", str(commands.reader),
                           str(events.writer), str(manifest.fileno()), *args]
                self.list_files(directory, CONTROLLED, args)
                write_json(directory / "command.json", command)
                out = stack.enter_context((directory / "stdout").open("wb"))
                err = stack.enter_context((directory / "stderr").open("wb"))
                child = subprocess.Popen(command, stdout=out, stderr=err,
                                         pass_fds=tuple(descriptors),
                                         env={**os.environ, "TMPDIR": str(directory)})
                commands.close_reader()
                events.close_writer()
                entered.close_writer()
                release.close_reader()
                try:
                    if phase == "evaluation":
                        self.wait_token(entered.reader, b"G", receipt)
                        self.assertEqual((directory / "engine-completions").read_text(), "0\n")
                        receipt["active_trial_length"] = 1
                        self.intervene(child, commands.writer, events.reader, intervention, receipt)
                        self.assertEqual((directory / "engine-completions").read_text(), "0\n")
                        self.assertEqual(os.write(release.writer, b"R"), 1)
                        receipt["events"].append({"engine_gate_released": 1})
                    self.wait_token(events.reader, b"E", receipt)
                    completed = (directory / "engine-completions").read_text().splitlines()
                    self.assertEqual(len(completed), 2)
                    receipt["engine_completions_at_boundary"] = completed
                    if phase == "boundary":
                        self.intervene(child, commands.writer, events.reader, intervention, receipt)
                    if phase == "drain":
                        self.wait_full_fifo(fifo_fd, capacity)
                    self.send_command(commands.writer, b"E", receipt)
                    self.wait_token(events.reader, b"L", receipt)
                    if phase == "drain":
                        self.assertEqual(self.queued_bytes(fifo_fd), capacity)
                        progress_rows = [json.loads(line) for line in
                                         (directory / "progress.jsonl").read_bytes().splitlines()]
                        self.assertEqual([row["trial_id"] for row in progress_rows], [0])
                        receipt.update(queued_bytes_before_event=capacity,
                                       terminal_ids_before_event=[0])
                        self.intervene(child, commands.writer, events.reader, intervention, receipt)
                        self.assertEqual(self.queued_bytes(fifo_fd), capacity)
                    self.send_command(commands.writer, b"L", receipt)
                    self.send_command(commands.writer, b"Q", receipt)
                    if phase == "drain":
                        self.drain_fifo(fifo_fd, directory / "trials.jsonl")
                    child.wait(timeout=PROTOCOL_TIMEOUT)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
                    record_process(directory, child)
            result = self.assert_result(directory, child, expected, patience, schedule)
            verdict = {"verdict": "PASS", "expected_stop_reason": expected,
                       "observed_stop_reason": result["stop_reason"],
                       "trial_statuses": [row["status"] for row in result["trials"]]}
        finally:
            if (directory / "result.json").is_file():
                result = json.loads((directory / "result.json").read_bytes())
                verdict["observed_stop_reason"] = result["stop_reason"]
                verdict["trial_statuses"] = [row["status"] for row in result["trials"]]
            write_json(directory / "admission.json", receipt)
            write_json(directory / "verdict.json", verdict)
        return directory

    def test_00_deliberate_crash_records_the_childs_nonzero_exit(self):
        directory = self.case_directory("deliberate-crash")
        with (directory / "followed-files").open("wb") as manifest:
            command = [str(CONTROLLED), "--test-channels", "-1", "-1",
                       str(manifest.fileno()), "--test-crash"]
            self.list_files(directory, CONTROLLED, [])
            write_json(directory / "command.json", command)
            with (directory / "stdout").open("wb") as out, (
                    directory / "stderr").open("wb") as err:
                child = subprocess.Popen(command, stdout=out, stderr=err,
                                         pass_fds=(manifest.fileno(),),
                                         env={**os.environ, "TMPDIR": str(directory)})
                try:
                    child.wait(timeout=PROTOCOL_TIMEOUT)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
                    record_process(directory, child)
        self.assertEqual(child.returncode, -signal.SIGABRT)
        self.assertEqual(int((directory / "work.exit").read_text()), -signal.SIGABRT)
        write_json(directory / "verdict.json", {
            "verdict": "PASS", "control": "deliberate crash", "returncode": child.returncode})

    def test_01_hook_absent_matches_product_bytes(self):
        for sampler in SAMPLERS:
            for schedule in SCHEDULES:
                for patience in (None, 0, 1, 8):
                    with self.subTest(sampler=sampler, schedule=schedule, patience=patience):
                        product = self.plain_run(PRODUCTION, sampler, schedule, patience)
                        test_copy = self.plain_run(CONTROLLED, sampler, schedule, patience)
                        for name in ("stdout", "stderr", "progress.jsonl", "trials.jsonl"):
                            self.assertEqual((product / name).read_bytes(),
                                             (test_copy / name).read_bytes(), name)

    def exercise_boundary_or_drain(self, phase, intervention):
        for sampler in SAMPLERS:
            for schedule in SCHEDULES:
                for patience in (1, 8):
                    with self.subTest(sampler=sampler, schedule=schedule, patience=patience):
                        self.controlled_run(sampler, schedule, patience, intervention, phase)

    def test_10_sigterm_at_evaluation_completion_wins(self):
        self.exercise_boundary_or_drain("boundary", "signal")

    def test_11_deadline_at_evaluation_completion_wins(self):
        self.exercise_boundary_or_drain("boundary", "deadline")

    def test_20_late_sigterm_during_output_drain_keeps_completed_outcome(self):
        self.exercise_boundary_or_drain("drain", "signal")

    def test_21_late_deadline_during_output_drain_keeps_completed_outcome(self):
        self.exercise_boundary_or_drain("drain", "deadline")

    def test_30_patience_absent_and_zero_keep_boundary_and_drain_behavior(self):
        for schedule in SCHEDULES:
            for patience in (None, 0):
                for phase in ("boundary", "drain"):
                    for intervention in ("signal", "deadline"):
                        with self.subTest(schedule=schedule, patience=patience, phase=phase,
                                          intervention=intervention):
                            self.controlled_run("grid", schedule, patience, intervention, phase)

    def test_31_deadline_disabled_ignores_clock_advance(self):
        for schedule in SCHEDULES:
            for phase in ("evaluation", "boundary", "drain"):
                with self.subTest(schedule=schedule, phase=phase):
                    self.controlled_run("grid", schedule, 1, "deadline", phase, wall=False)

    def test_40_sigterm_while_last_trial_is_in_evaluation_drains_ok(self):
        for schedule in SCHEDULES:
            with self.subTest(schedule=schedule):
                self.controlled_run("grid", schedule, 1, "signal", "evaluation")

    def test_41_deadline_while_last_trial_is_in_evaluation_drains_ok(self):
        for schedule in SCHEDULES:
            with self.subTest(schedule=schedule):
                self.controlled_run("grid", schedule, 1, "deadline", "evaluation")


if __name__ == "__main__":
    # The deliberate crash must not leave a core file outside the evidence directory.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    program = unittest.main(verbosity=2, exit=False, failfast=True)
    result = program.result
    report = {
        "verdict": "PASS" if result.wasSuccessful() else "FAIL",
        "tests_run": result.testsRun,
        "failures": [{"test": test.id(), "diagnostic": diagnostic}
                     for test, diagnostic in result.failures],
        "errors": [{"test": test.id(), "diagnostic": diagnostic}
                   for test, diagnostic in result.errors],
        "skips": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
    }
    if hasattr(EvaluationCompletionTests, "run_directory"):
        write_json(EvaluationCompletionTests.run_directory / "judge.json", report)
    print("EVALUATION_COMPLETION_JUDGE " + json.dumps(report), flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
