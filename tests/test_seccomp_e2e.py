"""Native seccomp regression: actual plugin initializers and worker-side syscalls.

The positive controls must run outside an outer jail that already denies these calls.
In particular clone3 must work with the option off; an old kernel/container returning
ENOSYS is a failed prerequisite, never a passing seccomp test. Retain the output directory
for review, like test_jailed_profile_e2e.py. This driver does not build anything.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def checked_path(value):
    path = Path(value).resolve()
    print(f"will follow: {path}", flush=True)
    return path


def run_suite(args):
    native = checked_path(args.native)
    plugin = checked_path(args.plugin)
    helper = checked_path(args.exec_helper) if args.exec_helper else None
    output = checked_path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    directory = checked_path(tempfile.mkdtemp(prefix="seccomp-", dir=output))
    csv = checked_path(directory / "bars.csv")
    csv.write_text(
        "timestamp,open,high,low,close,volume\n"
        "1700000000000,100,102,99,101,10\n"
        "1700000060000,101,103,100,102,11\n",
        encoding="utf-8",
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("PFH_SECCOMP_", "PF_SCRATCH_"))
    }
    rows = []
    metadata = {
        "platform": sys.platform, "machine": platform.machine(),
        "kernel": platform.release(), "driver_libc": platform.libc_ver(),
        "native": str(native), "plugin": str(plugin),
        "requested_operation": args.operation,
        "unavailable_reason": args.unavailable_reason,
    }
    if helper:
        numbers = subprocess.run(
            [str(helper), "syscall-numbers"],
            text=True,
            capture_output=True,
            check=True,
        )
        metadata["header_syscall_numbers"] = json.loads(numbers.stdout)

    def write_evidence(complete):
        (directory / "row-verdicts.json").write_text(
            json.dumps({"complete": complete, **metadata, "rows": rows}, indent=2) + "\n",
            encoding="utf-8",
        )

    def record(name, **observations):
        rows.append({"row": name, "verdict": "pass", **observations})
        write_evidence(False)
        print(f"PASS seccomp {name}", flush=True)

    write_evidence(False)

    def invoke(name, enabled, operation=None, phase="constructor", blocker=None,
               *, sampler="grid", trial_count=4, streaming=True, extra=()):
        home = checked_path(directory / name)
        home.mkdir()
        command = [
            str(native), "run", "--strategy", str(plugin), "--ohlcv", str(csv),
            "--objective", "metrics.all.net_profit", "--sampler", sampler,
            "--workers", "2", "--max-trials", str(trial_count), "--seed", "394",
            "--int-dim", "Length", "1", "4", "1", "--fixed-input", "DelayMs", "10",
            "--input-tf", "1", "--script-tf", "5", "--bar-magnifier", "true",
            "--magnifier-samples", "6", "--magnifier-distribution", "triangle",
            "--output", str(home / "result.json"), *extra,
        ]
        if streaming:
            command.extend(["--trials-file", str(home / "trials.jsonl")])
        if sampler == "tpe":
            command.extend([
                "--real-dim", "Alpha", "0", "1", "continuous",
                "--real-dim", "Beta", "0", "1", "continuous",
                "--tpe-startup-trials", "2", "--tpe-max-threads", "2",
            ])
        if enabled:
            command.append("--seccomp")
        if blocker:
            command = [str(helper), blocker, *command]
        env = environment.copy()
        if enabled:
            # LeakSanitizer needs a process-form clone and ptrace, both denied by seccomp.
            asan_options = env.get("ASAN_OPTIONS", "")
            env["ASAN_OPTIONS"] = (asan_options + ":" if asan_options else "") + "detect_leaks=0"
        reader = writer = None
        if operation:
            reader, writer = os.pipe()
            env.update({
                "PFH_SECCOMP_PROBE": operation,
                "PFH_SECCOMP_PHASE": phase,
                "PFH_SECCOMP_PROBE_FD": str(writer),
                "PFH_SECCOMP_EXEC_HELPER": str(helper),
            })
        print(f"will run: {command}", flush=True)
        (home / "command.json").write_text(json.dumps(command) + "\n", encoding="utf-8")
        try:
            completed = subprocess.run(
                command,
                env=env,
                pass_fds=() if writer is None else (writer,),
                cwd=home,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            if writer is not None:
                os.close(writer)
        trace = ""
        if reader is not None:
            with os.fdopen(reader, encoding="utf-8") as stream:
                trace = stream.read()
        (home / "stdout").write_text(completed.stdout, encoding="utf-8")
        (home / "stderr").write_text(completed.stderr, encoding="utf-8")
        (home / "probes.txt").write_text(trace, encoding="utf-8")
        probes = []
        for line in trace.splitlines():
            op, where, tid, result, error, status = line.split()
            probes.append({
                "operation": op, "phase": where, "tid": int(tid),
                "result": int(result), "errno": int(error), "child_exit": int(status),
            })
        return completed, probes, home

    def normal(completed, home, trial_count=4):
        require(completed.returncode == 0, (completed.returncode, completed.stderr, home))
        document = json.loads(completed.stdout)
        require(document["ok"] is True and len(document["trials"]) == trial_count, document)
        require(all(trial["status"] == "ok" for trial in document["trials"]), document)
        result = (home / "result.json").read_bytes()
        require(json.loads(result) == document, "published result differs from stdout")
        return result

    def refusal(completed, probes, home, reason):
        require(completed.returncode == 1, (completed.returncode, completed.stderr, home))
        document = json.loads(completed.stdout)
        require(document["ok"] is False, document)
        require(document["failure"] == {
            "origin": "hpo", "code": "hpo_seccomp_unavailable",
            "args": {"reason": reason}, "exit_code": 1,
        }, completed.stdout)
        require(probes == [], "plugin initializer ran after a refused seccomp installation")
        require(not (home / "result.json").exists(), "refusal published a result")
        require(not (home / "trials.jsonl").exists(), "refusal started the trial writer")

    if helper is None:
        # Unsupported platforms, compiler ABIs and headers still exercise a typed refusal.
        off, _, home = invoke("unsupported-off", False)
        normal(off, home)
        on, probes, home = invoke("unsupported-on", True)
        reason = args.unavailable_reason or (
            "platform" if sys.platform != "linux" else "architecture"
        )
        refusal(on, probes, home, reason)
        record(f"unavailable-{reason}", reason=reason)
        print(f"SKIP: Linux syscall probes unavailable ({reason}); typed refusal checked",
              flush=True)
        write_evidence(True)
        return

    for name, settings in (
        ("two-workers-and-writer-byte-parity", {}),
        ("wall-cap-watchdog-byte-parity", {
            "streaming": False,
            "extra": ("--max-wall-seconds", "60", "--wall-grace-seconds", "0"),
        }),
        ("tpe-study-and-writer-byte-parity", {"sampler": "tpe", "trial_count": 8}),
    ):
        trial_count = settings.get("trial_count", 4)
        off, off_probes, off_home = invoke(name + "-off", False, "threads", "all", **settings)
        off_bytes = normal(off, off_home, trial_count)
        on, on_probes, on_home = invoke(name + "-on", True, "threads", "all", **settings)
        on_bytes = normal(on, on_home, trial_count)
        # Timings are a separate --scheduler-stats sidecar; compare original result bytes.
        require(off_bytes == on_bytes, f"{name}: seccomp changed result.json bytes")
        if settings.get("streaming", True):
            require((off_home / "trials.jsonl").read_bytes()
                    == (on_home / "trials.jsonl").read_bytes(),
                    f"{name}: seccomp changed trial stream bytes")
        else:
            require(not (off_home / "trials.jsonl").exists()
                    and not (on_home / "trials.jsonl").exists(),
                    "wall-cap-only check unexpectedly started a trial stream")
        for probes in (off_probes, on_probes):
            constructors = [probe for probe in probes if probe["phase"] == "constructor"]
            workers = [probe for probe in probes if probe["phase"] == "worker"]
            require(len(constructors) == 1 and len(workers) == trial_count, probes)
            require(all(probe["operation"] == "threads" and probe["result"] > 0
                        and probe["result"] != probe["tid"]
                        and probe["errno"] == 0 and probe["child_exit"] == 0 for probe in probes),
                    probes)
            require(len({probe["tid"] for probe in workers}) == 2, probes)
            require(all(probe["tid"] != constructors[0]["tid"] for probe in workers), probes)
        record(name, off=off_probes, on=on_probes)

    operations = [
        "fork", "vfork", "execve", "execveat", "clone", "clone_high_flags", "clone3",
        "process_vm_readv", "process_vm_writev",
    ]
    if platform.machine().lower() in ("x86_64", "amd64"):
        operations.append("raw_fork")
    if args.operation:
        require(args.operation in operations, f"unsupported probe: {args.operation}")
        operations = [args.operation]
    for phase in ("constructor", "worker"):
        for operation in operations:
            name = f"{phase}-{operation}"
            off, off_probes, home = invoke(name + "-off", False, operation, phase)
            execution = operation in ("execve", "execveat")
            if execution:
                # 42 is emitted by the real exec-success helper, which also writes a probe.
                require(off.returncode == 42, (off.returncode, off.stderr, home))
                require(not (home / "result.json").exists(), "exec did not replace the runner")
            else:
                normal(off, home)
            expected_count = 1 if execution or phase == "constructor" else 4
            require(len(off_probes) == expected_count, off_probes)
            for probe in off_probes:
                require(probe["operation"] == operation and probe["phase"] == phase, probe)
                require(probe["errno"] == 0, (
                    "positive control failed; run outside any preexisting process/clone3 filter",
                    probe, home,
                ))
                if execution:
                    require(probe["result"] == 0 and probe["child_exit"] == 42, probe)
                elif operation.startswith("process_vm_"):
                    require(probe["result"] == 1, probe)
                else:
                    require(probe["result"] > 0 and probe["child_exit"] == 42, probe)
            if phase == "worker" and not execution:
                require(len({probe["tid"] for probe in off_probes}) == 2, off_probes)

            on, on_probes, home = invoke(name + "-on", True, operation, phase)
            normal(on, home)
            require(len(on_probes) == (1 if phase == "constructor" else 4), on_probes)
            for probe in on_probes:
                require(probe["operation"] == operation and probe["phase"] == phase, probe)
                require(probe["result"] == -1 and probe["child_exit"] == 0, probe)
                require(probe["errno"] == (errno.ENOSYS if operation == "clone3" else errno.EPERM),
                        probe)
            if phase == "worker":
                require(len({probe["tid"] for probe in on_probes}) == 2, on_probes)
            record(name, off=off_probes, on=on_probes)

    if args.operation:
        write_evidence(True)
        return

    # TRACEME succeeds for the single main thread with this Python process as its tracer.
    # There is no exec or signal delivery between TRACEME and the native runner's normal exit.
    off, off_probes, home = invoke("ptrace-off", False, "ptrace")
    normal(off, home)
    require(len(off_probes) == 1 and off_probes[0]["operation"] == "ptrace"
            and off_probes[0]["phase"] == "constructor", off_probes)
    require(off_probes[0]["result"] == 0 and off_probes[0]["errno"] == 0, off_probes)
    on, on_probes, home = invoke("ptrace-on", True, "ptrace")
    normal(on, home)
    require(len(on_probes) == 1 and on_probes[0]["operation"] == "ptrace"
            and on_probes[0]["phase"] == "constructor", on_probes)
    require(on_probes[0]["result"] == -1 and on_probes[0]["errno"] == errno.EPERM, on_probes)
    record("ptrace", off=off_probes, on=on_probes)

    blockers = [
        ("block-no-new-privs", "no_new_privs"),
        ("block-filter", "filter"),
        ("block-kernel", "kernel"),
    ]
    for blocker, reason in blockers:
        off, probes, home = invoke(blocker + "-off", False, "threads", blocker=blocker)
        normal(off, home)
        require(len(probes) == 1, probes)
        on, probes, home = invoke(blocker + "-on", True, "threads", blocker=blocker)
        refusal(on, probes, home, reason)
        record(blocker, reason=reason)

    if platform.machine().lower() in ("x86_64", "amd64"):
        for operation in ("x32", "compat"):
            off, off_probes, home = invoke(operation + "-off", False, operation)
            normal(off, home)
            require(len(off_probes) == 1, off_probes)
            if operation == "compat":
                require(off_probes[0]["result"] == -1
                        and off_probes[0]["errno"] in (errno.EBADF, errno.ENOSYS), off_probes)
            else:
                # x32 may be enabled (a successful getpid) or absent (ENOSYS).
                require((off_probes[0]["result"] > 0 and off_probes[0]["errno"] == 0)
                        or (off_probes[0]["result"] == -1
                            and off_probes[0]["errno"] == errno.ENOSYS), off_probes)
            on, on_probes, home = invoke(operation + "-on", True, operation)
            require(on.returncode == -signal.SIGSYS, (on.returncode, on.stderr, home))
            require(on_probes == [], on_probes)
            require(not (home / "result.json").exists(), "ABI violation published a result")
            record(operation + "-abi-killed", off=off_probes, signal=signal.SIGSYS)
    write_evidence(True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--exec-helper", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--unavailable-reason", choices=("platform", "architecture", "headers"))
    parser.add_argument("--operation", help="run one syscall's two phases for mutation review")
    run_suite(parser.parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
