# CI admission and runners

Pull request proofs run through `.github/workflows/gke-pr.yml`, whose
`pull_request_target` definition comes from `main`. The hosted `admit` job checks
out only the base SHA. It requires a same-repository, non-fork, open, non-draft PR
against `main`; the author, original actor and rerunning actor must each have
write, maintain or admin permission, or be `dependabot[bot]` or
`pineforge-app[bot]`. A live API read must still match the event's immutable head.
An API or parsing failure refuses admission.

Before allocating a pod, the base copy of `tests/release/pr_admission.py` fetches
the head's workflow/action blobs as data using the recursive Git tree API.
Truncated trees, symlinks, submodules, invalid names, duplicate paths, more than
100 files, a file over 1 MiB or a total over 4 MiB refuse admission. Git blob
lengths, hashes and UTF-8 are checked. Validated names are rebuilt beneath a fresh
runner scratch directory. Hosted admission and result execute only the base copy
of the checker; fetched head files remain data.

Files have a 4,096-character line bound, a 20,000-line bound and a 40-level
structure bound. Pinned actionlint parses the fetched workflows using main's
configuration before the policy checks their envelopes. The YAML reader refuses
aliases, anchors, tags, merge/duplicate keys, extra documents, explicit keys,
escaped or multiline quoted keys, control characters and tab indentation.
Flow mappings are limited to empty or plain scalar `permissions` mappings.
All runners must be literal scalars. These restrictions also cover valid YAML
forms that this policy deliberately does not support.
Before actionlint runs, scalar file references naming protected directories or
environment files, or escaping the staged directory, are refused. The linter
runs against the staged tree with script linters disabled.

Main's workflow inventory, triggers, runners, guards, dependencies, matrices,
permissions, checkouts and hosted boundary scripts judge the head. Admission and
result use no head-owned docs, tests or classifier. A PR cannot loosen the rule
that judges it. A PR that adds or removes a workflow, changes a job envelope or
changes a checkout step is refused at admission and needs an owner merge. This
includes a Dependabot action-version bump that changes a checkout or a frozen
boundary job. An owner must update main's inventory or envelope before a later
PR can use the change.

Pod checkouts name this repository, use only the admitted head SHA and disable
credential persistence. Pod jobs have empty permissions or `contents: read`;
there are no secrets, OIDC permissions, environments, callable workflow handoffs
or Actions cache steps. Setup actions do not enable caching. Trusted workflows,
including release and Pages deployment, restore no Actions cache: a member's PR
code can still access its run's runtime/cache credential directly. PR benchmark
artifacts are review evidence; trusted jobs never consume them. The PR docs job
never uploads a Pages artifact.

Only a complete, rename-aware, payload-SHA-bound compare read may prove a
documentation-only change. Admission and result fetch 100 rows per page until
a page has fewer than 100 rows. An empty overall list, an unreadable page or a
total of 3,000 or more rows refuses admission or success. Plain
documentation skips heavy proofs; workflow changes run the complete suite.
Workflow lint, light checks and the warning-free API docs build run for every
admitted PR. The hosted result judge re-reads live identity and the same compare
and requires agreement with admission, every declared job result, and a separate
code-extension veto. Failure, cancellation or an unexplained skip refuses success.
Both reads use the same classifier, so the second read protects against data
and plumbing errors; it is not a second implementation of classification.
Draft conversion refuses admission; `ready_for_review` starts a fresh run.

The PR-reachable `ci.yml` and `benchmark-smoke.yml` retain only their hosted
fail-closed `authz` jobs. A workflow with an event starting with `pull_request`,
except `pull_request_target`, cannot name a GKE runner. Complete push-to-main and dispatch proofs live in `ci-post.yml` and
`benchmark-smoke-post.yml`, including engine integration and historical
comparisons, with no `changes` job and every proof needing `authz`. The hosted native macOS job stays with `build-gate` in `ci-post.yml`;
its admitted PR counterpart stays in `gke-pr.yml`.
Linux jobs use bare scalar labels; macOS remains hosted as a separate native
job with the original displayed matrix name.
`docs-pages.yml` runs only for its filtered pushes to main and dispatches. Its
former PR check, `API documentation / Build API reference`, is covered by
`gke-pr.yml`'s `docs` job with the same displayed check and warning-free site
validation. Pages artifact upload and hosted OIDC deployment keep their main-ref
guards.

| Proof | Runner |
| --- | --- |
| workflow-lint, Light, release contract, API docs | `gke-8cpu-16gb-amd64` |
| Native Linux, Python 3.11/3.14, memory gate, Optuna smoke | `gke-16cpu-32gb-amd64` |
| Admission and final build-gate judgment | hosted `ubuntu-24.04` |
| Native macOS | hosted `macos-latest` |
| Pages deployment and all of `release.yml` | hosted `ubuntu-24.04` |

The pod image supplies compilers, Clang, CMake, Ninja, Python 3.12 with venv,
git/git-lfs, jq, curl, gh, actionlint and shellcheck, with an 80 GiB job disk.
Steps install Eigen, MPFR, GNU time, Doxygen, alternate Python versions and
pinned Python dependencies where needed. GitHub downloads, PyPI and apt need
public network access; package installation needs sudo. No job invokes system
node/npm; JavaScript actions require the Actions runner's bundled runtime.
Image compatibility, network access and disk headroom require CI confirmation.

## Check mapping and activation

The admitted proofs retain the displayed checks mapped below in `GKE PR`.
Hosted `admit` owns identity and classification; `lint` shares workflow checks.

| Previous workflow / check | Admitted job / displayed check |
| --- | --- |
| CI / authz, changes, workflow-lint, Light | admit owns authz/changes; lint / workflow-lint; light / Light |
| CI / Release notification contract | release-notification / unchanged |
| CI / Native (ubuntu-latest) | native / unchanged |
| CI / Native (macos-latest) | native-macos / unchanged |
| CI / Python 3.11, Python 3.14 | python matrix / unchanged |
| CI / HPO memory growth gate | memory-gate / unchanged |
| CI / build-gate | result / build-gate |
| Benchmark smoke / authz, changes, workflow-lint | admit owns authz/changes; lint / workflow-lint |
| Benchmark smoke / Native TPE vs Optuna | optuna / unchanged |
| Benchmark smoke / benchmark-check | PR context retired and folded into result / build-gate; benchmark smoke failures fail build-gate. The ruleset requires no status checks. |
| API documentation / Build API reference | docs / unchanged |
| New admission and final workflow judgment | admit; result / build-gate |

Check names alone are insufficient merge evidence. The merge authority must read
workflow runs and require exact path
`.github/workflows/gke-pr.yml@refs/heads/main`, event `pull_request_target`, live
PR base `main`, open state, and the gated head SHA. The exact display title is
`gke-pr #<number> <40-hex-head>`. Among matching runs, the highest run ID must
conclude `success`; an older success cannot override a newer failed, cancelled,
unfinished or missing run. `bound_run` provides the run predicate; the external
consumer must also validate the live PR.

Before activation, configure runner access for main's admission workflow,
`ci-post.yml`, `benchmark-smoke-post.yml` and `docs-pages.yml` at the main ref.
Do not allow a PR merge ref, a branch copy or a reusable-workflow bypass.
Runner isolation must exclude
private networks and metadata, service-account credentials and protected nodes.
The merge-authority mapping must be deployed before enabling the runner route.
Read back the actual run path and check attachment; a missing ref is a refusal,
not a reason to relax the binding. Repository edits do not configure the runner
group, merge authority or rulesets, and do not establish their live state.
The workflow must exist on main before later PRs can trigger admission. Validate
the adoption change in authorized CI or on a box before activation; legacy
PR authorization alone does not validate this migration.

## CI memory growth gate

The `memory-gate` jobs in `.github/workflows/gke-pr.yml` and
`.github/workflows/ci-post.yml` build the Release
native CLI and existing `fake_strategy` fixture against the pinned engine
headers on `gke-16cpu-32gb-amd64`, using the native job's Release configuration
and default build, and builds the pinned engine runtime. It invokes the stdlib-only
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

For admitted PRs this heavy job needs only `admit`; push and dispatch jobs
need only `authz`. Admitted runs use the same condition as `native`.
`ready_for_review` reruns admission. Pushes and dispatches run the gate, and their aggregates always run and require every upstream to succeed. It runs
in parallel with `native`. Hosted `result`, displayed as `build-gate`, always runs
and lists every proof in `needs`. The base judge independently rechecks live
identity and the diff, and accepts skips only for proven documentation changes
with successful admission, workflow lint, light checks and the API docs build.
Failure, cancellation or an unexplained code-change skip fails the aggregate.

This fixture gate detects trial-retention regressions. Child RSS is a
resident-memory proxy, not cgroup charged memory or disk/page-cache usage.
It does not prove bounded window retention, other samplers, multiple workers,
real-strategy behavior, or the Python wrapper's memory use.
