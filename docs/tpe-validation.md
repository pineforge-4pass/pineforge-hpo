# Native product-TPE validation

This document records the deterministic nonlinear test suites used to decide
whether PineForge's clean-room C++ product-TPE is usable. It does not claim
proposal-sequence compatibility with Optuna, and it does not treat one lucky
seed as evidence.

Every comparison uses the same search space and trial budget for TPE, seeded
random, and diagnostic dlib global search. Objectives are minimized and results
are reported as regret from an independently checked known optimum. TPE is
gated against random; dlib is shown to expose where a product density model is
weaker or stronger.

## Nonlinear usability suite

This suite runs nine fixed seeds per problem:

| Problem | Dimensions | Budget | TPE median regret | Random median | Paired wins |
| --- | ---: | ---: | ---: | ---: | ---: |
| Branin | 2 real | 128 | 0.03433 | 0.1839 | 8/9 |
| Hartmann | 3 real | 192 | 0.002098 | 0.2617 | 9/9 |
| Rastrigin | 2 real | 384 | 0.4187 | 2.086 | 9/9 |
| Rosenbrock | 2 real | 320 | 0.05674 | 0.2344 | 6/9 |
| Mixed interaction | typed mixed | 160 | 0.004661 | 0.8733 | 7/9 |

TPE won all five problem medians and 39 of 45 seed/problem pairs. The geometric
median-regret ratio was `0.062`. Gates also require an independently calibrated
absolute regret for every problem, at least four median wins, at least a 60%
paired win rate, and an aggregate ratio no greater than `0.65`.

## Hard stress suite

The harder suite runs five fixed seeds per problem and deliberately includes
correlated and higher-dimensional surfaces:

| Problem | Dimensions | Budget | TPE median regret | Random median | dlib median |
| --- | ---: | ---: | ---: | ---: | ---: |
| Hartmann-6 | 6 real | 384 | 0.011758 | 0.72087 | approximately 0 |
| Rosenbrock-6 | 6 real | 512 | 4.6909 | 61.990 | 0.67459 |
| Shifted, rotated Rastrigin-6 | 6 real | 640 | 20.436 | 46.515 | 25.631 |
| Mixed log interaction | 6 typed | 384 | 0.35619 | 3.7366 | 0.57505 |

The rotated Rastrigin problem applies a fixed orthogonal Givens rotation and an
off-centre shift. Rotation removes the axis-aligned separability assumed by an
independent-marginal TPE, while the shift prevents the midpoint prior from
receiving a free optimum. The mixed problem combines categorical, Boolean,
integer, log-integer, log-real, and stepped-real parameters in coupled nonlinear
basins.

TPE won all four problem medians and all 20 seed/problem comparisons against
random. Its geometric paired-regret ratio was `0.092`. The suite requires at
least three median wins, a 55% paired win rate, a ratio no greater than `0.75`,
and separate Hartmann, Rosenbrock, and mixed-space quality gates. Rotated
Rastrigin intentionally has no easy problem-specific threshold: its full loss
is retained in every aggregate gate.

These results support that the implementation learns useful densities on hard
nonlinear and mixed spaces. They do not make product-TPE a covariance-aware
optimizer: dlib remains much stronger on Hartmann-6 and Rosenbrock-6 in this
run. Multivariate or grouped TPE would be a separate algorithmic extension.

## Official Optuna 4.9.0 comparison

A separate reproducible benchmark compares the final native implementation
directly with official Optuna 4.9.0 `TPESampler`. Both receive the same problem,
typed search space, seed, sequential minimize schedule, trial budget, 10 startup
trials, 24 EI candidates, prior weight, and default univariate/product mode.
Different RNGs and Parzen implementations mean proposal sequences are not
expected to match.

| Problem | Native median regret | Optuna median regret | Paired native/Optuna |
| --- | ---: | ---: | ---: |
| Branin-2 | 0.01112884 | 0.05180082 | 3/2 |
| Hartmann-3 | 0.003242738 | 0.008050801 | 3/2 |
| Rosenbrock-6 | 4.690910 | 5.828791 | 4/1 |
| Shifted, rotated Rastrigin-6 | 20.43591 | 17.13027 | 2/3 |
| Mixed typed/log | 0.3561900 | 0.6045869 | 4/1 |

Across 25 paired runs, native won 16 and Optuna won 9; the bounded geometric
paired-regret ratio was `0.664`. Native therefore compares credibly with the
official implementation on this suite, but it does not dominate it. In
particular, Optuna won the rotated Rastrigin median, consistent with this being
a hostile correlated surface for PineForge's product-density model.

The suite also includes a six-integer-dimension space with exactly `10^6`
candidates and a coupled, rugged objective whose unique optimum and second-best
value were checked by full enumeration. With 1,000 trials over five paired
seeds, the default 10-startup profile produced:

| Metric | Native TPE | Optuna TPE |
| --- | ---: | ---: |
| Median best regret | 8711 | 10552 |
| Paired wins | 3/5 | 2/5 |
| Median unique candidates | 517 | 386 |
| Median duplicate candidates | 483 | 614 |
| Exact-optimum runs | 0/5 | 1/5 |

Optuna's one exact-hit run proposed the optimum 144 times. This is a useful
example of exploitation succeeding once it finds the right basin, while also
showing how expensive backtest budgets can be consumed by duplicate parameter
sets.

An exploration-heavy companion changed only `startup_trials` from 10 to 200,
or 20% of the fixed budget:

| Metric | Native TPE | Optuna TPE |
| --- | ---: | ---: |
| Median best regret | 3852 | 8854 |
| Paired wins | 4/5 | 1/5 |
| Median unique candidates | 985 | 439 |
| Median duplicate candidates | 15 | 561 |
| Exact-optimum runs | 0/5 | 0/5 |

The larger startup allocation improved median regret for both implementations,
and cut native median regret by about 56%. It did not uniformly improve every
seed or retain Optuna's lucky exact hit. This supports testing an exploration-
heavy profile for discontinuous strategy objectives; it does not establish 20%
as a universal default. `startup_trials` is front-loaded exploration, not a
persistent random-mixture policy, and it does not protect against backtest
overfitting. Strategy studies must still compare multiple seeds and validate
on held-out windows or markets.

The recorded public API sampler overhead is lower for native on every problem,
but those timings include Python, Optuna Study/storage bookkeeping, and the
different language runtimes. They are end-to-end scheduling overhead, not a
pure estimator-kernel microbenchmark. Full raw methodology and reproduction
commands are in
[`benchmarks/optuna/results-2026-07-18.md`](../benchmarks/optuna/results-2026-07-18.md).

## Correctness contracts

The tests additionally enforce:

- declared optimum points are inside each search space and reproduce the
  benchmark formula's expected optimum;
- every proposal is type- and range-valid;
- fixed seed, worker/batch contract, and ordered feedback reproduce proposals;
- `ask`, `tell`, `abandon`, completion, and outstanding-request state remain
  consistent;
- continuous, quantized, categorical, Boolean, linear, and log domains are
  exercised;
- narrow log-integer ranges near `INT64_MAX` and narrow log-real ranges near
  `1e300` retain distinct proposals through relative coordinates;
- concurrent lifecycle transitions preserve unique IDs and counters, while
  reset is rejected until all outstanding requests finish;
- all objective and sampler values remain finite under the tested domains.

The executable tests are `tests/test_tpe_quality.cpp` and
`tests/test_tpe_stress.cpp`. Canonical definitions are from the
[Branin](https://www.sfu.ca/~ssurjano/branin.html),
[Hartmann-3](https://www.sfu.ca/~ssurjano/hart3.html),
[Hartmann-6](https://www.sfu.ca/~ssurjano/hart6.html),
[Rastrigin](https://www.sfu.ca/~ssurjano/rastr.html), and
[Rosenbrock](https://www.sfu.ca/~ssurjano/rosen.html) benchmark pages.

## Real strategy

Synthetic validation is complemented by 128 TPE trials on the scraped Shiroi
Supertrend strategy using 3,334,340 one-minute ETH-USDT bars from 2020 onward.
The engine aggregated those bars to the strategy's 15-minute timeframe. All
trials were feasible, repeated runs were byte-for-byte deterministic, and the
best account objective exceeded the equal-seed random and dlib results at the
same budget. See [real-strategy-validation.md](real-strategy-validation.md).
