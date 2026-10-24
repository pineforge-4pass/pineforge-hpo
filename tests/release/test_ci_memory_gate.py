"""Exercise the gate's judgments and accounting without building or invoking HPO."""

from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import resource
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


def gnu_time_skip_reason():
    if not sys.platform.startswith("linux") or not hasattr(GATE.os, "wait4"):
        return "requires Linux os.wait4 and GNU /usr/bin/time"
    (launcher,) = GATE.list_paths("test GNU time version", [Path(GATE.GNU_TIME)])
    if not GATE.os.path.exists(GATE.GNU_TIME):
        return "GNU /usr/bin/time is absent; install the time package"
    try:
        result = GATE.subprocess.run(
            [str(launcher), "--version"], capture_output=True, text=True, check=False
        )
    except OSError:
        return "GNU /usr/bin/time cannot be executed"
    if result.returncode != 0 or "gnu time" not in result.stdout.lower():
        return "/usr/bin/time is not GNU time; install the time package"
    return None


GNU_TIME_SKIP_REASON = gnu_time_skip_reason()


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

    def timed_child(self, output=b"12345\n", *, label="wait4", exit_code=0):
        child = SimpleNamespace(pid=4321, returncode=None)

        def launch(command, **_kwargs):
            if output is not None:
                (path,) = GATE.list_paths(
                    "mock GNU time output write", [Path(command[4])]
                )
                path.write_bytes(output)
            return child

        with (
            mock.patch.object(GATE.os.path, "exists", return_value=True) as exists,
            mock.patch.object(GATE.subprocess, "Popen", side_effect=launch) as popen,
            mock.patch.object(
                GATE.os,
                "wait4",
                create=True,
                side_effect=[
                    InterruptedError(),
                    # This is the Python-floored launcher's usage, never the peak.
                    (4321, exit_code << 8, SimpleNamespace(ru_maxrss=999_999)),
                ],
            ) as wait4,
        ):
            row = GATE.run_child(
                self.directory, label, [sys.executable, "-I", "-c", "pass"], []
            )
        exists.assert_called_once_with("/usr/bin/time")
        self.assertEqual(wait4.call_args_list, [mock.call(4321, 0)] * 2)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(popen.call_args.args[0], row["command"])
        self.assertEqual(popen.call_args.kwargs["cwd"], self.directory)
        self.assertEqual(popen.call_args.kwargs["env"]["TMPDIR"], str(self.directory))
        self.assertEqual(
            popen.call_args.kwargs["stdout"].name, str(self.path(f"{label}.stdout"))
        )
        self.assertEqual(
            popen.call_args.kwargs["stderr"].name, str(self.path(f"{label}.stderr"))
        )
        self.assertEqual(child.returncode, exit_code)
        self.assertEqual(self.read_json(Path(row["result_file"])), row)
        return row

    def test_gnu_time_reaps_exact_child_and_supplies_peak_not_python_wait4(self):
        row = self.timed_child()
        self.assertEqual(row["pid"], row["waited_pid"])
        self.assertEqual(row["peak_rss_bytes"], 12345 * 1024)
        self.assertEqual(row["peak_source"], "gnu_time_wait4")
        self.assertEqual(row["gnu_time_output"], "12345\n")
        self.assertIn("about 1 MiB", row["launcher_floor_note"])
        self.assertNotIn("parent_floor_bytes", row)
        self.assertNotIn("ru_maxrss", row)
        self.assertEqual(
            row["command"],
            [
                "/usr/bin/time",
                "-f",
                "%M",
                "-o",
                str(self.path("wait4.time")),
                "--",
                *row["native_command"],
            ],
        )
        self.assertEqual(row["native_command"], [sys.executable, "-I", "-c", "pass"])
        self.assertEqual(row["exit_code"], 0)

    def test_signal_diagnostic_before_last_peak_line_is_parsed(self):
        output = b"Command terminated by signal 9\n12345\n"
        row = self.timed_child(output, exit_code=128 + signal.SIGKILL)
        self.assertEqual(row["peak_rss_bytes"], 12345 * 1024)
        self.assertEqual(row["gnu_time_output"], output.decode("ascii"))
        self.assertEqual(row["exit_code"], 128 + signal.SIGKILL)
        (marker,) = GATE.list_paths("mock signal marker read", [Path(row["exit_file"])])
        self.assertEqual(int(marker.read_text()), row["exit_code"])

    def test_normal_nonzero_exit_and_diagnostic_preserve_child_status(self):
        output = b"Command exited with non-zero status 7\n12345\n"
        row = self.timed_child(output, exit_code=7)
        self.assertEqual(row["exit_code"], 7)
        self.assertEqual(row["peak_rss_bytes"], 12345 * 1024)
        self.assertEqual(row["gnu_time_output"], output.decode("ascii"))
        (marker,) = GATE.list_paths("mock exit marker read", [Path(row["exit_file"])])
        self.assertEqual(int(marker.read_text()), 7)

    def test_missing_empty_nonnumeric_or_zero_peak_output_is_refused(self):
        for index, output in enumerate((None, b"", b"invalid\n", b"0\n", b"-1\n")):
            label = f"invalid-{index}"
            with (
                self.subTest(output=output),
                self.assertRaisesRegex(ValueError, "GNU time peak"),
            ):
                self.timed_child(output, label=label)
            row = self.read_json(self.path(f"{label}.result.json"))
            self.assertEqual(row["verdict"], "ERROR")
            self.assertEqual(row["exit_code"], 0)
            self.assertEqual(row["peak_rss_bytes"], 0)
            self.assertEqual(row["gnu_time_output"], (output or b"").decode("ascii"))
            self.assertEqual(int(self.path(f"{label}.exit").read_text()), 0)

    def test_peak_output_is_bounded_to_four_kib(self):
        output = b"x" * 4096 + b"\n12345\n"
        with self.assertRaisesRegex(ValueError, "exceeds 4 KiB"):
            self.timed_child(output)
        row = self.read_json(self.path("wait4.result.json"))
        self.assertEqual(row["gnu_time_output"], "x" * 4096)
        self.assertEqual(row["verdict"], "ERROR")

    def test_non_gnu_launcher_cannot_publish_a_measurement(self):
        with self.assertRaisesRegex(ValueError, "install the GNU time package"):
            # A launcher rejecting GNU -f/-o options cannot write a peak file.
            self.timed_child(None, exit_code=1)
        row = self.read_json(self.path("wait4.result.json"))
        self.assertEqual(row["exit_code"], 1)
        self.assertEqual(row["verdict"], "ERROR")
        self.assertEqual(int(self.path("wait4.exit").read_text()), 1)

    @unittest.skipIf(GNU_TIME_SKIP_REASON, GNU_TIME_SKIP_REASON or "")
    def test_real_launcher_measures_child_peaks_below_python_parent_floor(self):
        # Touch the parent's pages so wait4's inherited floor exceeds both children.
        padding = bytearray(96 * 1024 * 1024)
        padding[::4096] = b"x" * (len(padding) // 4096)
        parent_floor = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
        small = GATE.run_child(
            self.directory,
            "small-child",
            [sys.executable, "-I", "-c", "pass"],
            [],
        )
        large = GATE.run_child(
            self.directory,
            "40mib-child",
            [
                sys.executable,
                "-I",
                "-c",
                "data = bytearray(40 * 1024 * 1024); "
                "data[::4096] = b'x' * (len(data) // 4096)",
            ],
            [],
        )
        for row in (small, large):
            with self.subTest(command=row["command"]):
                self.assertEqual(row["exit_code"], 0)
                self.assertGreater(row["peak_rss_bytes"], 0)
                self.assertEqual(row["peak_source"], "gnu_time_wait4")
                self.assertEqual(self.read_json(Path(row["result_file"])), row)
        self.assertLess(small["peak_rss_bytes"], parent_floor // 2)
        self.assertGreaterEqual(large["peak_rss_bytes"], 38 * 1024 * 1024)
        self.assertGreaterEqual(
            large["peak_rss_bytes"] - small["peak_rss_bytes"], 35 * 1024 * 1024
        )
        self.assertEqual(padding[0], ord("x"))

    @unittest.skipIf(GNU_TIME_SKIP_REASON, GNU_TIME_SKIP_REASON or "")
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
        self.assertNotEqual(row["exit_code"], 0)
        self.assertIn(
            f"Command terminated by signal {signal.SIGKILL}",
            row["gnu_time_output"].splitlines()[:-1],
        )
        self.assertEqual(row["pid"], row["waited_pid"])
        self.assertGreater(row["peak_rss_bytes"], 0)
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

    def measurement_row(self, stdout, *, peak=BASE):
        return {
            "exit_code": 0,
            "peak_rss_bytes": peak,
            "peak_source": "gnu_time_wait4",
            "stdout": str(stdout),
            "result_file": str(self.path("native.result.json")),
            "verdict": "ERROR",
        }

    def test_positive_child_peak_requires_complete_trial_rows(self):
        stdout = self.output_fixture()
        arguments = (self.directory, Path(sys.executable), stdout, stdout, 10, 2)
        for peak in (1024 * 1024, BASE // 2, BASE):
            row = self.measurement_row(stdout, peak=peak)
            with (
                self.subTest(peak=peak),
                mock.patch.object(GATE, "run_child", return_value=row),
                redirect_stdout(io.StringIO()),
            ):
                measured = GATE.measure(*arguments)
                self.assertEqual(measured["trial_rows"], 10)
            result = self.read_json(Path(row["result_file"]))
            self.assertEqual(result["verdict"], "PASS")
            self.assertEqual(result["peak_rss_bytes"], peak)

    def test_nonpositive_peaks_are_refused_on_both_axes(self):
        stdout = self.output_fixture()
        for peak in (0, -1):
            for bars_axis in (False, True):
                row = self.measurement_row(stdout, peak=peak)
                with (
                    self.subTest(peak=peak, bars_axis=bars_axis),
                    mock.patch.object(GATE, "run_child", return_value=row),
                    redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(ValueError, "GNU time peak RSS"),
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
            "exit_code": 128 + signal.SIGKILL,
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

    def crash_control_row(self, marker_exit=128 + signal.SIGKILL):
        marker = self.path("crash.exit")
        marker.write_text(f"{marker_exit}\n", encoding="ascii")
        output = f"Command terminated by signal {signal.SIGKILL}\n12345\n"
        return {
            "exit_code": 128 + signal.SIGKILL,
            "gnu_time_output": output,
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
    def test_missing_gnu_time_exits_two_without_launching_a_child(self):
        args = self.gate_inputs()
        parent = self.path("missing-time")
        arguments = self.arguments(parent)
        arguments[1] = str(args.native)
        with (
            mock.patch.object(GATE.os.path, "exists", return_value=False) as exists,
            mock.patch.object(GATE.subprocess, "Popen") as child,
            mock.patch.object(GATE, "measure") as measure,
            mock.patch.object(GATE, "judge_trial_growth") as judge,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(GATE.main(arguments), 2)
        exists.assert_called_once_with("/usr/bin/time")
        child.assert_not_called()
        measure.assert_not_called()
        judge.assert_not_called()
        run = next(parent.iterdir())
        complete = self.read_json(run / "RUN.COMPLETE")
        self.assertEqual(complete["verdict"], "ERROR")
        self.assertEqual(complete["exit_code"], 2)
        self.assertIn("install the time package", complete["reason"])
        (marker,) = GATE.list_paths(
            "missing launcher gate marker read", [run / "gate.exit"]
        )
        self.assertEqual(int(marker.read_text()), 2)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux CI owns the RSS gate")
    def test_unmeasurable_peak_exits_two_before_any_rss_judgment(self):
        args = self.gate_inputs()
        parent = self.path("unmeasurable")
        stdout = self.output_fixture()
        row = self.measurement_row(stdout, peak=0)
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
        self.assertIn("GNU time peak RSS", complete["reason"])
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
        sources = ["gnu_time_wait4"] * 3
        statement = (
            "peak is the child's maximum resident set size from GNU time "
            "(wait4 ru_maxrss of the native child reaped by a ~1 MiB launcher): "
            "exact including the final milliseconds, floored at the launcher's own "
            "size (about 1 MiB)"
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
                self.assertNotIn("poll_interval_seconds", verdict)
                self.assertNotIn("ru_maxrss_margin_bytes", verdict)
                self.assertEqual(verdict["peak_sources"], sources)
                self.assertEqual(verdict["measurement_resolution"], statement)
                if expected != 0:
                    self.assertIn(statement, verdict["reason"])

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
                            "bar-only" if kw.get("bars_axis") else "gnu_time_wait4"
                        ),
                    }

                with (
                    mock.patch.object(
                        GATE, "run_child", return_value=self.crash_control_row()
                    ),
                    mock.patch.object(GATE, "measure", side_effect=measured) as measure,
                ):
                    verdict = GATE.run_gate(args, directory)
                self.assertEqual(verdict["exit_code"], expected)
                self.assertEqual(verdict["peak_sources"], ["gnu_time_wait4"] * 3)
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

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux CI owns the RSS gate")
    def test_nonzero_exit_without_sigkill_diagnostic_fails_crash_control(self):
        args = self.gate_inputs()
        crash = self.crash_control_row()
        crash["gnu_time_output"] = "Command exited with non-zero status 137\n12345\n"
        with (
            mock.patch.object(GATE, "run_child", return_value=crash),
            mock.patch.object(GATE, "measure") as measure,
            self.assertRaisesRegex(ValueError, "crash control did not record SIGKILL"),
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
