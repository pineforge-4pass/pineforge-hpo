# Methodology

## Comparison contract

Each `(problem, seed)` pair is run once by PineForge native TPE and once by
Optuna TPE. Both receive the same:

- objective formula and parameter domains;
- minimization direction;
- unsigned seed value;
- sequential `ask -> objective -> tell` schedule;
- trial budget and random-startup budget;
- 24 expected-improvement candidates per fitted proposal;
- `min(ceil(0.10 * n), 25)` good-trial split;
- prior weight `1.0`, independent marginals, and no constant liar.

The two implementations intentionally use their own random-number generators
and Parzen-estimator code. Equal seeds make each implementation deterministic;
they do not imply identical proposal sequences.

Optuna uses an in-memory Study with no pruner. PineForge uses its public native
`ask`/`tell` sampler API. Trial evaluation is sequential so concurrency,
constant-liar behavior, and worker scheduling cannot confound optimizer
quality.

## Problem matrix

| Problem | Domain | Dimensions | Standard trials | Declared optimum |
|---|---|---:|---:|---:|
| `branin2` | continuous, multimodal | 2 | 128 | `0.39788735772973816` |
| `hartmann3` | continuous, multimodal | 3 | 192 | `-3.8627797869493365` |
| `rosenbrock6` | continuous, curved valley | 6 | 512 | `0` |
| `rotated_rastrigin6` | continuous, rotated, multimodal | 6 | 640 | `0` |
| `mixed_log` | categorical/Boolean/integer/log/stepped | 6 | 384 | `0` |
| `million_discrete` | integer, nonlinear coupled | 6 | 1,000 | `0` |

The continuous functions use published standard definitions. Rastrigin is
shifted and transformed by a fixed sequence of orthogonal Givens rotations to
make its coordinates non-separable. The mixed problem covers the parameter
types used by strategy HPO. The discrete problem contains six integer
dimensions over `[0, 9]`, giving exactly `10^6` candidates.

## Correctness gates

Before any timed optimization:

1. Python evaluates each declared optimum and requires agreement within the
   objective-specific tolerance.
2. The native executable independently validates the same property and rejects
   invalid known-optimum candidates.
3. The runner validates every native JSON field, problem identity, seed, trial
   budget, startup budget, finite score, and declared optimum.
4. If `million_discrete` is selected, Python enumerates all 1,000,000 candidates
   outside the optimization timer. It requires a unique minimum at
   `(8, 1, 6, 3, 9, 4)` and records the second-best score.

These checks catch configuration drift and many objective-definition errors.
They do not replace independent review of the duplicated C++ and Python
formulas.

## Quality metrics

The primary metric is best regret:

```text
best_regret = max(0, best_observed_value - known_global_optimum)
```

Lower is better. Results are paired by problem and seed. The report includes
per-seed regret, median regret, paired wins/ties, and a bounded geometric regret
ratio. A problem-specific positive floor avoids division by zero, and each
paired ratio is clipped to `[0.01, 100]` before aggregation. These stabilizers
must not be interpreted as objective values.

The million-candidate problem also reports unique and duplicate proposals,
unique coverage, exact-optimum hits, and Hamming/L1 distance from the
best-scoring observed candidate to the verified optimum.

Five seeds are useful regression evidence, not a confidence interval. A larger
seed set is required for a statistical optimizer study.

## Timing boundaries

`sampler_ns` measures public scheduling overhead:

- PineForge: native `ask` plus `tell`;
- Optuna: `Study.ask`, all `suggest_*` calls, and `Study.tell`.

Sampler/Study construction is excluded on both paths. `objective_ns` measures
only the benchmark objective. Native candidate validity checks are recorded
separately as `validation_ns`. `wall_ns` encloses the complete per-run trial
loop.

Consequently, sampler timing is an end-to-end integration measurement. It
includes the Python runtime, Optuna Study, and in-memory storage on one side and
native C++ API overhead on the other. It is not a pure Parzen-kernel
microbenchmark. Timing claims should be made only from release builds on an
otherwise idle machine and should always include the sidecar metadata.

The runner executes PineForge then Optuna for each pair. This stable order makes
runs reproducible but can retain thermal or background-load bias. Repeat and
counterbalance externally before making precise timing claims.

## Profiles

- **Full:** all six problems, seeds `17,41,73,109,149`, declared trial budgets,
  and 10 startup trials.
- **Smoke:** Branin-2, mixed/log, and million-discrete; seed `17`; 32 trials per
  problem; 8 startup trials. It verifies integration only.
- **Custom:** any explicit `--seeds`, `--problems`, `--trials`, or
  `--startup-trials` override without `--smoke`.

The `--smoke` preset may also be combined with explicit overrides; its metadata
profile remains `smoke` and records the effective configuration.

## Change control

Changes to objective formulas, domain bounds, standard budgets, startup policy,
EI candidate count, gamma split, or result semantics require:

1. an intentional schema or methodology review;
2. a new published result rather than rewriting the historical result;
3. a smoke run with CSV and metadata validation;
4. an explanation of whether old and new quality numbers remain comparable.
