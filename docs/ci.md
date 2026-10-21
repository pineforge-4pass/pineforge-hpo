# HPO CI memory growth gate

The `memory-gate` job in CI builds Release HPO with tests, the engine adapter and
the native runner enabled, then invokes `scripts/ci_memory_gate.py` through
`bash scripts/run_ci_memory_gate.sh`. CI and release use the same local composite
action at `.github/actions/memory-gate/action.yml` and the pinned engine runtime.
The measurement script and fixed fixtures are owned by SM-CX. They are absent at
the wiring base: **the job is wired ahead of the script and stays RED until SM-CX
lands it and the HPO memory growth is flat within the allowance.**

The measurement contract runs the HPO binary on a fixed set of cases at a small
and a large bar rung. It measures charged memory for the whole child process
tree, using cgroup `memory.peak` when available and `/proc` RSS plus mapped bytes
otherwise. It compares the per-rung spread with a fixed byte allowance that does
not scale with bars, prints both figures, and prints how many rows/cases it
judged. RSS is a proxy where cgroup accounting is unavailable. Fixture values,
rungs, allowance, child-tree accounting and spot-box measurements remain SM-CX
deliverables; this wiring supplies no substitute measurements.

The job checks `test -f scripts/ci_memory_gate.py` before any build. The shared
guard repeats that check, runs the script once, streams its output and retains
it as `build/memory-gate.log`. Exit 1 (growth) and exit 2 (usage or missing input)
fail the job. An exit-zero script also fails with `INCOMPLETE` if it reports zero
rows/cases or omits a positive count. Accepted integer summaries include `4 rows`,
`4 cases judged`, `rows=4`, `cases: 4` and `rows_judged=4`; any reported nonpositive
count is refused. A positive count only validates nonempty output: the measurement
script's exit status still decides whether the growth comparison passed.

The memory job uses the pinned engine's public Linux heavy-pool expression,
including its same-repository PR check. Its actual class label is
`pf-ci-x64-16`, alongside `self-hosted` and `pf-ci`. A hosted fallback refuses
before checkout or build. A repository missing pool/class admission remains
queued or refused; changing that allowlist belongs to laneboss.

`build-gate`, displayed as `build`, needs every CI job and runs with `if: always()`
on the standard runner. It only prints and checks results. A failed/cancelled job
or a failed/missing path classification fails the aggregate. A skipped verification
job is accepted only after successful classification of a docs-only pull request.
The memory gate follows that same skip condition; source, scripts, fixtures,
workflow and local-action changes run it. Administrators must require the `build`
check in branch protection as described in [repository setup](repository-setup.md).
Adding the dependency in YAML does not change GitHub settings.

On a published release, `package` needs `memory-gate` to succeed. The gate checks
out the release tag and exports its resolved commit; packaging checks out that
exact commit. Missing scripts, empty judgments and measured growth prevent asset
uploads and the subsequent hub notification. Notification replay skips both gate
and packaging, verifies existing assets, and publishes no new package bytes.

For a failure, open the `HPO memory growth gate` job. `INCOMPLETE` means the
measurement did not establish a nonempty judgment; repair missing inputs or the
summary contract before retrying. Exit 1 means the measured spread exceeded the
fixed allowance: inspect the two per-rung figures and case identities in the log.
Do not increase an allowance with the bar count or bypass a failed gate to publish.
