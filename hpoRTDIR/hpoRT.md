# CR-171: HPO Linux x64 runner migration

Branch: `core/cr171-ci-spot`. All four files under `.github/workflows` were reviewed.
Eight job definitions contain Linux x64 work; the native CI job also retains its hosted macOS leg.

Sources read before editing:

- `~/.local/state/pineforge-super-top/ci-spot-labels-1018.txt`.
- `git -C ~/code/pf-ci-spot show origin/main:workflows/public.yml`, at
  `origin/main` revision `908569dc04e33c7cdf5733dd65507ddebe71b0e3`.

## Exact public fragment

In the inventory below, **public fragment** means this expression copied verbatim, with only
the surrounding YAML indentation adjusted:

```yaml
runs-on: >-
  ${{ github.event_name != 'pull_request_target' &&
      contains(fromJSON('["push","pull_request","workflow_dispatch","schedule","release"]'), github.event_name) &&
      (github.event_name != 'pull_request' ||
       (github.event.pull_request.head.repo.full_name == github.repository && github.event.pull_request.head.repo.fork == false)) &&
      fromJSON('["self-hosted","pf-ci","pf-ci-x64"]') || 'ubuntu-latest' }}
env:
  PF_CI_LABELS: '["self-hosted","pf-ci","pf-ci-x64"]'
```

Each of the eight job definitions has exactly one job-level `PF_CI_LABELS` key with that exact
string. There were no existing job-level environment blocks to merge. Existing step-level
environment blocks are unchanged. The native job shares this environment across its two OS
legs, while only its Linux leg can request the pool.

## Every workflow and job: before -> after

| Workflow | Job / matrix leg | Before `runs-on` | After `runs-on` | Routing / hosted reason |
| --- | --- | --- | --- | --- |
| `.github/workflows/ci.yml` | `release-notification` | `ubuntu-latest` | Public fragment | Linux x64 uses `pf-ci-x64` for eligible events; otherwise hosted fallback. |
| `.github/workflows/ci.yml` | `native`, `os: ubuntu-latest` | `${{ matrix.os }}` -> `ubuntu-latest` | `${{ matrix.runner }}` -> public fragment in `matrix.include[].runner` | Linux x64 uses the exact fragment through the added runner field. |
| `.github/workflows/ci.yml` | `native`, `os: macos-latest` | `${{ matrix.os }}` -> `macos-latest` | `${{ matrix.runner }}` -> `macos-latest` | Stays GitHub-hosted: macOS is excluded from this migration. |
| `.github/workflows/ci.yml` | `python`, both `python: '3.11'` and `'3.14'` | `ubuntu-latest` | Public fragment | Both Linux x64 legs use the pool for eligible events; otherwise hosted fallback. |
| `.github/workflows/benchmark-smoke.yml` | `optuna` | `ubuntu-latest` | Public fragment | Linux x64 uses the pool for eligible events; otherwise hosted fallback. |
| `.github/workflows/docs-pages.yml` | `build` | `ubuntu-latest` | Public fragment | Linux x64 uses the pool for eligible events; otherwise hosted fallback. |
| `.github/workflows/docs-pages.yml` | `deploy` | `ubuntu-latest` | Public fragment | Linux x64 uses the pool for eligible events; the existing deploy condition is unchanged. |
| `.github/workflows/release.yml` | `package` | `ubuntu-latest` | Public fragment | Linux x64 uses the pool on release events; its existing release-only condition is unchanged. |
| `.github/workflows/release.yml` | `notify-hub` | `ubuntu-latest` | Public fragment | Linux x64 uses the pool on eligible release/replay events; its existing dependency and condition are unchanged. |

The native matrix still has `os: [ubuntu-latest, macos-latest]`. Its new `include` entries only
attach the appropriate `runner` value to those same two legs. The Python matrix is unchanged.

## Hosted exclusions and dispatcher policy

- The native macOS leg remains on `macos-latest`.
- For all migrated Linux legs, the supplied fragment keeps fork PRs and PRs whose head
  repository does not match the base repository on `ubuntu-latest`. It also falls back to
  `ubuntu-latest` for every event outside `push`, `pull_request`, `workflow_dispatch`,
  `schedule`, and `release`, including `pull_request_target` and `workflow_run`.
- There are no ARM jobs, `pull_request_target`-only workflows, or Dependabot-specific jobs in
  this checkout. There is no `.github/dependabot.yml` or `.github/dependabot.yaml`, and no
  workflow references Dependabot.
- Only the standard x64 class is requested: `["self-hosted","pf-ci","pf-ci-x64"]`.
  No large or ARM class, extra labels, or duplicate labels were introduced.
- The spec's actor allowlist remains `luisleo526`, `pineforge-app[bot]`, and
  `github-actions[bot]` for chain triggers only. The verbatim public fragment contains no
  actor predicate; non-allowlisted actors, including `dependabot[bot]`, remain subject to
  the dispatcher's rejection. No dispatcher or allowlist changes were made.

## Scope and verification

Changes are limited to runner routing, the required job environment, the native matrix's
runner mapping, and this requested report/completion marker. No steps, timeouts, trigger
filters, job conditions, permissions, dependencies, submodule gitlinks, or existing matrix
values were changed. All existing files were retained; no files were deleted.

Verification is limited to reviewing file contents and Git diffs. Per the execution brief,
no repository code, builds, tests, linters, benchmarks, documentation generators, workflows,
or CI jobs were run locally or dispatched. The AGENTS.md execution gates were therefore
not run. Local commits are separated by workflow file, with this report and `DONE` included
in the final workflow group. Commit hooks and signing are disabled for these invocations.
Nothing is pushed.

## Fix round 1

Re-read `git -C ~/code/pf-ci-spot show origin/main:workflows/public.yml` at revision
`1fe46216757d3f2733272f61320981f9fb215e42` for bulletins #614/#615. All eight Linux x64
runner expressions now match that fragment verbatim, allowing only the surrounding YAML
indentation to differ:

```yaml
runs-on: >-
  ${{ github.event_name != 'pull_request_target' &&
      github.actor != 'dependabot[bot]' && github.triggering_actor != 'dependabot[bot]' &&
      contains(fromJSON('["push","pull_request","workflow_dispatch","schedule","release"]'), github.event_name) &&
      (github.event_name != 'pull_request' ||
       (github.event.pull_request.head.repo.full_name == github.repository && github.event.pull_request.head.repo.fork == false)) &&
      fromJSON('["self-hosted","pf-ci","pf-ci-x64"]') || 'ubuntu-latest' }}
env:
  PF_CI_LABELS: '["self-hosted","pf-ci","pf-ci-x64"]'
```

Updated jobs:

- `ci.yml`: `release-notification`, `native` Linux matrix entry, and `python`.
- `benchmark-smoke.yml`: `optuna`.
- `docs-pages.yml`: `build` and `deploy`.
- `release.yml`: `package` and `notify-hub`.

Before -> after: each job used the original public fragment without an actor predicate;
each now uses the new public fragment with both actor checks. If either `github.actor` or
`github.triggering_actor` is `dependabot[bot]`, the expression selects `ubuntu-latest`.
This supersedes the earlier report's statement that the fragment had no actor predicate
and would leave Dependabot subject to dispatcher rejection.

The native macOS leg still selects `macos-latest`. All existing `PF_CI_LABELS` keys and
values, steps, timeouts, matrix values, and other workflow settings remain unchanged.
All existing files and the original completion marker are retained.

Verification is limited to reading the upstream fragment, reviewing the eight expression
bodies and Git diff, and inspecting Git whitespace/status output. No repository code,
tests, builds, linters, benchmarks, documentation generators, workflows, or CI jobs were
run or dispatched. This fix is recorded in one local commit named
`ci: track pf-ci-spot fragment 1fe46216 (dependabot stays hosted)`, with commit hooks and
signing disabled for that invocation. Nothing is pushed.
