# AGENTS.md — pineforge-hpo

> Project memory for coding agents. Keep this file terse and concrete.

## Product boundary

- This repository is a consumer of PineForge strategy plugins and the public
  PineForge C ABI.
- `external/pineforge-engine` and `external/pineforge-codegen-oss` are pinned
  optional git submodules. Do not advance their gitlinks without the full
  compatibility and end-to-end gates.
- Do not implement HPO as a `BacktestEngine` subclass.
- Do not move transpiler implementation into this repository.
- A direct PineScript UX may invoke a one-time `pineforge-codegen-oss` bridge
  through its `pineforge_codegen` Python module; the trial hot loop remains
  native C++.

## Correctness invariants

- Transpile and compile each unique strategy artifact once, not once per trial.
- Load each strategy plugin once per worker process.
- Create a fresh strategy handle for every trial.
- Apply `strategy_set_input` and `strategy_set_override` before running.
- Check `strategy_get_last_error` before scoring a report.
- Free the report before freeing the strategy handle.
- Treat OHLCV input as immutable shared data.
- Include codegen, engine ABI/runtime, compiler, target, and exact flags in the
  artifact cache identity.
- Preserve `-ffp-contract=off` in generated-strategy compilation.

## Architecture invariants

- Keep artifact compilation, trial execution, objective evaluation, and
  sampling behind separate interfaces.
- A report-based metric expression is one objective adapter, not the objective
  abstraction itself.
- Multiple-strategy objectives consume an explicitly requested account or
  portfolio observation and need not materialize all report fields.
- Do not call independent strategy-report aggregation a shared-account
  simulation. Shared cash/margin/order sequencing requires a separate execution
  contract.
- Do not add a third-party optimizer or storage dependency until the choice is
  recorded in the plan or an ADR.

## Language and style

- C++17, four-space indentation, 100-column limit.
- Use fixed-width integer types at ABI and persistence boundaries.
- Prefer typed IDs and enums in the hot path; resolve configuration strings
  during study initialization.
- Seed all stochastic samplers and persist the seed with every study.
- Bump `kTpeAlgorithmRevision` in `src/core/tpe_sampler.cpp` for every change to
  TPE numerical arithmetic or RNG consumption; revalidate checkpoint/golden contracts.

## Verification

Before claiming a change is complete:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j4
ctest --test-dir build --output-on-failure
python3 -m unittest discover -s tests/python -v
```

For documentation/public-header changes, also require a zero-warning API build:

```bash
rm -rf build/docs
mkdir -p build/docs
PROJECT_NUMBER="$(tr -d '\n' < VERSION)" doxygen Doxyfile
test ! -s build/docs/doxygen-warnings.log
python3 docs/pages/validate_generated_site.py build/docs/html
```

For sampler or benchmark-protocol changes, run the reproducible smoke profile
documented in `benchmarks/README.md` and retain its CSV metadata sidecar while
reviewing the result. Ad-hoc benchmark artifacts stay under ignored build
directories. A small raw evidence set may be committed only under
`benchmarks/optuna/evidence/` when it is tied to a reviewed published result and
includes its metadata sidecar plus verified hashes.

Once the engine integration test exists, also compare a fixed candidate against
the canonical PineForge harness and require identical metrics/trades for the
selected fixture.
