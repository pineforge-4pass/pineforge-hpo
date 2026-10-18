# HPO lane report

The original migration inventory is retained in [hpoRT.md](hpoRT.md).

## Fix round 1

Re-read `git -C ~/code/pf-ci-spot show origin/main:workflows/public.yml` at
`1fe46216757d3f2733272f61320981f9fb215e42` (bulletins #614/#615). Updated all eight Linux
x64 runner expressions to the new public fragment verbatim, with only surrounding YAML
indentation adjusted:

| Workflow | Jobs updated |
| --- | --- |
| `.github/workflows/ci.yml` | `release-notification`, `native` Linux matrix entry, `python` |
| `.github/workflows/benchmark-smoke.yml` | `optuna` |
| `.github/workflows/docs-pages.yml` | `build`, `deploy` |
| `.github/workflows/release.yml` | `package`, `notify-hub` |

Each expression gained exactly this condition after the `pull_request_target` check:

```text
github.actor != 'dependabot[bot]' && github.triggering_actor != 'dependabot[bot]' &&
```

Dependabot now stays on `ubuntu-latest` when either actor field identifies it, including
reruns. The native macOS leg remains on `macos-latest`. Required `PF_CI_LABELS` values,
steps, timeouts, matrices, and all other workflow settings are unchanged from the original
migration. All existing files and `DONE` are retained. `DONE2` is created in this directory.

The complete updated fragment and before/after explanation are appended under
"Fix round 1" in [hpoRT.md](hpoRT.md). No existing lane `REPORT.md` was present, so this
file was created beside that report.

Reviewed the upstream fragment, all eight expression bodies, and Git diffs. Verification
is limited to static inspection and Git whitespace/status checks. No repository code,
builds, tests, linters, benchmarks, documentation generators, workflows, or CI jobs were
run or dispatched. One local commit records this round:
`ci: track pf-ci-spot fragment 1fe46216 (dependabot stays hosted)`.
Commit hooks and signing are disabled for that invocation. Nothing is pushed.
