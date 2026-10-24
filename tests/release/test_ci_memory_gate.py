"""Exercise the gate's judgments and accounting without building or invoking HPO."""

from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/ci_memory_gate.py"
print(f"FILES gate module read: {SOURCE}", file=sys.stderr, flush=True)
SPEC = importlib.util.spec_from_file_location("ci_memory_gate", SOURCE)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
BASE = 16 * 1024 * 1024


def has_proc_vmhwm():
    if not sys.platform.startswith("linux") or not hasattr(GATE.os, "wait4"):
        return False
    (status,) = GATE.list_paths("test VmHWM availability", [Path("/proc/self/status")])
    try:
        return any(
            line.startswith("VmHWM:")
            for line in status.read_text(encoding="ascii").splitlines()
        )
    except OSError:
        return False


HAS_PROC_VMHWM = has_proc_vmhwm()


class MemoryGateLogicTests(unittest.TestCase):
    def test_flat_series_passes(self):
        verdict = GATE.judge_trial_growth([(count, BASE) for count in GATE.COUNTS])
        self.assertEqual(verdict["verdict"], "PASS")
        self.assertEqual(verdict["exit_code"], 0)
        self.assertEqual(verdict["measurements"], 3)
        self.assertEqual(
            [edge["bytes_per_trial"] for edge in verdict["trial_slopes"]], [0, 0]
        )
        self.assertEqual(
            [edge["limit_bytes_per_trial"] for edge in verdict["trial_slopes"]],
            [256, 32],
        )

    def test_growing_series_is_the_red_mutation_control(self):
        verdict = GATE.judge_trial_growth(
            [(count, BASE + 512 * count) for count in GATE.COUNTS]
        )
        self.assertEqual(verdict["verdict"], "FAIL")
        self.assertEqual(verdict["exit_code"], 1)
        self.assertTrue(
            all(edge["verdict"] == "FAIL" for edge in verdict["trial_slopes"])
        )

    def test_pre_spool_growth_cannot_pass_the_upper_bound(self):
        verdict = GATE.judge_trial_growth(
            [(10, BASE), (1_000, BASE), (100_000, BASE + 280_061_100)]
        )
        self.assertEqual(verdict["verdict"], "FAIL")
        self.assertEqual(verdict["exit_code"], 1)
        upper = verdict["trial_slopes"][1]
        self.assertAlmostEqual(upper["bytes_per_trial"], 2828.9)
        self.assertEqual(upper["verdict"], "FAIL")

    def test_exact_limits_pass_and_one_byte_over_either_edge_fails(self):
        exact = [
            (10, BASE),
            (1_000, BASE + 990 * 256),
            (100_000, BASE + 990 * 256 + 99_000 * 32),
        ]
        self.assertEqual(GATE.judge_trial_growth(exact)["exit_code"], 0)
        for edge in (1, 2):
            samples = [
                (count, peak + (1 if index >= edge else 0))
                for index, (count, peak) in enumerate(exact)
            ]
            with self.subTest(edge=edge):
                verdict = GATE.judge_trial_growth(samples)
                self.assertEqual(verdict["exit_code"], 1)
                self.assertEqual(verdict["trial_slopes"][edge - 1]["verdict"], "FAIL")

    def test_decreasing_rss_is_allowed_but_missing_or_invalid_samples_are_errors(self):
        self.assertEqual(
            GATE.judge_trial_growth(
                [(10, BASE + 8192), (1_000, BASE + 4096), (100_000, BASE)]
            )["exit_code"],
            0,
        )
        for samples in (
            [],
            [(10, BASE), (1_000, BASE)],
            [(10, BASE), (1_000, BASE), (1_000, BASE)],
            [(1_000, BASE), (10, BASE), (100_000, BASE)],
            [(10, 0), (1_000, BASE), (100_000, BASE)],
            [(10, BASE), (1_000, -1), (100_000, BASE)],
            [(10, BASE), (1_000, BASE), (100_000, float("nan"))],
            [(10, BASE), (1_000, BASE), (100_000, True)],
        ):
            with self.subTest(samples=samples), self.assertRaises(ValueError):
                GATE.judge_trial_growth(samples)

    def test_native_workload_keeps_all_trials_and_continuous_space(self):
        for trials in (10, 1_000, 100_000):
            command = GATE.native_command(
                Path("native"), Path("fixture.so"), Path("bars.csv"), trials
            )
            for flag, value in (
                ("--max-trials", str(trials)),
                ("--sampler", "random"),
                ("--seed", "17"),
                ("--workers", "1"),
                ("--batch-size", "1"),
                ("--schedule", "batches"),
                ("--trials-out", "all"),
            ):
                self.assertEqual(command[command.index(flag) + 1], value)
            self.assertEqual(
                command[-5:], ["--real-dim", "Length", "1", "100", "continuous"]
            )


class MemoryGateProcessTests(unittest.TestCase):
    def setUp(self):
        (scratch,) = GATE.list_paths(
            "test artifacts", [ROOT / "build/memory-gate-tests"]
        )
        scratch.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="case-", dir=scratch))
        GATE.list_paths("test directory", [self.directory])

    def path(self, name):
        (path,) = GATE.list_paths("test file", [self.directory / name])
        return path

    def read_json(self, path):
        (path,) = GATE.list_paths("test JSON read", [path])
        return json.loads(path.read_text(encoding="utf-8"))

    def arguments(self, directory):
        return [
            "--native",
            str(self.directory / "native"),
            "--plugin",
            str(self.directory / "fixture.so"),
            "--ohlcv",
            str(self.directory / "bars.csv"),
            "--artifacts",
            str(directory),
        ]

    def test_main_preserves_flat_and_growing_verdicts_and_own_exits(self):
        for growth, expected in ((0, 0), (512, 1)):
            with self.subTest(growth=growth):
                result = GATE.judge_trial_growth(
                    [(count, BASE + growth * count) for count in GATE.COUNTS]
                )
                parent = self.path(f"main-{growth}")
                output = io.StringIO()
                with (
                    mock.patch.object(GATE, "run_gate", return_value=result),
                    redirect_stdout(output),
                ):
                    self.assertEqual(GATE.main(self.arguments(parent)), expected)
                run = next(parent.iterdir())
                complete = self.read_json(run / "RUN.COMPLETE")
                self.assertEqual(complete["exit_code"], expected)
                self.assertEqual(complete, json.loads(output.getvalue()))
                (marker,) = GATE.list_paths("gate marker read", [run / "gate.exit"])
                self.assertEqual(int(marker.read_text()), expected)
                self.assertEqual(
                    complete["verdict"], "PASS" if expected == 0 else "FAIL"
                )

    def test_setup_errors_exit_two_and_cannot_publish_a_pass(self):
        output = io.StringIO()
        parent = self.path("missing-inputs")
        with redirect_stdout(output):
            code = GATE.main(self.arguments(parent))
        self.assertEqual(code, 2)
        verdict = json.loads(output.getvalue())
        self.assertEqual(verdict["verdict"], "ERROR")
        self.assertEqual(
            self.read_json(Path(verdict["artifact_directory"]) / "RUN.COMPLETE"),
            verdict,
        )

    def test_unexpected_gate_crash_records_nonzero_own_exit(self):
        parent = self.path("gate-crash")
        with (
            mock.patch.object(
                GATE, "run_gate", side_effect=RuntimeError("crash control")
            ),
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(RuntimeError, "crash control"),
        ):
            GATE.main(self.arguments(parent))
        run = next(parent.iterdir())
        complete = self.read_json(run / "RUN.COMPLETE")
        self.assertEqual(complete["verdict"], "ERROR")
        self.assertEqual(complete["exit_code"], 1)
        (marker,) = GATE.list_paths("gate crash marker read", [run / "gate.exit"])
        self.assertEqual(int(marker.read_text()), 1)

    def test_polling_keeps_postexec_hwm_and_reaps_the_exact_child_once(self):
        child = SimpleNamespace(pid=4321, returncode=None)
        with (
            mock.patch.object(
                GATE.resource,
                "getrusage",
                return_value=SimpleNamespace(ru_maxrss=8192),
            ) as getrusage,
            mock.patch.object(GATE.subprocess, "Popen", return_value=child),
            # Distinct synthetic values pin the last sample and max independently.
            mock.patch.object(
                GATE, "read_child_hwm", side_effect=[None, 52 * 1024, 48 * 1024, None]
            ) as read_hwm,
            mock.patch.object(GATE.time, "sleep") as sleep,
            mock.patch.object(
                GATE.os,
                "wait4",
                create=True,
                side_effect=[
                    InterruptedError(),
                    (0, 0, None),
                    (0, 0, None),
                    (4321, 0, SimpleNamespace(ru_maxrss=8192)),
                ],
            ) as wait4,
        ):
            row = GATE.run_child(
                self.directory, "wait4", [sys.executable, "-I", "-c", "pass"], []
            )
        getrusage.assert_called_once_with(GATE.resource.RUSAGE_SELF)
        self.assertEqual(wait4.call_args_list, [mock.call(4321, GATE.os.WNOHANG)] * 4)
        self.assertEqual(sleep.call_args_list, [mock.call(0.001)] * 2)
        native = GATE.allowed_path(Path(sys.executable))
        self.assertEqual(
            read_hwm.call_args_list,
            [mock.call(Path("/proc/4321/exe"), Path("/proc/4321/status"), native)] * 4,
        )
        self.assertEqual(row["pid"], row["waited_pid"])
        self.assertEqual(row["parent_floor_bytes"], 8192 * 1024)
        self.assertEqual(row["ru_maxrss"], 8192)
        self.assertEqual(row["peak_rss_bytes"], 48 * 1024)
        self.assertEqual(row["peak_source"], "vmhwm_last")
        self.assertEqual(row["vmhwm_last_bytes"], 48 * 1024)
        self.assertEqual(row["vmhwm_max_bytes"], 52 * 1024)
        self.assertEqual(row["vmhwm_samples"], 2)
        self.assertEqual(row["exit_code"], 0)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(self.read_json(Path(row["result_file"])), row)

    def sampled_child(self, *, ru_bytes, last_bytes=BASE // 2, parent_floor=BASE):
        child = SimpleNamespace(pid=4321, returncode=None)
        with (
            mock.patch.object(
                GATE.resource,
                "getrusage",
                return_value=SimpleNamespace(ru_maxrss=parent_floor // 1024),
            ),
            mock.patch.object(GATE.subprocess, "Popen", return_value=child),
            mock.patch.object(GATE, "read_child_hwm", return_value=last_bytes),
            mock.patch.object(
                GATE.os,
                "wait4",
                create=True,
                return_value=(4321, 0, SimpleNamespace(ru_maxrss=ru_bytes // 1024)),
            ),
        ):
            row = GATE.run_child(
                self.directory, "rss-source", [sys.executable, "-I", "-c", "pass"], []
            )
        self.assertEqual(row["parent_floor_bytes"], parent_floor)
        self.assertEqual(row["ru_maxrss"], ru_bytes // 1024)
        self.assertEqual(row["vmhwm_last_bytes"], last_bytes)
        self.assertEqual(row["vmhwm_max_bytes"], last_bytes)
        self.assertEqual(row["vmhwm_samples"], 1)
        self.assertEqual(self.read_json(Path(row["result_file"])), row)
        return row

    def test_ru_maxrss_above_parent_floor_and_margin_captures_exit_peak(self):
        ru_bytes = BASE + GATE.RU_MAXRSS_MARGIN_BYTES + 1024
        row = self.sampled_child(ru_bytes=ru_bytes)
        self.assertEqual(row["peak_rss_bytes"], ru_bytes)
        self.assertEqual(row["peak_source"], "ru_maxrss")

    def test_ru_maxrss_at_or_below_parent_floor_margin_keeps_last_vmhwm(self):
        for ru_bytes in (
            BASE - 1024,
            BASE,
            BASE + 1024,
            BASE + GATE.RU_MAXRSS_MARGIN_BYTES,
        ):
            with self.subTest(ru_bytes=ru_bytes):
                row = self.sampled_child(ru_bytes=ru_bytes)
                self.assertEqual(row["peak_rss_bytes"], BASE // 2)
                self.assertEqual(row["peak_source"], "vmhwm_last")

    def test_eligible_ru_maxrss_does_not_lower_a_larger_vmhwm_sample(self):
        ru_bytes = BASE + GATE.RU_MAXRSS_MARGIN_BYTES + 1024
        last_bytes = ru_bytes + 4096
        row = self.sampled_child(ru_bytes=ru_bytes, last_bytes=last_bytes)
        self.assertEqual(row["peak_rss_bytes"], last_bytes)
        self.assertEqual(row["peak_source"], "vmhwm_last")

    def test_preexec_samples_are_ignored_before_reading_status(self):
        executable = self.path("proc-exe")
        status = self.path("proc-status")
        native = GATE.allowed_path(Path(sys.executable))
        with (
            mock.patch.object(
                GATE.os, "readlink", return_value=str(self.directory / "forked-python")
            ),
            mock.patch.object(
                Path, "read_text", return_value="VmHWM:\t65536 kB\n"
            ) as read_status,
        ):
            self.assertIsNone(GATE.read_child_hwm(executable, status, native))
        read_status.assert_not_called()

    def test_postexec_vmhwm_is_converted_from_kib_and_missing_data_is_ignored(self):
        executable = self.path("proc-exe")
        status = self.path("proc-status")
        native = GATE.allowed_path(Path(sys.executable))
        with mock.patch.object(GATE.os, "readlink", return_value=str(native)):
            for text, expected in (
                ("Name:\tnative\nVmHWM:\t12345 kB\nVmRSS:\t42 kB\n", 12345 * 1024),
                ("VmHWM:\t0 kB\n", 0),
                ("Name:\tnative\n", None),
                ("VmHWM:\t12345 MB\n", None),
                ("VmHWM:\tinvalid kB\n", None),
            ):
                with self.subTest(text=text):
                    status.write_text(text, encoding="ascii")
                    self.assertEqual(
                        GATE.read_child_hwm(executable, status, native), expected
                    )
            with mock.patch.object(Path, "read_text", side_effect=FileNotFoundError):
                self.assertIsNone(GATE.read_child_hwm(executable, status, native))
        with mock.patch.object(GATE.os, "readlink", side_effect=ProcessLookupError):
            self.assertIsNone(GATE.read_child_hwm(executable, status, native))

    def test_zero_postexec_samples_are_refused_with_a_mocked_reader(self):
        child = SimpleNamespace(pid=4321, returncode=None)
        with (
            mock.patch.object(
                GATE.resource,
                "getrusage",
                return_value=SimpleNamespace(ru_maxrss=12345),
            ),
            mock.patch.object(GATE.subprocess, "Popen", return_value=child),
            mock.patch.object(GATE, "read_child_hwm", return_value=None),
            mock.patch.object(GATE.time, "sleep"),
            mock.patch.object(
                GATE.os,
                "wait4",
                create=True,
                return_value=(4321, 0, SimpleNamespace(ru_maxrss=12345)),
            ),
        ):
            row = GATE.run_child(
                self.directory, "no-postexec", [sys.executable, "-I", "-c", "pass"], []
            )
        self.assertEqual(row["vmhwm_samples"], 0)
        self.assertEqual(row["peak_rss_bytes"], 0)
        with (
            mock.patch.object(GATE, "run_child", return_value=row),
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ValueError, "post-exec VmHWM"),
        ):
            GATE.measure(self.directory, Path(sys.executable), SOURCE, SOURCE, 10, 2)
        result = self.read_json(Path(row["result_file"]))
        self.assertEqual(result["verdict"], "ERROR")
        self.assertEqual(result["exit_code"], 0)

    @unittest.skipUnless(
        HAS_PROC_VMHWM, "requires Linux /proc/self/status VmHWM and os.wait4"
    )
    def test_real_child_vmhwm_observes_allocation_below_the_parent_floor(self):
        # Touch the parent's pages so wait4's inherited floor exceeds both children.
        padding = bytearray(96 * 1024 * 1024)
        padding[::4096] = b"x" * (len(padding) // 4096)
        small = GATE.run_child(
            self.directory,
            "small-child",
            [sys.executable, "-I", "-c", "import time; time.sleep(0.05)"],
            [],
        )
        large = GATE.run_child(
            self.directory,
            "40mib-child",
            [
                sys.executable,
                "-I",
                "-c",
                "import time; data = bytearray(40 * 1024 * 1024); "
                "data[::4096] = b'x' * (len(data) // 4096); time.sleep(0.05)",
            ],
            [],
        )
        for row in (small, large):
            with self.subTest(command=row["command"]):
                self.assertEqual(row["exit_code"], 0)
                self.assertGreater(row["vmhwm_samples"], 0)
                self.assertGreater(row["peak_rss_bytes"], 0)
                self.assertEqual(row["peak_rss_bytes"], row["vmhwm_last_bytes"])
                self.assertEqual(row["peak_source"], "vmhwm_last")
                self.assertGreaterEqual(row["vmhwm_max_bytes"], row["peak_rss_bytes"])
                self.assertNotEqual(row["peak_rss_bytes"], row["parent_floor_bytes"])
                self.assertEqual(self.read_json(Path(row["result_file"])), row)
        self.assertLess(small["peak_rss_bytes"], small["parent_floor_bytes"])
        self.assertGreaterEqual(large["peak_rss_bytes"], 35 * 1024 * 1024)
        self.assertGreaterEqual(
            large["peak_rss_bytes"] - small["peak_rss_bytes"], 35 * 1024 * 1024
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux CI owns wait4 units")
    def test_real_deliberate_crash_records_the_child_signal(self):
        row = GATE.run_child(
            self.directory,
            "crash",
            [
                sys.executable,
                "-I",
                "-c",
                "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
            ],
            [],
        )
        self.assertEqual(row["exit_code"], -signal.SIGKILL)
        self.assertEqual(row["pid"], row["waited_pid"])
        # A deliberate immediate crash need not survive long enough for a proc sample.
        self.assertGreater(row["ru_maxrss"], 0)
        (marker,) = GATE.list_paths("child crash marker read", [Path(row["exit_file"])])
        self.assertEqual(int(marker.read_text()), row["exit_code"])
        row.update(verdict="PASS", reason="child signal and marker agree")
        GATE.write_json(Path(row["result_file"]), row)
        self.assertEqual(self.read_json(Path(row["result_file"]))["verdict"], "PASS")

    def output_fixture(self, *, rows=None, **metadata):
        document = {
            "ok": True,
            "sampler": "random",
            "trials_requested": 10,
            "trials_completed": 10,
            "stop_reason": "trial_budget_reached",
            **metadata,
        }
        if rows is None:
            rows = [
                {"trial_id": index, "status": "ok", "feasible": True, "objective": 1.0}
                for index in range(10)
            ]
        path = self.path("native.stdout")
        payload = json.dumps(document, indent=2)[:-2] + ',\n  "trials": [\n'
        payload += ",\n".join("    " + json.dumps(row) for row in rows)
        path.write_text(payload + '\n  ],\n  "trials_out": "all"\n}\n')
        return path

    def test_complete_native_output_is_observed_row_by_row(self):
        self.assertEqual(GATE.validate_output(self.output_fixture(), 10), 10)

    def test_infeasible_trial_is_refused(self):
        rows = [
            {
                "trial_id": index,
                "status": "ok",
                "feasible": index != 5,
                "objective": 1.0,
            }
            for index in range(10)
        ]
        with self.assertRaisesRegex(ValueError, "native trial 5 did not complete"):
            GATE.validate_output(self.output_fixture(rows=rows), 10)

    def test_exit_zero_or_completion_counts_cannot_hide_bad_work(self):
        for metadata in (
            {"ok": False},
            {"trials_completed": 9},
            {"trials_requested": 1_000},
            {"stop_reason": "cancelled"},
            {"failure": {"code": "hpo_output_io_failed"}},
        ):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                GATE.validate_output(self.output_fixture(**metadata), 10)
        for ids in ([], list(range(9)), [0] * 10):
            rows = [
                {"trial_id": index, "status": "ok", "feasible": True, "objective": 1.0}
                for index in ids
            ]
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                GATE.validate_output(self.output_fixture(rows=rows), 10)
        for status in ("error", "pruned", "trial_timeout"):
            rows = [
                {
                    "trial_id": index,
                    "status": status,
                    "feasible": True,
                    "objective": 1.0,
                }
                for index in range(10)
            ]
            with self.subTest(status=status), self.assertRaises(ValueError):
                GATE.validate_output(self.output_fixture(rows=rows), 10)

    def measurement_row(self, stdout, *, peak=BASE, parent_floor=BASE // 2, samples=3):
        return {
            "exit_code": 0,
            "parent_floor_bytes": parent_floor,
            "ru_maxrss": max(peak, parent_floor) // 1024,
            "peak_rss_bytes": peak,
            "peak_source": "vmhwm_last",
            "vmhwm_last_bytes": peak,
            "vmhwm_samples": samples,
            "stdout": str(stdout),
            "result_file": str(self.path("native.result.json")),
            "verdict": "ERROR",
        }

    def test_child_vmhwm_can_be_below_or_at_parent_floor(self):
        stdout = self.output_fixture()
        arguments = (self.directory, Path(sys.executable), stdout, stdout, 10, 2)
        for offset in (-4096, 0, 4095, 4096, 4097):
            row = self.measurement_row(stdout, peak=BASE + offset, parent_floor=BASE)
            with (
                self.subTest(offset=offset),
                mock.patch.object(GATE, "run_child", return_value=row),
                redirect_stdout(io.StringIO()),
            ):
                measured = GATE.measure(*arguments)
                self.assertEqual(measured["trial_rows"], 10)
            result = self.read_json(Path(row["result_file"]))
            self.assertEqual(result["verdict"], "PASS")
            self.assertEqual(result["parent_floor_bytes"], BASE)

    def test_zero_samples_or_zero_vmhwm_are_refused_on_both_axes(self):
        stdout = self.output_fixture()
        for peak, last, samples in ((BASE, BASE, 0), (0, 0, 3), (BASE, 0, 3)):
            for bars_axis in (False, True):
                row = self.measurement_row(stdout, peak=peak, samples=samples)
                row["vmhwm_last_bytes"] = last
                with (
                    self.subTest(
                        peak=peak, last=last, samples=samples, bars_axis=bars_axis
                    ),
                    mock.patch.object(GATE, "run_child", return_value=row),
                    redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(ValueError, "post-exec VmHWM"),
                ):
                    GATE.measure(
                        self.directory,
                        Path(sys.executable),
                        stdout,
                        stdout,
                        10,
                        2,
                        bars_axis=bars_axis,
                    )
                result = self.read_json(Path(row["result_file"]))
                self.assertEqual(result["verdict"], "ERROR")
                self.assertEqual(result["exit_code"], 0)

    def test_exit_zero_incomplete_native_output_is_not_a_measurement_pass(self):
        stdout = self.output_fixture(trials_completed=9)
        row = self.measurement_row(stdout)
        with (
            mock.patch.object(GATE, "run_child", return_value=row),
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                ValueError, "did not complete all 10 requested trials"
            ),
        ):
            GATE.measure(self.directory, Path(sys.executable), stdout, stdout, 10, 2)
        result = self.read_json(Path(row["result_file"]))
        self.assertEqual(result["verdict"], "ERROR")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("did not complete all 10 requested trials", result["reason"])

    def test_nonzero_native_exit_is_not_a_measurement_pass(self):
        stdout = self.path("failed.stdout")
        stderr = self.path("failed.stderr")
        stdout.write_text("{}\n")
        stderr.write_text("deliberate native failure\n")
        result = self.path("failed.result.json")
        row = {
            "exit_code": -signal.SIGKILL,
            "parent_floor_bytes": BASE // 2,
            "peak_rss_bytes": BASE,
            "stdout": str(stdout),
            "stderr": str(stderr),
            "result_file": str(result),
            "verdict": "ERROR",
        }
        with (
            mock.patch.object(GATE, "run_child", return_value=row),
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ValueError, "native child exited"),
        ):
            GATE.measure(self.directory, Path(sys.executable), stdout, stdout, 10, 2)
        self.assertEqual(self.read_json(result)["verdict"], "ERROR")

    def gate_inputs(self):
        plugin = self.path("fixture.so")
        manifest = self.path("manifest.json")
        bars = self.path("bars.csv")
        plugin.write_bytes(b"unit-test stub: never loaded\n")
        manifest.write_text("{}\n")
        GATE.write_bar_fixture(bars, 2)
        return SimpleNamespace(
            native=Path(sys.executable), plugin=plugin, ohlcv=bars, bars_axis=True
        )

    def crash_control_row(self, marker_exit=-signal.SIGKILL):
        marker = self.path("crash.exit")
        marker.write_text(f"{marker_exit}\n", encoding="ascii")
        return {
            "exit_code": -signal.SIGKILL,
            "exit_file": str(marker),
            "result_file": str(self.path("crash.result.json")),
        }

    def test_non_linux_is_refused_before_following_inputs(self):
        with (
            mock.patch.object(GATE.sys, "platform", "darwin"),
            mock.patch.object(GATE, "list_paths") as paths,
            self.assertRaisesRegex(ValueError, "requires Linux child os.wait4"),
        ):
            GATE.run_gate(SimpleNamespace(), self.directory)
        paths.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux CI owns the RSS gate")
    def test_missing_postexec_samples_exit_two_before_any_rss_judgment(self):
        args = self.gate_inputs()
        parent = self.path("unmeasurable")
        stdout = self.output_fixture()
        row = self.measurement_row(stdout, samples=0)
        arguments = self.arguments(parent)
        arguments[1] = str(args.native)
        with (
            mock.patch.object(
                GATE, "run_child", side_effect=[self.crash_control_row(), row]
            ) as child,
            mock.patch.object(GATE, "judge_trial_growth") as judge,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(GATE.main(arguments), 2)
        self.assertEqual(child.call_count, 2)
        judge.assert_not_called()
        complete = self.read_json(next(parent.iterdir()) / "RUN.COMPLETE")
        self.assertEqual(complete["exit_code"], 2)
        self.assertEqual(complete["verdict"], "ERROR")
        self.assertIn("post-exec VmHWM", complete["reason"])
        self.assertEqual(self.read_json(Path(row["result_file"]))["verdict"], "ERROR")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux CI owns the RSS gate")
    def test_crash_marker_mismatch_stops_before_any_measurement(self):
        args = self.gate_inputs()
        with (
            mock.patch.object(
                GATE, "run_child", return_value=self.crash_control_row(0)
            ),
            mock.patch.object(GATE, "measure") as measure,
            self.assertRaisesRegex(ValueError, "crash marker does not match"),
        ):
            GATE.run_gate(args, self.directory)
        measure.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux CI owns the RSS gate")
    def test_main_verdict_records_resolution_and_trial_peak_sources(self):
        args = self.gate_inputs()
        sources = ["vmhwm_last", "ru_maxrss", "vmhwm_last"]
        statement = (
            "a PASS means growth below the thresholds as measured by the last VmHWM "
            "sample before exit (lower bound) or ru_maxrss when it exceeded the parent "
            "floor by the margin (upper bound)"
        )
        for growth, expected in ((0, 0), (512, 1)):
            with self.subTest(growth=growth):
                parent = self.path(f"resolution-{growth}")
                arguments = self.arguments(parent)
                arguments[1] = str(args.native)
                rows = [
                    {"peak_rss_bytes": BASE + growth * count, "peak_source": source}
                    for count, source in zip(GATE.COUNTS, sources)
                ]
                output = io.StringIO()
                with (
                    mock.patch.object(
                        GATE, "run_child", return_value=self.crash_control_row()
                    ),
                    mock.patch.object(GATE, "measure", side_effect=rows) as measure,
                    redirect_stdout(output),
                ):
                    self.assertEqual(GATE.main(arguments), expected)
                self.assertEqual(measure.call_count, 3)
                verdict = json.loads(output.getvalue())
                self.assertEqual(
                    self.read_json(next(parent.iterdir()) / "RUN.COMPLETE"), verdict
                )
                self.assertEqual(
                    verdict["verdict"], "PASS" if expected == 0 else "FAIL"
                )
                self.assertEqual(verdict["poll_interval_seconds"], 0.001)
                self.assertEqual(verdict["ru_maxrss_margin_bytes"], 1024 * 1024)
                self.assertEqual(verdict["peak_sources"], sources)
                for text in (
                    "0.001 seconds (about 1 ms)",
                    "1048576 bytes (1 MiB)",
                    statement,
                ):
                    self.assertIn(text, verdict["measurement_resolution"])
                    if expected != 0:
                        self.assertIn(text, verdict["reason"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux CI owns the RSS gate")
    def test_measured_trial_peaks_drive_the_verdict_and_bar_growth_is_not_gated(self):
        args = self.gate_inputs()
        for growth, expected in ((0, 0), (512, 1)):
            with self.subTest(growth=growth):
                directory = self.path(f"axes-{growth}")
                directory.mkdir()

                def measured(_directory, _native, _plugin, _bars, trials, count, **kw):
                    # Deliberately huge synthetic bar growth must remain informational.
                    peak = BASE + (
                        count * 1024 * 1024 if kw.get("bars_axis") else growth * trials
                    )
                    return {
                        "peak_rss_bytes": peak,
                        "peak_source": (
                            "vmhwm_last" if kw.get("bars_axis") else "ru_maxrss"
                        ),
                    }

                measurements = mock.patch.object(GATE, "measure", side_effect=measured)
                with measurements as measure:
                    verdict = GATE.run_gate(args, directory)
                self.assertEqual(verdict["exit_code"], expected)
                self.assertEqual(verdict["peak_sources"], ["ru_maxrss"] * 3)
                self.assertEqual(
                    [call.args[4] for call in measure.call_args_list[:3]],
                    [10, 1_000, 100_000],
                )
                if expected == 0:
                    self.assertEqual(measure.call_count, 6)
                    self.assertTrue(
                        all(not edge["gated"] for edge in verdict["bar_slopes"])
                    )
                    self.assertTrue(
                        all(
                            edge["bytes_per_bar"] == 1024 * 1024
                            for edge in verdict["bar_slopes"]
                        )
                    )
                    bar_calls = measure.call_args_list[3:]
                    for call, count in zip(bar_calls, GATE.BAR_COUNTS):
                        self.assertEqual(call.args[4:6], (10, count))
                        self.assertEqual(call.kwargs, {"bars_axis": True})
                else:
                    self.assertEqual(measure.call_count, 3)
                    self.assertNotIn("bar_slopes", verdict)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux CI owns the RSS gate")
    def test_failed_crash_control_stops_before_any_measurement(self):
        args = self.gate_inputs()
        with (
            mock.patch.object(GATE, "run_child", return_value={"exit_code": 0}),
            mock.patch.object(GATE, "measure") as measure,
            self.assertRaisesRegex(ValueError, "crash control"),
        ):
            GATE.run_gate(args, self.directory)
        measure.assert_not_called()

    def test_protected_names_are_refused_before_resolving_or_opening(self):
        for name in (
            ".config/zsh/keys/private",
            ".config/pineforge/settings",
            ".ssh/id_ed25519",
            ".aws/credentials",
            ".codex/auth.json",
            "secret.env",
            "secret.env/../otherwise-safe",
        ):
            with (
                self.subTest(name=name),
                mock.patch.object(Path, "resolve") as resolve,
                self.assertRaisesRegex(ValueError, "refused protected path by name"),
            ):
                GATE.allowed_path(self.directory / name)
            resolve.assert_not_called()

    def test_protected_symlink_target_is_refused(self):
        alias = self.path("alias")
        alias.symlink_to(self.directory / "secret.env")
        with self.assertRaisesRegex(ValueError, "refused protected path by name"):
            GATE.allowed_path(alias)


if __name__ == "__main__":
    unittest.main()
