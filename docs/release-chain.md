# HPO release notification and replay

`.github/workflows/release.yml` receives GitHub's `release: published` event.
Its `memory-gate` job must pass the [HPO memory growth gate](ci.md) on the
released source before `package` builds and uploads the Python wheel, sdist and
two failure catalogs from that exact verified commit. `notify-hub` depends on
that job succeeding. A failed, cancelled or skipped package job cannot send the
producer event on this path.

The notification helper rechecks the published release and downloads these
four public assets before making any dispatch request:

- `pineforge_hpo-<version>-py3-none-any.whl`
- `pineforge_hpo-<version>.tar.gz`
- `hpo_failure_codes.json`
- `hpo_failure_codes_diff.json`

It requires one uploaded, nonempty copy of each asset at the expected HPO
release URL, checks byte sizes and GitHub SHA-256 digests when supplied, then
verifies package metadata and catalog agreement. The diff must be stamped for
the release version and the downloaded catalog's SHA-256. The tag must resolve
to a full source commit whose `VERSION` agrees with the package spelling.

Tags use exactly `vN.N.N` or `vN.N.NrcM`, with no leading zeros and `M >= 1`.
The existing catalog version validator supplies this rule. Semver aliases such
as `v1.6.0-rc.1`, alpha/beta, local and post releases are refused. The GitHub
release's boolean prerelease flag must agree with the tag. Canonical RC releases
notify the hub with `prerelease: true`; the hub controls channel eligibility.

## Wire contract

The only emitted event is `repository_dispatch` to the fixed
`pineforge-4pass/pineforge-release` repository:

```json
{
  "event_type": "hpo-release",
  "client_payload": {
    "version": "1.6.0rc1",
    "prerelease": true,
    "run_id": 123456
  }
}
```

`version` is the bare canonical PEP 440 package version. `prerelease` is a JSON
boolean and `run_id` is the positive integer Actions run ID making this attempt.
There are no additional payload fields. This is the established producer
contract accepted by the hub's `handle-upstream.yml`, as required by the release
chain design's section 3.1. The hub's `scripts/release_pair.py` owns conversion
to semver, the HPO pairing cutoff and duplicate/downgrade decisions; HPO does not
copy those decisions or bump a hub version.

Section 3.2's `pineforge/release-ready/v1` manifest belongs to the hub-to-consumer
`pineforge-release` event. Only the hub can resolve the combined image, partner
packages and immutable facts needed for that manifest. An HPO producer event
does not assert that the combination is ready.

## Replay after repair or a missed event

Run the same workflow on `main` with an existing published tag:

```sh
gh workflow run release.yml --repo pineforge-4pass/pineforge-hpo \
  --ref main -f tag=v1.6.0
```

The `workflow_dispatch` path skips `memory-gate` and `package` and calls the same
verifier and sender. It works for releases whose tagged source predates the
notification implementation. It never rebuilds, uploads, changes, removes or replaces release
assets, tags, commits or version files. Repair incomplete assets through the
producer's release process before replaying; replay refuses incomplete releases.

Replaying a duplicate sends the same version/prerelease identity. A new workflow
run changes only the producer's `run_id`; a rerun retains it. The hub must apply
its existing idempotency and monotonicity rules. This entry point can also be
invoked by the chain's reconciliation controller; it has no separate payload
override or bypass for missing assets. There is no HPO-side schedule in this link.

## Delivery evidence and failure

Only HTTP 204 from GitHub's dispatch API is acceptance. Every transport,
authentication, version, package or asset error fails the notification job.
The job emits an error annotation and a run-linked failure summary. Standard
Actions failure notifications and the chain's owner/incident monitor must watch
the producer run; this link does not send external messages or create issues.

A successful dispatch retains the `hpo-hub-notification-<run>-<attempt>` Actions
artifact. Its JSON receipt records `dispatch_accepted`, the exact event, source
tag/commit, asset hashes and attempt. The summary explicitly leaves hub processing
and downstream readiness pending. It is not a consumer deployment receipt.

## Repository setup and CI

Make the existing org App secrets `PINEFORGE_APP_ID` and
`PINEFORGE_APP_PRIVATE_KEY` available to HPO at repository scope or through
org-secret selected-repository access. The App installation must include
`pineforge-release` with `Contents: write`. Each notification job requests a
short-lived token scoped only to that hub repository. Source metadata uses the
HPO job's read-only `GITHUB_TOKEN`; public asset downloads carry no credential.
No new variable, PAT or consumer deployment credential is used.

The workflow and replay entry point must land on the default branch before use.
Add `Release notification contract` to the branch's required status checks and
retain all existing checks and pull-request protection. This lane neither
merges nor deploys consumer updates. Required-check enforcement is a repository
setting and cannot be enabled by these committed files.

The CI job runs `python -m unittest discover -s tests/release -v` and Ruff. The
fixtures cover stable/RC payloads, replay, missing assets, partial uploads,
metadata/catalog disagreement, wrong source/version, pagination and failed
authentication/transport. Workflow checks exercise the actual notification
condition for failed, skipped, cancelled and successful upstream jobs. Removing
the notification implementation or moving dispatch before asset validation makes
the corresponding cases fail. Tests run in CI or on cloud boxes; local work on
the owner's Mac is limited to static checks.

The hub lane still owns partner readiness, immutable manifests, reconciliation
and aggregate owner reporting. Consumer lanes own their update PRs, required CI
and deployed E2E, merge/deploy policy, served-state receipts and rollback.
