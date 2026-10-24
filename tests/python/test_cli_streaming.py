"""Run-wrapper contracts, including an out-of-process RSS regression control.

The two script modes below are fixtures for CI, not a native-engine dependency.
Each memory probe writes its own measurements and exit code to a result file.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

if __name__ == "__main__":
    # Isolated child interpreters must exercise this checkout, including the mutant.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from pineforge_hpo import cli  # noqa: E402


_MIB = 1024 * 1024
_SCRIPT = Path(__file__).resolve()


def _fixture_paths(*paths: Path, announce: bool = True) -> None:
    """List fixture inputs/outputs before following them; refuse credential paths."""
    forbidden = (
        Path.home() / ".config" / "zsh" / "keys",
        Path.home() / ".ssh",
        Path.home() / ".aws",
        Path.home() / ".codex",
        Path.home() / ".config" / "pineforge",
    )

    def refuse(path: Path) -> None:
        if any(path == root or root in path.parents for root in forbidden) or any(
            part.endswith(".env") for part in path.parts
        ):
            raise ValueError(f"refusing fixture path: {path}")

    resolved = []
    for path in paths:
        refuse(path.absolute())
        path = path.resolve()
        refuse(path)
        resolved.append(path)
    if announce:
        print(
            "fixture files: " + ", ".join(map(str, resolved)),
            file=sys.stderr,
            flush=True,
        )


def _fake_native(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", default="{}")
    parser.add_argument("--stderr", default="")
    parser.add_argument("--stderr-mib", type=int, default=0)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--mib", type=int)
    parser.add_argument("--crash", action="store_true")
    parser.add_argument("--failure-after-trials", action="store_true")
    parser.add_argument("--progress-fd", type=int)
    parser.add_argument("--trials-file", type=Path)
    parser.add_argument("--no-improvement-trials")
    args = parser.parse_args(arguments)
    _fixture_paths(
        _SCRIPT, *([args.trials_file] if args.trials_file else []), announce=False
    )
    if args.crash:
        raise RuntimeError("deliberate native fixture crash")
    if args.progress_fd is not None:
        os.write(args.progress_fd, b'{"trial_id":0}\n')
    if args.trials_file is not None:
        args.trials_file.write_bytes(b'{"trial_id":0,"status":"ok"}\n')
    if args.mib is None:
        sys.stdout.buffer.write(args.payload.encode("utf-8"))
    else:
        # Exactly 4096 bytes per row, produced without ever allocating N MiB.
        row = b'{"trial_id":0,"padding":"' + b"x" * 4069 + b'"}'
        assert len(row) == 4096
        sys.stdout.buffer.write(b'{"ok":true,"trials":[')
        for index in range(args.mib * _MIB // len(row)):
            if index:
                sys.stdout.buffer.write(b",")
            sys.stdout.buffer.write(row)
        sys.stdout.buffer.write(b"]")
        if args.failure_after_trials:
            sys.stdout.buffer.write(
                b',"failure":{"origin":"hpo","code":"hpo_output_io_failed",'
                b'"args":{},"exit_code":1}'
            )
        sys.stdout.buffer.write(b"}\n")
    sys.stderr.write(args.stderr)
    for _ in range(args.stderr_mib * 16):
        sys.stderr.write("e" * (64 * 1024))
    return args.exit_code


def _study(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        spec_path=root / "study.json",
        strategy=SimpleNamespace(id="strategy", dataset_ids=["bars"]),
    )


def _native_command(*arguments: str) -> list[str]:
    _fixture_paths(_SCRIPT)
    return [sys.executable, "-I", str(_SCRIPT), "--fake-native", *arguments]


def _measure(arguments: list[str]) -> int:
    import resource

    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("size")
    args = parser.parse_args(arguments)
    _fixture_paths(args.root)
    root = args.root.resolve()
    result_path = root / f"measure-{args.size}.json"
    output_path = root / f"stdout-{args.size}.json"
    error_path = root / f"stderr-{args.size}.txt"
    spool_directory = root / "spools"
    _fixture_paths(_SCRIPT, result_path, output_path, error_path, spool_directory)
    native_args = (
        ["--crash"] if args.size == "crash" else ["--mib", str(int(args.size))]
    )
    result = {"exit_code": 1, "size": args.size}
    try:
        with (
            output_path.open("w", encoding="utf-8") as stdout,
            error_path.open("w", encoding="utf-8") as stderr,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            mock.patch.dict(os.environ, {"TMPDIR": str(spool_directory)}),
            mock.patch.object(cli, "load_study_spec", return_value=_study(root)),
            mock.patch.object(
                cli, "prepare_run", return_value=(_native_command(*native_args), {})
            ),
        ):
            result["exit_code"] = cli.main(["run", str(root / "study.json")])
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        result["peak_rss_bytes"] = peak if sys.platform == "darwin" else peak * 1024
        result["stdout_bytes"] = output_path.stat().st_size
        result["spools_remaining"] = sorted(
            path.name for path in spool_directory.iterdir()
        )
    except BaseException:
        result["exit_code"] = 1
        raise
    finally:
        result_path.write_text(json.dumps(result), encoding="utf-8")
    return result["exit_code"]


class RunStreamingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.spools = self.root / "spools"
        self.spools.mkdir()
        self.study = _study(self.root)
        self.artifact = {"plugin": "plugin.so", "cache_hit": False}
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.enterContext(mock.patch.dict(os.environ, {"TMPDIR": str(self.spools)}))
        self.enterContext(
            mock.patch.object(cli, "load_study_spec", return_value=self.study)
        )
        self.prepare = self.enterContext(mock.patch.object(cli, "prepare_run"))
        self.files = []
        temporary_file = tempfile.TemporaryFile
        test = self

        class CheckedSpool:
            def __init__(self, source) -> None:
                self.source = source

            def read(self, size: int = -1) -> bytes:
                test.assertGreater(size, 0, "unbounded result-spool read")
                test.assertLessEqual(size, 64 * 1024)
                return self.source.read(size)

            def __getattr__(self, name):
                return getattr(self.source, name)

        def track_file(*args, **kwargs):
            source = temporary_file(*args, **kwargs)
            self.files.append(source)
            return CheckedSpool(source)

        self.enterContext(
            mock.patch.object(cli.tempfile, "TemporaryFile", side_effect=track_file)
        )

    def _run(self, command: list[str], *arguments: str) -> int:
        if "--trials-file" in arguments:
            _fixture_paths(Path(arguments[arguments.index("--trials-file") + 1]))
        self.prepare.return_value = (command, self.artifact)
        with (
            contextlib.redirect_stdout(self.output),
            contextlib.redirect_stderr(self.errors),
        ):
            return cli._run(
                cli._parser().parse_args(["run", str(self.study.spec_path), *arguments])
            )

    def _assert_clean(self) -> None:
        self.assertEqual(list(self.spools.iterdir()), [])
        self.assertTrue(all(source.closed for source in self.files))

    def test_success_and_no_feasible_exit_preserve_formatted_stdout_bytes(self) -> None:
        # Include escapes, surrogate pairs, exponents, duplicate keys, and values
        # crossing a 64 KiB read boundary. Compare the previous serializer's bytes.
        payload = (
            '{"z":-0.0,"a":1e-6,"trials":[{"z":"é\\\\\\\"\\n",'
            '"a":"\\ud83d\\ude80","padding":"'
            + "x" * (64 * 1024 - 11)
            + '"}],"warm_start_trials":[{"trial_id":7}],'
            '"completion_order":[3,1,2],"completed_before":[0,1,1],'
            '"nested":{"z":[],"a":[{},[],true,null]},'
            '"duplicate":0,"duplicate":1,"artifact":"replaced","ok":true}'
        )
        expected = json.loads(payload)
        expected.update(
            study=str(self.study.spec_path),
            strategy_id="strategy",
            dataset_id="bars",
            artifact=self.artifact,
        )
        expected_bytes = (
            json.dumps(expected, indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")
        for code in (0, 2):
            with self.subTest(code=code):
                destination = self.root / f"result-{code}.json"
                binary_stdout = io.BytesIO()
                text_stdout = io.TextIOWrapper(binary_stdout, encoding="utf-8")
                self.prepare.return_value = (
                    _native_command("--payload", payload, "--exit-code", str(code)),
                    self.artifact,
                )
                with (
                    contextlib.redirect_stdout(text_stdout),
                    contextlib.redirect_stderr(self.errors),
                ):
                    returned = cli._run(
                        cli._parser().parse_args(
                            [
                                "run",
                                str(self.study.spec_path),
                                "--output",
                                str(destination),
                            ]
                        )
                    )
                text_stdout.flush()
                self.assertEqual(returned, code)
                self.assertEqual(binary_stdout.getvalue(), expected_bytes)
                self.assertEqual(destination.read_bytes(), expected_bytes)
                text_stdout.detach()
                self._assert_clean()

    def test_warm_start_and_space_exhausted_print_stderr_before_raw_stdout(
        self,
    ) -> None:
        class OrderedOutput(io.StringIO):
            def __init__(self, name: str, events: list[tuple[str, str]]) -> None:
                super().__init__()
                self.name, self.events = name, events

            def write(self, value: str) -> int:
                self.events.append((self.name, value))
                return super().write(value)

        for code in (4, 5):
            with self.subTest(code=code):
                events: list[tuple[str, str]] = []
                self.output = OrderedOutput("stdout", events)
                self.errors = OrderedOutput("stderr", events)
                payload = '{"failure":{"code":"native_refusal"},"note":"é"}\n'
                self.assertEqual(
                    self._run(
                        _native_command(
                            "--payload",
                            payload,
                            "--stderr",
                            "  native diagnostic \n",
                            "--exit-code",
                            str(code),
                        )
                    ),
                    code,
                )
                self.assertEqual(self.output.getvalue(), payload)
                self.assertEqual(self.errors.getvalue(), "native diagnostic\n")
                first_stdout = next(
                    index for index, event in enumerate(events) if event[0] == "stdout"
                )
                self.assertTrue(
                    all(event[0] == "stderr" for event in events[:first_stdout])
                )
                self._assert_clean()

    def test_failure_after_large_trial_array_keeps_classification(self) -> None:
        command = _native_command(
            "--mib",
            "8",
            "--failure-after-trials",
            "--exit-code",
            "1",
        )
        with self.assertRaises(cli.CliError) as caught:
            self._run(command)
        self.assertEqual(caught.exception.code, "hpo_output_io_failed")
        self.assertEqual(caught.exception.origin, "hpo")
        self.assertEqual(caught.exception.failure_args, {})
        self.assertEqual(self.output.getvalue(), "")
        self._assert_clean()

    def test_classified_failure_and_invalid_metadata_cleanup(self) -> None:
        for origin, code, failure_args, expected in (
            ("engine", "engine_rejected", {"input": "Length"}, "engine_rejected"),
            ("hpo", "hpo_output_io_failed", {"reason": []}, "hpo_output_io_failed"),
            ("invalid", "bad", {}, "hpo_unclassified_error"),
        ):
            with self.subTest(origin=origin):
                payload = json.dumps(
                    {"failure": {"origin": origin, "code": code, "args": failure_args}}
                )
                command = _native_command("--payload", payload, "--exit-code", "1")
                with self.assertRaises(cli.CliError) as caught:
                    self._run(command)
                self.assertEqual(caught.exception.code, expected)
                if failure_args == {"reason": []}:
                    self.assertIsNone(caught.exception.failure_args)
                self._assert_clean()

    def test_oversized_failure_args_keep_the_code_with_bounded_reads(self) -> None:
        payload = json.dumps(
            {
                "failure": {
                    "origin": "engine",
                    "code": "engine_rejected",
                    "args": {"detail": "x" * (70 * 1024)},
                }
            }
        )
        with self.assertRaises(cli.CliError) as caught:
            self._run(_native_command("--payload", payload, "--exit-code", "1"))
        self.assertEqual(caught.exception.code, "engine_rejected")
        self.assertEqual(caught.exception.origin, "engine")
        self.assertIsNone(caught.exception.failure_args)
        self._assert_clean()

    def test_stderr_capture_is_drained_and_bounded(self) -> None:
        with self.assertRaises(cli.CliError) as caught:
            self._run(
                _native_command(
                    "--stderr",
                    "native diagnostic\n",
                    "--stderr-mib",
                    "8",
                    "--exit-code",
                    "1",
                )
            )
        self.assertIn("native diagnostic\n", str(caught.exception))
        self.assertIn("[native stderr truncated]", str(caught.exception))
        self.assertLess(len(str(caught.exception)), 64 * 1024 + 256)
        self._assert_clean()

    def test_utf8_and_json_escapes_cross_string_chunk_boundaries(self) -> None:
        for ending in ("é🚀", "\\ud83d\\ude80", "\\\\\\\"\\t\\n"):
            with self.subTest(ending=ending):
                value = "x" * (64 * 1024 - 2) + ending
                payload = '{"checkpoint":"' + value + '","trials":[]}'
                expected = json.loads(payload)
                expected.update(
                    study=str(self.study.spec_path),
                    strategy_id="strategy",
                    dataset_id="bars",
                    artifact=self.artifact,
                )
                self.output = io.StringIO()
                self.assertEqual(self._run(_native_command("--payload", payload)), 0)
                expected_text = json.dumps(
                    expected, indent=2, sort_keys=True, allow_nan=False
                )
                self.assertEqual(self.output.getvalue(), expected_text + "\n")
                self._assert_clean()

    def test_invalid_json_and_non_object_result_do_not_publish_partial_output(
        self,
    ) -> None:
        for payload in ('{"trials":[{},', '{"ok":true} trailing', "[]"):
            with self.subTest(payload=payload):
                with self.assertRaises(cli.CliError) as caught:
                    self._run(_native_command("--payload", payload))
                self.assertEqual(caught.exception.code, "hpo_plugin_invalid")
                self.assertEqual(self.output.getvalue(), "")
                self._assert_clean()

    def test_small_invalid_json_keeps_the_stdlib_diagnostic(self) -> None:
        payload = '{\n  "trials": [0,}\n'
        try:
            json.loads(payload)
        except json.JSONDecodeError as error:
            expected = str(error)
        with self.assertRaises(cli.CliError) as caught:
            self._run(_native_command("--payload", payload))
        self.assertEqual(
            str(caught.exception), f"native runner returned invalid JSON: {expected}"
        )
        self._assert_clean()

    def test_spawn_output_and_stdout_errors_close_spools(self) -> None:
        with self.assertRaises(cli.CliError) as caught:
            self._run([str(self.root / "missing-native")])
        self.assertEqual(caught.exception.code, "hpo_toolchain_unavailable")
        self._assert_clean()
        destination = self.root / "directory"
        destination.mkdir()
        with self.assertRaises(cli.CliError) as caught:
            self._run(_native_command(), "--output", str(destination))
        self.assertEqual(caught.exception.code, "hpo_output_io_failed")
        self.assertEqual(list(self.root.glob(".directory.*")), [])
        self._assert_clean()

        class BrokenOutput(io.StringIO):
            def write(self, value: str) -> int:
                raise BrokenPipeError("consumer closed stdout")

        self.output = BrokenOutput()
        with self.assertRaises(BrokenPipeError):
            self._run(_native_command())
        self._assert_clean()

    def test_formatted_spool_creation_and_write_errors_close_native_stdout(
        self,
    ) -> None:
        temporary_file = cli.tempfile.TemporaryFile
        created = 0

        def fail_second(*args, **kwargs):
            nonlocal created
            created += 1
            if created == 2:
                raise OSError("cannot create formatted spool")
            return temporary_file(*args, **kwargs)

        with mock.patch.object(cli.tempfile, "TemporaryFile", side_effect=fail_second):
            with self.assertRaises(cli.CliError) as caught:
                self._run(_native_command())
        self.assertEqual(caught.exception.code, "hpo_output_io_failed")
        self._assert_clean()
        with mock.patch.object(
            cli._ResultJson,
            "render",
            side_effect=OSError("cannot write formatted spool"),
        ):
            with self.assertRaises(cli.CliError) as caught:
                self._run(_native_command())
        self.assertEqual(caught.exception.code, "hpo_output_io_failed")
        self._assert_clean()

    def test_progress_descriptor_and_trials_file_remain_live(self) -> None:
        read_fd, write_fd = os.pipe()
        trials = self.root / "trials.jsonl"
        try:
            self.assertEqual(
                self._run(
                    _native_command(),
                    "--progress-fd",
                    str(write_fd),
                    "--trials-file",
                    str(trials),
                    "--no-improvement-trials",
                    "7",
                ),
                0,
            )
            self.assertEqual(os.read(read_fd, 4096), b'{"trial_id":0}\n')
            self.assertEqual(trials.read_bytes(), b'{"trial_id":0,"status":"ok"}\n')
            self._assert_clean()
        finally:
            os.close(write_fd)
            os.close(read_fd)

    def test_temp_directory_selection_and_explicit_failure_do_not_retry(self) -> None:
        selected = []
        temporary_file = cli.tempfile.TemporaryFile

        def select(*args, **kwargs):
            selected.append(kwargs["dir"])
            # Verify selection without actually writing outside this test's root.
            return temporary_file(*args, **{**kwargs, "dir": str(self.spools)})

        with mock.patch.object(cli.tempfile, "TemporaryFile", side_effect=select):
            for environment in ({"TMPDIR": ""}, {}):
                with mock.patch.dict(os.environ, environment, clear=True):
                    self.assertEqual(self._run(_native_command()), 0)
                    self.assertEqual(selected[-2:], ["/tmp", "/tmp"])
        self._assert_clean()

        with mock.patch.dict(os.environ, {"TMPDIR": str(self.root / "missing")}):
            with self.assertRaises(cli.CliError) as caught:
                self._run(_native_command())
        self.assertEqual(caught.exception.code, "hpo_output_io_failed")
        self._assert_clean()

    @unittest.skipUnless(
        sys.platform in {"linux", "darwin"}, "ru_maxrss units are known"
    )
    def test_peak_rss_does_not_scale_with_trial_stdout(self) -> None:
        measurements = {}
        # The crash row proves the harness records the work's nonzero exit, rather
        # than a successful wrapper/marker write. It is a control, not an RSS sample.
        for size in ("crash", "8", "128"):
            result_path = self.root / f"measure-{size}.json"
            _fixture_paths(_SCRIPT, result_path, self.spools)
            completed = subprocess.run(
                [sys.executable, "-I", str(_SCRIPT), "--measure", str(self.root), size],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=180,
            )
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(
                completed.returncode, result["exit_code"], completed.stderr
            )
            self.assertEqual(result["spools_remaining"], [])
            if size == "crash":
                self.assertEqual(result["exit_code"], 1)
                failure = json.loads(
                    (self.root / "stdout-crash.json").read_text(encoding="utf-8")
                )["failure"]
                self.assertEqual(failure["code"], "hpo_unclassified_error")
            else:
                self.assertEqual(result["exit_code"], 0, completed.stderr)
                self.assertGreater(result["stdout_bytes"], int(size) * _MIB)
                measurements[size] = result["peak_rss_bytes"]
        # Input increases by 120 MiB. A 16 MiB delta tolerates allocator/import
        # noise, but cannot hide even one retained copy of the larger document.
        self.assertLess(
            measurements["128"] - measurements["8"],
            16 * _MIB,
            measurements,
        )


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fake-native"]:
        raise SystemExit(_fake_native(sys.argv[2:]))
    if sys.argv[1:2] == ["--measure"]:
        raise SystemExit(_measure(sys.argv[2:]))
    unittest.main()
