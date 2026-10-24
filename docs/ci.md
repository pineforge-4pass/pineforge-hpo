# CI memory growth gate

The canonical `memory-gate` job in `.github/workflows/ci.yml` builds the Release
native CLI and existing `fake_strategy` fixture against the pinned engine
headers on hosted `ubuntu-24.04`. It invokes the stdlib-only
`scripts/ci_memory_gate.py` directly. The trial axis uses a fixed two-bar CSV,
random sampling, seed 17, one worker, batch size one and `--trials-out all`.

Each of 10, 1,000 and 100,000 trials runs in a fresh native child through
`/usr/bin/time -f %M -o <artifact-file> -- <native-command>`. The workflow installs
the GNU `time` package. The small launcher reaps the native child with `wait4`;
`%M` is that child's maximum resident set size in KiB. The gate reads the last
line of the launcher's output file after exit and multiplies it by 1,024 for
`peak_rss_bytes`. This is the exact kernel peak, including the final milliseconds;
there is no polling resolution. The reading has the launcher's own floor of
**about 1 MiB**, so peaks below that size cannot be distinguished.

`ru_maxrss` of a child launched directly from Python includes Python's high-water.
That inherited floor can exceed the native peak. GNU time replaces the large
Python floor with the small launcher's floor. Python's
`os.wait4(launcher.pid, 0)` reaps the launcher for its exit status; the launcher's
Python-floored rusage does not enter the measurement. Normal child exit codes
propagate through GNU time. A signalled child produces a nonzero launcher exit
and a `Command terminated by signal N` line before the final numeric peak.

Each row records `peak_source: "gnu_time_wait4"`, the full launcher `command`,
the `native_command`, `gnu_time_file`, raw `gnu_time_output` bounded to 4 KiB,
and `launcher_floor_note`. Missing GNU time, missing or unparseable output, a
nonpositive peak or output exceeding 4 KiB refuses the gate with exit 2 and an
`ERROR`. The actual `-f`/`-o` invocation checks GNU compatibility without an
extra probe process. The PASS/FAIL verdict records the three trial
`peak_sources` in order; `measurement_resolution` and any growth-failure
`reason` describe the exact peak and the launcher's floor.

The gate uses stdlib-only imports and writes native stdout directly to disk.
After reaping, it checks every successful trial in order, plus the result's
completion counts, before judging RSS. It reads trial rows incrementally and
never loads the whole trial array into the measuring parent.

The adjacent RSS differences divided by the trial-count differences must be
at most **256 bytes/trial** (10 to 1,000) and **32 bytes/trial** (1,000 to
100,000), matching `tests/test_streamed_trial_retention.py`. Comparisons use
integer byte allowances, so equality passes and one extra byte fails. The
pre-spool 2,828.9 bytes/trial at the upper interval exceeds the bound.
`--bars-axis` additionally reports 2 / 1,000 / 100,000 bars at ten trials,
using the fixture's existing prefix mode with jitter disabled. Bar slopes
are informational: OHLCV storage may scale with input size.

The script prints one JSON measurement per size and a final JSON verdict.
Exit 0 means both trial slopes passed; 1 means growth exceeded a bound or an
unexpected gate error occurred; 2 means invalid setup, an incomplete workload
or an unmeasurable trial peak. Each run retains stdout, stderr,
child `.exit`, `.time` and `.result.json` files under a fresh
`build/memory-gate/run-*` directory, with `RUN.COMPLETE` containing the gate
verdict and `gate.exit` its own exit code. Every run first checks a deliberate
SIGKILL child's signal diagnostic and nonzero launcher status, requiring its
`.exit` marker to agree. Read the JSON verdict and workload results;
an exit marker alone is not a memory judgment. No later workload runs after
a failed control. Unit tests include flat, growing and exact-bound series.

Like the other CI suites, this heavy job needs `authz` and `changes` and runs
on relevant changes once a PR is ready for review (LB-387). `ready_for_review`
reruns CI. Pushes and dispatches run the gate. `build-gate` always runs and
includes the job in both `needs` and `HEAVY_JOBS`. Its existing independent
diff classifier accepts skips only for proven cheap changes with successful
authorization, classification and workflow lint. A code-changing draft
therefore skips suites without acquiring a successful merge check; making
it ready runs them. Failure, cancellation or an unexplained code-change skip
fails the aggregate.

This fixture gate detects trial-retention regressions. Child RSS is a
resident-memory proxy, not cgroup charged memory or disk/page-cache usage.
It does not prove bounded window retention, other samplers, multiple workers,
real-strategy behavior, or the Python wrapper's memory use.
