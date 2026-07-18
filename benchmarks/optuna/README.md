# Native TPE vs Optuna 4.9.0

This suite compares PineForge's native C++ TPE sampler with official Optuna
4.9.0. Optuna and its fully pinned Python environment are benchmark-only; they
are not PineForge build or runtime dependencies.

The suite is designed to answer two bounded questions:

- Under equal problem definitions, seeds, and trial budgets, what solution
  quality does each implementation reach?
- What is the end-to-end overhead of each implementation's public
  proposal/feedback path?

It does not claim that either implementation is universally faster or better.
Read [`METHODOLOGY.md`](METHODOLOGY.md) before interpreting results.

## Quick smoke run

From the repository root:

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

`--smoke` runs seed `17` for 32 trials on three representative problems. It
also exhaustively validates all one million candidates in the discrete problem.
The profile is intentionally too small for optimizer-quality conclusions.

## Full comparison

After the same setup and build, omit `--smoke`:

```bash
build/benchmarks/venv/bin/python benchmarks/optuna/run_benchmark.py \
  --native build/benchmarks/optuna/pineforge_hpo_optuna_native \
  --output build/benchmarks/optuna/results.csv
```

The standard profile runs five fixed seeds over all six problems using their
declared budgets. It takes materially longer than the smoke profile.

The strategy-exploration companion changes only the random-startup allocation
from 10 to 200 trials and runs the million-candidate problem:

```bash
build/benchmarks/venv/bin/python benchmarks/optuna/run_benchmark.py \
  --native build/benchmarks/optuna/pineforge_hpo_optuna_native \
  --problems million_discrete \
  --startup-trials 200 \
  --output build/benchmarks/optuna/results-million-startup200.csv
```

Every CSV row records `startup_trials`, so profiles cannot be silently
combined. For targeted diagnostics, use `--seeds`, `--problems`, or `--trials`;
run `run_benchmark.py --help` for the complete CLI.

## Outputs

When `--output PATH` is supplied, the runner writes:

- `PATH`: raw result rows for both implementations;
- `PATH.metadata.json`: the effective profile, all optimizer controls,
  environment and dependency versions, native executable hash, deterministic
  source snapshot, Git state, validation evidence, row count, and CSV SHA-256.

Use `--metadata-output` to choose another sidecar path. The stable field
contract and timing units are documented in
[`RESULTS_SCHEMA.md`](RESULTS_SCHEMA.md).

## Included problems

- Branin-2 and Hartmann-3: low-dimensional continuous multimodal functions;
- Rosenbrock-6: a narrow curved continuous valley;
- shifted, fixed-orthogonally-rotated Rastrigin-6: non-separable and
  multimodal;
- mixed log interaction: categorical, Boolean, integer, log-integer, log-real,
  and stepped-real dimensions;
- million discrete: exactly `10^6` candidates with nonlinear modular and
  product interactions and a unique verified optimum.

The standard continuous definitions follow the Simon Fraser University
Virtual Library of Simulation Experiments:

- <https://www.sfu.ca/~ssurjano/branin.html>
- <https://www.sfu.ca/~ssurjano/hart3.html>
- <https://www.sfu.ca/~ssurjano/rosen.html>
- <https://www.sfu.ca/~ssurjano/rastr.html>

Optuna is pinned to the official
[4.9.0 release](https://github.com/optuna/optuna/releases/tag/v4.9.0).
The checked-in [`results-2026-07-18.md`](results-2026-07-18.md) preserves the
five-seed comparison and its limitations; the linked
[`evidence/`](evidence/README.md) directory publishes its raw rows and metadata.
