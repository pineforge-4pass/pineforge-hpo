# ADR 0003: finite candidate policies and coverage semantics

- Status: accepted
- Date: 2026-07-18

## Context

Adaptive samplers may propose the same parameter vector more than once. That is
valid for an unconstrained continuous optimizer, but an expensive deterministic
backtest often benefits from spending each trial on a previously unseen point.
PineForge also needs a precise answer to when an adaptive run with as many
trials as legal parameter combinations is equivalent to exhaustive grid search.

The guarantee cannot be inferred from `trials` alone. Continuous real dimensions
have no finite cardinality, pending workers can collide, and failed evaluations
can leave a parameter point without a usable objective even when it was tried.

## Decision

StudySpec exposes `sampler.candidate_policy` with three values:

- `sampler_default` preserves the sampler's native behavior. Continuous real
  dimensions remain valid for TPE, random, and dlib, and repeated vectors remain
  possible where the sampler permits them.
- `without_replacement` requires a finite domain and reserves each complete
  parameter vector at most once. `trials` must be no greater than the exact
  domain cardinality.
- `exhaustive` has the same uniqueness contract and additionally requires
  `trials` to equal the exact domain cardinality.

The finite policies apply only to TPE and grid. Random and dlib reject them.
Grid already has a non-repeating order; the policy makes its validation and
coverage provenance explicit. TPE retains adaptive ordering and uses unseen
finite candidates until the requested budget or the domain is exhausted.

An integer or varying linear real dimension is the lattice

```text
{ low + k * step | k is a non-negative integer and low + k * step <= high }
```

A varying real requires an explicit positive `step`. A fixed real with
`low == high` contributes one value and needs no step. A non-constant log real
is continuous under StudySpec v1 and is rejected by both finite policies; a
fixed log real is allowed. Boolean and categorical dimensions contribute their
declared finite values. Domain cardinality is the checked Cartesian product of
the per-dimension counts.

Stepped-real decoding uses one fused multiply-add. A final result at most one
binary64 value above `high` is snapped to `high` for decimal endpoint stability;
other off-lattice bounds remain excluded. Real lattices above `2^53` ordinals
and any binary64 or strategy-ABI collisions are initialization errors.

The coordinator reserves a complete parameter vector as seen before exposing it
to a worker. The seen set includes pending, successfully completed, infeasible,
and failed trials. Abandoning a trial excludes it from adaptive model training
but does not make that vector eligible for another proposal in the same study.

`exhaustive` establishes full parameter coverage once all requested trial
records are terminal. It does not by itself establish full objective coverage.
Equality with the best result of a successful deterministic grid search also
requires every feasible vector to yield a comparable finite objective under the
same artifact, data, runtime configuration, objective, and constraints. Proposal
order and intermediate best-so-far values are intentionally not equivalent.

Result provenance records `candidate_policy`,
`candidate_policy_implementation`, `search_space_finite`,
`search_space_cardinality`, `trials_requested`, `trials_completed`,
`unique_candidates_attempted`, `duplicate_proposals_skipped`,
`remaining_candidates`, `search_space_exhausted`, `stop_reason`,
`full_parameter_coverage`, and `exhaustive_equivalent`.

`full_parameter_coverage` describes parameter-set coverage even when an
evaluation failed. `exhaustive_equivalent` additionally requires every trial
status to be either `ok` or `constraint_violation`; an engine, objective,
constraint-evaluation, serialization, or other trial error makes it false. The
flag establishes equivalence only under the contract's fixed deterministic
study inputs. It cannot detect nondeterminism outside the scheduler.

## Consequences

- Existing continuous TPE studies retain their behavior because
  `sampler_default` is the default.
- Finite policies fail during initialization when cardinality or real-domain
  quantization is undefined; they never silently fall back to with-replacement
  sampling.
- A failed or infeasible point still consumes one unique parameter vector, even
  though it does not train TPE.
- Exhaustive adaptive and grid runs cover the same declared set but may discover
  their best point at different times.
- Durable resume and distributed reservation remain separate future contracts;
  the in-process seen set does not imply cross-process persistence.
