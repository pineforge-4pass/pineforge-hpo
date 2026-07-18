# Result and metadata schema

The benchmark produces a CSV data file and a JSON sidecar. CSV schema version 1
is append-stable: existing columns keep their names, types, units, and meaning.
A breaking change requires a new version in the sidecar.

## CSV schema version 1

Each row represents one implementation running one problem with one seed.

| Field | Type | Unit | Meaning |
|---|---|---|---|
| `implementation` | string | - | `pineforge_native` or `optuna_4.9.0` |
| `problem` | string | - | Stable problem identifier |
| `seed` | unsigned integer | - | Paired sampler seed |
| `trials` | positive integer | trials | Effective trial budget |
| `startup_trials` | positive integer | trials | Random proposals before fitted TPE |
| `best_value` | finite number | objective-specific | Lowest observed objective |
| `optimum` | finite number | objective-specific | Declared global optimum |
| `best_regret` | non-negative number | objective-specific | `max(0, best_value - optimum)` |
| `sampler_ns` | non-negative integer | nanoseconds | Public proposal/feedback API time |
| `objective_ns` | non-negative integer | nanoseconds | Objective evaluation time |
| `validation_ns` | non-negative integer | nanoseconds | Explicit candidate validation time |
| `wall_ns` | non-negative integer | nanoseconds | Complete optimization-loop time |
| `unique_candidates` | integer | candidates | Unique proposals for `million_discrete`; otherwise `-1` |
| `duplicate_candidates` | integer | candidates | Duplicate proposals for `million_discrete`; otherwise `-1` |
| `exact_optimum_hits` | non-negative integer | trials | Exact optimum proposals; `0` outside the discrete diagnostic |
| `best_hamming` | integer | coordinates | Best candidate distance for `million_discrete`; otherwise `-1` |
| `best_l1` | integer | coordinate steps | Best candidate distance for `million_discrete`; otherwise `-1` |

`validation_ns` is zero on the Optuna path because its parameter suggestions
already satisfy the declared distributions; this field must not be added to
`sampler_ns` when comparing proposal overhead.

## Metadata sidecar

With `--output results.csv`, the default sidecar is
`results.csv.metadata.json`. Its root contract is:

```json
{
  "schema": "pineforge-hpo.optuna-benchmark-metadata.v1",
  "csv_schema_version": 1,
  "suite": "native_tpe_vs_optuna",
  "generated_at_utc": "RFC 3339 timestamp",
  "configuration_sha256": "SHA-256 of canonical effective configuration",
  "configuration": {},
  "environment": {},
  "validation": {},
  "artifacts": {},
  "execution": {}
}
```

### `configuration`

- `profile`: `full`, `smoke`, or `custom`;
- `direction` and `schedule`;
- ordered seed list;
- ordered problem list with effective trials and declared optimum;
- complete shared TPE controls: startup trials, EI candidates, gamma fraction
  and cap, prior weight, marginal mode, and constant-liar mode.

`configuration_sha256` identifies this effective configuration independently of
the machine and output location. It does not identify source code or the native
binary.

### `environment`

Records platform, architecture, processor string, Python implementation and
version, pinned dependency versions, native executable path and SHA-256,
repository revision when available, and dirty-worktree state. Artifact and
native paths inside the repository are stored repository-relative; external
paths remain absolute. A null revision means the source was not inside a
committed Git checkout.

`source_snapshot` fingerprints the exact benchmark runner, native benchmark,
root and suite CMake inputs, pinned requirements, public HPO headers, and native
core sources. It lists each relative path, byte size, and SHA-256 plus a stable
aggregate hash over the ordered manifest. This is the source identity when
`repository_revision` is null or the worktree is dirty.

The native executable hash is the authoritative identity for the compiled
comparison implementation. The source snapshot identifies its declared source
inputs but cannot prove that a given binary was produced from them; publish and
compare both hashes. Compiler and CMake versions are not embedded by the current
executable, so record them in any published human-readable result.

### `validation`

Records whether both implementations checked their declared optima. When the
million-candidate problem is selected, it also records exhaustive candidate
count, minimum, unique minimizer, multiplicity, second-best value, and
enumeration wall time. Otherwise that entry is null.

### `artifacts` and `execution`

`artifacts.csv` contains the stored path, SHA-256, and data-row count. The path
is repository-relative when the artifact is inside the checkout and absolute
otherwise. The object is null when the run did not request CSV output.
`execution` records a replayable command beginning with the logical executable
`python` and the whole-driver wall time. The exact Python implementation and
version are in `environment`; contributor home-directory paths are not
published.

## Consumer checks

A result consumer should reject or quarantine a run when:

- `schema` or `csv_schema_version` is unknown;
- the CSV hash or row count differs from the sidecar;
- a `(problem, seed)` pair lacks either implementation;
- paired rows have different trial or startup budgets;
- an objective or timing value violates the types above;
- optimizer controls or binary hashes differ across runs being aggregated.
