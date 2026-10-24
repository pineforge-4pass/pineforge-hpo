# CI memory growth gate

The canonical `memory-gate` job in `.github/workflows/ci.yml` builds the Release
native CLI and existing `fake_strategy` fixture against the pinned engine
headers on hosted `ubuntu-24.04`. It invokes the stdlib-only
`scripts/ci_memory_gate.py` directly. The trial axis uses a fixed two-bar CSV,
random sampling, seed 17, one worker, batch size one and `--trials-out all`.

Each of 10, 1,000 and 100,000 trials runs in a fresh native child. `os.wait4`
reaps that exact PID and supplies its peak RSS (Linux KiB converted to bytes).
The gate writes stdout to disk and checks every successful trial in order,
plus the result's completion counts, before judging RSS. It never loads the
whole trial array into the measuring parent.

The adjacent RSS differences divided by the trial-count differences must be
at most **256 bytes/trial** (10 to 1,000) and **32 bytes/trial** (1,000 to
100,000), matching `tests/test_streamed_trial_retention.py`. Comparisons use
integer byte allowances, so equality passes and one extra byte fails. The
pre-spool 2,828.9 bytes/trial at the upper interval exceeds the bound.
`--bars-axis` additionally reports 2 / 1,000 / 100,000 bars at ten trials,
using the fixture's existing prefix mode with jitter disabled. Bar slopes
are informational: OHLCV storage may scale with input size.

The script prints one JSON measurement per size and a final JSON verdict.
Exit 0 means both trial slopes passed; 1 means growth exceeded a bound; 2
means setup or a workload was incomplete. Each run retains stdout, stderr,
child `.exit` and `.result.json` files under a fresh
`build/memory-gate/run-*` directory, with `RUN.COMPLETE` containing the gate
verdict and `gate.exit` its own exit code. Every run first checks a deliberate
SIGKILL child's nonzero marker. Read the JSON verdict and workload results;
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
