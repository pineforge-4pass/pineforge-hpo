# ADR 0002: native product Tree-structured Parzen Estimator

- Status: accepted
- Date: 2026-07-18

## Context

PineForge needs an adaptive single-objective sampler that can remain inside the
native trial loop. Backtests are expensive enough that completed observations
should inform later proposals, while the optimizer itself must not introduce a
Python callback, database round trip, or additional runtime dependency for each
trial.

The sampler must handle the typed Pine input space, deterministic replay, and
several outstanding worker requests. It must also be testable independently of
the backtest engine. A successful API smoke test is not sufficient: the
estimator needs mathematical regression coverage and multi-seed nonlinear
quality gates against an equal-budget random baseline.

## Decision

Implement a clean-room C++17 product TPE in `pineforge-hpo` with an explicit
`ask()`, `tell()`, and `abandon()` lifecycle.

The sampler separates finite completed observations into good and bad groups.
The good count is the configured gamma fraction capped by `gamma_cap` and,
whenever at least two observations exist, clamped to leave one completed bad
observation. Objective direction is normalized before ranking; objective ties
are broken by trial ID.

Numeric dimensions use independent bounded Gaussian-mixture marginals. Kernel
widths come from sorted neighboring observations, use the magic-clip lower
bound, and add a broad midpoint prior after observation widths are computed.
Discrete numeric likelihood is probability mass over the legal quantization
bin, not point density. Categorical and boolean dimensions use smoothed
categorical kernels plus a uniform prior. Linear and logarithmic numeric
domains are transformed before the estimator is fitted.

Logarithmic transforms use relative `log1p`/`expm1` coordinates rather than
subtracting two large absolute logarithms. Discrete log-integer construction
fails explicitly if legal bin boundaries cannot remain distinct. Acquisition
contributions and their sum must be finite; estimator corruption is surfaced
instead of silently returning the first EI draw.

Each proposal draws `ei_candidates` factorized joint samples from the good
model and selects the first sample with the maximum summed per-dimension
`log(l(x)) - log(g(x))`. Startup proposals use the sampler's fixed-seed random
path. With the PineForge default `constant_liar=true`, outstanding candidates
enter only the bad estimator and do not count as completed or receive a
fabricated objective.

Only finite, feasible objective results are reported through `tell()`. Engine
failures and infeasible trials use `abandon()` and train neither density. The
coordinator reports a completed worker batch in trial-ID order, making seed,
worker count, sampler configuration, and native build part of the replay
contract.

`reset()` is allowed only when no request is outstanding. This prevents an old
worker result from being accepted after candidate IDs are reset and reused.

The expression-constraint API has no violation magnitude, so abandoned
infeasible points provide no negative model signal. A narrow feasible island is
a documented limitation until a constraint-aware observation contract exists.

## Compatibility boundary

This is a native PineForge product-TPE, not an Optuna proposal-sequence clone.
Intentional differences from Optuna's default univariate TPE include:

- joint factorized candidates are ranked by their summed log-density ratio;
- gamma is clamped to retain a completed bad observation;
- constant-liar batching defaults to enabled for native worker batches;
- failed and infeasible trials are abandoned instead of using Optuna's pruned
  or constraint-aware ranking state.

Conditional search trees, multivariate/group TPE, categorical distance
functions, multi-objective TPE, pruning, and durable study storage require
separate contracts. They must not be inferred from the `tpe` sampler name.

## Verification

The accepted implementation is gated by:

- deterministic mixed-type API tests, guarded reset, reverse feedback ordering,
  cancellation, invalid IDs, and maximize/minimize equivalence;
- numerical boundary tests for truncated Gaussian interval mass, discrete
  endpoint bins, high-magnitude relative-log domains, extreme domains, and
  categorical smoothing;
- concurrent `ask`/`tell`/`abandon` lifecycle and reset-race regressions;
- multi-seed nonlinear benchmarks covering multimodal, non-separable, narrow
  valley, and mixed categorical/discrete objectives, compared with an
  equal-budget seeded-random baseline;
- a full PineScript-to-plugin run over the canonical one-minute corpus fixture.

## Consequences

- The trial hot loop remains native and adds no optimizer runtime dependency.
- Seeded replay is stable for the same ordered transcript and native build, but
  is not promised to match Optuna or a different math-library implementation.
- Full usable history is retained and refitted. Proposal cost and memory grow
  with the study, so TPE is an adaptive refinement sampler rather than the
  primary path for `10^6`-`10^8` stateless candidate sweeps.
- Any future bounded-history approximation must be explicit in configuration
  and persistence; history is never silently discarded.

## References

- Bergstra et al., [Algorithms for Hyper-Parameter
  Optimization](https://papers.nips.cc/paper/4443-algorithms-for-hyper-parameter-optimization.pdf).
- Optuna 4.9.0, [TPE implementation](https://github.com/optuna/optuna/tree/v4.9.0/optuna/samplers/_tpe).
