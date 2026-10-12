#!/usr/bin/env python3
"""Native gates for the rolling schedule (`--schedule rolling`).

Every test runs the real pineforge-hpo-native binary against the test strategy plugin and reads
its result. The default schedule must keep its contract and gain no rolling field; a rolling
result must carry its own contract and a completion log that audits; the log must explain the
pruner cuts and the patience trigger; the refusals must hold; samplers without feedback must keep
id order; and a continuation across the two schedules must rebuild its history. The proposal-level
replay (re-deriving every proposal from the log with a fresh sampler) is done by
tests/test_rolling_replay.cpp, which reads the result file alone; this file checks everything else
that the result shows.

Each test's docstring says how it can fail.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest


NATIVE = Path(sys.argv.pop(1)).resolve()
PLUGIN = Path(sys.argv.pop(1)).resolve()

FOUR_FIELDS = ("batch_size", "batch_lag", "replay_contract", "continuation_contract")

# How long a test waits for a pipe the strategy writes or reads before it fails with a message.
PIPE_SECONDS = 20.0
# The most runs the finish-order test makes of its two-gate scenario.
FINISH_ORDER_RUNS = 30


def pruner_cuts(kind: str, rungs: list[float], eta: int, minimize: bool,
                history: list[list[float]]) -> list[float | None]:
    """A Python model of pfh::Pruner::cuts over a fed history (src/core/pruner.cpp)."""
    cuts: list[float | None] = [None] * len(history)
    if kind == "none":
        return cuts
    for rung, values in enumerate(history):
        if len(values) < eta:
            continue
        ordered = sorted(values, reverse=not minimize)
        if kind == "median" or eta == 2:
            middle = len(ordered) // 2
            cuts[rung] = (ordered[middle] if len(ordered) % 2
                          else ordered[middle - 1] / 2.0 + ordered[middle] / 2.0)
        else:
            survivors = (len(ordered) + eta - 1) // eta
            cuts[rung] = ordered[survivors - 1]
    return cuts


def fed_history(order: list[int], trials_by_id: dict[int, dict], rungs: int,
                prefix: int) -> list[list[float]]:
    """Feed a fresh pruner the rung scores of the first `prefix` trials of completion_order."""
    history: list[list[float]] = [[] for _ in range(rungs)]
    cursors = [0] * rungs
    for entry in order[:prefix]:
        scores = trials_by_id[entry]["pruning"]["rung_scores"]
        for rung in range(min(len(scores), rungs)):
            value = scores[rung]
            if value is None or not math.isfinite(value):
                continue
            if len(history[rung]) < 1024:
                history[rung].append(value)
            else:
                history[rung][cursors[rung]] = value
                cursors[rung] = (cursors[rung] + 1) % 1024
    return history


def ideal_finish_order(delays: list[int], workers: int) -> tuple[list[int], list[int]]:
    """The order, and the times in ms, in which trials finish under the plain model of the
    rolling schedule: the trials start in id order, `workers` of them run at once, a free slot
    starts the next trial at the very moment its trial ends, and a trial takes exactly its delay.
    `delays[i]` is the delay of trial i."""
    running = [(delays[index], index) for index in range(min(workers, len(delays)))]
    next_id = len(running)
    order: list[int] = []
    times: list[int] = []
    while running:
        running.sort()
        finished_at, trial_id = running.pop(0)
        order.append(trial_id)
        times.append(finished_at)
        if next_id < len(delays):
            running.append((finished_at + delays[next_id], next_id))
            next_id += 1
    return order, times


class RollingScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.csv = self.root / "bars.csv"
        self.csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            + "".join(
                f"{1700000000000 + index * 60000},100,102,99,101,10\n"
                for index in range(64)
            ),
            encoding="utf-8",
        )

    def command(self, *extra: str, workers: int = 4, sampler: str = "tpe",
                trials: int = 32) -> list[str]:
        return [
            str(NATIVE), "run", "--strategy", str(PLUGIN),
            "--ohlcv", str(self.csv), "--objective", "metrics.all.net_profit",
            "--sampler", sampler, "--max-trials", str(trials), "--seed", "90210",
            "--workers", str(workers), "--input-tf", "1", "--script-tf", "5",
            "--chart-timezone", "Asia/Taipei", "--bar-magnifier", "true",
            "--magnifier-samples", "6", "--magnifier-distribution", "triangle",
            "--fixed-input", "BatchPrefixTest", "1",
            "--int-dim", "Length", "1", "100", "1", *extra,
        ]

    def invoke(self, *extra: str, workers: int = 4, sampler: str = "tpe", trials: int = 32):
        return subprocess.run(
            self.command(*extra, workers=workers, sampler=sampler, trials=trials),
            text=True, capture_output=True, check=False,
        )

    def result(self, *extra: str, workers: int = 4, sampler: str = "tpe", trials: int = 32):
        completed = self.invoke(*extra, workers=workers, sampler=sampler, trials=trials)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        return json.loads(completed.stdout)

    def trial_bytes(self, completed) -> bytes:
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        start = completed.stdout.index('  "trials": [') + len('  "trials": ')
        return completed.stdout[start:completed.stdout.rfind("\n}")].encode()

    def write_candidates(self, lengths: list[int]) -> Path:
        path = self.root / "candidates.jsonl"
        path.write_text(
            "".join(json.dumps({"Length": length}, separators=(",", ":")) + "\n"
                    for length in lengths),
            encoding="utf-8",
        )
        return path

    def assert_log(self, result: dict, workers: int, first_id: int = 0) -> None:
        """Audit a rolling result's completion log and check how far a proposal can lag.

        The audit: every terminal trial of the run appears in completion_order once, every id in
        a trial's prefix is smaller than that trial's own id, and completed_before never
        decreases. The lag bound: the runner keeps at most `workers` trials in flight, so when
        trial i is proposed at most workers - 1 of the earlier trials of this run are unfinished.
        With e = i - first_id earlier trials in this run, the proposal therefore saw at least
        e - workers + 1 and at most e of them, and the first `workers` proposals saw none.
        """
        order = result["completion_order"]
        completed_before = result["completed_before"]
        self.assertTrue(order, "completion_order is empty")
        self.assertEqual(len(order), len(set(order)), "completion_order repeats an id")
        self.assertEqual(sorted(order), list(range(first_id, first_id + len(order))),
                         "completion_order is not every terminal trial of the run once")
        self.assertEqual(len(order), result["trials_completed"])
        self.assertEqual(len(completed_before), first_id + len(order))
        self.assertEqual(completed_before[:first_id], [0] * first_id)
        previous = 0
        for trial_id in range(first_id, first_id + len(order)):
            prefix = completed_before[trial_id]
            earlier = trial_id - first_id
            self.assertGreaterEqual(prefix, previous, "completed_before decreased")
            previous = prefix
            self.assertLessEqual(prefix, earlier, "a proposal saw more trials than were proposed")
            self.assertGreaterEqual(prefix, earlier - workers + 1,
                                    "more than workers - 1 earlier trials were unfinished")
            if earlier < workers:
                self.assertEqual(prefix, 0, "the first proposals must see nothing finished")
            for entry in order[:prefix]:
                self.assertGreaterEqual(entry, first_id)
                self.assertLess(entry, trial_id, "a prefix holds an id not below the trial's own")

    # ---- result contract -------------------------------------------------------------

    def test_default_result_is_batches_and_gains_no_rolling_field(self) -> None:
        """Fails if a default run names a rolling contract or carries the log keys."""
        result = self.result()
        self.assertEqual(result["replay_contract"], "ordered_batches_v1")
        self.assertEqual(result["continuation_contract"], "sampler_checkpoint_v2")
        self.assertEqual(result["batch_size"], 4)
        self.assertEqual(result["batch_lag"], 0)
        self.assertNotIn("completion_order", result)
        self.assertNotIn("completed_before", result)

    def test_rolling_result_contract(self) -> None:
        """Fails if a rolling run reports a batches contract, a batch field or a TPE checkpoint,
        or omits the log."""
        result = self.result("--schedule", "rolling")
        self.assertEqual(result["replay_contract"], "rolling_completion_v1")
        self.assertEqual(result["continuation_contract"], "rolling_completion_v1")
        self.assertIsNone(result["batch_size"])
        self.assertIsNone(result["batch_lag"])
        self.assertNotIn("tpe_sampler_state", result)
        self.assertIn("completion_order", result)
        self.assertIn("completed_before", result)

    def test_no_per_trial_record_carries_the_log(self) -> None:
        """Fails if completion_order or completed_before leaks into a trial record."""
        result = self.result("--schedule", "rolling")
        self.assertTrue(result["trials"])
        for trial in result["trials"]:
            self.assertNotIn("completion_order", trial)
            self.assertNotIn("completed_before", trial)

    # ---- the completion log ----------------------------------------------------------

    def test_rolling_log_audit_and_lag_bound(self) -> None:
        """Fails if the log of a real TPE run, at 1, 2, 4 and 8 workers, repeats or omits an id,
        names an id that is not a terminal trial, lets a prefix hold an id not below its own
        trial, decreases, lets a proposal lag more than workers - 1 trials behind (which a loop
        that refills before it feeds back, or that keeps more than `workers` trials in flight,
        would do) or sees a finished trial before the first proposals."""
        for workers in (1, 2, 4, 8):
            with self.subTest(workers=workers):
                result = self.result("--schedule", "rolling", workers=workers)
                self.assertEqual({trial["trial_id"] for trial in result["trials"]},
                                 set(result["completion_order"]),
                                 "completion_order is not the set of terminal trials")
                self.assert_log(result, workers)
                if workers == 1:
                    self.assertEqual(result["completion_order"], list(range(32)))
                    self.assertEqual(result["completed_before"], list(range(32)))

    def test_rolling_proposals_of_one_pass_share_a_prefix(self) -> None:
        """Fails if the loop refills a slot between two feeds. Sixteen trials of equal length
        finish within a fraction of a millisecond of each other, so some pass of the coordinator
        finds several ready. The rule is to feed all of them back and only then fill each free
        slot, so those proposals see the same prefix: two consecutive proposals after the first
        fill have an equal completed_before. A loop that proposes right after each feed gives
        every proposal a prefix one longer than the previous one, and never an equal pair.
        Which trials are ready in the same pass depends on timing, so up to three runs are made;
        one pass with two ready trials in any of them is enough."""
        workers = 16
        for _ in range(3):
            result = self.result("--schedule", "rolling", "--fixed-input", "BatchPrefixJitter", "0",
                                 "--fixed-input", "DelayMs", "20", workers=workers,
                                 sampler="random", trials=96)
            self.assert_log(result, workers)
            completed_before = result["completed_before"]
            if any(completed_before[index] == completed_before[index + 1]
                   for index in range(workers, len(completed_before) - 1)):
                return
        self.fail("no pass ever fed back two trials before proposing: proposals interleave "
                  "with feeds")

    def test_rolling_refills_a_free_slot_while_a_long_trial_runs(self) -> None:
        """Fails if a slot is not refilled while another trial runs, or if a finished trial is
        fed back out of completion order. Two workers: candidate 0 takes a second and the other
        seven take milliseconds (SequenceDelayMs picks values[Length % 2]). A rolling run
        finishes 1, 2, ..., 7 before 0, and each proposal sees the trials fed back so far but not
        the long trial that is still running; a loop with a batch barrier would hold proposal 2
        until trial 0 ended. The log is exact because the timings differ tenfold."""
        lengths = [2, 1, 3, 5, 7, 9, 11, 13]
        path = self.write_candidates(lengths)
        result = self.result("--schedule", "rolling", "--candidates", str(path),
                             "--fixed-input", "BatchPrefixJitter", "0",
                             "--fixed-input", "SequenceDelayMs", "1000,15",
                             workers=2, sampler="candidates", trials=len(lengths))
        self.assertEqual(result["completion_order"], [1, 2, 3, 4, 5, 6, 7, 0])
        self.assertEqual(result["completed_before"], [0, 0, 1, 2, 3, 4, 5, 6])
        self.assertEqual([trial["parameters"]["Length"] for trial in result["trials"]], lengths)
        self.assertEqual(result["candidate_list"]["evaluated"], len(lengths))
        self.assert_log(result, 2)

    def test_rolling_completion_order_is_the_finish_order_the_delays_imply(self) -> None:
        """Fails if the log lists the trials in an order other than the one in which they finish:
        id order, a batch barrier, or a position fixed when a trial is proposed instead of when
        it ends. Four workers run eight listed candidates in id order, and SequenceDelayMs gives
        each one a fixed delay (it picks values[Length % len(values)]). A slot is refilled when
        its trial ends, so the delays alone fix the order of the finishes; they are chosen so
        that two consecutive finishes are 400 ms apart, far more than the scheduling noise and the
        per-trial cost of a slow machine. The expected completion_order is computed from the
        delays that the plugin applies to the lengths recorded in the result, not copied from a
        result. Trials this far apart
        are fed back in separate passes of the coordinator, so this does not tell proposal order
        from finish order among trials that finish inside one pass."""
        workers = 4
        step = 400
        lengths = [1, 2, 3, 4, 5, 6, 7, 8]
        values = [0, 2 * step, step, 4 * step, 3 * step, 5 * step, 3 * step, 5 * step, 3 * step]
        result = self.result("--schedule", "rolling", "--candidates",
                             str(self.write_candidates(lengths)),
                             "--fixed-input", "BatchPrefixJitter", "0",
                             "--fixed-input", "SequenceDelayMs",
                             ",".join(str(value) for value in values),
                             workers=workers, sampler="candidates", trials=len(lengths))
        recorded = [trial["parameters"]["Length"] for trial in result["trials"]]
        self.assertEqual(recorded, lengths)
        delays = [values[length % len(values)] for length in recorded]
        expected, times = ideal_finish_order(delays, workers)
        self.assertNotEqual(expected, sorted(expected), "the delays must finish out of id order")
        self.assertGreaterEqual(min(later - earlier for earlier, later in zip(times, times[1:])),
                                step, "two finishes of the delay plan are too close to order")
        self.assertEqual(result["completion_order"], expected)
        self.assert_log(result, workers)

    def test_completion_log_is_complete_whatever_trials_out_says(self) -> None:
        """Fails if the log depends on how many trials are kept: with --trials-out best-k or
        none the result keeps few or no trials, yet completion_order must still name all of
        them and completed_before must still cover every trial id."""
        for mode, kept in (("best-k", 3), ("none", 0)):
            with self.subTest(trials_out=mode):
                result = self.result("--schedule", "rolling", "--trials-out", mode,
                                     "--best-k", "3")
                self.assertEqual(len(result["trials"]), kept)
                self.assertEqual(result["trials_completed"], 32)
                self.assert_log(result, 4)
                for trial in result["trials"]:
                    self.assertNotIn("completion_order", trial)
                    self.assertNotIn("completed_before", trial)

    def test_rolling_dlib_global_runs_with_its_log(self) -> None:
        """Fails if dlib_global is refused or run on another schedule under rolling, or writes a
        log that does not audit."""
        result = self.result("--schedule", "rolling", workers=4, sampler="dlib_global",
                             trials=24)
        self.assertEqual(result["sampler"], "dlib_global")
        self.assertEqual(result["replay_contract"], "rolling_completion_v1")
        self.assertEqual(result["continuation_contract"], "rolling_completion_v1")
        self.assert_log(result, 4)

    # ---- the order inside one pass ---------------------------------------------------

    def read_token(self, descriptor: int, expected: bytes, what: str) -> None:
        """Read exactly `expected` from a pipe the strategy writes; fail with `what` if it does
        not arrive within PIPE_SECONDS or is something else."""
        if not select.select([descriptor], [], [], PIPE_SECONDS)[0]:
            self.fail(f"{what}: nothing arrived within {PIPE_SECONDS} s")
        self.assertEqual(os.read(descriptor, len(expected)), expected, what)

    def write_token(self, descriptor: int, what: str) -> None:
        """Write the byte that releases a gate; fail with `what` if the pipe takes no byte within
        PIPE_SECONDS or is closed."""
        if not select.select([], [descriptor], [], PIPE_SECONDS)[1]:
            self.fail(f"{what}: the pipe took no byte within {PIPE_SECONDS} s")
        try:
            os.write(descriptor, b"R")
        except OSError as error:
            self.fail(f"{what}: {error}")

    def gated_pair(self, lengths: list[int]) -> dict:
        """One rolling run with two workers in which the first two listed candidates each wait in
        a gate of the test plugin (lengths[0] in the first gate, lengths[1] in the second), then
        trial 1 and, with the next write, trial 0 are released. SequenceDelayMs "1,0" makes a trial
        with an even length sleep 1 ms after its release and one with an odd length not sleep, so
        lengths[0] must be even and lengths[1] odd: trial 1 then ends about 1 ms before trial 0,
        whichever of the two the scheduler wakes first. Returns the result."""
        self.assertEqual((lengths[0] % 2, lengths[1] % 2), (0, 1), "the delay plan needs this")
        older_entered, older_entered_write = os.pipe()
        older_release_read, older_release = os.pipe()
        younger_entered, younger_entered_write = os.pipe()
        younger_release_read, younger_release = os.pipe()
        for_child = (older_entered_write, older_release_read, younger_entered_write,
                     younger_release_read)
        ours = (older_entered, older_release, younger_entered, younger_release)
        command = self.command(
            "--schedule", "rolling", "--candidates", str(self.write_candidates(lengths)),
            "--fixed-input", "BatchPrefixJitter", "0",
            "--fixed-input", "SequenceDelayMs", "1,0",
            "--fixed-input", "GateAtLength", str(lengths[0]),
            "--fixed-input", "GateEnteredFd", str(older_entered_write),
            "--fixed-input", "GateReleaseFd", str(older_release_read),
            "--fixed-input", "Gate2AtLength", str(lengths[1]),
            "--fixed-input", "Gate2EnteredFd", str(younger_entered_write),
            "--fixed-input", "Gate2ReleaseFd", str(younger_release_read),
            workers=2, sampler="candidates", trials=len(lengths),
        )
        child = subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, pass_fds=for_child)
        for descriptor in for_child:
            os.close(descriptor)
        try:
            self.read_token(older_entered, b"G", "trial 0 never entered its gate")
            self.read_token(younger_entered, b"G", "trial 1 never entered its gate")
            self.write_token(younger_release, "cannot release trial 1")
            self.write_token(older_release, "cannot release trial 0")
            stdout, stderr = child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            for descriptor in ours:
                os.close(descriptor)
        self.assertEqual(child.returncode, 0, stderr + stdout)
        return json.loads(stdout)

    def test_rolling_pass_feeds_trials_back_in_finish_order(self) -> None:
        """Fails if a pass that finds two trials ready feeds them back in the order they were
        proposed instead of the order they finished (a loop that feeds a pass in proposal order);
        it does not tell finish order from reverse proposal order.
        test_rolling_completion_order_is_the_finish_order_the_delays_imply cannot tell the two
        apart: its trials finish 400 ms apart, so they are fed back in separate passes.

        Two workers run the first two of eight listed candidates. Each of the two is held inside
        the test plugin by its own gate, so both are in flight and no third trial can be
        proposed. The test releases the younger one (trial 1) and, with the next write, the older
        one (trial 0). Trial 0 then sleeps 1 ms (its length is even, and SequenceDelayMs picks
        values[Length % 2] from "1,0") and trial 1 does not, so trial 1 ends about 1 ms before
        trial 0 whichever of them the scheduler wakes first. The coordinator waits at most 2 ms on
        the oldest trial (trial 0) before it looks again, so in some runs it finds both ready in
        one pass. Trial 1 took its finish number first, so that pass must feed it first; a pass
        that feeds in proposal order feeds trial 0 first. Both workers were held, so the next
        proposal follows that pass directly, and completed_before[2] says what the pass fed: 2
        when it fed both trials, 1 when it fed trial 1 alone (the coordinator looked between the
        two ends), which shows nothing about the order.

        The test passes at the first of up to 30 runs in which one pass fed both trials and fed
        trial 1 first, and fails if no run does. It does not ask for this in every run. Whether
        the coordinator looks between the two ends depends on where its 2 ms rhythm falls, which
        the test cannot control, and a task takes its finish number after the plugin returns, so
        a stall of trial 1 longer than the 1 ms would let trial 0 take its number first, and
        then feeding trial 0 first is right. With the sort, a good share of the runs show the
        finish order, so the test fails only if all 30 runs miss it. Without the sort no run can
        show it, because a pass that finds both feeds trial 0 first, so all 30 runs end in this
        failure. The log of every run must also audit."""
        lengths = [22, 11, 3, 5, 7, 9, 13, 15]
        seen: list[str] = []
        together = 0
        for _ in range(FINISH_ORDER_RUNS):
            result = self.gated_pair(lengths)
            self.assert_log(result, 2)
            self.assertEqual([trial["parameters"]["Length"] for trial in result["trials"]],
                             lengths)
            if result["completed_before"][2] != 2:
                seen.append("trial 1 fed back alone")
                continue
            together += 1
            fed = result["completion_order"][:2]
            seen.append(f"both fed back by one pass, {fed}")
            if fed == [1, 0]:
                return
        if not together:
            self.fail(f"no pass fed back trials 0 and 1 together in {FINISH_ORDER_RUNS} runs, so "
                      f"the order inside a pass was never exercised: {seen}")
        self.fail("every pass that fed back trials 0 and 1 together fed trial 0 first, although "
                  f"trial 1 finished first ({together} of {FINISH_ORDER_RUNS} runs): a pass feeds "
                  "back in proposal order, not in finish order")

    # ---- the default is unchanged, within one build ----------------------------------

    def test_default_matches_explicit_batches(self) -> None:
        """Fails if setting the schedule to batches changes the trials[] bytes or the four
        replay fields compared with leaving it unset, at 1, 2, 4 and 8 workers. One binary is on
        both sides, so this only shows that the explicit flag is a no-op; the comparison with a
        v0.12.0 build is in test_rolling_default_v012.py."""
        for sampler in ("grid", "random", "tpe", "dlib_global"):
            for workers in (1, 2, 4, 8):
                omitted = self.invoke(sampler=sampler, workers=workers)
                explicit = self.invoke("--schedule", "batches", sampler=sampler, workers=workers)
                self.assertEqual(self.trial_bytes(omitted), self.trial_bytes(explicit),
                                 f"schedule unset vs batches changed at {sampler}/{workers}")
                before = json.loads(omitted.stdout)
                after = json.loads(explicit.stdout)
                for field in FOUR_FIELDS:
                    self.assertEqual(before[field], after[field],
                                     f"{field} changed at {sampler}/{workers}")

    # ---- pruner cuts and the patience trigger, recomputed from the log ---------------

    def test_rolling_pruner_cuts_recomputed_from_log(self) -> None:
        """Fails if a pruned trial's recorded cut is not the cut a fresh pruner gives at that
        rung when it is fed the rung scores of the trial's prefix in completion_order, or if no
        trial was pruned (the check would then not exercise the pruner)."""
        result = self.result(
            "--schedule", "rolling", "--pruner", "median", "--pruner-rungs", "0.25,0.5",
            "--pruner-eta", "2", "--fixed-input", "DelayMs", "4",
        )
        rungs = [0.25, 0.5]
        minimize = result["direction"] == "minimize"
        order = result["completion_order"]
        completed_before = result["completed_before"]
        trials_by_id = {trial["trial_id"]: trial for trial in result["trials"]}
        pruned = [trial for trial in result["trials"] if trial["status"] == "pruned"]
        self.assertTrue(pruned, "no trial pruned; the check cannot exercise the pruner")
        for trial in pruned:
            prefix = completed_before[trial["trial_id"]]
            history = fed_history(order, trials_by_id, len(rungs), prefix)
            cuts = pruner_cuts("median", rungs, 2, minimize, history)
            rung = len(trial["pruning"]["rung_scores"]) - 1
            self.assertIsNotNone(cuts[rung], "the recomputed cut rung was still warming up")
            self.assertAlmostEqual(cuts[rung], trial["pruning"]["cut"], places=9,
                                   msg=f"trial {trial['trial_id']} cut not recomputed")

    def test_rolling_patience_trigger_recomputed_from_log(self) -> None:
        """Fails if the Nth non-improving trial in completion_order is not the recorded trigger,
        if the drain id is not the highest proposed id, or if no trigger latched (the check
        would then not exercise patience)."""
        result = self.result("--schedule", "rolling", "--no-improvement-trials", "2",
                             "--fixed-input", "DelayMs", "4", sampler="random")
        early_stop = result["early_stop"]
        minimize = result["direction"] == "minimize"
        trials_by_id = {trial["trial_id"]: trial for trial in result["trials"]}
        best = None
        non_improving = 0
        trigger = None
        patience = early_stop["patience_trials"]
        for entry in result["completion_order"]:
            trial = trials_by_id[entry]
            feasible = trial["status"] == "ok" and trial["feasible"]
            objective = trial["objective"]
            if trigger is not None:
                continue
            improves = (feasible and objective is not None and math.isfinite(objective)
                        and (best is None or (objective < best if minimize else objective > best)))
            if improves:
                best = objective
                non_improving = 0
                continue
            if best is None:
                continue
            non_improving = min(non_improving + 1, patience)
            if non_improving == patience:
                trigger = entry
        self.assertIsNotNone(trigger, "no patience trigger latched in this run")
        self.assertEqual(trigger, early_stop["trigger_trial_id"])
        self.assertEqual(early_stop["drained_through_trial_id"],
                         max(trials_by_id), "drain id is not the highest proposed id")

    def test_rolling_patience_tail_is_bounded_by_workers_minus_one(self) -> None:
        """Fails if, after the patience latch, a further trial is proposed (some proposal's
        prefix then includes the trigger) or more than workers - 1 trials are fed back after the
        trigger, or if a proposed trial is left unfinished (an id missing below the highest), at
        2, 4 and 8 workers."""
        for workers in (2, 4, 8):
            with self.subTest(workers=workers):
                result = self.result("--schedule", "rolling", "--no-improvement-trials", "2",
                                     "--fixed-input", "DelayMs", "4",
                                     workers=workers, sampler="random", trials=64)
                early_stop = result["early_stop"]
                trigger = early_stop["trigger_trial_id"]
                self.assertIsNotNone(trigger, "no patience trigger latched in this run")
                order = result["completion_order"]
                self.assert_log(result, workers)
                position = order.index(trigger)
                self.assertLessEqual(len(order) - 1 - position, workers - 1,
                                     "more than workers - 1 trials were fed back after the trigger")
                self.assertLessEqual(max(result["completed_before"]), position,
                                     "a trial was proposed after the patience latch")
                self.assertEqual(early_stop["drained_through_trial_id"], max(order))
                self.assertLess(len(order), 64, "the study ran to its budget despite the latch")
                self.assertEqual(result["stop_reason"], "no_improvement")

    def test_rolling_deadline_stop_names_a_trial_that_ran(self) -> None:
        """Fails if a deadline stop does not drain the running trials into the log, or if
        drained_through_trial_id names a trial that is not in the result (a trial proposed but
        never started when the deadline passed must not be named)."""
        result = self.result("--schedule", "rolling", "--max-wall-seconds", "0.4",
                             "--no-improvement-trials", "1000",
                             "--fixed-input", "BatchPrefixJitter", "0",
                             "--fixed-input", "DelayMs", "40",
                             workers=4, sampler="random", trials=1000)
        self.assertEqual(result["stop_reason"], "deadline")
        self.assertTrue(result["trials"])
        self.assertLess(len(result["trials"]), 1000)
        ids = {trial["trial_id"] for trial in result["trials"]}
        self.assertEqual(set(result["completion_order"]), ids)
        early_stop = result["early_stop"]
        self.assertIsNone(early_stop["trigger_trial_id"])
        self.assertEqual(early_stop["drained_through_trial_id"], max(ids))

    # ---- the schedule is exercised ---------------------------------------------------

    def test_rolling_completion_order_varies_with_workers(self) -> None:
        """Fails if a one-worker order is not id order, or if a four-worker order with varied
        per-trial delays is also id order (the schedule would then not have been exercised)."""
        delays = ",".join(str((index * 7) % 40 + 3) for index in range(50))
        single = self.result("--schedule", "rolling", workers=1)
        self.assertEqual(single["completion_order"], list(range(32)),
                         "one worker must complete in id order")
        multi = self.result("--schedule", "rolling",
                            "--fixed-input", "SequenceDelayMs", delays, workers=4)
        self.assertNotEqual(multi["completion_order"], list(range(32)),
                            "four workers with varied delays completed in id order")

    # ---- samplers without feedback keep id order under rolling -----------------------

    def test_feedbackless_samplers_keep_id_order_under_rolling(self) -> None:
        """Fails if grid, random, sobol or the candidate list propose something else under
        rolling: their per-id parameters must equal those of the batches run, because their
        sequence never depends on the completion order."""
        lengths = [3, 41, 7, 7, 90, 12, 55, 8]
        candidates = self.write_candidates(lengths)
        cases = (("grid", (), 32), ("random", (), 32), ("sobol", (), 32),
                 ("candidates", ("--candidates", str(candidates)), len(lengths)))
        for sampler, extra, trials in cases:
            with self.subTest(sampler=sampler):
                batches = self.result(*extra, sampler=sampler, trials=trials)
                rolling = self.result("--schedule", "rolling", *extra, sampler=sampler,
                                      trials=trials)
                self.assertEqual([t["parameters"] for t in rolling["trials"]],
                                 [t["parameters"] for t in batches["trials"]])
                self.assert_log(rolling, 4)

    # ---- continuation across the two schedules rebuilds its history ------------------

    def parent(self, name: str, *extra: str, trials: int = 16) -> tuple[dict, Path]:
        path = self.root / name
        return self.result(*extra, "--output", str(path), trials=trials), path

    def test_rolling_parent_continues_as_batches_by_rebuilding(self) -> None:
        """Fails if a rolling result carries a TPE checkpoint, or if a batches study continued
        from it restores one: it must find none and rebuild its history from the rows. The
        contrast is a batches parent, whose checkpoint a batches child does restore."""
        rolling, rolling_path = self.parent("rolling-parent.json", "--schedule", "rolling")
        self.assertNotIn("tpe_sampler_state", rolling)
        child = self.result("--warm-start", str(rolling_path), trials=8)
        self.assertEqual(child["warm_start_model"], "rebuilt_history")
        self.assertEqual(child["warm_start"]["trials"], 16)
        self.assertEqual(child["replay_contract"], "ordered_batches_v1")
        self.assertEqual(child["continuation_contract"], "sampler_checkpoint_v2")
        self.assertIn("tpe_sampler_state", child)
        self.assertNotIn("completion_order", child)
        self.assertEqual([trial["trial_id"] for trial in child["trials"]], list(range(16, 24)))
        batches, batches_path = self.parent("batches-parent.json")
        self.assertIn("tpe_sampler_state", batches)
        restored = self.result("--warm-start", str(batches_path), trials=8)
        self.assertEqual(restored["warm_start_model"], "restored_sampler_state")

    def test_batches_parent_continues_as_rolling_without_restoring(self) -> None:
        """Fails if a rolling study restores the checkpoint of a batches parent (it must ignore
        it and rebuild), writes a checkpoint of its own, names a batches contract, or writes a log
        that does not audit for the trial ids that continue after the parent."""
        parent, path = self.parent("batches-parent.json")
        self.assertIn("tpe_sampler_state", parent)
        child = self.result("--schedule", "rolling", "--warm-start", str(path), trials=8)
        self.assertEqual(child["warm_start_model"], "rebuilt_history")
        self.assertEqual(child["warm_start"]["trials"], 16)
        self.assertNotIn("tpe_sampler_state", child)
        self.assertEqual(child["replay_contract"], "rolling_completion_v1")
        self.assertEqual(child["continuation_contract"], "rolling_completion_v1")
        self.assertEqual([trial["trial_id"] for trial in child["trials"]], list(range(16, 24)))
        self.assert_log(child, 4, first_id=16)

    def test_rolling_parent_continues_as_rolling_by_rebuilding(self) -> None:
        """Fails if a rolling study continued from a rolling result restores or writes a
        checkpoint, or writes a log that does not audit."""
        parent, path = self.parent("rolling-parent.json", "--schedule", "rolling")
        child = self.result("--schedule", "rolling", "--warm-start", str(path), trials=8)
        self.assertEqual(child["warm_start_model"], "rebuilt_history")
        self.assertNotIn("tpe_sampler_state", child)
        self.assert_log(child, 4, first_id=16)

    # ---- refusals --------------------------------------------------------------------

    def test_refuses_batch_size_with_rolling(self) -> None:
        """Fails if --schedule rolling accepts --batch-size, or refuses without naming it."""
        completed = self.invoke("--schedule", "rolling", "--batch-size", "4")
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertIn("remove --batch-size", completed.stdout + completed.stderr)

    def test_refuses_explicit_batch_lag_with_rolling(self) -> None:
        """Fails if --schedule rolling accepts an explicit --batch-lag, even the default 0, or
        refuses without naming the flag to remove."""
        for value in ("0", "1"):
            with self.subTest(batch_lag=value):
                completed = self.invoke("--schedule", "rolling", "--batch-lag", value)
                self.assertEqual(completed.returncode, 1, completed.stdout)
                self.assertIn("remove --batch-lag", completed.stdout + completed.stderr)

    def test_refuses_rolling_tpe_without_constant_liar(self) -> None:
        """Fails if rolling TPE accepts constant_liar=false."""
        completed = self.invoke("--schedule", "rolling", "--tpe-constant-liar", "false")
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertIn("rolling", completed.stdout + completed.stderr)

    def test_accepts_rolling_tpe_with_constant_liar(self) -> None:
        """Fails if a legal rolling TPE study (constant_liar true) is refused."""
        result = self.result("--schedule", "rolling", "--tpe-constant-liar", "true")
        self.assertEqual(result["replay_contract"], "rolling_completion_v1")

    def test_rejects_unknown_schedule(self) -> None:
        """Fails if --schedule accepts a value other than batches or rolling."""
        self.assertEqual(self.invoke("--schedule", "bogus").returncode, 1)

    def test_rolling_smoke_runs(self) -> None:
        """Fails if a plain rolling study does not run to its budget."""
        result = self.result("--schedule", "rolling")
        self.assertEqual(len(result["trials"]), 32)
        self.assertEqual(result["stop_reason"], "trial_budget_reached")


if __name__ == "__main__":
    unittest.main()
