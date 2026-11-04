#!/usr/bin/env python3
"""Observe the UNCHANGED HPO memory gate: one immutable sample per CI task.

This is a measurement instrument only. It never changes the gate, its workload,
seed, counts, output retention, parser, thresholds, caps, tmpfs policy or
controls. It runs scripts/ci_memory_gate.py (SHA256 ORIGINAL_GATE_SHA256) as an
immutable subprocess and keeps everything that gate wrote.

  sample     Run the original gate exactly once into a fresh sample root and
             write OBSERVATION.json there.
             Exit 0: valid sample, the gate exited 0.
             Exit 1: valid sample, the gate exited 1 with complete measurements
                     (a real observed gate red; never relabelled as passed).
             Exit 3: invalid sample whose gate exit was 0 or 1, or the gate was
                     never run (identity pre-check or a failed control).
             Any other nonzero exit is the gate's own exit (2) or 128+signal,
             and that sample is invalid.
  summarize  Read exactly ten named sample roots and write descriptive
             statistics. Exit 0: MEASURED, no original gate failure. Exit 1:
             MEASURED with at least one original gate failure. Exit 2:
             INCOMPLETE. It never reports a gate PASS.
             The ten samples must carry the SAME binary bytes: the native CLI,
             plugin and plugin manifest SHA256 are HARD identity, equal in all
             ten samples like the source, gate and CSV identity. There is no
             soft mode: ten independently rebuilt binaries whose digests differ
             are not a same-byte variance collection, and the collection is
             INCOMPLETE (exit 2).

A verdict is read from the gate's own RUN.COMPLETE, gate.exit and per-trial
result files, never from an exit code or a marker alone.
"""

from __future__ import annotations

import argparse
import datetime
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import types

ORIGINAL_GATE_SHA256 = (
    "f37e18cf8452d1dfdd7a8c7cd679877d82c73eeef0ea58b2995b20c695a38cff"
)
GATE_RELATIVE_PATH = Path("scripts/ci_memory_gate.py")
ADMITTED_RUNNER_LABEL = "gke-16cpu-32gb-amd64-64gb"
COUNTS = (10, 1_000, 100_000)
BAR_COUNTS = (2, 1_000, 100_000)
# Guard only: the original gate's limits, checked, never applied or proposed here.
ORIGINAL_SLOPE_LIMITS = (256, 32)
REQUIRED_SAMPLES = 10
REPEAT_IDS = tuple(f"r{index:02d}" for index in range(1, REQUIRED_SAMPLES + 1))
GNU_TIME = "/usr/bin/time"
SIGKILL = int(signal.SIGKILL)
# The exact bytes the admitted workflow step writes with printf.
EXPECTED_BARS_BYTES = (
    b"timestamp,open,high,low,close,volume\n"
    b"1700000000000,100,102,99,101,10\n"
    b"1700000060000,100,102,99,101,10\n"
)
ORIGINAL_COMMAND = (
    "python3 -I scripts/ci_memory_gate.py "
    "--native build/bin/pineforge-hpo-native "
    "--plugin build/lib/fake_strategy.so "
    "--ohlcv build/memory-gate-bars.csv "
    "--artifacts build/memory-gate --bars-axis"
)
CRASH_REASON = "GNU time observed deliberate SIGKILL; wait4 and marker agree"
FAIL_REASON_PREFIX = "trial RSS growth exceeded a threshold"
CRASH_CHILD = ("-I", "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
# Named, non-secret CI coordinates only (no environment dump): they let the
# summary state how many distinct pods the samples really came from.
CI_CONTEXT_NAMES = (
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_JOB",
    "GITHUB_WORKFLOW",
    "GITHUB_REPOSITORY",
    "RUNNER_NAME",
    "RUNNER_OS",
    "RUNNER_ARCH",
)
SCHEMA = "pineforge-hpo-gate-variance-observation/1"
SUMMARY_SCHEMA = "pineforge-hpo-gate-variance-summary/1"
OBSERVATION = "OBSERVATION.json"
OBSERVER_EXIT = "observer.exit"
ARTIFACTS = "gate-artifacts"
CONTROL_LABEL_KEY = "instrument_control"
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_TIME_BYTES = 4096
MAX_MARKER_BYTES = 64
MARKER = re.compile(r"-?[0-9]{1,6}\n")
RECORDED_KEYS = (
    "gate_verdict",
    "gate_reason",
    "trial_points",
    "trial_slopes",
    "bar_points",
    "crash_control",
)
# SAME BINARY BYTES is a hard contract: the native CLI, the plugin and the
# plugin's adjacent manifest must have equal SHA256 in all ten samples. There is
# no opt-out and no soft or report-only mode for these three digests.
BINARY_IDENTITY_KEYS = ("native_sha256", "plugin_sha256", "manifest_sha256")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
# Every sample in one collection must agree on these exactly.
HARD_IDENTITY_KEYS = (
    "source_head",
    "source_tree",
    "gitlinks",
    "submodule_heads",
    "tracked_changes",
    "gate_sha256",
    "bars_sha256",
    *BINARY_IDENTITY_KEYS,
    "observer_sha256",
    "python_version",
    "python_sha256",
    "gnu_time_version",
    "gnu_time_sha256",
    "machine",
    "runner_label",
    "gate_flags",
)
PROTECTED_PARTS = frozenset({".ssh", ".aws", ".codex"})


class Refused(ValueError):
    """A protected or escaping path; it was not followed."""


class SampleInvalid(ValueError):
    """This sample cannot count as an observation of the unchanged gate."""


def _check_name(candidate: Path) -> None:
    parts = candidate.parts
    if any(part in PROTECTED_PARTS or part.endswith(".env") for part in parts):
        raise Refused(f"refused protected path by name: {candidate}")
    for index in range(len(parts)):
        if parts[index : index + 3] == (".config", "zsh", "keys") or parts[
            index : index + 2
        ] == (".config", "pineforge"):
            raise Refused(f"refused protected path by name: {candidate}")


def approved(path: Path, within: Path | None = None) -> Path:
    """Refuse protected names before resolution, symlink targets and escapes."""
    path = Path(path)
    _check_name(path)
    absolute = Path(os.path.abspath(path))
    _check_name(absolute)
    try:
        resolved = absolute.resolve()
    except (RuntimeError, OSError) as error:
        raise Refused(f"cannot resolve path: {absolute}") from error
    _check_name(resolved)
    if within is not None:
        root = Path(os.path.abspath(within)).resolve()
        if resolved != root and root not in resolved.parents:
            raise Refused(f"refused path escaping {root}: {path} -> {resolved}")
    return resolved


def list_files(label: str, paths: list, within: Path | None = None) -> list:
    """List the approved literal paths before anything follows them."""
    checked = [approved(Path(path), within) for path in paths]
    print(
        f"FILES {label}: {json.dumps([str(path) for path in checked])}",
        file=sys.stderr,
        flush=True,
    )
    return checked


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_bounded(path: Path, limit: int) -> bytes:
    try:
        with Path(path).open("rb") as source:
            data = source.read(limit + 1)
    except FileNotFoundError as error:
        raise SampleInvalid(f"missing file: {Path(path).name}") from error
    except IsADirectoryError as error:
        raise SampleInvalid(f"not a regular file: {Path(path).name}") from error
    if len(data) > limit:
        raise SampleInvalid(f"{Path(path).name} exceeds {limit} bytes")
    return data


def read_json(path: Path) -> dict:
    data = read_bounded(path, MAX_JSON_BYTES)
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SampleInvalid(f"invalid JSON in {Path(path).name}: {error}") from error
    if not isinstance(value, dict):
        raise SampleInvalid(f"{Path(path).name} is not a JSON object")
    return value


def read_marker(path: Path) -> int:
    text = read_bounded(path, MAX_MARKER_BYTES).decode("ascii", errors="replace")
    if not MARKER.fullmatch(text):
        raise SampleInvalid(f"invalid exit marker {Path(path).name}: {text!r}")
    return int(text)


def shell_status(returncode: int) -> int:
    """The status a shell reports: 128+signal for a signalled child."""
    return 128 - returncode if returncode < 0 else returncode


def invalid_exit(returncode) -> int:
    """Exit code for an invalid sample: never 0, and never 1 (a valid red)."""
    if returncode is None or returncode in (0, 1):
        return 3
    return shell_status(returncode)


def _c_locale_environment() -> dict:
    """A fresh environment for one child: the caller's variables, LC_ALL forced to C.

    Tool text that is compared or parsed (GNU time, git) must not depend on the
    caller's language. os.environ itself is never changed, printed or recorded.
    """
    return {**os.environ, "LC_ALL": "C"}


def run_and_mark(argv: list, stdout: Path, stderr: Path, marker: Path) -> int:
    """Run one child, wait for its own status and record that status exactly."""
    command = [str(part) for part in argv]
    print("COMMAND " + json.dumps(command), file=sys.stderr, flush=True)
    with Path(stdout).open("xb") as output, Path(stderr).open("xb") as errors:
        child = subprocess.Popen(
            command, stdout=output, stderr=errors, env=_c_locale_environment()
        )
        returncode = child.wait()
    with Path(marker).open("x", encoding="ascii") as handle:
        handle.write(f"{returncode}\n")
    return returncode


def git(root: Path, *arguments: str) -> str:
    command = ["git", "-C", str(root), *arguments]
    print("COMMAND " + json.dumps(command), file=sys.stderr, flush=True)
    try:
        result = subprocess.run(
            command, capture_output=True, check=False, env=_c_locale_environment()
        )
    except OSError as error:
        raise SampleInvalid(f"cannot run git: {error}") from error
    if result.returncode != 0:
        detail = result.stderr[:500].decode("utf-8", errors="replace").strip()
        raise SampleInvalid(
            f"git {' '.join(arguments)} exited {result.returncode}: {detail}"
        )
    return result.stdout.decode("utf-8", errors="replace")


def _require_gitlink_directory(root: Path, name: str, commit: str) -> None:
    """A mode 160000 entry is a pinned commit plus a real, non-symlink directory.

    Checked on the literal path under the source root, before approved() resolves
    anything: every component must be a directory and none may be a symlink, so no
    link is followed. The path is only lstat-ed here, never opened, hashed or read
    as a file, and nothing is initialized or fetched.
    """
    where = f"mode 160000 gitlink {name} (pinned commit {commit})"
    current = root
    for part in name.split("/"):
        current = current / part
        try:
            kind = os.lstat(current).st_mode
        except OSError as error:
            raise SampleInvalid(
                f"{where} is not a real directory at {current}: {error}"
            ) from error
        if stat.S_ISLNK(kind):
            raise SampleInvalid(
                f"{where} has a symlink at {current}; the path must be a real "
                "directory and is not followed"
            )
        if not stat.S_ISDIR(kind):
            raise SampleInvalid(f"{where} is not a real directory at {current}")


def source_identity(root: Path) -> dict:
    (root,) = list_files("git repository read", [root])
    toplevel = approved(Path(git(root, "rev-parse", "--show-toplevel").strip()))
    if toplevel != root:
        raise SampleInvalid(f"source root {root} is not the repository top {toplevel}")
    gitlinks = []
    for entry in git(root, "ls-tree", "-r", "-z", "HEAD").split("\0"):
        meta, _, name = entry.partition("\t")
        fields = meta.split(" ")
        if len(fields) == 3 and fields[0] == "160000":
            gitlinks.append([name, fields[2]])
    submodule_heads = []
    for name, commit in gitlinks:
        _require_gitlink_directory(root, name, commit)
        (submodule,) = list_files("submodule read", [root / name], within=root)
        if next(submodule.iterdir(), None) is None:
            # A real empty directory is an uninitialized submodule. Git run inside
            # it would walk up and answer for the parent repository, so git is not
            # run there: the pinned commit stays in gitlinks, nothing is claimed as
            # checked out (None), and the pre-check in sample_into then rejects the
            # sample as a mismatch.
            submodule_heads.append([name, None])
            continue
        top = approved(Path(git(submodule, "rev-parse", "--show-toplevel").strip()))
        if top != submodule:
            raise SampleInvalid(f"submodule {name} is not checked out")
        submodule_heads.append([name, git(submodule, "rev-parse", "HEAD").strip()])
    return {
        "source_root": str(root),
        "source_head": git(root, "rev-parse", "HEAD").strip(),
        "source_tree": git(root, "rev-parse", "HEAD^{tree}").strip(),
        "gitlinks": gitlinks,
        "submodule_heads": submodule_heads,
        "tracked_changes": git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
            "--ignore-submodules=untracked",
        ),
    }


def python_identity() -> dict:
    (python,) = list_files("Python interpreter", [Path(sys.executable)])
    on_path = shutil.which("python3")
    return {
        "python_executable": str(python),
        "python_sha256": sha256_file(python),
        "python_version": sys.version,
        "python3_on_path_is_this_interpreter": (
            on_path is not None and approved(Path(on_path)) == python
        ),
    }


def gnu_time_identity() -> dict:
    (launcher,) = list_files("GNU time launcher", [Path(GNU_TIME)])
    command = [GNU_TIME, "--version"]
    print("COMMAND " + json.dumps(command), file=sys.stderr, flush=True)
    try:
        result = subprocess.run(
            command, capture_output=True, check=False, env=_c_locale_environment()
        )
    except OSError as error:
        raise SampleInvalid(f"cannot execute {GNU_TIME}: {error}") from error
    text = (result.stdout + result.stderr)[:4096].decode("utf-8", errors="replace")
    lines = text.strip().splitlines()
    first = lines[0] if lines else ""
    if result.returncode != 0 or "gnu time" not in text.lower():
        raise SampleInvalid(
            f"{GNU_TIME} is not GNU time (exit {result.returncode}): {first!r}"
        )
    return {
        "gnu_time_path": str(launcher),
        "gnu_time_sha256": sha256_file(launcher),
        "gnu_time_version": first,
    }


def load_gate_module(gate: Path) -> types.ModuleType:
    """Load the original gate's functions from the exact bytes just hashed."""
    (gate,) = list_files("original gate module read", [gate])
    source = gate.read_bytes()
    if hashlib.sha256(source).hexdigest() != ORIGINAL_GATE_SHA256:
        raise SampleInvalid("gate bytes are not the original gate")
    module = types.ModuleType("ci_memory_gate_original")
    module.__file__ = str(gate)
    exec(compile(source, str(gate), "exec"), module.__dict__)
    constants = (
        tuple(module.COUNTS),
        tuple(module.SLOPE_LIMITS),
        tuple(module.BAR_COUNTS),
        module.GNU_TIME,
    )
    if constants != (COUNTS, ORIGINAL_SLOPE_LIMITS, BAR_COUNTS, GNU_TIME):
        raise SampleInvalid(f"original gate constants differ from this kit: {constants}")
    return module


def _row_paths(row: dict, run_dir: Path, label: str) -> None:
    for key, suffix in (
        ("stdout", ".stdout"),
        ("stderr", ".stderr"),
        ("exit_file", ".exit"),
        ("result_file", ".result.json"),
        ("gnu_time_file", ".time"),
    ):
        value = row.get(key)
        if (
            not isinstance(value, str)
            or Path(value).name != label + suffix
            or Path(value).parent.name != run_dir.name
        ):
            raise SampleInvalid(f"{label}: {key} does not name this run's {label}{suffix}")


def _gnu_time_kib(run_dir: Path, label: str, row: dict) -> tuple:
    (time_file,) = list_files(
        f"{label} GNU time read", [run_dir / f"{label}.time"], within=run_dir
    )
    text = read_bounded(time_file, MAX_TIME_BYTES).decode("utf-8", errors="replace")
    if text != row.get("gnu_time_output"):
        raise SampleInvalid(f"{label}: GNU time file differs from the gate's own record")
    lines = text.splitlines()
    if not lines or not re.fullmatch(r"[0-9]+", lines[-1]) or int(lines[-1]) <= 0:
        raise SampleInvalid(f"{label}: GNU time last line is not a positive KiB count")
    return int(lines[-1]), lines


def _crash_control(run_dir: Path) -> dict:
    label = "deliberate-crash"
    (result,) = list_files(
        "original crash control read", [run_dir / f"{label}.result.json"], within=run_dir
    )
    row = read_json(result)
    code = row.get("exit_code")
    if (
        row.get("verdict") != "PASS"
        or row.get("reason") != CRASH_REASON
        or not is_int(code)
        or code == 0
    ):
        raise SampleInvalid("the original deliberate crash control did not pass")
    _row_paths(row, run_dir, label)
    _kib, lines = _gnu_time_kib(run_dir, label, row)
    if f"Command terminated by signal {SIGKILL}" not in lines[:-1]:
        raise SampleInvalid("the original crash control recorded no SIGKILL diagnostic")
    (marker,) = list_files(
        "original crash marker read", [run_dir / f"{label}.exit"], within=run_dir
    )
    if read_marker(marker) != code:
        raise SampleInvalid("the original crash marker differs from the child's own exit")
    return {"exit_code": code, "verdict": "PASS", "reason": CRASH_REASON}


def _measurement(run_dir: Path, axis: str, count: int, trials: int, bars: int) -> dict:
    label = f"{axis}-{count}"
    (result,) = list_files(
        f"{label} result read", [run_dir / f"{label}.result.json"], within=run_dir
    )
    row = read_json(result)
    expected = {
        "type": "measurement",
        "axis": axis,
        "trials": trials,
        "bars": bars,
        "verdict": "PASS",
        "exit_code": 0,
        "peak_source": "gnu_time_wait4",
        "trial_rows": trials,
    }
    for key, value in expected.items():
        if row.get(key) != value or (is_int(value) and not is_int(row.get(key))):
            raise SampleInvalid(f"{label}: {key}={row.get(key)!r}, expected {value!r}")
    if not is_int(row.get("pid")) or row.get("pid") != row.get("waited_pid"):
        raise SampleInvalid(f"{label}: wait4 did not reap the launched child")
    _row_paths(row, run_dir, label)
    kib, _lines = _gnu_time_kib(run_dir, label, row)
    peak = row.get("peak_rss_bytes")
    if not is_int(peak) or peak <= 0 or peak != kib * 1024:
        raise SampleInvalid(f"{label}: peak {peak!r} is not the raw GNU time {kib} KiB")
    (exit_file,) = list_files(
        f"{label} exit marker read", [run_dir / f"{label}.exit"], within=run_dir
    )
    if read_marker(exit_file) != 0:
        raise SampleInvalid(f"{label}: the native child's own exit marker is not 0")
    command = row.get("command")
    native = row.get("native_command")
    if (
        not isinstance(command, list)
        or not isinstance(native, list)
        or not native
        or not all(isinstance(part, str) for part in command + native)
        or command[:4] != [GNU_TIME, "-f", "%M", "-o"]
        or len(command) < 6
        or command[4] != row["gnu_time_file"]
        or command[5:] != ["--", *native]
    ):
        raise SampleInvalid(f"{label}: command is not GNU time around the native child")
    try:
        max_trials = native[native.index("--max-trials") + 1]
    except (ValueError, IndexError):
        max_trials = None
    if max_trials != str(trials):
        raise SampleInvalid(f"{label}: native command does not request {trials} trials")
    return {
        "axis": axis,
        "trials": trials,
        "bars": bars,
        "peak_rss_bytes": peak,
        "gnu_time_kib": kib,
        "trial_rows": row["trial_rows"],
        "pid": row["pid"],
        "native_command": native,
        "result_file": f"{run_dir.name}/{label}.result.json",
    }


def extract_gate_run(run_dir: Path, returncode) -> dict:
    """Read one gate run's own files; raise SampleInvalid on the first defect."""
    complete_path, exit_path = list_files(
        "gate run verdict and marker read",
        [run_dir / "RUN.COMPLETE", run_dir / "gate.exit"],
        within=run_dir,
    )
    complete = read_json(complete_path)
    marker = read_marker(exit_path)
    code = complete.get("exit_code")
    verdict = complete.get("verdict")
    if complete.get("type") != "verdict" or not is_int(code):
        raise SampleInvalid("RUN.COMPLETE is not a gate verdict")
    directory = complete.get("artifact_directory")
    if not isinstance(directory, str) or Path(directory).name != run_dir.name:
        raise SampleInvalid("RUN.COMPLETE names a different run directory")
    if marker != code or returncode != code:
        raise SampleInvalid(
            f"gate return code {returncode}, gate.exit {marker} and "
            f"RUN.COMPLETE exit_code {code} disagree"
        )
    if (code, verdict) not in ((0, "PASS"), (1, "FAIL")):
        raise SampleInvalid(
            f"gate exit {code} with verdict {verdict!r} is not a measurement: "
            f"{complete.get('reason', '')}"
        )
    if code == 1 and not str(complete.get("reason", "")).startswith(FAIL_REASON_PREFIX):
        raise SampleInvalid("gate exit 1 does not carry the trial-growth reason")
    crash = _crash_control(run_dir)
    points = [_measurement(run_dir, "trials", count, count, 2) for count in COUNTS]
    if complete.get("measurements") != 3 or not is_int(complete.get("measurements")):
        raise SampleInvalid("RUN.COMPLETE does not record three measurements")
    if complete.get("peak_sources") != ["gnu_time_wait4"] * 3:
        raise SampleInvalid("RUN.COMPLETE peaks are not all GNU time wait4 peaks")
    slopes = complete.get("trial_slopes")
    if not isinstance(slopes, list) or len(slopes) != 2:
        raise SampleInvalid("RUN.COMPLETE lacks the two original trial slopes")
    for index, edge in enumerate(slopes):
        left, right = points[index], points[index + 1]
        span = right["trials"] - left["trials"]
        delta = right["peak_rss_bytes"] - left["peak_rss_bytes"]
        limit = ORIGINAL_SLOPE_LIMITS[index]
        expected_edge = {
            "from_trials": left["trials"],
            "to_trials": right["trials"],
            "delta_rss_bytes": delta,
            "bytes_per_trial": delta / span,
            "limit_bytes_per_trial": limit,
            "verdict": "PASS" if delta <= limit * span else "FAIL",
        }
        if edge != expected_edge:
            raise SampleInvalid(f"trial slope {index} differs from the raw peaks or limits")
    passed = all(edge["verdict"] == "PASS" for edge in slopes)
    if passed != (verdict == "PASS"):
        raise SampleInvalid("RUN.COMPLETE verdict contradicts its own trial slopes")
    bar_points = []
    if verdict == "PASS":
        bar_points = [
            _measurement(run_dir, "bars", count, 10, count) for count in BAR_COUNTS
        ]
        bar_slopes = complete.get("bar_slopes")
        if not isinstance(bar_slopes, list) or len(bar_slopes) != 2:
            raise SampleInvalid("a PASS run lacks the informational bar slopes")
        for index, edge in enumerate(bar_slopes):
            left, right = bar_points[index], bar_points[index + 1]
            expected_edge = {
                "from_bars": left["bars"],
                "to_bars": right["bars"],
                "bytes_per_bar": (right["peak_rss_bytes"] - left["peak_rss_bytes"])
                / (right["bars"] - left["bars"]),
                "gated": False,
            }
            if edge != expected_edge:
                raise SampleInvalid(f"bar slope {index} differs from the raw bar peaks")
    else:
        if "bar_slopes" in complete:
            raise SampleInvalid("a FAIL run reports bar slopes it must not measure")
        absent = list_files(
            "absent bar results check",
            [run_dir / f"bars-{count}.result.json" for count in BAR_COUNTS],
            within=run_dir,
        )
        if any(path.exists() for path in absent):
            raise SampleInvalid("a FAIL run unexpectedly measured the bar axis")
    return {
        "gate_verdict": verdict,
        "gate_reason": complete.get("reason"),
        "trial_points": points,
        "trial_slopes": slopes,
        "bar_points": bar_points,
        "crash_control": crash,
        "run_complete": complete,
    }


def check_gate_stdout(root: Path, complete: dict) -> None:
    (stdout,) = list_files("gate stdout read", [root / "gate.stdout"], within=root)
    text = read_bounded(stdout, MAX_JSON_BYTES).decode("utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    try:
        last = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError as error:
        raise SampleInvalid("gate stdout does not end with its verdict JSON") from error
    if last != complete:
        raise SampleInvalid("gate stdout verdict differs from RUN.COMPLETE")


def verify_with_gate_module(module, run_dir, extracted, native, plugin, bars) -> dict:
    """Re-check retained rows with the original gate's own functions."""
    try:
        for point in extracted["trial_points"]:
            trials = point["trials"]
            expected = module.native_command(native, plugin, bars, trials)
            if point["native_command"] != expected:
                raise SampleInvalid(f"trials-{trials}: not the original native workload")
            stdout = run_dir / f"trials-{trials}.stdout"
            if module.validate_output(stdout, trials) != trials:
                raise SampleInvalid(f"trials-{trials}: original validate_output disagrees")
        for point in extracted["bar_points"]:
            count = point["bars"]
            fixture = bars if count == 2 else run_dir / f"bars-{count}.csv"
            expected = module.native_command(native, plugin, fixture, 10, bars_axis=True)
            if point["native_command"] != expected:
                raise SampleInvalid(f"bars-{count}: not the original bar-axis workload")
            if module.validate_output(run_dir / f"bars-{count}.stdout", 10) != 10:
                raise SampleInvalid(f"bars-{count}: original validate_output disagrees")
        verdict = module.judge_trial_growth(
            [(point["trials"], point["peak_rss_bytes"]) for point in extracted["trial_points"]]
        )
    except SampleInvalid:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SampleInvalid(f"original gate re-check failed: {error}") from error
    complete = extracted["run_complete"]
    if (verdict["verdict"], verdict["exit_code"], verdict["trial_slopes"]) != (
        complete["verdict"],
        complete["exit_code"],
        complete["trial_slopes"],
    ):
        raise SampleInvalid("original judge_trial_growth does not reproduce RUN.COMPLETE")
    return {
        "status": "verified",
        "native_command": "every row equals the original native_command()",
        "validate_output": "the original validate_output() re-read every native row",
        "judge": "the original judge_trial_growth() reproduced RUN.COMPLETE",
    }


def locate_run_directory(root: Path) -> Path:
    (artifacts,) = list_files("gate artifact directory", [root / ARTIFACTS], within=root)
    if not artifacts.is_dir():
        raise SampleInvalid("the gate created no artifact directory")
    entries = sorted(artifacts.iterdir())
    if (
        len(entries) != 1
        or not entries[0].name.startswith("run-")
        or entries[0].is_symlink()
        or not entries[0].is_dir()
    ):
        raise SampleInvalid(
            f"expected exactly one gate run directory, found {[e.name for e in entries]}"
        )
    (run_dir,) = list_files("gate run directory", [entries[0]], within=root)
    return run_dir


def new_record(repeat_id: str) -> dict:
    return {
        "schema": SCHEMA,
        "repeat_id": repeat_id,
        "status": "INVALID",
        "invalid_reasons": [],
        "started_utc": utc_now(),
        "original_command": ORIGINAL_COMMAND,
        "gate_returncode": None,
        "scope_note": (
            "one original gate run in one CI task; independence across tasks "
            "is stated only by summarize"
        ),
    }


def ci_context() -> dict:
    context = {name: os.environ.get(name) for name in CI_CONTEXT_NAMES}
    context["hostname"] = platform.node()
    return context


def observe_gate_run(root: Path, record: dict, returncode: int, gate_check=None) -> int:
    """Validate the gate's retained files for one sample; return the exit code."""
    reasons = record["invalid_reasons"]
    record["gate_returncode"] = returncode
    record["gate_shell_status"] = shell_status(returncode)
    try:
        run_dir = locate_run_directory(root)
        record["gate_run_directory"] = run_dir.relative_to(root).as_posix()
        extracted = extract_gate_run(run_dir, returncode)
        check_gate_stdout(root, extracted["run_complete"])
        if gate_check is None:
            record["gate_module_checks"] = {"status": "NOT RUN"}
        else:
            record["gate_module_checks"] = gate_check(run_dir, extracted)
    except SampleInvalid as error:
        if returncode not in (0, 1):
            reasons.append(f"gate exit {returncode} invalidates this sample")
        reasons.append(str(error))
        return invalid_exit(returncode)
    for key in RECORDED_KEYS:
        record[key] = extracted[key]
    if reasons:
        return invalid_exit(returncode)
    record["status"] = "VALID"
    return returncode


def observer_crash_control(root: Path) -> dict:
    """Every observer run first proves a crashing child leaves a nonzero marker."""
    stdout, stderr, marker = list_files(
        "observer deliberate crash row",
        [root / "observer-crash.stdout", root / "observer-crash.stderr", root / "observer-crash.exit"],
        within=root,
    )
    returncode = run_and_mark([sys.executable, *CRASH_CHILD], stdout, stderr, marker)
    recorded = read_marker(marker)
    if returncode != -SIGKILL or recorded != returncode:
        raise SampleInvalid(
            f"observer crash control failed: returncode {returncode}, marker {recorded}"
        )
    return {"returncode": returncode, "marker": recorded, "status": "PASS"}


def build_manifest(root: Path) -> dict:
    files = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        dirnames.sort()
        for name in dirnames:
            if (base / name).is_symlink():
                raise SampleInvalid(f"symlink inside sample root: {base / name}")
        for name in sorted(filenames):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if relative in (OBSERVATION, OBSERVER_EXIT):
                continue
            if path.is_symlink() or not path.is_file():
                raise SampleInvalid(f"non-regular file inside sample root: {relative}")
            files.append((relative, path))
    checked = list_files("manifest hash", [path for _, path in files], within=root)
    return {relative: sha256_file(path) for (relative, _), path in zip(files, checked)}


def finalize_observation(root: Path, record: dict) -> None:
    record["finished_utc"] = utc_now()
    record["manifest"] = build_manifest(root)
    observation, marker = list_files(
        "observation and observer exit write",
        [root / OBSERVATION, root / OBSERVER_EXIT],
        within=root,
    )
    with observation.open("x", encoding="utf-8") as output:
        output.write(json.dumps(record, sort_keys=True, indent=1) + "\n")
    with marker.open("x", encoding="ascii") as output:
        output.write(f"{record['observer_exit_code']}\n")


def sample_into(root: Path, args: argparse.Namespace, record: dict) -> int:
    reasons = record["invalid_reasons"]
    record["observer_crash_control"] = observer_crash_control(root)
    (source_root,) = list_files("source root", [args.source_root])
    gate, native, plugin, bars = list_files(
        "gate and gate inputs", [args.gate, args.native, args.plugin, args.ohlcv]
    )
    (manifest,) = list_files("plugin adjacent manifest", [plugin.parent / "manifest.json"])
    identity = {"runner_label": args.runner_label, "expected_head": args.expected_head}
    record["identity"] = identity
    inputs = (
        ("gate", gate),
        ("native", native),
        ("plugin", plugin),
        ("manifest", manifest),
        ("bars", bars),
    )
    for name, path in inputs:
        identity[f"{name}_path"] = str(path)
        if not path.is_file():
            reasons.append(f"{name} is not a regular file: {path}")
            continue
        identity[f"{name}_sha256"] = sha256_file(path)
        identity[f"{name}_bytes"] = path.stat().st_size
    if native.is_file() and not os.access(native, os.X_OK):
        reasons.append(f"native CLI is not executable: {native}")
    if gate != source_root / GATE_RELATIVE_PATH:
        reasons.append(f"gate {gate} is not {GATE_RELATIVE_PATH} of the source root")
    if identity.get("gate_sha256") != ORIGINAL_GATE_SHA256:
        reasons.append(
            f"gate SHA256 {identity.get('gate_sha256')} is not {ORIGINAL_GATE_SHA256}"
        )
    if bars.is_file():
        try:
            if read_bounded(bars, 4096) != EXPECTED_BARS_BYTES:
                reasons.append("OHLCV file is not the workflow's exact two-bar CSV")
        except SampleInvalid as error:
            reasons.append(f"OHLCV file: {error}")
    (observer,) = list_files("observer self read", [Path(__file__)])
    identity["observer_sha256"] = sha256_file(observer)
    for collect in (
        lambda: source_identity(source_root),
        python_identity,
        gnu_time_identity,
    ):
        try:
            identity.update(collect())
        except SampleInvalid as error:
            reasons.append(str(error))
    identity["machine"] = platform.machine()
    identity["kernel_release"] = platform.release()
    if "source_head" in identity and identity["source_head"] != args.expected_head:
        reasons.append(
            f"source HEAD {identity['source_head']} is not --expected-head {args.expected_head}"
        )
    if "gitlinks" in identity and identity["submodule_heads"] != identity["gitlinks"]:
        reasons.append("checked-out submodule commits differ from the pinned gitlinks")
    if identity.get("tracked_changes"):
        reasons.append("tracked source files differ from HEAD")
    argv = [
        sys.executable,
        "-I",
        str(args.gate),
        "--native",
        str(args.native),
        "--plugin",
        str(args.plugin),
        "--ohlcv",
        str(args.ohlcv),
        "--artifacts",
        str(args.sample_root / ARTIFACTS),
        "--bars-axis",
    ]
    record["gate_argv"] = argv
    identity["gate_flags"] = [token for token in argv[1:] if token.startswith("-")]
    if reasons:
        reasons.insert(0, "identity pre-check failed; the gate was not run")
        return 3
    module = load_gate_module(gate)
    stdout, stderr, marker = list_files(
        "gate stdout, stderr and own exit marker",
        [root / "gate.stdout", root / "gate.stderr", root / "gate.returncode"],
        within=root,
    )
    record["gate_started_utc"] = utc_now()
    returncode = run_and_mark(argv, stdout, stderr, marker)
    record["gate_returncode"] = returncode
    record["gate_finished_utc"] = utc_now()
    for name, path in inputs:
        if sha256_file(path) != identity[f"{name}_sha256"]:
            reasons.append(f"{name} bytes changed during the gate run")
    record["gate_sha256_after"] = sha256_file(gate)

    def gate_check(run_dir: Path, extracted: dict) -> dict:
        return verify_with_gate_module(module, run_dir, extracted, native, plugin, bars)

    return observe_gate_run(root, record, returncode, gate_check)


def cmd_sample(args: argparse.Namespace) -> int:
    if args.repeat_id not in REPEAT_IDS:
        print(f"ERROR --repeat-id must be one of {list(REPEAT_IDS)}", file=sys.stderr)
        return 3
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_head):
        print("ERROR --expected-head must be a full 40-hex commit", file=sys.stderr)
        return 3
    if args.runner_label != ADMITTED_RUNNER_LABEL:
        print(f"ERROR --runner-label must be {ADMITTED_RUNNER_LABEL}", file=sys.stderr)
        return 3
    try:
        (root,) = list_files("fresh sample root create", [args.sample_root])
        root.parent.mkdir(parents=True, exist_ok=True)
        root.mkdir()
    except (OSError, ValueError) as error:
        print(f"ERROR sample root refused or not fresh: {error}", file=sys.stderr)
        return 3
    record = new_record(args.repeat_id)
    record["ci_context"] = ci_context()
    try:
        exit_code = sample_into(root, args, record)
    except Exception as error:  # Any observer fault invalidates this sample.
        record["status"] = "INVALID"
        record["invalid_reasons"].append(f"observer error: {type(error).__name__}: {error}")
        exit_code = invalid_exit(record.get("gate_returncode"))
    if record["status"] != "VALID" and exit_code in (0, 1):
        exit_code = 3
    record["observer_exit_code"] = exit_code
    try:
        finalize_observation(root, record)
    except Exception as error:  # No observation file means no sample.
        print(f"ERROR observation not written: {type(error).__name__}: {error}", file=sys.stderr)
        return exit_code if exit_code not in (0, 1) else 3
    print(
        json.dumps(
            {
                "repeat_id": record["repeat_id"],
                "status": record["status"],
                "gate_returncode": record.get("gate_returncode"),
                "gate_verdict": record.get("gate_verdict"),
                "observer_exit_code": exit_code,
                "invalid_reasons": record["invalid_reasons"],
                "observation": str(root / OBSERVATION),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return exit_code


def load_sample(argument: Path, allow_instrument_controls: bool) -> dict:
    (root,) = list_files("sample root read", [argument])
    if not root.is_dir():
        raise SampleInvalid("sample root is missing")
    observation, observer_exit = list_files(
        "observation read", [root / OBSERVATION, root / OBSERVER_EXIT], within=root
    )
    record = read_json(observation)
    if record.get("schema") != SCHEMA:
        raise SampleInvalid("not a gate-variance observation")
    control = CONTROL_LABEL_KEY in record
    if control and not allow_instrument_controls:
        raise SampleInvalid(
            f"refused INSTRUMENT CONTROL fixture ({record[CONTROL_LABEL_KEY]}): "
            "never native memory evidence"
        )
    exit_code = read_marker(observer_exit)
    if not is_int(record.get("observer_exit_code")) or exit_code != record["observer_exit_code"]:
        raise SampleInvalid("observer.exit differs from the observation's exit code")
    manifest = record.get("manifest")
    if not isinstance(manifest, dict) or "gate.stdout" not in manifest:
        raise SampleInvalid("observation has no file manifest")
    on_disk = build_manifest(root)
    changed = sorted(
        name for name in set(on_disk) | set(manifest) if on_disk.get(name) != manifest.get(name)
    )
    if changed:
        raise SampleInvalid(f"retained files changed after observation: {changed[:10]}")
    if record.get("status") != "VALID":
        raise SampleInvalid(f"sample recorded INVALID: {record.get('invalid_reasons')}")
    returncode = record.get("gate_returncode")
    if not is_int(returncode) or returncode not in (0, 1) or exit_code != returncode:
        raise SampleInvalid(
            f"a valid sample needs gate exit 0 or 1 equal to its observer exit; "
            f"got {returncode!r} and {exit_code}"
        )
    crash = record.get("observer_crash_control")
    crash_marker, gate_marker = list_files(
        "sample markers read",
        [root / "observer-crash.exit", root / "gate.returncode"],
        within=root,
    )
    if (
        not isinstance(crash, dict)
        or crash.get("status") != "PASS"
        or crash.get("returncode") != -SIGKILL
        or read_marker(crash_marker) != -SIGKILL
    ):
        raise SampleInvalid("the observer's deliberate crash row did not record SIGKILL")
    if read_marker(gate_marker) != returncode:
        raise SampleInvalid("gate.returncode differs from the recorded gate exit")
    relative = record.get("gate_run_directory")
    if not isinstance(relative, str):
        raise SampleInvalid("observation names no gate run directory")
    (run_dir,) = list_files("named gate run directory", [root / relative], within=root)
    if run_dir != locate_run_directory(root):
        raise SampleInvalid("observation names a different gate run directory")
    extracted = extract_gate_run(run_dir, returncode)
    check_gate_stdout(root, extracted["run_complete"])
    for key in RECORDED_KEYS:
        if record.get(key) != extracted[key]:
            raise SampleInvalid(f"recorded {key} differs from the retained gate files")
    checks = record.get("gate_module_checks")
    if not control and (not isinstance(checks, dict) or checks.get("status") != "verified"):
        raise SampleInvalid("the original gate module re-check was not verified")
    identity = record.get("identity")
    if not isinstance(identity, dict) or any(key not in identity for key in HARD_IDENTITY_KEYS):
        raise SampleInvalid("observation identity is incomplete")
    for key in BINARY_IDENTITY_KEYS:
        digest = identity.get(key)
        if not isinstance(digest, str) or not SHA256_HEX.fullmatch(digest):
            raise SampleInvalid(f"observation {key} is not a SHA256 hex digest: {digest!r}")
    if identity.get("source_head") != identity.get("expected_head"):
        raise SampleInvalid("observation HEAD differs from its expected head")
    if identity.get("tracked_changes") or identity.get("submodule_heads") != identity.get("gitlinks"):
        raise SampleInvalid("observation source differs from its pinned commits")
    context = record.get("ci_context")
    return {
        "root": root,
        "record": record,
        "control": control,
        "manifest": manifest,
        "context": context if isinstance(context, dict) else {},
    }


def peek_sample(argument: Path) -> tuple:
    """Best-effort repeat id and gate exit of a sample that failed validation."""
    try:
        (root,) = list_files("invalid sample peek", [argument])
        (observation,) = list_files("observation peek", [root / OBSERVATION], within=root)
        record = read_json(observation)
        return record.get("repeat_id"), record.get("gate_returncode")
    except (OSError, ValueError):
        return None, None


def exact_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def plain(value: Fraction):
    return value.numerator if value.denominator == 1 else float(value)


def describe(values: list) -> dict:
    """n, mean, sample variance (n-1), stdev, min, max, median; nothing removed."""
    exact = [Fraction(value) for value in values]
    result = {
        "n": len(exact),
        "values": [plain(value) for value in exact],
        "values_exact": [exact_text(value) for value in exact],
    }
    if not exact:
        return result
    ordered = sorted(exact)
    count = len(exact)
    mean = sum(exact, Fraction(0)) / count
    middle = count // 2
    if count % 2:
        median = ordered[middle]
    else:
        median = (ordered[middle - 1] + ordered[middle]) / 2
    result.update(
        mean=float(mean),
        mean_exact=exact_text(mean),
        median=float(median),
        median_exact=exact_text(median),
        min=plain(ordered[0]),
        max=plain(ordered[-1]),
        range=plain(ordered[-1] - ordered[0]),
    )
    if count >= 2:
        variance = sum(((value - mean) ** 2 for value in exact), Fraction(0)) / (count - 1)
        result.update(
            sample_variance=float(variance),
            sample_variance_exact=exact_text(variance),
            sample_stdev=math.sqrt(float(variance)),
        )
    else:
        result.update(sample_variance=None, sample_variance_exact=None, sample_stdev=None)
    return result


def binomial_upper_bound(failures: int, trials: int, confidence: float = 0.95) -> float:
    """Exact one-sided Clopper-Pearson upper bound, found by bisection."""
    if failures >= trials:
        return 1.0
    alpha = 1.0 - confidence

    def cdf(probability: float) -> float:
        return sum(
            math.comb(trials, k) * probability**k * (1 - probability) ** (trials - k)
            for k in range(failures + 1)
        )

    low, high = failures / trials, 1.0
    for _ in range(200):
        middle = (low + high) / 2
        if cdf(middle) > alpha:
            low = middle
        else:
            high = middle
    return high


def sample_row(sample: dict) -> dict:
    record = sample["record"]
    identity = record["identity"]
    context = sample["context"]
    points = record["trial_points"]
    return {
        "repeat_id": record["repeat_id"],
        "sample_root": str(sample["root"]),
        "instrument_control": sample["control"],
        "gate_returncode": record["gate_returncode"],
        "gate_verdict": record["gate_verdict"],
        "peak_rss_bytes": {str(p["trials"]): p["peak_rss_bytes"] for p in points},
        "gnu_time_kib": {str(p["trials"]): p["gnu_time_kib"] for p in points},
        "trial_rows": {str(p["trials"]): p["trial_rows"] for p in points},
        "delta_rss_bytes": [edge["delta_rss_bytes"] for edge in record["trial_slopes"]],
        "bytes_per_trial": [edge["bytes_per_trial"] for edge in record["trial_slopes"]],
        "edge_verdicts": [edge["verdict"] for edge in record["trial_slopes"]],
        "bar_peak_rss_bytes_informational": {
            str(p["bars"]): p["peak_rss_bytes"] for p in record["bar_points"]
        },
        "hostname": context.get("hostname"),
        "runner_name": context.get("RUNNER_NAME"),
        "run_id": context.get("GITHUB_RUN_ID"),
        "run_attempt": context.get("GITHUB_RUN_ATTEMPT"),
        "kernel_release": identity.get("kernel_release"),
        **{key: identity.get(key) for key in BINARY_IDENTITY_KEYS},
    }


METHOD = (
    "Every raw row is emitted; no outlier removal, negative clipping, imputation or selective rerun.",
    "Per trial point: n, mean, sample variance (n-1), sample stdev, min, max, median; the same for "
    "each sample's raw delta and bytes/trial on the original 990-trial and 99000-trial edges.",
    "Focus: how often the small 990-trial span (10 -> 1000 trials) crosses the original 256 "
    "bytes/trial limit, counted from the raw peaks with the gate's own integer rule. All three "
    "trial counts and the upper 99000-trial edge stay beside it, and a missing or invalid sample "
    "is never counted as a non-crossing one.",
    "Same binary bytes is a hard contract: native, plugin and manifest SHA256 must be equal in "
    "all ten samples, or the collection is INCOMPLETE; there is no soft mode.",
    "Peaks are the gate's own GNU time %M KiB x 1024 for the native child, re-derived from each "
    "retained .time file; Python or GNU time launcher RSS is never substituted.",
    "The fail fraction is the observed red fraction of the unchanged gate on unchanged bytes. With "
    "ten samples, zero failures does not establish a small true false-fail rate; see the exact "
    "one-sided 95% upper bound (about 0.26 for 0 of 10).",
    "Independence is only what independence_scope states.",
    "No threshold, median-of-K or extra-point design is proposed here; any such proposal depends "
    "on this data and is a separate, later decision.",
    "This collection does not accept or reject product memory complexity.",
)


def summarize_roots(roots: list, *, allow_instrument_controls: bool = False) -> tuple:
    """Summarize exactly the named roots. Instrument controls are refused unless
    a caller inside the control suite allows them; such a collection is then
    labelled INSTRUMENT_CONTROL and can never be MEASURED."""
    errors = []
    loaded = []
    invalid = {}
    if len(roots) != REQUIRED_SAMPLES:
        errors.append(
            f"exactly {REQUIRED_SAMPLES} named sample roots are required; got {len(roots)}"
        )
    seen = set()
    for argument in roots:
        try:
            sample = load_sample(Path(argument), allow_instrument_controls)
        except (OSError, ValueError) as error:
            errors.append(f"{argument}: {error}")
            repeat_id, returncode = peek_sample(Path(argument))
            if isinstance(repeat_id, str):
                invalid.setdefault(repeat_id, returncode)
            continue
        if sample["root"] in seen:
            errors.append(f"{argument}: the same sample root was named twice")
            continue
        seen.add(sample["root"])
        loaded.append(sample)
    groups = {}
    for sample in loaded:
        groups.setdefault(str(sample["record"].get("repeat_id")), []).append(sample)
    for repeat_id, group in sorted(groups.items()):
        if repeat_id not in REPEAT_IDS:
            errors.append(f"unknown repeat id {repeat_id!r}")
        if len(group) > 1:
            errors.append(
                f"duplicate repeat id {repeat_id}: {[str(s['root']) for s in group]}"
            )
    missing = [repeat_id for repeat_id in REPEAT_IDS if repeat_id not in groups]
    if missing:
        errors.append(f"no valid sample for repeat ids {missing}")
    owners = {}
    for sample in loaded:
        for name, digest in sample["manifest"].items():
            if name == "gate.stdout" or name.endswith(".result.json"):
                owners.setdefault(digest, set()).add(str(sample["record"].get("repeat_id")))
    copied = sorted({tuple(sorted(ids)) for ids in owners.values() if len(ids) > 1})
    for ids in copied:
        errors.append(
            f"identical retained gate output in samples {list(ids)}: copied data, "
            "not independent observations"
        )
    ordered = sorted(loaded, key=lambda sample: str(sample["record"].get("repeat_id")))
    reference = {}
    if ordered:
        first = ordered[0]["record"]
        reference = {key: first["identity"].get(key) for key in HARD_IDENTITY_KEYS}
        for sample in ordered[1:]:
            identity = sample["record"]["identity"]
            for key in HARD_IDENTITY_KEYS:
                if identity.get(key) != reference[key]:
                    errors.append(
                        f"{sample['record']['repeat_id']}: foreign identity: {key} "
                        f"differs from {first['repeat_id']}"
                    )
        if reference["gate_sha256"] != ORIGINAL_GATE_SHA256:
            errors.append("the collection's gate SHA256 is not the original gate")
        if reference["runner_label"] != ADMITTED_RUNNER_LABEL:
            errors.append("the collection was not declared on the admitted runner label")
    rows = [sample_row(sample) for sample in ordered]
    points = [
        {"trials": trials, **describe([row["peak_rss_bytes"][str(trials)] for row in rows])}
        for trials in COUNTS
    ]
    edges = []
    for index, (left, right) in enumerate(zip(COUNTS, COUNTS[1:])):
        span = right - left
        deltas = [
            row["peak_rss_bytes"][str(right)] - row["peak_rss_bytes"][str(left)]
            for row in rows
        ]
        edges.append(
            {
                "from_trials": left,
                "to_trials": right,
                "trial_span": span,
                "original_limit_bytes_per_trial": ORIGINAL_SLOPE_LIMITS[index],
                "delta_rss_bytes": describe(deltas),
                "bytes_per_trial": describe([Fraction(delta, span) for delta in deltas]),
                "original_edge_fail_count": sum(
                    1 for row in rows if row["edge_verdicts"][index] == "FAIL"
                ),
            }
        )
    vector = []
    for repeat_id in REPEAT_IDS:
        group = groups.get(repeat_id, [])
        if len(group) == 1:
            entry = {"gate_returncode": group[0]["record"]["gate_returncode"], "sample": "VALID"}
        elif len(group) > 1:
            entry = {
                "gate_returncode": [s["record"]["gate_returncode"] for s in group],
                "sample": "DUPLICATE",
            }
        elif repeat_id in invalid:
            entry = {"gate_returncode": invalid[repeat_id], "sample": "INVALID"}
        else:
            entry = {"gate_returncode": None, "sample": "MISSING"}
        vector.append({"repeat_id": repeat_id, **entry})
    fails = sum(1 for row in rows if row["gate_returncode"] == 1)
    hosts = sorted({str(row["hostname"]) for row in rows if row["hostname"]})
    runners = sorted({str(row["runner_name"]) for row in rows if row["runner_name"]})
    independent = len(rows) == len(hosts) == len(runners) == REQUIRED_SAMPLES
    scope = {
        "valid_samples": len(rows),
        "distinct_hostnames": len(hosts),
        "distinct_runner_names": len(runners),
        "ci_run_ids": sorted({str(row["run_id"]) for row in rows if row["run_id"]}),
        "statement": (
            "ten valid samples from ten distinct runner pods (distinct hostnames and "
            "runner names); independence is pod-level only: node, node pool, image "
            "cache, kernel and neighbour load are neither controlled nor established"
            if independent
            else f"NOT ten independent CI pods: {len(hosts)} distinct hostnames and "
            f"{len(runners)} distinct runner names among {len(rows)} valid samples"
        ),
    }
    distinct = {
        key: sorted({str(row[key]) for row in rows}) for key in BINARY_IDENTITY_KEYS
    }
    same_bytes = len(rows) == REQUIRED_SAMPLES and all(
        len(values) == 1 for values in distinct.values()
    )
    small_from, small_to = COUNTS[0], COUNTS[1]
    small_span = small_to - small_from
    small_limit = ORIGINAL_SLOPE_LIMITS[0]
    small_limit_delta = small_limit * small_span
    small_margins = [
        row["peak_rss_bytes"][str(small_to)]
        - row["peak_rss_bytes"][str(small_from)]
        - small_limit_delta
        for row in rows
    ]
    small_crossings = sum(1 for margin in small_margins if margin > 0)
    if small_crossings != edges[0]["original_edge_fail_count"]:
        errors.append("small-span crossing count differs from the original edge verdicts")
    small_focus = {
        "focus": (
            "how often the small 990-trial span (10 -> 1000 trials) crosses the original "
            "256 bytes/trial limit"
        ),
        "from_trials": small_from,
        "to_trials": small_to,
        "trial_span": small_span,
        "original_limit_bytes_per_trial": small_limit,
        "limit_delta_bytes": small_limit_delta,
        "crossing_rule": (
            "a sample crosses when delta_rss_bytes > limit_bytes_per_trial * trial_span, the "
            "original gate's own integer rule; margin_bytes is delta_rss_bytes minus "
            "limit_delta_bytes"
        ),
        "valid_samples": len(rows),
        "crossings": small_crossings,
        "crossing_repeat_ids": [
            row["repeat_id"] for row, margin in zip(rows, small_margins) if margin > 0
        ],
        "crossing_fraction_exact": f"{small_crossings}/{len(rows)}" if rows else None,
        "crossing_fraction": small_crossings / len(rows) if rows else None,
        "one_sided_95_upper_bound_exact_binomial": (
            binomial_upper_bound(small_crossings, len(rows)) if rows else None
        ),
        "margin_bytes": describe(small_margins),
        "upper_edge_original_fail_count": edges[1]["original_edge_fail_count"],
        "not_counted_as_flat": (
            "the fraction is over valid samples only; a missing, invalid or duplicate sample "
            "is never counted as a non-crossing one and makes the collection INCOMPLETE"
        ),
        "other_counts_retained": (
            "the 10, 1000 and 100000 trial peaks and the upper 1000 -> 100000 edge are all "
            "kept in points, edges and raw_rows; this focus replaces none of them"
        ),
    }
    controls = sum(1 for sample in loaded if sample["control"])
    complete = not errors and len(rows) == REQUIRED_SAMPLES
    exit_code = (1 if fails else 0) if complete else 2
    if not complete:
        status = "INCOMPLETE"
    elif controls:
        status = "INSTRUMENT_CONTROL"
    else:
        status = "MEASURED"
    summary = {
        "schema": SUMMARY_SCHEMA,
        "status": status,
        "summary_exit_code": exit_code,
        "status_meaning": "MEASURED means complete valid data; it is never a gate PASS",
        "evidence": (
            "NONE: synthetic INSTRUMENT CONTROL fixtures" if controls else "CI observations"
        ),
        "errors": errors,
        "n_required": REQUIRED_SAMPLES,
        "n_valid": len(rows),
        "named_sample_roots": [str(argument) for argument in roots],
        "gate_exit_vector": vector,
        "original_gate_fail_count": fails,
        "empirical_false_fail_fraction": {
            "original_gate_failures": fails,
            "valid_samples": len(rows),
            "fraction_exact": f"{fails}/{len(rows)}" if rows else None,
            "fraction": fails / len(rows) if rows else None,
            "one_sided_95_upper_bound_exact_binomial": (
                binomial_upper_bound(fails, len(rows)) if rows else None
            ),
        },
        "points": points,
        "edges": edges,
        "small_span_focus": small_focus,
        "raw_rows": rows,
        "independence_scope": scope,
        "binary_identity": {
            "contract": (
                "HARD: native, plugin and manifest SHA256 must be equal in all ten samples "
                "(same binary bytes), like the source, gate and CSV identity; there is no "
                "soft mode"
            ),
            "status": "SAME_BYTES" if same_bytes else "DIFFERS_OR_UNVERIFIED",
            "distinct": distinct,
            "digests": {key: reference.get(key) for key in BINARY_IDENTITY_KEYS},
            "note": (
                "a collection whose digests differ is INCOMPLETE, with one foreign identity "
                "error per differing key and sample; nothing is averaged across differing binaries"
            ),
        },
        "identity_reference": reference,
        "instrument_control_samples": controls,
        "method": list(METHOD),
    }
    return summary, exit_code


def cmd_summarize(args: argparse.Namespace) -> int:
    try:
        (output,) = list_files("summary write", [args.output])
        if output.exists():
            print("ERROR summary output exists; name a fresh path", file=sys.stderr)
            return 2
        summary, exit_code = summarize_roots(list(args.samples))
        with output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(summary, sort_keys=True, indent=1) + "\n")
    except Exception as error:  # No summary file means nothing was measured.
        print(f"ERROR summary not produced: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": summary["status"],
                "summary_exit_code": exit_code,
                "gate_exit_vector": summary["gate_exit_vector"],
                "errors": summary["errors"],
                "summary": str(output),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample", help="run the original gate once")
    sample.add_argument("--repeat-id", required=True, help="r01 .. r10")
    sample.add_argument("--sample-root", type=Path, required=True, help="fresh directory")
    sample.add_argument("--source-root", type=Path, required=True)
    sample.add_argument("--expected-head", required=True)
    sample.add_argument("--runner-label", required=True)
    sample.add_argument("--gate", type=Path, required=True)
    sample.add_argument("--native", type=Path, required=True)
    sample.add_argument("--plugin", type=Path, required=True)
    sample.add_argument("--ohlcv", type=Path, required=True)
    summary = commands.add_parser("summarize", help="summarize ten named samples")
    summary.add_argument(
        "--sample", dest="samples", type=Path, action="append", required=True
    )
    summary.add_argument("--output", type=Path, required=True, help="fresh JSON path")
    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sample":
        return cmd_sample(args)
    return cmd_summarize(args)


if __name__ == "__main__":
    sys.exit(main())
