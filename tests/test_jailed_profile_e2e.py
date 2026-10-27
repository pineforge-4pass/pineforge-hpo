"""Real engine jailed studies; called by the existing Linux symbol-feed CI gate."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CHUNK = 1024 * 1024
CRASH = "deliberate jailed-profile crash"

# Only this child creates/rebinds fd 5. The test process never operates on fd 5.
# exec preserves the work's status, including the deliberate crash control.
LAUNCHER = """import errno
import fcntl
import os
from pathlib import Path
import sys

if sys.platform != "linux":
    raise RuntimeError("sealed-memfd launcher requires Linux")
for value in sys.argv[3:]:
    path = Path(value)
    print("will follow command argument: " + value, file=sys.stderr, flush=True)
    candidates = [path]
    for candidate in candidates:
        parts = candidate.parts
        if (
            any(part in {".ssh", ".aws", ".codex"} for part in parts)
            or any(part.endswith(".env") for part in parts)
            or (".config", "pineforge") in tuple(zip(parts, parts[1:]))
            or (".config", "zsh", "keys") in tuple(zip(parts, parts[1:], parts[2:]))
        ):
            raise RuntimeError("refused sensitive path by name: " + str(candidate))
        if len(candidates) == 1:
            candidates.append(path.resolve())
mode, size = sys.argv[1], int(sys.argv[2])
if mode == "closed":
    try:
        os.close(5)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
else:
    arena = os.memfd_create("hpo-jailed-e2e", os.MFD_ALLOW_SEALING)
    os.ftruncate(arena, size)
    seals = fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
    if mode == "write-sealed":
        seals |= fcntl.F_SEAL_WRITE
    fcntl.fcntl(arena, fcntl.F_ADD_SEALS, seals)
    os.dup2(arena, 5, inheritable=True)
    os.set_inheritable(5, True)
    if arena != 5:
        os.close(arena)
print("will exec: " + sys.argv[3], file=sys.stderr, flush=True)
os.execv(sys.argv[3], sys.argv[3:])
"""


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def checked_path(value, *, stream=None):
    """List every explicit input/output before following it; refuse sensitive names."""
    path = Path(value)
    print(f"will follow: {path}", file=stream, flush=True)

    def refuse_named(candidate):
        parts = candidate.parts
        if (
            any(part in {".ssh", ".aws", ".codex"} for part in parts)
            or any(part.endswith(".env") for part in parts)
            or (".config", "pineforge") in tuple(zip(parts, parts[1:]))
            or (".config", "zsh", "keys") in tuple(zip(parts, parts[1:], parts[2:]))
        ):
            raise RuntimeError(f"refused sensitive path by name: {candidate}")

    refuse_named(path)
    resolved = path.resolve()
    refuse_named(resolved)
    if resolved != path:
        print(f"will follow resolved path: {resolved}", file=stream, flush=True)
    return resolved


def read_json(path):
    return json.loads(checked_path(path).read_text(encoding="utf-8"))


def fixture_arena_bytes(bars, workers):
    """A generous reservation for this finite, one-ID, no-partial-exit fixture.

    Use the engine's P0 slab arithmetic (scratch-tables.md), 24 bytes per curve
    point and 30 + len("Long") per possible position-tail row (arena-bound.md).
    Even charge the table slab here although bounded tables live outside the
    arena. Apply the engine's 14-chunk rounding/three command-store formula to
    each worker, then sum. This test budget is not a certified general bound.
    """
    page = os.sysconf("SC_PAGESIZE")

    def rounded(value, unit):
        return ((value + unit - 1) // unit) * unit

    slab = sum(
        rounded(region, page)
        for region in (
            40 * 8192 + 3145728,
            16 * 16384,
            40 * 4096 + 2101248,
            16 * 8192,
            1 + 96 * 4096 + 1048576,
            1 + 60 * 27 + 6912,
            32 * bars,
        )
    )
    payload = slab + 24 * (bars + 1) + (30 + len("Long")) * bars
    chunks = rounded(payload, CHUNK) // CHUNK + 14
    command_chunks = max(1, (chunks - 3) // 13)
    return workers * (chunks + 3 * command_chunks) * CHUNK


def run_suite(args):
    import fcntl

    native = checked_path(args.native)
    probe = checked_path(args.probe)
    harness = checked_path(args.harness)
    checked_path(harness.parent / "pf_sink.py")
    checked_path(ROOT / "tests/test_symbol_feeds_e2e.py")
    study = checked_path(args.study)
    script = checked_path(__file__)
    interpreter = checked_path(sys.executable)
    checked_path(ROOT / "python/pineforge_hpo/cli.py")
    parent = checked_path(args.output)
    parent.mkdir(parents=True, exist_ok=True)
    directory = checked_path(tempfile.mkdtemp(prefix="jailed-", dir=parent))
    document = read_json(study)
    strategy = document["strategies"][0]
    require(
        "source" not in strategy, "jailed study must reuse the real compiled artifact"
    )
    plugin = checked_path(strategy["artifact"])
    checked_path(plugin.parent / "manifest.json")
    checked_path(plugin.parent / "provenance.json")
    chart = checked_path(document["datasets"][0]["ohlcv"])
    index = checked_path(document["symbol_feeds"])
    for symbol in read_json(index)["symbols"].values():
        for value in symbol.get("feeds", {}).values():
            checked_path(index.parent / value)
    bars = len(chart.read_text(encoding="utf-8").splitlines()) - 1
    workers = document["execution"]["workers"]
    require(workers == 2, "jailed fixture must exercise two native worker threads")
    require(document["sampler"]["trials"] == 3, "jailed fixture must run three trials")
    size = fixture_arena_bytes(bars, workers)
    launcher = checked_path(directory / "launch.py")
    launcher.write_text(LAUNCHER, encoding="utf-8")
    evidence_path = checked_path(directory / "row-verdicts.json")
    rows = []
    environment = os.environ.copy()
    for name in (
        "PF_SCRATCH_ARENA_FD",
        "PF_SCRATCH_ARENA_BYTES",
        "PF_SCRATCH_ARENA_JAILED",
        "PF_SCRATCH_WINDOW_BYTES",
        "PINEFORGE_COMMAND_SCRATCH_DIR",
        "PINEFORGE_COMMAND_SCRATCH_MAX_BYTES",
        "TMPDIR",
    ):
        environment.pop(name, None)
    environment["PYTHONPATH"] = str(ROOT / "python")
    missing_tmp = checked_path(directory / "must-not-exist")
    jailed_environment = {
        **environment,
        "PF_SCRATCH_ARENA_FD": "5",
        "PF_SCRATCH_ARENA_BYTES": str(size),
        "PF_SCRATCH_WINDOW_BYTES": str(4 * CHUNK),
        "TMPDIR": str(missing_tmp),
    }

    def write_evidence(complete):
        evidence_path.write_text(
            json.dumps(
                {
                    "complete": complete,
                    "bars": bars,
                    "workers": workers,
                    "arena_bytes": size,
                    "rows": rows,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def record(name, **observations):
        rows.append({"row": name, "verdict": "pass", **observations})
        write_evidence(False)
        print(f"PASS jailed-profile {name}", flush=True)

    def invoke(name, command, env, mode="sealed", pass_fds=()):
        print(f"will run: {command}", flush=True)
        completed = subprocess.run(
            [str(interpreter), "-I", str(launcher), mode, str(size), *command],
            env=env,
            pass_fds=pass_fds,
            text=True,
            capture_output=True,
            check=False,
        )
        checked_path(directory / f"{name}.stdout").write_text(
            completed.stdout, encoding="utf-8"
        )
        checked_path(directory / f"{name}.stderr").write_text(
            completed.stderr, encoding="utf-8"
        )
        return completed

    def cli_command(name, *extra):
        return [
            str(interpreter),
            "-m",
            "pineforge_hpo.cli",
            "run",
            str(study),
            "--native",
            str(native),
            "--output",
            str(checked_path(directory / f"{name}.json")),
            "--trials-file",
            str(checked_path(directory / f"{name}.jsonl")),
            *extra,
        ]

    crash = invoke(
        "deliberate-crash",
        [str(interpreter), "-I", str(script), "--deliberate-crash"],
        environment,
    )
    require(crash.returncode == 1 and CRASH in crash.stderr, crash.stderr)
    record("deliberate-crash", work_exit=crash.returncode, exception=CRASH)
    baseline = invoke("baseline", cli_command("baseline"), environment, "closed")
    require(baseline.returncode == 0, baseline.stderr)
    expected = read_json(directory / "baseline.json")
    require(expected["ok"] is True and len(expected["trials"]) == 3, expected)
    require(all(trial["status"] == "ok" for trial in expected["trials"]), expected)
    require(all(trial["total_trades"] > 0 for trial in expected["trials"]), expected)
    record("live-unjailed-baseline", trials=expected["trials"])
    inherited = invoke("unset-with-fd", cli_command("unset-with-fd"), environment)
    require(inherited.returncode == 0, inherited.stderr)
    require(
        read_json(directory / "unset-with-fd.json")["trials"] == expected["trials"],
        "an inherited fd changed an unjailed study",
    )
    require(
        checked_path(directory / "unset-with-fd.jsonl").read_bytes()
        == checked_path(directory / "baseline.jsonl").read_bytes(),
        "unjailed trial bytes changed with an inherited fd",
    )
    record("no-environment-no-change")
    require(
        not missing_tmp.exists() and directory.is_dir(), "invalid jail test directories"
    )
    # Use an ordinary progress descriptor distinct from the reserved slot.
    reader, writer = os.pipe()
    progress = fcntl.fcntl(writer, fcntl.F_DUPFD_CLOEXEC, 6)
    os.close(writer)
    try:
        jailed = invoke(
            "jailed",
            cli_command("jailed", "--progress-fd", str(progress)),
            jailed_environment,
            pass_fds=(progress,),
        )
    finally:
        os.close(progress)
    with os.fdopen(reader, encoding="utf-8") as stream:
        events = [json.loads(line) for line in stream if line.strip()]
    require(jailed.returncode == 0, jailed.stderr)
    actual = read_json(directory / "jailed.json")
    require(actual["ok"] is True and actual["trials"] == expected["trials"], actual)
    require(
        checked_path(directory / "jailed.jsonl").read_bytes()
        == checked_path(directory / "baseline.jsonl").read_bytes(),
        "jailed trial JSONL bytes differ from the live unjailed row",
    )
    require(events == actual["trials"], f"lost or changed progress records: {events}")
    require(not missing_tmp.exists(), "the engine fallback directory became usable")
    record("jailed-study-and-progress", trials=actual["trials"], progress_events=events)
    direction = document["objective"]["direction"]
    require(direction in ("maximize", "minimize"), f"objective direction: {direction!r}")

    def rank_best_k(trials, k):
        eligible = [
            trial
            for trial in trials
            if trial["status"] == "ok"
            and trial["feasible"]
            and trial["objective"] is not None
            and math.isfinite(trial["objective"])
        ]
        ranked = sorted(
            eligible,
            key=lambda trial: (
                -trial["objective"] if direction == "maximize" else trial["objective"],
                trial["trial_id"],
            ),
        )
        # Native selects by objective, then emits retained winners in trial-ID order.
        return ranked, sorted(ranked[:k], key=lambda trial: trial["trial_id"])

    def require_best_k_progress(name, result, progress_events, k):
        require(
            len(progress_events) == len(expected["trials"]) == 3,
            {
                "run": name,
                "progress_count": len(progress_events),
                "baseline_count": len(expected["trials"]),
                "progress_events": progress_events,
            },
        )
        require(
            progress_events == expected["trials"],
            {
                "run": name,
                "progress_events": progress_events,
                "baseline_trials": expected["trials"],
            },
        )
        ranked, progress_best_k = rank_best_k(progress_events, k)
        _, baseline_best_k = rank_best_k(expected["trials"], k)
        require(
            result["trials_out"] == "best-k" and result["best_k"] == k,
            {"run": name, "requested_k": k, "result": result},
        )
        require(
            len(result["trials"]) == k < len(progress_events),
            {
                "run": name,
                "requested_k": k,
                "retained_count": len(result["trials"]),
                "progress_count": len(progress_events),
            },
        )
        require(
            result["trials"] == progress_best_k == result["summary"]["best_k"],
            {
                "run": name,
                "progress_best_k": progress_best_k,
                "trials": result["trials"],
                "summary_best_k": result["summary"]["best_k"],
            },
        )
        require(
            progress_best_k == baseline_best_k,
            {
                "run": name,
                "progress_best_k": progress_best_k,
                "baseline_best_k": baseline_best_k,
            },
        )
        discarded = ranked[k:]
        require(
            len(discarded) == len(progress_events) - k,
            {"run": name, "discarded": discarded, "progress_events": progress_events, "k": k},
        )
        for rank, trial in enumerate(discarded, start=k + 1):
            trial_id = trial["trial_id"]
            require(
                trial in progress_events
                and all(row["trial_id"] != trial_id for row in result["trials"])
                and all(row["trial_id"] != trial_id for row in result["summary"]["best_k"]),
                {
                    "run": name,
                    "discarded_rank": rank,
                    "discarded_trial_id": trial_id,
                    "progress_events": progress_events,
                    "trials": result["trials"],
                    "summary_best_k": result["summary"]["best_k"],
                },
            )

    eligible, expected_best_k = rank_best_k(expected["trials"], 2)
    eligible_trial_ids = [trial["trial_id"] for trial in eligible]
    require(
        len(eligible) > 2,
        {
            "required_more_than": 2,
            "eligible_count": len(eligible),
            "eligible_trial_ids": eligible_trial_ids,
        },
    )
    require(
        len(expected_best_k) == 2,
        {"direction": direction, "baseline_trials": expected["trials"]},
    )
    baseline_trial_bytes = checked_path(directory / "baseline.jsonl").read_bytes()
    best_k_results = {}
    best_k_trial_bytes = {}
    best_k_progress_events = {}
    # Both the unjailed reference and the jailed comparison request the same K=2.
    for name, env, mode in (
        ("unjailed-best-k", environment, "closed"),
        ("jailed-best-k", jailed_environment, "sealed"),
    ):
        reader, writer = os.pipe()
        progress = fcntl.fcntl(writer, fcntl.F_DUPFD_CLOEXEC, 6)
        os.close(writer)
        try:
            result = invoke(
                name,
                cli_command(
                    name,
                    "--trials-out",
                    "best-k",
                    "--best-k",
                    "2",
                    "--progress-fd",
                    str(progress),
                ),
                env,
                mode,
                pass_fds=(progress,),
            )
        finally:
            os.close(progress)
        with os.fdopen(reader, encoding="utf-8") as stream:
            progress_events = [json.loads(line) for line in stream if line.strip()]
        require(
            result.returncode == 0,
            f"{name}: exit {result.returncode}, stdout={result.stdout!r}, "
            f"stderr={result.stderr!r}",
        )
        retained = read_json(directory / f"{name}.json")
        require_best_k_progress(name, retained, progress_events, 2)
        require(retained["trials_out"] == "best-k", retained)
        require(len(retained["trials"]) == 2, retained)
        require(
            retained["trials"] == expected_best_k,
            {"run": name, "actual": retained["trials"], "expected": expected_best_k},
        )
        summary = retained["summary"]
        require(
            sum(summary["counts_by_status"].values()) == len(expected["trials"]),
            {"run": name, "summary": summary, "baseline_trials": len(expected["trials"])},
        )
        require(
            summary["best_k"] == expected_best_k,
            {"run": name, "actual": summary["best_k"], "expected": expected_best_k},
        )
        trial_bytes = checked_path(directory / f"{name}.jsonl").read_bytes()
        require(
            trial_bytes == baseline_trial_bytes,
            f"{name}: trial bytes={trial_bytes!r}, baseline={baseline_trial_bytes!r}",
        )
        best_k_results[name] = retained
        best_k_trial_bytes[name] = trial_bytes
        best_k_progress_events[name] = progress_events
    unjailed_best_k = best_k_results["unjailed-best-k"]
    jailed_best_k = best_k_results["jailed-best-k"]
    for field in ("trials", "trials_out", "best_k", "summary"):
        require(
            jailed_best_k[field] == unjailed_best_k[field],
            {
                "field": field,
                "jailed": jailed_best_k[field],
                "unjailed": unjailed_best_k[field],
            },
        )
    require(
        best_k_trial_bytes["jailed-best-k"] == best_k_trial_bytes["unjailed-best-k"],
        f"jailed trial bytes={best_k_trial_bytes['jailed-best-k']!r}, "
        f"unjailed={best_k_trial_bytes['unjailed-best-k']!r}",
    )
    # A jailed K=1 control must retain fewer rows than the same fixture with K=2.
    reader, writer = os.pipe()
    progress = fcntl.fcntl(writer, fcntl.F_DUPFD_CLOEXEC, 6)
    os.close(writer)
    try:
        wrong_k = invoke(
            "jailed-best-k-wrong-k",
            cli_command(
                "jailed-best-k-wrong-k",
                "--trials-out",
                "best-k",
                "--best-k",
                "1",
                "--progress-fd",
                str(progress),
            ),
            jailed_environment,
            pass_fds=(progress,),
        )
    finally:
        os.close(progress)
    with os.fdopen(reader, encoding="utf-8") as stream:
        wrong_k_events = [json.loads(line) for line in stream if line.strip()]
    require(
        wrong_k.returncode == 0,
        f"wrong-k: exit {wrong_k.returncode}, stdout={wrong_k.stdout!r}, "
        f"stderr={wrong_k.stderr!r}",
    )
    wrong_k_document = read_json(directory / "jailed-best-k-wrong-k.json")
    require_best_k_progress("jailed-best-k-wrong-k", wrong_k_document, wrong_k_events, 1)
    require(
        wrong_k_document["trials"] != jailed_best_k["trials"]
        and len(wrong_k_document["trials"]) < len(jailed_best_k["trials"]),
        {"wrong_k_trials": wrong_k_document["trials"], "k_2_trials": jailed_best_k["trials"]},
    )
    record(
        "jailed-best-k-equals-unjailed",
        trials=jailed_best_k["trials"],
        summary=jailed_best_k["summary"],
        eligible_trial_ids=eligible_trial_ids,
        progress_events=best_k_progress_events,
        wrong_k_work_exit=wrong_k.returncode,
        wrong_k_trials=wrong_k_document["trials"],
        wrong_k_summary=wrong_k_document["summary"],
        wrong_k_progress_events=wrong_k_events,
    )
    reader, writer = os.pipe()
    progress = fcntl.fcntl(writer, fcntl.F_DUPFD_CLOEXEC, 6)
    os.close(writer)
    try:
        stdout_only = invoke(
            "jailed-best-k-no-writable-dir",
            [
                str(interpreter),
                "-m",
                "pineforge_hpo.cli",
                "run",
                str(study),
                "--native",
                str(native),
                "--trials-out",
                "best-k",
                "--best-k",
                "2",
                "--progress-fd",
                str(progress),
            ],
            jailed_environment,
            pass_fds=(progress,),
        )
    finally:
        os.close(progress)
    with os.fdopen(reader, encoding="utf-8") as stream:
        stdout_events = [json.loads(line) for line in stream if line.strip()]
    require(
        stdout_only.returncode == 0,
        f"stdout-only: exit {stdout_only.returncode}, stdout={stdout_only.stdout!r}, "
        f"stderr={stdout_only.stderr!r}",
    )
    stdout_document = json.loads(stdout_only.stdout)
    require_best_k_progress("jailed-best-k-no-writable-dir", stdout_document, stdout_events, 2)
    for field in ("trials", "trials_out", "best_k", "summary"):
        require(
            stdout_document[field] == unjailed_best_k[field],
            {
                "field": field,
                "stdout_only": stdout_document[field],
                "unjailed": unjailed_best_k[field],
            },
        )
    require(
        not missing_tmp.exists(),
        f"fallback directory {missing_tmp}: exists={missing_tmp.exists()}",
    )
    record(
        "jailed-best-k-no-writable-dir",
        work_exit=stdout_only.returncode,
        trials=stdout_document["trials"],
        summary=stdout_document["summary"],
        missing_tmp_exists=missing_tmp.exists(),
        eligible_trial_ids=eligible_trial_ids,
        progress_events=stdout_events,
    )
    candidate = str(expected["trials"][0]["parameters"]["ETH threshold"])
    for name, env in (
        ("unjailed-probe", environment),
        ("jailed-probe", jailed_environment),
    ):
        output = checked_path(directory / name)
        command = [
            str(probe),
            str(plugin),
            str(chart),
            str(index),
            candidate,
            str(output),
            "--arena-stats",
        ]
        result = invoke(name, command, env)
        require(result.returncode == 0 and int(result.stdout) > 0, result.stderr)
        stats = read_json(output / "arena-stats.json")
        require(stats["before_high_water"] == stats["before_bytes"] == 0, stats)
        require(stats["preflight_high_water"] == 0, stats)
        if name == "jailed-probe":
            require(stats["preflight_bytes"] == stats["after_bytes"] == size, stats)
            require(0 < stats["after_high_water"] <= size, stats)
            require(stats["after_high_water"] % CHUNK == 0, stats)
        else:
            require(stats["preflight_bytes"] == stats["after_bytes"] == 0, stats)
            require(stats["after_high_water"] == 0, stats)
        record(name, **stats)
    reference_metrics = checked_path(
        directory / "unjailed-probe/metrics.bin"
    ).read_bytes()
    observed_metrics = checked_path(directory / "jailed-probe/metrics.bin").read_bytes()
    require(
        bool(reference_metrics) and observed_metrics == reference_metrics,
        "native jailed/unjailed C-ABI metric bytes differ or are empty",
    )
    # Use the release harness's ABI fields and the existing gate's padding rule.
    from test_symbol_feeds_e2e import canonical_abi_bytes

    sys.path.insert(0, str(harness.parent))
    module = importlib.util.spec_from_file_location("jailed_release_abi", harness)
    abi = importlib.util.module_from_spec(module)
    module.loader.exec_module(abi)
    reference = checked_path(directory / "unjailed-probe/trades.bin").read_bytes()
    observed = checked_path(directory / "jailed-probe/trades.bin").read_bytes()
    require(bool(reference), "real native probe produced an empty trade file")
    require(
        canonical_abi_bytes(observed, abi.TradeC)
        == canonical_abi_bytes(reference, abi.TradeC),
        "native jailed/unjailed C-ABI trade fields differ",
    )
    require(not missing_tmp.exists(), "the engine fallback directory became usable")
    record("real-arena-used", proof="positive native high-water after a real trial")
    # Dropping fd 5 is tested directly at native startup: an absent parent fd
    # cannot be put in Python pass_fds without failing before the native exec.
    sys.path.insert(0, str(ROOT / "python"))
    from pineforge_hpo.cli import prepare_run

    native_command, _ = prepare_run(
        study, ROOT / "external/pineforge-engine", directory / "cache", native=native
    )
    for name, mode in (
        ("write-sealed", "write-sealed"),
        ("missing-fd", "closed"),
        ("missing-bytes", "sealed"),
        ("empty-fd-value", "sealed"),
    ):
        env = jailed_environment.copy()
        if name == "missing-bytes":
            env.pop("PF_SCRATCH_ARENA_BYTES")
        if name == "empty-fd-value":
            env["PF_SCRATCH_ARENA_FD"] = ""
        if name == "missing-fd":
            command = [
                *native_command,
                "--output",
                str(checked_path(directory / f"{name}.json")),
                "--trials-file",
                str(checked_path(directory / f"{name}.jsonl")),
            ]
        else:
            command = cli_command(name)
        result = invoke(name, command, env, mode)
        require(result.returncode == 1, f"{name}: {result.returncode}: {result.stderr}")
        failure = json.loads(result.stdout)
        require(failure["ok"] is False, failure)
        require(
            failure["failure"]
            == {
                "origin": "engine",
                "code": "report_sink_failed",
                "args": {"reason": "arena_backing"},
                "exit_code": 1,
            },
            failure,
        )
        require(not checked_path(directory / f"{name}.json").exists(), name)
        require(not checked_path(directory / f"{name}.jsonl").exists(), name)
        record(name, failure=failure["failure"], result_published=False)
    # Exercise prepare_run's native argv separately from the CLI best-k rows above.
    require(
        not any(
            prep_flag in native_command
            for prep_flag in (
                "--trials-out",
                "--best-k",
                "--output",
                "--trials-file",
                "--progress-fd",
            )
        ),
        f"prepare_run supplied retention or output flags: {native_command}",
    )
    # The shared ranker uses truthiness; preserve strict feasible booleans here.
    require(
        all(prep_trial["feasible"] is True for prep_trial in eligible),
        f"prepare_run eligible trials must be explicitly feasible: {eligible}",
    )
    # Compare jailed and unjailed best-k runs at the same k=2; k=1 is only a control.
    prep_best_k_command = [*native_command, "--trials-out", "best-k", "--best-k", "2"]
    # These three-record streams fit in the pipe; read only after invoke returns.
    prep_reader, prep_writer = os.pipe()
    prep_progress = fcntl.fcntl(prep_writer, fcntl.F_DUPFD_CLOEXEC, 6)
    os.close(prep_writer)
    try:
        prep_unjailed = invoke(
            "prepare-best-k-unjailed",
            [*prep_best_k_command, "--progress-fd", str(prep_progress)],
            environment,
            "closed",
            pass_fds=(prep_progress,),
        )
    finally:
        os.close(prep_progress)
    with os.fdopen(prep_reader, encoding="utf-8") as prep_stream:
        prep_unjailed_events = [
            json.loads(prep_line) for prep_line in prep_stream if prep_line.strip()
        ]
    require(prep_unjailed.returncode == 0, prep_unjailed.stderr)
    prep_unjailed_result = json.loads(prep_unjailed.stdout)
    require(prep_unjailed_result["ok"] is True, prep_unjailed_result)
    require(prep_unjailed_result["trials_out"] == "best-k", prep_unjailed_result)
    require(prep_unjailed_result["best_k"] == 2, prep_unjailed_result)
    require(len(prep_unjailed_result["trials"]) == 2, prep_unjailed_result)
    require(prep_unjailed_result["trials"] == expected_best_k, prep_unjailed_result)
    require(
        sum(prep_unjailed_result["summary"]["counts_by_status"].values())
        == len(expected["trials"]),
        prep_unjailed_result["summary"],
    )
    require(
        prep_unjailed_result["summary"]["best_k"] == expected_best_k,
        prep_unjailed_result,
    )
    require_best_k_progress(
        "prepare-best-k-unjailed", prep_unjailed_result, prep_unjailed_events, 2
    )
    # Pre-create invoke's captures so the directory check measures native residue.
    for prep_name in ("prepare-best-k-jailed", "prepare-best-k-one-jailed"):
        for prep_suffix in ("stdout", "stderr"):
            checked_path(directory / f"{prep_name}.{prep_suffix}").touch()
    prep_before = sorted(prep_path.name for prep_path in directory.iterdir())
    # Best-k opens no trial spool.
    prep_reader, prep_writer = os.pipe()
    prep_progress = fcntl.fcntl(prep_writer, fcntl.F_DUPFD_CLOEXEC, 6)
    os.close(prep_writer)
    try:
        prep_jailed = invoke(
            "prepare-best-k-jailed",
            [*prep_best_k_command, "--progress-fd", str(prep_progress)],
            jailed_environment,
            pass_fds=(prep_progress,),
        )
    finally:
        os.close(prep_progress)
    with os.fdopen(prep_reader, encoding="utf-8") as prep_stream:
        prep_jailed_events = [
            json.loads(prep_line) for prep_line in prep_stream if prep_line.strip()
        ]
    require(prep_jailed.returncode == 0, prep_jailed.stderr)
    prep_jailed_result = json.loads(prep_jailed.stdout)
    require(prep_jailed_result["ok"] is True, prep_jailed_result)
    prep_differing_keys = [
        prep_key
        for prep_key in ("trials", "trials_out", "best_k", "summary")
        if prep_jailed_result.get(prep_key) != prep_unjailed_result[prep_key]
    ]
    require(
        not prep_differing_keys,
        f"prepare_run jailed/unjailed result keys differ: {prep_differing_keys}",
    )
    require_best_k_progress(
        "prepare-best-k-jailed", prep_jailed_result, prep_jailed_events, 2
    )
    prep_after = sorted(prep_path.name for prep_path in directory.iterdir())
    require(
        prep_after == prep_before,
        f"prepare_run best-k changed files under {directory}: "
        f"before={prep_before}, after={prep_after}",
    )
    require(not missing_tmp.exists(), f"prepare_run best-k created {missing_tmp}")
    prep_reader, prep_writer = os.pipe()
    prep_progress = fcntl.fcntl(prep_writer, fcntl.F_DUPFD_CLOEXEC, 6)
    os.close(prep_writer)
    try:
        prep_control = invoke(
            "prepare-best-k-one-jailed",
            [
                *native_command,
                "--trials-out",
                "best-k",
                "--best-k",
                "1",
                "--progress-fd",
                str(prep_progress),
            ],
            jailed_environment,
            pass_fds=(prep_progress,),
        )
    finally:
        os.close(prep_progress)
    with os.fdopen(prep_reader, encoding="utf-8") as prep_stream:
        prep_control_events = [
            json.loads(prep_line) for prep_line in prep_stream if prep_line.strip()
        ]
    require(prep_control.returncode == 0, prep_control.stderr)
    prep_control_result = json.loads(prep_control.stdout)
    require(prep_control_result["ok"] is True, prep_control_result)
    require(
        prep_control_result["trials_out"] == "best-k"
        and prep_control_result["best_k"] == 1,
        prep_control_result,
    )
    require(len(prep_control_result["trials"]) == 1, prep_control_result)
    require(prep_control_result["trials"] == eligible[:1], prep_control_result)
    require(
        len(prep_control_result["trials"]) != len(prep_jailed_result["trials"]),
        "prepare_run best-k control did not change the retained trial count",
    )
    require_best_k_progress(
        "prepare-best-k-one-jailed", prep_control_result, prep_control_events, 1
    )
    prep_after = sorted(prep_path.name for prep_path in directory.iterdir())
    require(
        prep_after == prep_before,
        f"prepare_run best-k control changed files under {directory}: "
        f"before={prep_before}, after={prep_after}",
    )
    require(
        not missing_tmp.exists(), f"prepare_run best-k control created {missing_tmp}"
    )
    record(
        "prepare-run-best-k-jailed",
        trial_ids=[
            prep_trial["trial_id"] for prep_trial in prep_jailed_result["trials"]
        ],
        eligible_trial_ids=eligible_trial_ids,
        progress_trial_ids=[
            prep_trial["trial_id"] for prep_trial in prep_jailed_events
        ],
        unjailed_progress_trial_ids=[
            prep_trial["trial_id"] for prep_trial in prep_unjailed_events
        ],
        control_trial_ids=[
            prep_trial["trial_id"] for prep_trial in prep_control_result["trials"]
        ],
    )
    # Include invoke's captures in the parent listing before creating the output home.
    for prep_suffix in ("stdout", "stderr"):
        checked_path(
            directory / f"prepare-best-k-jailed-with-output.{prep_suffix}"
        ).touch()
    prep_output_parent_before = sorted(
        prep_path.name for prep_path in directory.iterdir()
    )
    prep_output_directory = checked_path(directory / "prepare-best-k-output")
    prep_output_directory.mkdir(mode=0o700)
    prep_output_before = sorted(
        prep_path.name for prep_path in prep_output_directory.iterdir()
    )
    require(
        prep_output_before == [],
        f"output directory must start empty: {prep_output_before}",
    )
    prep_output_path = checked_path(prep_output_directory / "result.json")
    prep_reader, prep_writer = os.pipe()
    prep_progress = fcntl.fcntl(prep_writer, fcntl.F_DUPFD_CLOEXEC, 6)
    os.close(prep_writer)
    try:
        prep_output = invoke(
            "prepare-best-k-jailed-with-output",
            [
                *prep_best_k_command,
                "--output",
                str(prep_output_path),
                "--progress-fd",
                str(prep_progress),
            ],
            jailed_environment,
            pass_fds=(prep_progress,),
        )
    finally:
        os.close(prep_progress)
    with os.fdopen(prep_reader, encoding="utf-8") as prep_stream:
        prep_output_events = [
            json.loads(prep_line) for prep_line in prep_stream if prep_line.strip()
        ]
    require(prep_output.returncode == 0, prep_output.stderr)
    prep_output_result = json.loads(prep_output.stdout)
    require(prep_output_result["ok"] is True, prep_output_result)
    # Native truncates/writes directly, not atomically via temp+rename; the Python
    # wrapper performs that rewrite. Check complete JSON and no residue after exit.
    require(
        prep_output_path.is_file(), f"native output file is missing: {prep_output_path}"
    )
    prep_output_document = read_json(prep_output_path)
    for prep_key in ("trials", "trials_out", "best_k", "summary"):
        require(
            prep_output_document[prep_key]
            == prep_output_result[prep_key]
            == prep_jailed_result[prep_key],
            f"prepare_run best-k output key {prep_key} differs: "
            f"file={prep_output_document[prep_key]}, "
            f"stdout={prep_output_result[prep_key]}, "
            f"no_output={prep_jailed_result[prep_key]}",
        )
    require_best_k_progress(
        "prepare-best-k-jailed-with-output", prep_output_result, prep_output_events, 2
    )
    # Best-k opens no trial spool; --output's parent only houses the result file.
    prep_output_after = sorted(
        prep_path.name for prep_path in prep_output_directory.iterdir()
    )
    require(
        prep_output_after == ["result.json"],
        f"prepare_run best-k left files under {prep_output_directory}: "
        f"before={prep_output_before}, after={prep_output_after}",
    )
    prep_output_parent_after = sorted(
        prep_path.name for prep_path in directory.iterdir()
    )
    require(
        prep_output_parent_after
        == sorted([*prep_output_parent_before, prep_output_directory.name]),
        f"prepare_run best-k output changed files under {directory}: "
        f"before={prep_output_parent_before}, after={prep_output_parent_after}",
    )
    require(
        not missing_tmp.exists(), f"prepare_run best-k output created {missing_tmp}"
    )
    record(
        "prepare-run-best-k-jailed-with-output",
        trial_ids=[
            prep_trial["trial_id"] for prep_trial in prep_output_result["trials"]
        ],
        eligible_trial_ids=eligible_trial_ids,
        progress_trial_ids=[
            prep_trial["trial_id"] for prep_trial in prep_output_events
        ],
        output_file=prep_output_path.name,
        output_entries_before=prep_output_before,
        output_entries_after=prep_output_after,
    )
    collision = invoke(
        "progress-collision",
        cli_command("progress-collision", "--progress-fd", "5"),
        jailed_environment,
    )
    require(collision.returncode == 1, collision.stderr)
    failure = json.loads(collision.stdout)["failure"]
    expected_failure = {
        "origin": "hpo",
        "code": "hpo_cli_usage",
        "args": {},
        "exit_code": 1,
    }
    require(failure == expected_failure, failure)
    require(not checked_path(directory / "progress-collision.json").exists(), failure)
    require(not checked_path(directory / "progress-collision.jsonl").exists(), failure)
    record("progress-collision", failure=failure, result_published=False)
    require(not missing_tmp.exists(), "the engine fallback directory became usable")
    write_evidence(True)


def main():
    if sys.argv[1:] == ["--deliberate-crash"]:
        raise RuntimeError(CRASH)
    if sys.platform != "linux":
        print("SKIP: jailed profile requires Linux sealed memfds", flush=True)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--harness", type=Path, required=True)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    run_suite(parser.parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
