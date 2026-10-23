# HPO CI memory growth gate

The `memory-gate` job in CI builds Release HPO with tests, the engine adapter and
the native runner enabled, then invokes `scripts/ci_memory_gate.py` through
`bash scripts/run_ci_memory_gate.sh`. CI and release use the same local composite
action at `.github/actions/memory-gate/action.yml` and the pinned engine runtime.
The measurement script and its fixed fixtures are delivered separately from this
wiring. They are absent at the wiring base: **the job is wired ahead of the script
and stays RED until the script lands and the HPO memory growth is flat within the
allowance.**

The measurement contract runs the HPO binary on a fixed set of cases at a small
and a large bar rung. It measures charged memory for the whole child process
tree, using cgroup `memory.peak` when available and `/proc` RSS plus mapped bytes
otherwise. It compares the per-rung spread with a fixed byte allowance that does
not scale with bars, prints both figures, and prints how many rows/cases it
judged. RSS is a proxy where cgroup accounting is unavailable. Fixture values,
rungs, allowance, child-tree accounting and measured figures belong to the
measurement script; this wiring supplies no substitute measurements.

The job checks `test -f scripts/ci_memory_gate.py` before any build. The shared
guard repeats that check, runs the script once, streams its output and retains
it as `build/memory-gate.log`. Exit 1 (growth) and exit 2 (usage or missing input)
fail the job. An exit-zero script also fails with `INCOMPLETE` if it reports zero
rows/cases or omits a positive count. Accepted integer summaries include `4 rows`,
`4 cases judged`, `rows=4`, `cases: 4` and `rows_judged=4`; any reported nonpositive
count is refused. A positive count only validates nonempty output: the measurement
script's exit status still decides whether the growth comparison passed.

CI and release run the memory job on GitHub-hosted `ubuntu-24.04`. In CI it is a
canonical heavy job with `needs: [authz, changes]` and exactly
`if: needs.changes.outputs.code == 'true'`. Code diffs run it for draft and ready
pull requests alike, and pushes always run it. Source, scripts, fixtures,
local actions and workflow-dependent changes require the gate.

`build-gate` is the required CI check. It needs every CI job, includes
`memory-gate` in its sorted `HEAVY_JOBS` list and runs with `if: ${{ always() }}`.
It refuses unsuccessful authorization before checkout, then independently
recomputes the PR diff classification. Failure or cancellation of any dependency
fails the aggregate. A skipped heavy job is accepted only for an independently
proven docs-only or workflow-only PR with successful `authz`, `changes` and
`workflow-lint`; every other skip fails. A skipped memory gate on a code diff
therefore fails `build-gate`. Administrators must require `CI / build-gate` in
branch protection as described in [repository setup](repository-setup.md).
Adding dependencies in YAML does not change GitHub settings.

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
