# Architecture

## Status

The hardened single-strategy native path is implemented. PineScript
initialization is Python; sampling, plugin execution, objective evaluation, and
trial scheduling are C++. The executable path currently accepts one strategy
and one dataset.

Portfolio observation and custom-objective types exist as C++ contracts, but no
account aggregator or portfolio CLI has been implemented.

## Product boundary

`pineforge-hpo` composes two separately distributed products:

- `pineforge-codegen-oss` provides the `pineforge-codegen` Python distribution
  and its `pineforge_codegen` module. `transpile_full()` emits generated C++, an
  input manifest, and literal `strategy(...)` parameters.
- `pineforge-engine` executes a compiled strategy through its public C ABI.

The HPO repository does not subclass or link against `BacktestEngine` internals.
The native adapter loads a generated strategy plugin with local symbol scope and
resolves only its C ABI.

```text
                     initialization path (Python)
Pine source
    |
    v
transpile_full() -> generated.cpp + inputs + strategy params
    |
    v
ArtifactBuilder -> strategy.so/dylib + manifest + provenance
    |
    +------------------------- content-addressed cache
    |
    v
                     trial path (native C++)
immutable OHLCV + loaded StrategyPlugin
    |
Sampler -> Candidate -> fresh handle -> runtime overrides -> backtest
   ^                                                    |
   |                                                    v
   +-------- adaptive tell <---- Objective <---- ReportSnapshot -> Result
```

The Python `pineforge-hpo run` wrapper resolves a StudySpec and translates it
into a `pineforge-hpo-native run` invocation. Python is not in the trial hot
loop.

## Implemented components

### Transpiler bridge

`python/pineforge_hpo/transpile.py` imports
`pineforge_codegen.transpile_full` and normalizes its response:

```text
TranspileResult
  generated_cpp
  inputs[]
  strategy_params
  diagnostics[]
```

Pine lexer/parser/analyzer errors remain distinct from native compiler errors.
The codegen identity records both the installed distribution version and a hash
of its Python implementation, so an editable or dirty codegen checkout cannot
silently reuse a stale strategy artifact.

### Artifact compiler and cache

`ArtifactBuilder` supports Linux and macOS and writes:

```text
artifacts/<artifact-key>/
  strategy.so | strategy.dylib
  generated.cpp
  manifest.json
  provenance.json
```

The cache has two identities:

1. A preliminary request key contains the Pine source hash, codegen identity,
   engine version/ABI/header/library hashes, Eigen identity, compiler path and
   version, compiler target, platform, relevant compiler environment, and exact
   flags. A valid request-index hit skips both transpilation and compilation.
2. The final artifact key adds `generated_cpp_sha256`. This preserves the
   generated translation unit in the content identity without forcing a repeat
   transpilation merely to discover the key.

Per-key advisory locks serialize the first build. Artifact directories and
request indexes are published atomically. Cache hits re-hash the plugin and
generated C++ and verify their manifest/provenance identities.

The compile contract is:

```text
-std=c++17 -O2 -ffp-contract=off -fPIC -shared
```

When the compiler identifies as Clang, the builder adds `-fbracket-depth=1024`: engine
1.0 requires it of Clang builds outside CMake, because generated C++ for long Pine
expression chains nests deeper than Clang's default limit of 256. GCC has no such limit.
The flags used are part of the cache identity.

The engine static library is linked with GNU whole-archive on Linux or
`force_load` on macOS. Before publication, the builder loads the plugin,
requires `pf_abi_version`, `strategy_create`, `strategy_free`,
`strategy_set_input`, `strategy_set_override`, `strategy_get_last_error`,
`run_backtest_full`, and `report_free`, then checks that `pf_abi_version()`
matches the selected engine ABI.

### Search spaces and samplers

The native search-space model implements:

- bounded integer dimensions with a positive step, optionally log-scaled when
  positive and unstepped (`step = 1`);
- bounded real dimensions, continuous or stepped, with log scaling available
  only for positive continuous bounds;
- boolean dimensions;
- categorical dimensions over typed choices.

Candidates are type- and range-checked before serialization to the strategy
ABI. Fixed inputs are rejected if they overlap a search dimension.

The optional candidate-policy layer is separate from sampler scoring and
ordering:

- `sampler_default` delegates reuse and exhaustion semantics to the sampler and
  preserves continuous real dimensions;
- `without_replacement` requires a finite domain and atomically reserves every
  complete parameter vector at most once;
- `exhaustive` adds the requirement that the requested trial count exactly
  equals the finite domain cardinality.

Only TPE and grid accept the finite policies. Random and dlib reject them rather
than silently changing optimizer. This keeps existing continuous TPE behavior
backward compatible while making finite coverage an explicit contract.

Finite integer and varying-real dimensions are indexed by non-negative lattice
ordinal `k`, with legal values `low + k * step <= high`. A varying real must
declare a positive step; a fixed real where `low == high` contributes one value
without a step. A non-constant log real remains continuous in StudySpec v1 and
is not eligible for a finite policy, while a fixed log real is eligible. Boolean
and categorical cardinalities are two and the declared choice count. Checked
Cartesian multiplication resolves the full domain cardinality before execution.
The stepped-real codec uses fused multiply-add decoding, permits a one-ULP snap
only at the declared upper endpoint, caps real lattices at `2^53` ordinals, and
rejects any binary64 or strategy-ABI collision before execution.

Four native samplers are implemented:

- `GridSampler` exhausts the Cartesian product in stable dimension/value order;
- `RandomSampler` uses `std::mt19937_64`, an explicit seed, rejection sampling
  for bounded integers, and a stable 53-bit conversion for real values;
- `DlibGlobalSampler` wraps dlib `global_function_search` with move-only
  outstanding requests and explicit `ask()`, `tell()`, and `abandon()` calls;
- `TpeSampler` fits independent per-dimension Parzen marginals and exposes the
  same explicit adaptive request lifecycle.

Grid and random candidates may be generated before execution. dlib and TPE
candidates must be interleaved with feedback. The scheduler proposes a fixed
worker-sized batch, runs its backtests concurrently, and reports results in
trial-id order. This keeps a fixed seed and worker count repeatable without
letting thread completion timing alter the model.

For a finite candidate policy, the coordinator reserves a full vector in the
seen set before returning it from `ask()`. The reservation is therefore visible
to every later request in the same batch. Pending, completed, infeasible, and
failed vectors remain seen. This uniqueness state is independent of TPE's
constant-liar model: constant liar changes acquisition density, while the seen
set provides the exact no-repeat guarantee.

Stepped/discrete dlib dimensions are integer indices; linear continuous real
dimensions retain their bounds. Log integer and real dimensions use continuous
`ln(value)` coordinates. Log integer coordinate bounds expand by half a step
before transformation, and values decode by nearest-integer `exp(z)`. Constant
dimensions are omitted and injected on decode. dlib always maximizes, so
minimize objectives are negated internally. The dlib dependency is pinned to
20.0.1 by default and remains behind the sampler PIMPL.

TPE separates completed observations into good and bad groups using a bounded
gamma fraction. Numeric marginals use bounded Gaussian mixtures with quantized
bin-mass likelihoods for discrete values; categorical and boolean marginals use
prior-smoothed probabilities. Candidate ranking maximizes the summed per-field
log likelihood ratio. Constant-liar batching adds outstanding parameters only
to the bad estimator. Failed and infeasible requests are abandoned and do not
train either model. Under a finite policy, abandonment does not release the
parameter vector from the seen set.

TPE finite sampling retains adaptive proposal order while selecting only unseen
vectors. `without_replacement` validates `trials <= cardinality`;
`exhaustive` validates equality. Grid covers the same declared set in its stable
order. The two orders need not have the same intermediate best-so-far sequence.

The current TPE model is rebuilt from all usable observations. Its proposal
cost and memory therefore grow with study history; it is a refinement sampler,
not the engine for a `10^6`-`10^8` candidate sweep. The implementation does not
silently cap or discard history.

Log dimensions share one transform contract across adaptive samplers. TPE
encodes observations as `z = ln(value)` and fits numeric kernels in normalized
log space. A real proposal decodes with `exp(z)`. A log integer proposal rounds
`exp(z)` and its likelihood is the Gaussian mass over that integer's transformed
rounding bin. Its latent bounds are `ln(low - 0.5)` and `ln(high + 0.5)`, while
observation means remain `ln(k)`. The `l/g` likelihood ratio can be evaluated
in log coordinates because the real-valued Jacobian is common to both models
and cancels. Priors are uniform in the transformed coordinate, not in the
original linear units.

The implementation computes logarithmic coordinates relative to the lower
boundary with `log1p` and reconstructs them with `expm1`. This is mathematically
equivalent to subtracting absolute logarithms but avoids catastrophic
cancellation in narrow, high-magnitude ranges. Log-integer construction also
checks representative lower, middle, and upper quantization bins and rejects a
domain if normalized double precision cannot keep every bin distinct.

### Strategy plugin and trial executor

One `StrategyPlugin` loads the dynamic library and resolves its function table.
One immutable `Dataset` loads the OHLCV CSV. Each trial independently performs:

```text
strategy_create()
strategy_set_override(...)       fixed StudySpec strategy overrides
strategy_set_input(...)          fixed inputs plus sampled candidate
run_backtest_full(...)
strategy_get_last_error(...)
copy requested report/equity data
report_free(...)
strategy_free(...)
```

The plugin and dataset may be shared by worker threads, but strategy handles and
reports are never shared. Cleanup order is structural: report-owned buffers are
freed before the handle.

`ReportSnapshot` retains report counters and all/long/short/equity metrics
independently of the plugin handle lifetime. It optionally copies the equity
curve for portfolio/custom-objective consumers; the scalar expression CLI
disables that copy.

### Objectives and constraints

`MetricExpression` compiles an expression once. It supports:

```text
finite numbers
metric identifiers
+ - * /
unary + and -
parentheses
min(a,b), max(a,b), abs(a)
< <= > >= == !=
```

Comparisons yield `1.0` or `0.0`. The executable CLI evaluates constraints as
metric expressions and treats a finite non-zero result as satisfied. Metric
identifiers resolve before any backtest. Constraints always reject division by
zero and non-finite operands. Objective policies are explicit, and a non-finite
final score is never ranked or treated as feasible.

The generic library boundary remains independent of PineForge reports:

```cpp
struct ObjectiveResult {
    std::vector<double> values;
    std::vector<Constraint> constraints;
    bool valid;
    std::string diagnostic;
};

template <typename Observation>
using ObjectiveFn = std::function<ObjectiveResult(
    const Observation&, const TrialContext&)>;
```

Applications can therefore define a custom C++ objective over any observation.
The executable StudySpec path currently resolves expression objectives only.

## Executable interfaces

### Python orchestration CLI

The intended user path accepts Pine source and StudySpec JSON:

```bash
pineforge-hpo compile strategy.pine --engine-root external/pineforge-engine

pineforge-hpo run study.json \
  --engine-root external/pineforge-engine \
  --native ./build/bin/pineforge-hpo-native
```

`compile` owns transpilation, compilation, caching, and artifact provenance.
`run` resolves paths relative to the StudySpec, reuses the artifact when valid,
and launches the native executable.

### Native CLI

`pineforge-hpo-native` consumes a compiled strategy plugin, one OHLCV CSV,
search dimensions, fixed inputs, strategy overrides, objective/constraint
expressions, sampler settings, and worker count. It produces a JSON study
summary with every trial and the best feasible trial.

The native CLI intentionally knows nothing about Pine parsing or codegen.
Its result JSON includes `schema_version`, `pineforge_hpo_version`, and a stable
`sampler_implementation` identifier in addition to the sampler configuration,
seed, complete trial table, and artifact key. Proposal-sequence comparisons must
match all of those fields plus worker count and search-space definition. The
policy layer also persists `candidate_policy`,
`candidate_policy_implementation`, `search_space_finite`,
`search_space_cardinality`, `trials_requested`, `trials_completed`,
`unique_candidates_attempted`, `duplicate_proposals_skipped`,
`remaining_candidates`, `search_space_exhausted`, `stop_reason`,
`full_parameter_coverage`, and `exhaustive_equivalent`.

`full_parameter_coverage` is a parameter-set statement and may remain true when
a terminal trial failed. `exhaustive_equivalent` additionally requires every
trial status to be `ok` or `constraint_violation`. It is false after any engine,
objective, constraint-evaluation, serialization, or other trial error. The flag
still relies on the study's artifact, data, runtime, objective, and constraints
being deterministic; it cannot detect external nondeterminism.

## Current study mode

The executable MVP supports:

```text
mode = single_strategy
strategies = exactly one
datasets = exactly one
sampler = grid | random | dlib_global | tpe
sampler.candidate_policy = sampler_default | without_replacement | exhaustive
objective.kind = expression
execution = sequential | threads
```

Independent trials share the artifact and immutable bars and use fresh handles.
Runtime `input.*()` and `strategy(...)` values are applied without rebuilding
the plugin.

## Portfolio contract and boundary

The C++ API defines `PortfolioObservation`, `AccountEquityPoint`, `Allocation`,
`SleeveSummary`, and `PortfolioObjectiveFn`. A portfolio objective may request
only account return, drawdown, allocations, turnover, concentration, or other
application-defined features; it does not need every constituent report field.

The following are not implemented:

- aligning and aggregating sleeve equity curves;
- portfolio accounting and allocation search;
- strategy/market assignment;
- process-worker orchestration across separate plugins;
- registered-objective lookup from StudySpec;
- portfolio CLI output.

Completed independent reports also cannot reconstruct a true shared account.
Shared cash, margin, order admission, and cross-strategy order sequencing need a
future strategy signal/order-intent ABI and a shared broker ledger.

## Failure model

Initialization failures stop before any trial:

- invalid StudySpec or unresolved file;
- Pine diagnostics;
- missing engine/compiler/Eigen inputs;
- native compile/link failure;
- missing plugin symbols or ABI mismatch;
- invalid search space or objective expression;
- unknown report metric or manifest input;
- incompatible input type, bound, option, or strategy override;
- malformed OHLCV.

Trial-local failures remain visible in the trial table:

- candidate serialization/validation failure;
- engine last-error;
- missing or non-finite metric;
- division by zero under the reject policy;
- failed constraint evaluation.

Finite-policy coverage treats every terminal trial vector as seen, including a
constraint violation or trial-local failure. Consequently, full parameter
coverage means every declared vector received a terminal trial record. It does
not mean every vector produced a usable objective. Equality with the best value
from a successful deterministic grid search additionally requires
`exhaustive_equivalent=true` under identical artifact, dataset, runtime,
objective, and constraint inputs.

## Not implemented yet

- account/portfolio execution;
- study persistence, checkpoint/resume, and pruning;
- distributed execution or process-worker recovery;
- CMA-ES, evolutionary, or other additional Optuna-style samplers;
- conditional/hierarchical spaces and multi-objective Pareto studies;
- walk-forward and multi-dataset executable studies;
- per-study timeout and fail-fast cancellation.

## Licensing boundary

Original `pineforge-hpo` code is Apache-2.0. `pineforge-engine` is a separate
Apache-2.0 runtime dependency. `pineforge-codegen`, from
`pineforge-codegen-oss`, is a separate source-available dependency with its own
terms. The direct-PineScript convenience path does not relicense codegen under
Apache-2.0. Precompiled plugin users can run the native HPO layer without
installing codegen.

dlib 20.0.1 is the native global-search dependency and is distributed under the
Boost Software License 1.0. CMake verifies the pinned source archive hash unless
the build explicitly selects an exact-version system package.
