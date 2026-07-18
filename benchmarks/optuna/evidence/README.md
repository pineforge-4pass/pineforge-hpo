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
