# ADR 0001: dlib global function search

- Status: accepted
- Date: 2026-07-18

> **Current license status (2026-10-07).** Starting with v0.11.0, original HPO
> code uses the [PineForge Source License 1.2](../../LICENSE). Releases up to and
> including v0.10.0 remain available under Apache-2.0. The Apache-2.0 references
> below describe the boundary at this ADR's 2026-07-18 decision; dlib's Boost
> Software License 1.0 is unchanged.

## Context

Grid and seeded-random search are useful deterministic baselines, but they do
not use completed objective values to improve later proposals. Single-strategy
parameter studies need an adaptive optimizer for expensive backtests. The same
ask/tell boundary must remain reusable when portfolio-level observations and
objectives are added.

The initial optimizer must support bounded continuous and discrete variables,
deterministic seeding, more than one outstanding evaluation, and an Apache-2.0
compatible redistribution boundary. Study persistence, pruning, conditional
spaces, and multi-objective Pareto search are separate concerns.

## Decision

Integrate dlib v20.0.1 and expose its `global_function_search` through a native
`DlibGlobalSampler` ask/tell adapter.

Each stepped integer, stepped real, boolean, and categorical dimension is
encoded as a bounded integer index. Continuous real dimensions retain their
native bounds. Constant dimensions are removed from the dlib vector and
restored when a candidate is materialized.

Positive log-scaled integer and real dimensions are continuous
`ln(value)` coordinates. Real values decode with `exp(z)`; integer values decode
with nearest-integer `exp(z)` and clamp to the declared inclusive bounds. The
integer coordinate interval expands the bounds by half a step before applying
the logarithm, matching the complete rounding bins. This keeps multiplicative
distance meaningful to dlib while preserving original-unit values at the
strategy ABI.

The runner asks for one deterministic batch of candidates, evaluates that
batch with the configured worker count, and reports finite maximization scores
back in trial-id order. Minimization objectives are sign-inverted. Failed and
infeasible requests are abandoned rather than assigned a fabricated score;
only genuine feasible observations train the model or become a reported best
result.

CMake downloads the pinned official source archive with a SHA-256 check by
default. A build may instead explicitly require the exact compatible system
package. GUI, media, database, CUDA, BLAS, LAPACK, and FFmpeg integrations are
disabled because HPO does not use them.

## Consequences

- Later proposals depend on earlier objective feedback, unlike grid/random.
- Initially, seed plus worker count defines the deterministic batch proposal
  sequence. Since 0.3.0, explicit logical batch size and fixed feedback lag
  replace worker count in that replay contract; the compatible default still
  uses workers as batch size. See [batching](../batching.md).
- dlib's Boost Software License 1.0 notice is retained alongside the
  Apache-2.0 project notice.
- This is not feature parity with Optuna. Native product-TPE is a separate
  decision in ADR 0002; pruning, durable study storage, conditional parameters,
  distributed coordination, and Pareto studies remain separate capabilities.
- dlib's global search is a model-based global optimizer with a local
  trust-region component; it is not described as Optuna TPE.
