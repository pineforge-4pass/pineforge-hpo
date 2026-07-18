# Published benchmark evidence

This directory contains the compact machine-readable evidence for
[`results-2026-07-18.md`](../results-2026-07-18.md). These are current reruns of
the documented profiles, not reconstructed metadata for an older unpublished
CSV.

| Profile | CSV | Rows | CSV SHA-256 | Metadata SHA-256 |
|---|---|---:|---|---|
| Full, startup 10 | [`2026-07-18-full.csv`](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/benchmarks/optuna/evidence/2026-07-18-full.csv) | 60 | `e525f85c892d05e4deeb65b4a720b4041aa48b61eaa5d716acec0eccf7ac61c8` | `2ceabeeb9a637aa78ff88bcf6e48cb52fd34b5dfcc872476ac86c075cd373e9a` |
| Million discrete, startup 200 | [`2026-07-18-startup-200.csv`](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/benchmarks/optuna/evidence/2026-07-18-startup-200.csv) | 10 | `d06348b1af4dd08e725f1c7a957e826c8c6c976b3e21041bcf328824f3c49fd8` | `4448e7059c0d9dfc914e102fb5d5dc16cdc2d14c12f5d0bc96f85b67d5d615ad` |

Each CSV has an adjacent metadata sidecar:

- [`2026-07-18-full.csv.metadata.json`](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/benchmarks/optuna/evidence/2026-07-18-full.csv.metadata.json)
- [`2026-07-18-startup-200.csv.metadata.json`](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/benchmarks/optuna/evidence/2026-07-18-startup-200.csv.metadata.json)

Both runs use:

- source snapshot SHA-256
  `b4645e5e8bde808df5a44e137cf60d3e2adc71e65e3c6d92ce76e1d1f3d17a8e`;
- native executable SHA-256
  `2b624c6bcad5b5bb9bcbb2198258054d53658fd32a5ab67198d15b354bd4d732`.

The repository had no first commit at generation time. The metadata therefore
records `repository_revision: null` and `repository_dirty: true`. This is an
explicit limitation, not a placeholder. Source identity is supplied by the
sidecar's ordered 20-file manifest: every benchmark/build input has a relative
path, size, and SHA-256, plus the aggregate above. Configuration and native
binary hashes provide the remaining run identity.

The sidecars retain a replayable invocation under `execution.command`.
Published artifact and native-binary paths are repository-relative, and the
command uses the logical executable `python`; the exact Python implementation
and version remain recorded without exposing a contributor's home directory.

Ad-hoc benchmark outputs belong under ignored `build/`. Files enter this
directory only when they are small, tied to a reviewed result, and published
with a verified metadata sidecar.
