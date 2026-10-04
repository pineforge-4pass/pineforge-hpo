# Published benchmark evidence

This directory contains the compact machine-readable evidence for
[`results-2026-07-18.md`](../results-2026-07-18.md). These are current reruns of
the documented profiles, not reconstructed metadata for an older unpublished
CSV.

| Profile | CSV | Rows | CSV SHA-256 | Metadata SHA-256 |
|---|---|---:|---|---|
| Full, startup 10 | [`2026-07-18-full.csv`](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/benchmarks/optuna/evidence/2026-07-18-full.csv) | 60 | `7dcc63346df87367d7fe0c151faef369f103508eb3117b2f50009bcb3f83e629` | `2ca193efffc0c25b23948f2c2a8d7c67f819310b4cab633ef7f25cdbc01364ae` |
| Million discrete, startup 200 | [`2026-07-18-startup-200.csv`](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/benchmarks/optuna/evidence/2026-07-18-startup-200.csv) | 10 | `479c28e9f3825fb152e1b0193350af98922be6bf528c618c04091eb75c207e9a` | `896cd01dec6039c75057935250ae668c51772aa549b008c25c00406f1ba38214` |

Each CSV has an adjacent metadata sidecar:

- [`2026-07-18-full.csv.metadata.json`](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/benchmarks/optuna/evidence/2026-07-18-full.csv.metadata.json)
- [`2026-07-18-startup-200.csv.metadata.json`](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/benchmarks/optuna/evidence/2026-07-18-startup-200.csv.metadata.json)

Both runs use:

- source snapshot SHA-256
  `12abddc13f35933acaebf80772a74e3b9fa0af3d637c05f6afc0713a616fb404`;
- native executable SHA-256
  `2b624c6bcad5b5bb9bcbb2198258054d53658fd32a5ab67198d15b354bd4d732`.

The metadata records base revision
`297df69294ac9bbfce0d59deae47724165d7ea63` and `repository_dirty: true`, because
the double-domain `gamma_count` portability fix and evidence updates were not
committed at generation time. Exact source identity is supplied by the sidecar's
ordered 20-file manifest: every benchmark/build input has a relative path,
size, and SHA-256, plus the aggregate above. Configuration and native binary
hashes provide the remaining run identity.

The sidecars retain a replayable invocation under `execution.command`.
Published artifact and native-binary paths are repository-relative, and the
command uses the logical executable `python`; the exact Python implementation
and version remain recorded without exposing a contributor's home directory.

Ad-hoc benchmark outputs belong under ignored `build/`. Files enter this
directory only when they are small, tied to a reviewed result, and published
with a verified metadata sidecar.

## Native scaling 0.4.0

The reviewed measurements in [`../../scaling/README.md`](../../scaling/README.md)
publish three compact CSVs with adjacent `.csv.metadata.json` sidecars:

| Profile | CSV | Verified CSV SHA-256 |
|---|---|---|
| Acquisition versus history | [`2026-10-03-scale-ask.csv`](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/benchmarks/optuna/evidence/2026-10-03-scale-ask.csv) | `47f9d9c85add8c06ae9fd0e26edb481e8d02bae4a4ccb717fca52e1d331e05be` |
| Native throughput, retention, billing | [`2026-10-03-scale-native.csv`](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/benchmarks/optuna/evidence/2026-10-03-scale-native.csv) | `7aa11607484bce378946ce7d66faac68797a8f290be63393e0c4bfc464a763d5` |
| Paired normalized-regret quality | [`2026-10-03-scale-quality.csv`](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/benchmarks/optuna/evidence/2026-10-03-scale-quality.csv) | `c37edecfcae2881c19c63ee6e3f753978ec739117f442ed929d37df96b427574` |

The sidecars include exact source and measured binary hashes, compiler/platform,
engine/codegen/COCO pins, row counts, and verified CSV hashes. Acquisition rows
distinguish baseline snapshot asks from after batch-eight feedback. Native rows
distinguish the light million-trial gate, output-only matrix, hourly real-strategy
shapes, and the slow magnifier shape. Quality includes the pinned hard-suite
replica and 32D extension, with 144 paired studies and zero maximum paired delta
at 100/300/1,000 trials. No long-budget quality-equivalence claim is made.

## Review-fix evidence (release blocked)

The [review-fix report](../../scaling/review-2026-10-03.md) supersedes the original
sampler measurements. `2026-10-03-review-quality.csv` contains 500 per-seed
comparisons across five complete configurations at 3k/10k, with trace hashes,
binary hashes, frozen-reference description, normalizer and source provenance.
Adjacent `review-ask`, `review-native`, and `review-ordinal` CSVs record flat
history probes, finite before/after plus the final million-trial run, and dense
versus disk insertion. Every CSV has a verified `.csv.metadata.json` sidecar.
The selected 3k geomean is 1.015738: **the required <=1.01 gate fails**.

## Historical history-switch retry evidence (superseded)

The [history-switch report](../../scaling/switch-2026-10-03.md) supersedes the
review-fix estimator measurements. `2026-10-03-switch-quality.csv` contains 100
per-seed comparisons (80 at 3k, 20 at 10k). Adjacent `switch-ask`, `switch-native`,
`switch-ordinal`, and `switch-identity` CSVs record the default-selection and flat
history probes, 100k/1M billing runs, dense/disk insertion, and 96 prefix identity
studies. Each CSV has a verified metadata sidecar with source/binary/trace hashes,
pins and protocol limitations. The historical 3k/10k geomeans are 1.164189/1.118446:
**both quality gates fail**. Identity has zero differing proposal/value records;
it is not a long-budget quality-equivalence claim.

## Final 1,000-observation switch evidence (bounded opt-in)

The [final report](../../scaling/final-2026-10-04.md) records the completed-only
1,000-observation model, now explicit opt-in rather than the default.
`2026-10-04-final-1000-quality.csv` contains 100 per-seed
comparisons: 80 at 3k versus pinned 0.3.0 and 20 at 10k versus the benchmark-only
full-history variant. Final geomeans are 1.036492/1.031570; both revised geomean
gates failed; these are accepted opt-in tradeoffs. Rotated ellipsoid 20D is 1.245558
at 3k. The earlier 1.6% tradeoff
must not be substituted for these measurements. Adjacent `ask`, `native`, `ordinal`,
and `identity` CSVs have verified source/binary/trace/hash sidecars and record the
flat-history, billing/memory, membership, and exact-prefix gates.
