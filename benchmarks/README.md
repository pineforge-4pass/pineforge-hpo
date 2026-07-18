# PineForge HPO benchmarks

This directory contains reproducible benchmark suites for optimizer quality and
proposal/feedback API overhead. Benchmarks are not product dependencies and do not run as
part of the normal build.

## Suites

| Suite | Purpose | Status |
|---|---|---|
| [`optuna/`](optuna/README.md) | Compare PineForge native TPE with official Optuna TPE on paired synthetic problems | Active |

The Optuna suite includes continuous, non-separable, mixed-type, log-scale, and
million-candidate discrete objectives. Its checked-in
[`results-2026-07-18.md`](optuna/results-2026-07-18.md) is a historical result,
not a universal performance claim. Its reviewed
[`evidence/`](optuna/evidence/README.md) directory contains the raw CSVs,
metadata sidecars, and verified hashes.

## Quick smoke run

Run these commands from the repository root. All generated files stay under the
ignored `build/` tree.

```bash
python3 -m venv build/benchmarks/venv
build/benchmarks/venv/bin/python -m pip install \
  -r benchmarks/optuna/requirements.txt

cmake -S benchmarks/optuna -B build/benchmarks/optuna \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build/benchmarks/optuna \
  --target pineforge_hpo_optuna_native -j4

build/benchmarks/venv/bin/python benchmarks/optuna/run_benchmark.py \
  --native build/benchmarks/optuna/pineforge_hpo_optuna_native \
  --smoke \
  --output build/benchmarks/optuna/smoke.csv
```

The smoke profile uses one seed and 32 trials on Branin-2, the mixed/log
objective, and the million-candidate discrete objective. It exercises setup,
both proposal/feedback loops, exhaustive validation of the discrete optimum,
CSV output, and metadata generation. It is an integration check, not a quality
comparison.

The command writes:

- `smoke.csv`: one row per implementation, problem, and seed;
- `smoke.csv.metadata.json`: configuration, dependency versions, native-binary
  hash, source revision, validation evidence, and CSV hash.

## Benchmark policy

New or changed suites should follow these rules:

1. Compare the same objective, search space, direction, seed set, schedule, and
   trial budget.
2. Keep optimizer quality separate from objective runtime and orchestration
   overhead.
3. Pin external comparison dependencies and record the complete runtime
   environment.
4. Validate declared optima independently where the search space permits it.
5. Publish raw machine-readable output alongside a concise interpretation.
6. Report limitations and paired-seed variance; do not infer universal
   superiority from one machine or a small problem set.
7. Keep ad-hoc environments, binaries, and result files under ignored `build/`.
   A small reviewed evidence set may be committed under
   `optuna/evidence/` only when it is linked to a published result and includes
   its metadata sidecar and verified hashes.

See the Optuna suite's [methodology](optuna/METHODOLOGY.md) and
[result schema](optuna/RESULTS_SCHEMA.md) for its exact contract.
